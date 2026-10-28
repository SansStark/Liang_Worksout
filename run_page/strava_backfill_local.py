"""Continuously fetch historical activities and push each saved batch; stop on 429."""

import argparse
import json
import logging
import os
import subprocess
import time

from stravalib.exc import RateLimitExceeded

from strava_auto_sync import ROOT, STATE_FILE, run_auto_sync
from strava_credentials import load_env, read_env


def git(*args, check=True):
    executable = os.getenv("GIT_EXECUTABLE", "git")
    command = [executable, "-c", "http.version=HTTP/1.1"]
    proxy = os.getenv("HTTPS_PROXY")
    if proxy:
        command += ["-c", "http.proxy=" + proxy]
    result = subprocess.run(
        command + list(args), cwd=ROOT, capture_output=True, text=True
    )
    if check and result.returncode:
        raise RuntimeError(
            f"Git {args[0]} failed; local data and commits are retained."
        )
    return result


def push_progress():
    paths = [
        "src/static/activities.json",
        "public/data/activity-timelines",
        "public/data/pace-preview.json",
    ]
    if STATE_FILE.exists():
        paths.append(str(STATE_FILE.relative_to(ROOT)).replace("\\", "/"))
    git("add", "--", *paths)
    if not git("diff", "--cached", "--quiet", check=False).returncode:
        return None
    staged = git(
        "diff", "--cached", "--name-only", "--diff-filter=ACMR"
    ).stdout.splitlines()
    values = read_env()
    secrets = [
        values[key].encode()
        for key in (
            "STRAVA_CLIENT_SECRET",
            "STRAVA_CLIENT_REFRESH_TOKEN",
            "STRAVA_ACCESS_TOKEN",
        )
        if values.get(key)
    ]
    for path in staged:
        if not any(
            path == allowed or path.startswith(allowed + "/") for allowed in paths
        ):
            raise RuntimeError(
                "Unrelated staged files detected; refusing an unattended commit."
            )
        blob = subprocess.run(
            [os.getenv("GIT_EXECUTABLE", "git"), "show", ":" + path],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        if any(secret in blob for secret in secrets):
            raise RuntimeError("A plaintext credential was found in staged data.")
    activities = json.loads(
        (ROOT / "src/static/activities.json").read_text(encoding="utf-8")
    )
    git(
        "commit",
        "--quiet",
        "-m",
        f"Backfill Strava activities ({len(activities)} total) [skip ci]",
    )
    for attempt in range(3):
        if git("push", "origin", "HEAD:main", check=False).returncode == 0:
            commit = git("rev-parse", "--short", "HEAD").stdout.strip()
            print(f"Pushed {commit}: {len(activities)} activities.", flush=True)
            return commit
        if attempt < 2:
            time.sleep(3)
    raise RuntimeError("Git push failed three times; the local commit is retained.")


def is_rate_limited(error):
    response = getattr(error, "response", None)
    return isinstance(error, RateLimitExceeded) or (
        response is not None and response.status_code == 429
    )


def main():
    load_env()
    logging.getLogger("stravalib.attributes").setLevel(logging.ERROR)
    logging.getLogger("stravalib.model").setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive.")
    status_path = ROOT / ".cache" / "strava-backfill-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    batch = 0
    while True:
        batch += 1
        print(
            f"\nHistorical batch {batch} (up to {args.batch_size} activities)",
            flush=True,
        )
        error = None
        result = None
        try:
            result = run_auto_sync(
                args.batch_size, 0, 0, sync_recent=batch == 1, geocode=False
            )
        except Exception as caught:
            error = caught
        # Push even a partial page and its encrypted rotated token before stopping.
        commit = push_progress()
        status = {
            "batch": batch,
            "last_commit": commit,
            "state": "running",
            **(result or {}),
        }
        if error is not None:
            status.update(
                state="rate_limited" if is_rate_limited(error) else "error",
                error=type(error).__name__,
            )
            response = getattr(error, "response", None)
            if response is not None:
                status["quota"] = {
                    key: value
                    for key, value in response.headers.items()
                    if "ratelimit" in key.lower() or key.lower() == "retry-after"
                }
            status_path.write_text(
                json.dumps(status, indent=2) + "\n", encoding="utf-8"
            )
            print(
                f"Stopped: {status['state']} ({type(error).__name__}). Saved progress was pushed.",
                flush=True,
            )
            if not is_rate_limited(error):
                raise SystemExit(1)
            return
        if result["history_complete"]:
            status["state"] = "complete"
        status_path.write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        if result["history_complete"]:
            print(
                "All available Strava history has been fetched and pushed.", flush=True
            )
            return


if __name__ == "__main__":
    main()
