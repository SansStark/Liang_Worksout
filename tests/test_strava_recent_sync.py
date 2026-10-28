import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import arrow

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run_page"))

from generator import Generator, newest_activities_first  # noqa: E402
from generator.db import Activity  # noqa: E402
from strava_credentials import read_env, update_env  # noqa: E402
from strava_sync import run_strava_sync  # noqa: E402


class RecentSyncTest(unittest.TestCase):
    def test_oldest_first_api_results_are_processed_newest_first(self):
        activities = [
            SimpleNamespace(id=1, start_date="2026-07-31T00:00:00Z"),
            SimpleNamespace(id=2, start_date="2026-10-27T00:00:00Z"),
            SimpleNamespace(id=3, start_date="2026-09-15T00:00:00Z"),
        ]
        self.assertEqual([a.id for a in newest_activities_first(activities)], [2, 3, 1])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.generator = Generator(str(self.root / "test.db"))
        self.generator.check_access = lambda: None
        self.filters = {}

        def activities(**filters):
            self.filters.update(filters)
            return []

        self.generator.client = SimpleNamespace(get_activities=activities)
        self.now = arrow.get("2026-10-28T00:00:00Z")
        self.clock = patch("generator.arrow.utcnow", return_value=self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.generator.session.close()
        self.generator.session.get_bind().dispose()
        self.directory.cleanup()

    def seed(self, date, source="strava", run_id=1):
        self.generator.session.add(
            Activity(run_id=run_id, start_date=date, source=source)
        )
        self.generator.session.commit()

    def test_first_sync_uses_recent_window(self):
        self.generator.sync(False, recent_days=90)
        self.assertEqual(self.filters, {"after": self.now.shift(days=-90).datetime})

    def test_incremental_sync_overlaps_seven_days(self):
        self.seed("2026-10-27 00:00:00+00:00")
        self.generator.sync(False, recent_days=90)
        self.assertEqual(self.filters["after"], self.now.shift(days=-8).datetime)

    def test_old_database_cannot_force_a_full_history_download(self):
        self.seed("2020-01-01 00:00:00+00:00")
        self.generator.sync(False, recent_days=90)
        self.assertEqual(self.filters["after"], self.now.shift(days=-90).datetime)

    def test_non_strava_dates_do_not_advance_strava_cursor(self):
        self.seed("2026-10-28 00:00:00+00:00", source="gpx")
        self.generator.sync(False, recent_days=90)
        self.assertEqual(self.filters["after"], self.now.shift(days=-90).datetime)

    def test_history_batch_ignores_recent_cursor(self):
        self.seed("2026-10-27 00:00:00+00:00")
        self.generator.sync(False, recent_days=90, before="2026-07-01")
        boundary = arrow.get("2026-07-01")
        self.assertEqual(
            self.filters,
            {
                "before": boundary.datetime,
                "after": boundary.shift(days=-90).datetime,
            },
        )

    def test_force_has_no_recent_cutoff(self):
        self.generator.sync(True, recent_days=90)
        self.assertNotIn("after", self.filters)
        self.assertIn("before", self.filters)

    def test_rate_limit_preserves_and_exports_newest_activity(self):
        activity = SimpleNamespace(
            id=123,
            name="My recent run",
            type="Run",
            sport_type="Run",
            distance=1000.0,
            moving_time=datetime.timedelta(minutes=5),
            elapsed_time=datetime.timedelta(minutes=6),
            start_date="2026-10-27 00:00:00+00:00",
            start_date_local="2026-10-27 08:00:00",
            start_latlng=None,
            map=None,
            average_heartrate=None,
            average_speed=3.3,
            total_elevation_gain=0,
        )

        def interrupted(**_filters):
            yield activity
            raise RuntimeError("rate limit")

        self.generator.client.get_activities = interrupted
        output = self.root / "activities.json"
        with (
            patch("strava_sync.Generator", return_value=self.generator),
            patch("strava_sync.JSON_FILE", output),
            self.assertRaisesRegex(RuntimeError, "rate limit"),
        ):
            run_strava_sync("id", "secret", "refresh", timeline_limit=0)
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([item["run_id"] for item in data], [123])
        self.assertEqual(
            self.generator.session.get(Activity, 123).name, "My recent run"
        )
        # Reopen the database as a new invocation would; unfinished days stay in range.
        retry = Generator(str(self.root / "test.db"))
        retry.check_access = lambda: None
        captured = {}

        def remaining(**filters):
            captured.update(filters)
            return []

        retry.client = SimpleNamespace(get_activities=remaining)
        try:
            retry.sync(False, recent_days=90)
            self.assertEqual(captured["after"], self.now.shift(days=-90).datetime)
            captured.clear()
            retry.sync(False, recent_days=90)
            self.assertEqual(captured["after"], self.now.shift(days=-8).datetime)
        finally:
            retry.session.close()
            retry.session.get_bind().dispose()


class CredentialsTest(unittest.TestCase):
    def test_update_preserves_settings_and_replaces_rotated_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                '# My settings\nSTRAVA_CLIENT_ID="123"\n'
                "STRAVA_CLIENT_REFRESH_TOKEN=old\nSTRAVA_RECENT_DAYS=90\n",
                encoding="utf-8",
            )
            update_env(
                {"STRAVA_CLIENT_REFRESH_TOKEN": "new", "STRAVA_ATHLETE_ID": "456"}, path
            )
            values = read_env(path)
            self.assertEqual(values["STRAVA_CLIENT_ID"], "123")
            self.assertEqual(values["STRAVA_CLIENT_REFRESH_TOKEN"], "new")
            self.assertEqual(values["STRAVA_RECENT_DAYS"], "90")
            self.assertEqual(values["STRAVA_ATHLETE_ID"], "456")
            self.assertIn("# My settings", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
