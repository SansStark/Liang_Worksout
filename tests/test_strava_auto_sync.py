import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run_page"))

from generator import Generator  # noqa: E402
from generator.db import Activity  # noqa: E402
from strava_auto_sync import (  # noqa: E402
    decrypt_token,
    duration,
    encrypt_token,
    restore_export,
    run_auto_sync,
)
from strava_backfill_local import is_rate_limited  # noqa: E402


class AutoSyncTest(unittest.TestCase):
    def test_refresh_token_is_encrypted_and_bound_to_account(self):
        envelope = encrypt_token("private-token", "private-key", "123", "456")
        self.assertNotIn("private-token", json.dumps(envelope))
        self.assertEqual(
            decrypt_token(envelope, "private-key", "123", "456"), "private-token"
        )
        for secret, athlete in [("wrong-key", "456"), ("private-key", "999")]:
            with self.assertRaises(ValueError):
                decrypt_token(envelope, secret, "123", athlete)

    def test_duration_preserves_days_and_fractional_seconds(self):
        self.assertEqual(
            duration("1 day, 02:03:04.5"),
            dt.timedelta(days=1, hours=2, minutes=3, seconds=4.5),
        )

    def test_cloud_rebuild_retains_history_and_optional_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "activities.json"
            source.write_text(
                json.dumps(
                    [
                        {
                            "run_id": 1,
                            "name": "Existing",
                            "source": "strava",
                            "type": "Run",
                            "moving_time": "1 day, 00:00:01",
                            "elapsed_time": "1 day, 00:02:00",
                            "start_date": "2026-10-01T00:00:00Z",
                            "start_date_local": "2026-10-01 08:00:00",
                            "best_efforts": [
                                {"name": "5k", "distance": 5000, "elapsed_time": 1400}
                            ],
                            "temperature_min": 0,
                            "summary_polyline": "existing-route",
                            "streak": 1,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            generator = Generator(str(root / "test.db"))
            try:
                self.assertEqual(restore_export(generator.session, source), 1)
                self.assertEqual(restore_export(generator.session, source), 0)
                saved = generator.session.get(Activity, 1)
                self.assertEqual(saved.moving_time, dt.timedelta(days=1, seconds=1))
                self.assertEqual(saved.temperature_min, 0)
                self.assertEqual(saved.best_efforts[0]["elapsed_time"], 1400)
                self.assertEqual(saved.summary_polyline, "existing-route")
            finally:
                generator.session.close()
                generator.session.get_bind().dispose()

    def test_recent_history_and_restart_use_committed_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "activities.json"
            source.write_text("[]", encoding="utf-8")
            state_path = root / "state.json"
            calls = []

            def incoming():
                return SimpleNamespace(
                    id=7,
                    name="Old activity",
                    type="Run",
                    sport_type="Run",
                    distance=1000,
                    moving_time=dt.timedelta(minutes=5),
                    elapsed_time=dt.timedelta(minutes=6),
                    start_date="2020-01-01T00:00:00Z",
                    start_date_local="2020-01-01 08:00:00",
                    map=None,
                    start_latlng=None,
                    average_heartrate=None,
                    average_speed=3.3,
                    total_elevation_gain=0,
                )

            def factory(path):
                generator = Generator(path)

                def authenticate():
                    generator.access_token = "access-only-in-memory"
                    generator.refresh_token = "rotated-only-in-memory"

                def listing(**filters):
                    calls.append(filters)
                    return (
                        [incoming()] if "before" in filters and len(calls) == 2 else []
                    )

                generator.check_access = authenticate
                generator.client.get_activities = listing
                return generator

            environment = {
                "STRAVA_CLIENT_ID": "123",
                "STRAVA_CLIENT_SECRET": "test-secret",
                "STRAVA_CLIENT_REFRESH_TOKEN": "initial-token",
                "STRAVA_ATHLETE_ID": "456",
            }
            with (
                patch.dict(os.environ, environment),
                patch("strava_auto_sync.load_env"),
                patch("strava_auto_sync.Generator", side_effect=factory),
                patch("strava_auto_sync.SQL_FILE", str(root / "test.db")),
                patch("strava_auto_sync.JSON_FILE", source),
                patch("strava_sync.JSON_FILE", source),
            ):
                first = run_auto_sync(1, 0, 0, state_path, geocode=False)
                self.assertEqual(first, {"activities": 1, "history_complete": False})
                second = run_auto_sync(
                    1, 0, 0, state_path, sync_recent=False, geocode=False
                )
                self.assertEqual(second, {"activities": 1, "history_complete": True})
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["history_before"], "2020-01-01T00:00:00+00:00")
            self.assertNotIn(
                "rotated-only-in-memory", state_path.read_text(encoding="utf-8")
            )
            self.assertEqual(calls[-1]["before"].year, 2020)
            self.assertEqual(calls[-1]["limit"], 1)

    def test_detects_server_429_without_treating_other_failures_as_limits(self):
        from requests import HTTPError

        limited = HTTPError(response=SimpleNamespace(status_code=429))
        unauthorized = HTTPError(response=SimpleNamespace(status_code=401))
        self.assertTrue(is_rate_limited(limited))
        self.assertFalse(is_rate_limited(unauthorized))
        self.assertFalse(is_rate_limited(RuntimeError()))


if __name__ == "__main__":
    unittest.main()
