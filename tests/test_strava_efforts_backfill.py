import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run_page"))

from generator.db import Activity, init_db  # noqa: E402
from strava_efforts_backfill import (
    EffortsClient,
    next_window,
    prioritize_records,
    run_batch,
)  # noqa: E402
from strava_timeline_sync import TimelineClient  # noqa: E402


class FakeClient(TimelineClient):
    def get(self, endpoint):
        self.ensure_budget()
        self.requests += 1
        if endpoint == "/athlete":
            return {"id": 7}
        return {
            "id": int(endpoint.rsplit("/", 1)[1]),
            "best_efforts": [{"name": "400m", "distance": 400, "elapsed_time": 80}],
        }


class EffortsBackfillTest(unittest.TestCase):
    def test_transient_network_failure_retries_without_exposing_token_in_url(self):
        import requests

        client = EffortsClient("private-token", max_requests=4)
        try:
            response = SimpleNamespace(
                status_code=200, headers={}, json=lambda: {"id": 1}
            )
            client.http.get = Mock(side_effect=[requests.ConnectionError(), response])
            self.assertEqual(client.get("/activities/1"), {"id": 1})
            self.assertEqual(client.requests, 2)
            call = client.http.get.call_args
            self.assertNotIn("private-token", call.args[0])
            self.assertEqual(
                call.kwargs["headers"]["Authorization"], "Bearer private-token"
            )
        finally:
            client.http.close()

    def test_priority_covers_each_standard_distance_and_never_duplicates(self):
        runs = [
            SimpleNamespace(
                run_id=i,
                distance=distance,
                average_speed=i,
                start_date_local="2026-01-01",
            )
            for i, distance in enumerate([43000, 22000, 12000, 6000, 1500, 500], 1)
        ]
        self.assertEqual(
            [a.run_id for a in prioritize_records(runs)], [1, 2, 3, 4, 5, 6]
        )

    def test_reset_times_use_utc_quarters_and_next_day(self):
        now = dt.datetime(2026, 10, 28, 23, 59, 59, tzinfo=dt.timezone.utc)
        expected = dt.datetime(2026, 10, 29, 0, 0, 15, tzinfo=dt.timezone.utc)
        self.assertEqual(next_window(now), expected)
        self.assertEqual(next_window(now, daily=True), expected)

    def test_budget_stop_exports_progress_and_next_batch_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "test.db"
            data = root / "activities.json"
            progress = root / "progress.json"
            data.write_text("[]", encoding="utf-8")
            session = init_db(str(db))
            for i in range(1, 4):
                session.add(
                    Activity(
                        run_id=i,
                        source="strava",
                        type="Run",
                        name="Test",
                        distance=5000,
                        moving_time=dt.timedelta(minutes=30),
                        average_speed=3,
                        start_date="2026-01-01T00:00:00Z",
                        start_date_local="2026-01-01 08:00:00",
                    )
                )
            session.commit()
            session.close()
            session.get_bind().dispose()
            with (
                patch("strava_efforts_backfill.SQL_FILE", str(db)),
                patch("strava_efforts_backfill.JSON_FILE", data),
                patch("strava_sync.JSON_FILE", data),
                patch("strava_efforts_backfill.PROGRESS_FILE", progress),
                patch(
                    "strava_efforts_backfill.authenticate",
                    return_value=("not-a-real-token", "7"),
                ),
                patch("strava_efforts_backfill.EffortsClient", FakeClient),
            ):
                first = run_batch(2)
                self.assertEqual(first["updated_in_batch"], 1)
                self.assertEqual(first["running_pending"], 2)
                self.assertFalse(first["finished"])
                self.assertEqual(
                    len(
                        [
                            a
                            for a in json.loads(data.read_text())
                            if a.get("best_efforts")
                        ]
                    ),
                    1,
                )
                second = run_batch(10)
                self.assertEqual(second["updated_in_batch"], 2)
                self.assertEqual(second["running_pending"], 0)
                self.assertTrue(second["finished"])
                self.assertIsNone(second["next_retry_at"])


if __name__ == "__main__":
    unittest.main()
