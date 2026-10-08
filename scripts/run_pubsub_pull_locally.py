"""Local-dev stand-in for the deployed Cloud Function — a thin CLI over
``PubSubPullService`` (the same service the app runs in-process when
``PUBSUB_PULL_ENABLED=true``). Use this only if you want the puller as its own
process; otherwise just run the app.

Usage:
    python scripts/run_pubsub_pull_locally.py
"""
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from src.services.pubsub_pull_service import PubSubPullService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "google_genai", "googleapiclient.discovery_cache", "google.auth", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

if __name__ == "__main__":
    PubSubPullService().run_blocking()
