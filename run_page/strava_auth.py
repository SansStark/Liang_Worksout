"""Authorize your own Strava account and save credentials locally, without pip."""

import argparse
import getpass
import json
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from strava_credentials import load_env, read_env, update_env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    load_env()
    values = read_env()
    client_id = values.get("STRAVA_CLIENT_ID") or input("Strava Client ID: ").strip()
    client_secret = (
        values.get("STRAVA_CLIENT_SECRET")
        or getpass.getpass("Strava Client Secret (hidden): ").strip()
    )
    if not client_id.isdigit() or not client_secret:
        raise SystemExit("Client ID must be numeric and Client Secret is required.")
    state = secrets.token_urlsafe(32)
    callback = f"http://127.0.0.1:{args.port}/callback"
    outcome = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # The callback URL contains the single-use authorization code.

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != "/callback" or query.get("state") != [state]:
                self.send_error(400, "Invalid authorization callback")
                return
            if query.get("error"):
                outcome["error"] = "Authorization denied."
            elif "activity:read_all" not in query.get("scope", [""])[0].split(","):
                outcome["error"] = "Please grant activity:read_all and authorize again."
            elif not query.get("code"):
                outcome["error"] = "Missing authorization code."
            else:
                outcome["code"] = query["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("授权步骤已结束，请返回终端查看结果。".encode("utf-8"))

    with HTTPServer(("127.0.0.1", args.port), CallbackHandler) as server:
        server.timeout = 1
        authorize_url = "https://www.strava.com/oauth/authorize?" + urlencode(
            {
                "client_id": client_id,
                "redirect_uri": callback,
                "response_type": "code",
                "approval_prompt": "force",
                "scope": "read,activity:read_all",
                "state": state,
            }
        )
        print("Authorize your own Strava account in the browser (10-minute timeout).")
        print(authorize_url)
        webbrowser.open(authorize_url)
        deadline = time.monotonic() + 600
        while not outcome and time.monotonic() < deadline:
            server.handle_request()
    if "code" not in outcome:
        raise SystemExit(outcome.get("error", "Authorization timed out; run again."))
    request = Request(
        "https://www.strava.com/oauth/token",
        data=urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "code": outcome["code"],
                "grant_type": "authorization_code",
            }
        ).encode(),
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            token = json.load(response)
    except Exception as error:
        raise SystemExit(
            f"Token exchange failed ({type(error).__name__}); run authorization again."
        ) from None
    athlete_id = token["athlete"]["id"]
    previous_id = values.get("STRAVA_ATHLETE_ID")
    if previous_id and previous_id != str(athlete_id):
        raise SystemExit(
            "A different athlete is configured. Use a separate project copy."
        )
    update_env(
        {
            "STRAVA_CLIENT_ID": client_id,
            "STRAVA_CLIENT_SECRET": client_secret,
            "STRAVA_CLIENT_REFRESH_TOKEN": token["refresh_token"],
            "STRAVA_ATHLETE_ID": str(athlete_id),
        }
    )
    print(f"Authorized athlete {athlete_id}. Credentials saved to .env (not printed).")
    print("Next: powershell -ExecutionPolicy Bypass -File .\\sync-strava-local.ps1")


if __name__ == "__main__":
    main()
