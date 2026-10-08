"""Registers (or renews) Gmail push notifications — a thin CLI over
``GmailWatchService`` (the same logic as ``POST /gmail/watch``).

Gmail watch() registrations expire after 7 days — run this once to start,
then again on a schedule to keep notifications flowing.

Usage:
    python scripts/register_gmail_watch.py
"""
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from src.api.exceptions import AppException
from src.gmail.client import GmailClient
from src.services.gmail_watch_service import GmailWatchService

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    # interactive=True: if token.json predates a newly required scope, re-open the Google consent screen here
    # instead of failing. send=True makes gmail.send required, so a token granted without it (which the app
    # cannot send the "reply ENTERPRISE or GROWTH" emails with) triggers the consent.
    service = GmailWatchService(gmail_client_factory=lambda: GmailClient(interactive=True, send=True))
    try:
        print((await service.register()).model_dump_json(indent=2))
    except AppException as exc:
        raise SystemExit(exc.message)


if __name__ == "__main__":
    # psycopg's async mode can't run on Windows' default ProactorEventLoop.
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(main(), loop_factory=loop_factory)
