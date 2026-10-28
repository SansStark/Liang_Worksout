"""Bounded recent-first sync, resumable history and durable encrypted token rotation."""

import argparse
import base64
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path

import arrow
from Crypto.Cipher import AES
from sqlalchemy import func

from backfill_temperatures import backfill_temperatures
from config import JSON_FILE, SQL_FILE
from generator import Generator
from generator.db import Activity
from strava_best_efforts import sync_best_efforts
from strava_credentials import load_env
from strava_power_efforts import sync_power_efforts
from strava_sync import export_activities
from strava_timeline_sync import backfill_timelines

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / ".strava-sync" / "state.json"


def token_cipher(client_secret, client_id, athlete_id, nonce=None):
    key = hashlib.sha256(b"liang-strava-sync-v1\0" + client_secret.encode()).digest()
    cipher = (
        AES.new(key, AES.MODE_GCM, nonce=nonce) if nonce else AES.new(key, AES.MODE_GCM)
    )
    cipher.update(f"{client_id}:{athlete_id}".encode())
    return cipher


def encrypt_token(token, client_secret, client_id, athlete_id):
    cipher = token_cipher(client_secret, client_id, athlete_id)
    ciphertext, tag = cipher.encrypt_and_digest(token.encode())
    return {
        key: base64.b64encode(value).decode("ascii")
        for key, value in {
            "nonce": cipher.nonce,
            "ciphertext": ciphertext,
            "tag": tag,
        }.items()
    }


def decrypt_token(envelope, client_secret, client_id, athlete_id):
    fields = {
        key: base64.b64decode(envelope[key], validate=True)
        for key in ("nonce", "ciphertext", "tag")
    }
    cipher = token_cipher(client_secret, client_id, athlete_id, fields["nonce"])
    return cipher.decrypt_and_verify(fields["ciphertext"], fields["tag"]).decode()


def save_state(state, path=STATE_FILE):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def duration(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return dt.timedelta(seconds=value)
    days = 0
    if ", " in value:
        day_part, value = value.split(", ", 1)
        days = int(day_part.split()[0])
    hours, minutes, seconds = value.split(":")
    return dt.timedelta(
        days=days, hours=int(hours), minutes=int(minutes), seconds=float(seconds)
    )


def restore_export(session, json_path=JSON_FILE):
    """Rebuild a fresh CI database from versioned data; preserve all cached optional fields."""
    path = Path(json_path)
    rows = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else []
    columns = set(Activity.__table__.columns.keys())
    restored = 0
    for item in rows:
        if session.get(Activity, int(item["run_id"])) is not None:
            continue
        values = {key: value for key, value in item.items() if key in columns}
        for key in ("moving_time", "elapsed_time"):
            if key in values:
                values[key] = duration(values[key])
        session.add(Activity(**values))
        restored += 1
    session.commit()
    return restored


def oldest_strava_date(session):
    return (
        session.query(func.min(Activity.start_date))
        .filter(Activity.source == "strava")
        .scalar()
    )


def run_auto_sync(
    history_limit=50,
    detail_limit=5,
    weather_limit=5,
    state_path=STATE_FILE,
    sync_recent=True,
    geocode=True,
):
    load_env()
    names = (
        "STRAVA_CLIENT_ID",
        "STRAVA_CLIENT_SECRET",
        "STRAVA_CLIENT_REFRESH_TOKEN",
        "STRAVA_ATHLETE_ID",
    )
    if any(not os.getenv(name) for name in names):
        raise RuntimeError(
            "Configure all Strava credentials and STRAVA_ATHLETE_ID first."
        )
    client_id, client_secret, refresh_token, athlete_id = (
        os.environ[name] for name in names
    )
    state_path = Path(state_path)
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {
            "version": 1,
            "client_id": client_id,
            "athlete_id": athlete_id,
            "history_complete": False,
        }
    )
    if state.get("client_id") != client_id or state.get("athlete_id") != athlete_id:
        raise RuntimeError("Sync state belongs to another Strava account/application.")
    if state.get("refresh_token"):
        refresh_token = decrypt_token(
            state["refresh_token"], client_secret, client_id, athlete_id
        )
    generator = Generator(SQL_FILE)
    try:
        restored = restore_export(generator.session, JSON_FILE)
        print(f"Restored {restored} activities from committed JSON.", flush=True)
        # Stop at the server's 429 instead of the old SDK sleeping inside a request.
        generator.client.protocol.rate_limiter = lambda _headers: None
        generator.set_strava_config(client_id, client_secret, refresh_token)
        generator.check_access()
        if generator.refresh_token != refresh_token or not state.get("refresh_token"):
            state["refresh_token"] = encrypt_token(
                generator.refresh_token, client_secret, client_id, athlete_id
            )
        # Save rotated credentials before any activity or stream request can fail.
        save_state(state, state_path)
        generator.check_access = lambda: None
        try:
            if sync_recent:
                recent = generator.sync(
                    False, recent_days=90, enhance_routes=False, geocode=geocode
                )
                export_activities(generator)
                print(f"Recent activities checked: {recent}", flush=True)
            if history_limit and not state.get("history_complete"):
                boundary = (
                    state.get("history_before")
                    or oldest_strava_date(generator.session)
                    or arrow.utcnow().shift(days=-90).isoformat()
                )
                historical = generator.sync(
                    False,
                    before=boundary,
                    limit=history_limit,
                    enhance_routes=False,
                    geocode=geocode,
                )
                state["history_complete"] = historical == 0
                print(f"Historical activities checked: {historical}", flush=True)
            if detail_limit:
                sync_best_efforts(
                    generator.client, generator.session, limit=detail_limit
                )
                sync_power_efforts(
                    generator.client, generator.session, limit=detail_limit
                )
            activities = export_activities(generator)
            if detail_limit:
                try:
                    backfill_timelines(
                        activities=activities,
                        token=generator.access_token,
                        limit=detail_limit,
                        max_requests=detail_limit * 2 + 2,
                    )
                except Exception as error:
                    print(
                        f"Timeline batch deferred ({type(error).__name__}).", flush=True
                    )
            if weather_limit:
                try:
                    backfill_temperatures(
                        db_path=SQL_FILE,
                        json_path=JSON_FILE,
                        limit=weather_limit,
                        all_missing=False,
                        sleep_seconds=0.2,
                    )
                except Exception as error:
                    print(
                        f"Weather batch deferred ({type(error).__name__}).", flush=True
                    )
        finally:
            # A failed page retains saved records and resumes before the oldest saved activity.
            generator.session.rollback()
            generator.session.expire_all()
            export_activities(generator)
            oldest = oldest_strava_date(generator.session)
            if oldest:
                state["history_before"] = arrow.get(oldest).isoformat()
            save_state(state, state_path)
        count = generator.session.query(Activity).count()
        print(
            f"Activities saved: {count}; "
            f"history complete: {state['history_complete']}",
            flush=True,
        )
        return {"activities": count, "history_complete": state["history_complete"]}
    finally:
        generator.session.close()
        generator.session.get_bind().dispose()


def main():
    logging.getLogger("stravalib.attributes").setLevel(logging.ERROR)
    logging.getLogger("stravalib.model").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-limit", type=int, default=50)
    parser.add_argument("--detail-limit", type=int, default=5)
    parser.add_argument("--weather-limit", type=int, default=5)
    args = parser.parse_args()
    if min(args.history_limit, args.detail_limit, args.weather_limit) < 0:
        parser.error("Batch limits cannot be negative.")
    try:
        run_auto_sync(args.history_limit, args.detail_limit, args.weather_limit)
    except Exception as error:
        raise SystemExit(
            f"Sync stopped ({type(error).__name__}); saved progress will be retried."
        ) from None


if __name__ == "__main__":
    main()
