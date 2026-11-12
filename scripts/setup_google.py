"""One-time Google Calendar OAuth flow.

Prereqs:
 1. console.cloud.google.com -> create a project -> APIs & Services ->
    enable "Google Calendar API".
 2. OAuth consent screen (Google Auth Platform): External, add yourself as
    a test user, then **Publish app** (Audience -> "In production").
    Apps left in "Testing" get refresh tokens that EXPIRE AFTER 7 DAYS,
    which would silently break Calendar a week later. An unverified app in
    production is fine for personal use — you just click through a
    "Google hasn't verified this app" warning once.
 3. Clients -> Create client -> Desktop app -> download the JSON as
    google_client_secret.json in the repo root (next to .env).
 4. pip install -r requirements.txt -e .
 5. Run this ON YOUR LAPTOP from the repo root (it opens a browser), then
    upload the resulting google_token.json to the server:
      PUSH_SECRETS=1 bash scripts/deploy.sh
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("pip install -r requirements.txt -e . first")

    secret_file = Path(settings.google_client_secret_file)
    if not secret_file.exists():
        sys.exit(
            f"{secret_file} not found — download the OAuth Desktop-app client "
            "JSON from Google Cloud console (see this file's docstring)"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
    creds = flow.run_local_server(port=0)
    token_file = Path(settings.google_token_file)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    print(f"wrote {token_file} — upload it with PUSH_SECRETS=1 bash scripts/deploy.sh")


if __name__ == "__main__":
    main()
