"""Quota-aware official personal-best backfill, resumed by the cloud workflow."""

import argparse
import json
import os
from collections import deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests

from config import JSON_FILE, SQL_FILE
from generator import Generator
from generator.db import Activity
from strava_auto_sync import (
    ROOT,
    STATE_FILE,
    decrypt_token,
    encrypt_token,
    restore_export,
    save_state,
)
from strava_best_efforts import BEST_EFFORTS_VERSION, official_efforts
from strava_credentials import ENV_PATH, load_env, update_env
from strava_power_efforts import calculate_power_efforts, is_cycling
from strava_streams import is_run_activity
from strava_sync import export_activities
from strava_timeline_sync import ApiError, BudgetStop, TimelineClient

PROGRESS_FILE = ROOT / ".strava-sync" / "efforts-progress.json"


class EffortsClient(TimelineClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http = requests.Session()

    def get(self, endpoint):
        for attempt in range(3):
            self.ensure_budget()
            self.requests += 1
            self.budgets = [(short - 1, daily - 1) for short, daily in self.budgets]
            try:
                response = self.http.get(
                    "https://www.strava.com/api/v3" + endpoint,
                    headers={
                        "Authorization": "Bearer " + self.token,
                        "Cache-Control": "no-cache",
                    },
                    timeout=(10, 25),
                )
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError("Strava network request failed") from None
                continue
            self.update_limits(response.headers)
            if response.status_code == 429:
                raise BudgetStop("Strava rate limited this batch; progress retained")
            if response.status_code >= 400:
                if response.status_code >= 500 and attempt < 2:
                    continue
                raise ApiError(response.status_code)
            return response.json()


def pending_runs(session):
    return [
        a
        for a in session.query(Activity)
        if a.source == "strava"
        and is_run_activity(a.type)
        and (a.distance or 0) >= 400
        and (a.best_efforts is None or a.best_efforts_version != BEST_EFFORTS_VERSION)
    ]


def pending_rides(session):
    return [
        a
        for a in session.query(Activity)
        if a.source == "strava" and is_cycling(a.type) and a.power_efforts is None
    ]


def prioritize_records(activities):
    """Spread requests across standard distances; only official responses set PBs."""
    ordered = sorted(
        activities,
        key=lambda a: (a.average_speed or 0, a.start_date_local or ""),
        reverse=True,
    )
    queues = [
        deque(a for a in ordered if a.distance >= distance)
        for distance in (42195, 21097.5, 10000, 5000, 1000, 400)
    ]
    seen = set()
    while any(queues):
        for queue in queues:
            while queue and queue[0].run_id in seen:
                queue.popleft()
            if queue:
                activity = queue.popleft()
                seen.add(activity.run_id)
                yield activity


def next_window(now, daily=False):
    if daily:
        return (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=15, microsecond=0
        )
    return now.replace(
        minute=(now.minute // 15) * 15, second=15, microsecond=0
    ) + timedelta(minutes=15)


def authenticate():
    load_env()
    keys = (
        "STRAVA_CLIENT_ID",
        "STRAVA_CLIENT_SECRET",
        "STRAVA_CLIENT_REFRESH_TOKEN",
        "STRAVA_ATHLETE_ID",
    )
    if any(not os.getenv(key) for key in keys):
        raise RuntimeError("Missing Strava application credentials or athlete ID.")
    client_id, secret, refresh, athlete_id = (os.environ[key] for key in keys)
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if state.get("client_id") != client_id or state.get("athlete_id") != athlete_id:
        raise RuntimeError("Configured Strava account does not match saved state.")
    if state.get("refresh_token"):
        refresh = decrypt_token(state["refresh_token"], secret, client_id, athlete_id)
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": client_id,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
        timeout=40,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Token refresh failed: HTTP {response.status_code}")
    token = response.json()
    if token["refresh_token"] != refresh:
        state["refresh_token"] = encrypt_token(
            token["refresh_token"], secret, client_id, athlete_id
        )
        save_state(state)
    if ENV_PATH.exists():
        update_env(
            {
                "STRAVA_CLIENT_REFRESH_TOKEN": token["refresh_token"],
                "STRAVA_ACCESS_TOKEN": token["access_token"],
            }
        )
    return token["access_token"], athlete_id


def save_running(activity, detail, session):
    efforts = official_efforts(detail)
    if efforts is None:
        return False
    activity.best_efforts = efforts
    activity.best_efforts_version = BEST_EFFORTS_VERSION
    activity.best_efforts_synced_at = datetime.now(timezone.utc).isoformat()
    session.commit()
    return True


def save_cycling(activity, detail, client, session):
    if detail.get("id") != activity.run_id:
        return False
    measured = detail.get("device_watts") is True
    efforts = []
    if measured:
        query = urlencode({"keys": "time,watts", "key_by_type": "true"})
        streams = client.get(f"/activities/{activity.run_id}/streams?{query}")
        data = []
        for key in ("time", "watts"):
            stream = streams.get(key, {})
            samples = stream.get("data", [])
            if not samples or stream.get("original_size", len(samples)) > len(samples):
                return False
            data.append(samples)
        efforts = calculate_power_efforts(*data)
    activity.device_watts = measured
    activity.power_efforts = efforts
    activity.power_efforts_synced_at = datetime.now(timezone.utc).isoformat()
    session.commit()
    return True


def run_batch(max_requests=90):
    now = datetime.now(timezone.utc)
    progress = (
        json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        if PROGRESS_FILE.exists()
        else {}
    )
    deferred = progress.get("deferred", {})
    generator = Generator(SQL_FILE)
    client = None
    updated = 0
    stopped = None
    fatal_error = None
    try:
        restore_export(generator.session, JSON_FILE)

        def due(activity):
            return (
                deferred.get(str(activity.run_id), {}).get("retry_after", "")
                <= now.isoformat()
            )

        runs = [a for a in pending_runs(generator.session) if due(a)]
        rides = [a for a in pending_rides(generator.session) if due(a)]
        if runs or rides:
            token, athlete_id = authenticate()
            client = EffortsClient(
                token, max_requests=max_requests, short_reserve=8, daily_reserve=10
            )
            athlete = client.get("/athlete")
            if str(athlete.get("id")) != athlete_id:
                raise RuntimeError(
                    "Authenticated Strava athlete does not match configured account."
                )
            queue = [(a, True) for a in prioritize_records(runs)] + [
                (a, False) for a in rides
            ]
            for activity, running in queue:
                key = str(activity.run_id)
                try:
                    detail = client.get(f"/activities/{activity.run_id}")
                    saved = (
                        save_running(activity, detail, generator.session)
                        if running
                        else save_cycling(activity, detail, client, generator.session)
                    )
                    if not saved:
                        deferred[key] = {
                            "retry_after": (now + timedelta(days=1)).isoformat(),
                            "reason": "data_unavailable",
                        }
                        continue
                    deferred.pop(key, None)
                    updated += 1
                    print(
                        f"Saved {'running' if running else 'cycling'} record {activity.run_id} ({updated} this batch)",
                        flush=True,
                    )
                except ApiError as error:
                    if error.status not in (403, 404):
                        raise
                    deferred[key] = {
                        "retry_after": (now + timedelta(days=1)).isoformat(),
                        "reason": f"HTTP_{error.status}",
                    }
    except BudgetStop as error:
        stopped = str(error)
    except Exception as error:
        stopped = type(error).__name__
        fatal_error = error
    finally:
        generator.session.rollback()
        generator.session.expire_all()
        export_activities(generator)
        remaining_run = len(pending_runs(generator.session))
        remaining_ride = len(pending_rides(generator.session))
        budgets = client.budgets if client else []
        daily_low = any(daily <= 12 for _short, daily in budgets)
        retry = next_window(datetime.now(timezone.utc), daily=daily_low)
        progress = {
            "version": 1,
            "running_pending": remaining_run,
            "cycling_pending": remaining_ride,
            "finished": remaining_run + remaining_ride == 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_in_batch": updated,
            "requests": client.requests if client else 0,
            "next_retry_at": (
                retry.isoformat() if remaining_run + remaining_ride else None
            ),
            "stopped": stopped,
            "deferred": deferred,
        }
        save_state(progress, PROGRESS_FILE)
        generator.session.close()
        generator.session.get_bind().dispose()
        if client and hasattr(client, "http"):
            client.http.close()
    print(
        json.dumps({k: v for k, v in progress.items() if k != "deferred"}), flush=True
    )
    if fatal_error is not None:
        raise RuntimeError(
            f"Effort backfill stopped ({type(fatal_error).__name__}); progress saved."
        ) from None
    return progress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-requests", type=int, default=90)
    args = parser.parse_args()
    if args.max_requests < 2:
        parser.error("--max-requests must be at least 2.")
    try:
        run_batch(args.max_requests)
    except Exception as error:
        raise SystemExit(
            f"Backfill failed ({type(error).__name__}); saved results retained."
        ) from None


if __name__ == "__main__":
    main()
