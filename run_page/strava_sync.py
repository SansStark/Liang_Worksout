import argparse
import json
import logging
import os
from pathlib import Path

from backfill_temperatures import backfill_temperatures
from config import JSON_FILE, SQL_FILE
from generator import Generator
from strava_best_efforts import sync_best_efforts
from strava_credentials import load_env
from strava_power_efforts import sync_power_efforts
from strava_timeline_sync import backfill_timelines


def export_activities(generator):
    activities = generator.loadForMapping()
    target = Path(JSON_FILE)
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(activities, ensure_ascii=False, indent=0), encoding="utf-8"
    )
    temporary.replace(target)
    return activities


def run_strava_sync(
    client_id,
    client_secret,
    refresh_token,
    sync_types=None,
    only_run=False,
    force=False,
    skip_temperature_backfill=False,
    temperature_limit=25,
    timeline_limit=20,
    recent_days=90,
    before=None,
    skip_details=False,
):
    generator = Generator(SQL_FILE)
    generator.set_strava_config(client_id, client_secret, refresh_token)
    generator.only_run = only_run or sync_types == ["running"]
    try:
        try:
            generator.sync(force, recent_days=recent_days, before=before)
        finally:
            # Export committed recent records even if history hits a rate limit.
            activities = export_activities(generator)
        if not skip_details:
            sync_best_efforts(
                generator.client, generator.session, limit=20, refresh=force
            )
            sync_power_efforts(
                generator.client, generator.session, limit=20, refresh=force
            )
            activities = export_activities(generator)
        if timeline_limit > 0:
            try:
                backfill_timelines(
                    activities=activities,
                    token=generator.access_token,
                    limit=timeline_limit,
                )
            except Exception as error:
                print(f"Timeline backfill deferred ({type(error).__name__}).")
        if not skip_temperature_backfill:
            try:
                backfill_temperatures(
                    db_path=SQL_FILE,
                    json_path=JSON_FILE,
                    limit=temperature_limit,
                    all_missing=False,
                    sleep_seconds=0.2,
                )
            except Exception as error:
                print(f"Temperature backfill deferred ({type(error).__name__}).")
    finally:
        generator.session.close()
        generator.session.get_bind().dispose()


def main():
    load_env()
    # Strava's extra id_str fields are harmless with the existing SDK version.
    logging.getLogger("stravalib.attributes").setLevel(logging.ERROR)
    logging.getLogger("stravalib.model").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(
        description="Sync your Strava, recent activities first."
    )
    # Positional credentials remain compatible with automation; prefer .env locally.
    parser.add_argument("client_id", nargs="?", default=os.getenv("STRAVA_CLIENT_ID"))
    parser.add_argument(
        "client_secret", nargs="?", default=os.getenv("STRAVA_CLIENT_SECRET")
    )
    parser.add_argument(
        "refresh_token", nargs="?", default=os.getenv("STRAVA_CLIENT_REFRESH_TOKEN")
    )
    parser.add_argument("--only-run", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Fetch all history (may use many requests).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=os.getenv("STRAVA_RECENT_DAYS", "90"),
        help="Recent window in days; default 90. Also sets history batch size.",
    )
    parser.add_argument(
        "--before", help="Fetch a history batch before this ISO date, e.g. 2026-07-01."
    )
    parser.add_argument(
        "--skip-details",
        action="store_true",
        help="Skip best-effort and power lookups.",
    )
    parser.add_argument("--skip-temperature-backfill", action="store_true")
    parser.add_argument("--temperature-limit", type=int, default=25)
    parser.add_argument(
        "--timeline-limit", type=int, default=20, help="0 skips timeline backfill."
    )
    options = parser.parse_args()
    if not all([options.client_id, options.client_secret, options.refresh_token]):
        parser.error("Configure .env and run python run_page/strava_auth.py first.")
    if options.days <= 0 or min(options.timeline_limit, options.temperature_limit) < 0:
        parser.error("--days must be positive; backfill limits cannot be negative.")
    if options.force and options.before:
        parser.error("Choose either --force or --before.")
    if options.before:
        import arrow

        try:
            arrow.get(options.before)
        except (ValueError, TypeError):
            parser.error("--before must be an ISO date such as 2026-07-01.")
    try:
        run_strava_sync(
            options.client_id,
            options.client_secret,
            options.refresh_token,
            only_run=options.only_run,
            force=options.force,
            skip_temperature_backfill=options.skip_temperature_backfill,
            temperature_limit=options.temperature_limit,
            timeline_limit=options.timeline_limit,
            recent_days=options.days,
            before=options.before,
            skip_details=options.skip_details,
        )
    except Exception as error:
        raise SystemExit(
            f"Strava sync stopped ({type(error).__name__}); saved activities are retained. "
            "For 401/403 reauthorize; for 429 wait for your API quota to reset."
        ) from None


if __name__ == "__main__":
    main()
