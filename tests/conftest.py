"""Test-session setup shared by every test module."""
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

# psycopg's async mode can't run on Windows' default ProactorEventLoop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

ROOT = Path(__file__).resolve().parents[1]

# Tests must not depend on whatever is in the developer's own .env (real environment variables
# outrank .env in pydantic-settings), so pin the settings that would otherwise be read from it.
os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")
os.environ.setdefault("GROQ_API_KEY", "test-key-not-used")
os.environ["LLM_PROVIDER"] = "groq"
os.environ["GROQ_MODEL"] = "llama-test-model"
os.environ["GEMINI_MODEL"] = "gemini-test-model"
os.environ["ROCKETLANE_ENABLED"] = "true"        # the default flow; a developer's .env may switch it off

@pytest.fixture(autouse=True)
def never_touch_real_gmail(monkeypatch):
    """The app sends real email through the cached ``token.json``. No test may ever
    reach it: building a real ``GmailClient`` fails loudly. Tests that need Gmail
    inject a fake (``tests/fakes.py``) instead."""
    from src.gmail.client import GmailClient

    def _refuse(self, *args, **kwargs):
        raise RuntimeError("tests must not build a real GmailClient — inject a fake")

    monkeypatch.setattr(GmailClient, "__init__", _refuse)


def _server_base() -> str:
    uri = os.environ.get("TEST_POSTGRES_ADMIN_URI") or os.environ.get(
        "POSTGRES_URI", "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    )
    return uri.rsplit("/", 1)[0]


def reset_app_singletons() -> None:
    """Forget the process-wide pool / settings / graph so the next use re-reads the environment."""
    from src.agents.intake.graph import intake_graph
    from src.agents.provisioning.graph import provisioning_graph
    from src.agents.tier_confirmation.graph import tier_graph
    from src.config import settings as settings_module
    from src.config.checkpointer_config import checkpointer_config
    from src.config.database_config import db_pool

    settings_module._settings = None
    db_pool._pool = None
    checkpointer_config._saver = None
    for workflow_graph in (intake_graph, tier_graph, provisioning_graph):
        workflow_graph._graph = None


@pytest.fixture
async def temp_database(monkeypatch):
    """A throwaway Postgres database with ``db/schema.sql`` applied, and the app
    pointed at it. Yields its connection URI; dropped afterwards.

    Skips cleanly when no Postgres server is reachable (set
    ``TEST_POSTGRES_ADMIN_URI`` or ``POSTGRES_URI`` to run these).
    """
    import psycopg

    os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")
    base = _server_base()
    try:
        admin = psycopg.connect(f"{base}/postgres", autocommit=True, connect_timeout=3)
    except Exception as exc:
        pytest.skip(f"No reachable Postgres server ({exc!r}) — set TEST_POSTGRES_ADMIN_URI or POSTGRES_URI")

    db_name = f"test_onboarding_{uuid.uuid4().hex[:10]}"
    admin.execute(f'CREATE DATABASE "{db_name}"')
    db_uri = f"{base}/{db_name}"
    with psycopg.connect(db_uri, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))

    monkeypatch.setenv("POSTGRES_URI", db_uri)
    reset_app_singletons()
    yield db_uri

    from src.config.database_config import db_pool

    await db_pool.close()
    reset_app_singletons()
    admin.execute(f'DROP DATABASE "{db_name}" WITH (FORCE)')
    admin.close()
