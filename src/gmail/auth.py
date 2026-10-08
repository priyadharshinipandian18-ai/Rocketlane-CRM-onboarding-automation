"""Gmail OAuth2 installed-app flow.

First run opens a browser for one-time consent and caches a refresh token to
``token.json``; every run after that refreshes silently with no browser step.
"""
import json
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src.config.settings import get_settings

log = logging.getLogger(__name__)

READ_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.metadata",
]
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
"""Only needed to send the "reply ENTERPRISE or GROWTH" emails to the AE. Reading mail and the
watch() registration work without it, so a token granted before it existed keeps working."""

SCOPES = [*READ_SCOPES, SEND_SCOPE]
"""What a *new* consent asks for."""


class GmailAuthError(RuntimeError):
    """The cached token can't be used as-is and a human has to re-consent."""


def _granted_scopes(token_path: Path) -> set[str]:
    try:
        return set(json.loads(token_path.read_text(encoding="utf-8")).get("scopes") or [])
    except (OSError, ValueError):
        return set()


def get_gmail_service(interactive: bool = False, send: bool = False):
    """Return an authenticated Gmail API service client.

    Blocking (the underlying OAuth libraries and ``googleapiclient`` are
    synchronous) — callers from async code should run this via
    ``asyncio.to_thread``.

    ``send=True`` additionally requires the ``gmail.send`` permission (only the escalation
    emails need it). A cached token granted without a *required* scope can't be used: with
    ``interactive=True`` (the CLI scripts) the consent screen re-opens; otherwise — the running
    app, where a browser prompt would just hang a worker thread — it raises ``GmailAuthError``
    telling you what to do. A token that has every scope the caller needs is used as it is.
    """
    settings = get_settings()
    token_path = Path(settings.gmail_token_path)
    credentials_path = Path(settings.gmail_credentials_path)

    creds: Credentials | None = None
    if token_path.exists():
        granted = _granted_scopes(token_path)
        missing = (set(READ_SCOPES) | ({SEND_SCOPE} if send else set())) - granted
        if missing and not interactive:
            raise GmailAuthError(
                f"'{token_path}' was granted without: {', '.join(sorted(missing))}. Delete it and run "
                "`python scripts/register_gmail_watch.py` once to approve the permission "
                "(or set HUMAN_EMAIL_ENABLED=false to run without sending emails)."
            )
        if not missing:
            # Use the token's own scopes, so refreshing never asks for more than was granted.
            creds = Credentials.from_authorized_user_file(str(token_path), sorted(granted))

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing expired Gmail OAuth token")
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Gmail OAuth client secret not found at '{credentials_path}'. "
                    "Download it from Google Cloud Console (OAuth client, type "
                    "'Desktop app') and save it there. See README.md."
                )
            log.info("Starting Gmail OAuth consent flow (browser will open)")
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json())
        log.info("Gmail OAuth token cached at '%s'", token_path)

    return build("gmail", "v1", credentials=creds)
