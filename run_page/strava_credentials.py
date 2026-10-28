"""Local Strava credentials; never export them into frontend assets."""

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def read_env(path=ENV_PATH):
    values = {}
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    return values


def load_env(path=ENV_PATH):
    for key, value in read_env(path).items():
        if value and not os.environ.get(key):
            os.environ[key] = value


def update_env(values, path=ENV_PATH):
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    remaining = dict(values)
    result = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if not line.lstrip().startswith("#") and key in values:
            result.append(f"{key}={values[key]}")
            remaining.pop(key, None)
        else:
            result.append(line)
    result.extend(f"{key}={value}" for key, value in remaining.items())
    temporary = path.with_suffix(".env.tmp")
    temporary.write_text("\n".join(result) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def persist_refresh_token(client_id, refresh_token):
    # In CI there is no .env; the supplied repository Secret remains authoritative.
    values = read_env()
    if values.get("STRAVA_CLIENT_ID") == str(client_id):
        update_env({"STRAVA_CLIENT_REFRESH_TOKEN": refresh_token})
