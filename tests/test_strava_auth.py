import io
import json
import socket
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run_page"))

import strava_auth  # noqa: E402


class StravaAuthorizationTest(unittest.TestCase):
    def authorize(self, scope="read,activity:read_all", previous_id=""):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        requests = []
        workers = []
        callback_errors = []

        def open_browser(url):
            query = parse_qs(urlparse(url).query)
            self.assertEqual(query["scope"], ["read,activity:read_all"])
            callback = (
                query["redirect_uri"][0]
                + "?"
                + urlencode(
                    {
                        "state": query["state"][0],
                        "scope": scope,
                        "code": "single-use-code",
                    }
                )
            )

            def visit():
                try:
                    with urlopen(callback, timeout=5) as response:
                        response.read()
                except Exception as error:
                    callback_errors.append(error)

            worker = threading.Thread(target=visit)
            workers.append(worker)
            worker.start()

        def exchange(request, **_kwargs):
            requests.append(request)
            return io.BytesIO(
                json.dumps(
                    {
                        "athlete": {"id": 456},
                        "refresh_token": "local-refresh",
                    }
                ).encode()
            )

        with (
            patch.object(sys, "argv", ["strava_auth.py", "--port", str(port)]),
            patch("strava_auth.load_env"),
            patch(
                "strava_auth.read_env",
                return_value={
                    "STRAVA_CLIENT_ID": "123",
                    "STRAVA_CLIENT_SECRET": "local-secret",
                    "STRAVA_ATHLETE_ID": previous_id,
                },
            ),
            patch("strava_auth.webbrowser.open", side_effect=open_browser),
            patch("strava_auth.urlopen", side_effect=exchange),
            patch("strava_auth.update_env") as save,
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            try:
                strava_auth.main()
            except SystemExit as error:
                return {
                    "error": str(error),
                    "saved": save.call_args,
                    "requests": requests,
                }
            finally:
                for worker in workers:
                    worker.join(timeout=6)
                self.assertEqual(callback_errors, [])
            self.assertNotIn("local-secret", output.getvalue())
            self.assertNotIn("local-refresh", output.getvalue())
            return {"saved": save.call_args, "requests": requests}

    def test_callback_exchanges_code_and_saves_own_athlete(self):
        result = self.authorize()
        self.assertEqual(len(result["requests"]), 1)
        request = result["requests"][0]
        self.assertEqual(request.full_url, "https://www.strava.com/oauth/token")
        self.assertEqual(parse_qs(request.data.decode())["code"], ["single-use-code"])
        self.assertEqual(result["saved"].args[0]["STRAVA_ATHLETE_ID"], "456")

    def test_missing_activity_scope_never_exchanges_or_saves(self):
        result = self.authorize(scope="read")
        self.assertIn("activity:read_all", result["error"])
        self.assertEqual(result["requests"], [])
        self.assertIsNone(result["saved"])

    def test_other_athlete_never_replaces_existing_credentials(self):
        result = self.authorize(previous_id="999")
        self.assertIn("different athlete", result["error"])
        self.assertIsNone(result["saved"])


if __name__ == "__main__":
    unittest.main()
