"""FastAPI app — routers (see ``src/api/register_routes.py``), plus the
background workers that keep the workflow moving: the call-result poller, the
call-retry scheduler and, optionally, the Pub/Sub pull subscriber.

Run with:  python main.py        (or: uvicorn main:app --port 8000 --reload)
"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI

from src.api.exceptions import register_exception_handlers
from src.api.register_routes import register_routers
from src.config.database_config import db_pool
from src.config.model_factory import check_llm_configuration
from src.config.settings import get_settings
from src.scheduler.call_result_poller import call_poll_loop
from src.scheduler.retry_runner import retry_loop
from src.scheduler.tier_escalation import tier_escalation_loop
from src.scheduler.workflow_sweeper import workflow_sweeper_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Libraries that log every request / retry at INFO: keep the warnings, drop the per-call noise.
for noisy in ("httpx", "httpcore", "google_genai", "googleapiclient.discovery_cache", "google.auth", "urllib3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("LLM provider: %s", check_llm_configuration())     # fail fast if the API key is missing
    stop = asyncio.Event()
    background_loops = [
        asyncio.create_task(retry_loop(stop)),        # places retry calls when due
        asyncio.create_task(call_poll_loop(stop)),    # fetches finished-call results from Bolna
        asyncio.create_task(tier_escalation_loop(stop)),  # AE hasn't replied to the escalation email: stop the run
        asyncio.create_task(workflow_sweeper_loop(stop)),  # starts pending hand-offs (tier -> provisioning)
    ]

    pull_service = None
    if get_settings().pubsub_pull_enabled:
        from src.services.pubsub_pull_service import PubSubPullService

        pull_service = PubSubPullService()
        pull_service.start(asyncio.get_running_loop())   # Gmail notifications → onboarding runs

    try:
        yield
    finally:
        if pull_service is not None:
            pull_service.stop()
        stop.set()
        await asyncio.gather(*background_loops)
        await db_pool.close()


app = FastAPI(title="Customer Onboarding Automation", version="0.3.0", lifespan=lifespan)
register_exception_handlers(app)
register_routers(app)


if __name__ == "__main__":
    import uvicorn

    # psycopg's async mode can't run on Windows' default ProactorEventLoop.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    log.info("Starting Customer Onboarding Automation on port 8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
