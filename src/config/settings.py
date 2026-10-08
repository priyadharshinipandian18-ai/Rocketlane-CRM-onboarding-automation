import re
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All external-service configuration, loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM — the email classifier and the deal parser
    llm_provider: Literal["groq", "gemini"] = "groq"
    """Which hosted model runs them: ``groq`` (Llama) or ``gemini``."""
    llm_max_retries: int = 2
    """Retries the provider's client makes itself on 429/5xx (it backs off between them). Gemini's
    default is 6, which is ~35s per call — and it multiplies with ``llm_attempts``."""
    llm_attempts: int = 2
    """Our own attempts around an LLM call (classifier, deal parser) before giving up."""
    llm_timeout_seconds: float = 60.0
    """Per-request timeout for an LLM call."""

    # Groq — hosted Llama (LLM_PROVIDER=groq)
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    """A Groq model ID (see https://console.groq.com/docs/models)."""

    # Gemini (LLM_PROVIDER=gemini)
    google_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    @model_validator(mode="after")
    def _the_chosen_providers_model_must_be_an_id(self) -> "Settings":
        """Providers want a model *ID* (``gemini-3.5-flash``, ``llama-3.3-70b-versatile``), not the
        display name shown in a dashboard (``Gemini 3.5 Flash``). Catch that at startup instead of as a
        400 deep in a retry — but only for the provider in use, so a stale value for the other one
        can't block startup."""
        if self.llm_provider == "gemini":
            model = self.gemini_model.strip().removeprefix("models/")
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", model):
                raise ValueError(
                    f"GEMINI_MODEL={self.gemini_model!r} is not a model ID. Use the lowercase ID with hyphens and no "
                    "spaces, e.g. 'gemini-3.5-flash' — list the IDs your key can use with genai.Client(...).models.list()."
                )
            self.gemini_model = model
        else:
            model = self.groq_model.strip()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", model):
                raise ValueError(
                    f"GROQ_MODEL={self.groq_model!r} is not a model ID. Use the ID from the Groq console, "
                    "e.g. 'llama-3.3-70b-versatile' — lowercase, hyphens, no spaces."
                )
            self.groq_model = model
        return self

    # Gmail
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"

    # Pub/Sub
    gcp_project_id: str = ""
    gmail_watch_topic: str = ""
    pubsub_subscription: str = "deal-intake-sub"
    pubsub_pull_enabled: bool = False
    """Pull Gmail notifications inside the app process (local dev). Off by default;
    a deployed setup uses the Cloud Function or ``POST /pubsub/push`` instead."""
    pubsub_push_token: str = ""
    """Shared secret for ``POST /pubsub/push?token=...``. Empty = push endpoint disabled."""
    gmail_mailbox_email: str = ""
    """The AE-inbox address being watched — the key into app.mailboxes."""
    require_ae_sender: bool = True
    """Only analyse emails sent by an active app.account_executives address;
    everything else is ignored before any LLM call. Set false to analyse all mail."""

    # Postgres — both application tables (db/schema.sql) and the LangGraph
    # checkpointer live in this one database.
    postgres_uri: str = "postgresql://postgres:postgres@127.0.0.1:5432/onboarding_automation"

    # Public base URL the Voice AI provider can reach (ngrok URL in local dev)
    webhook_base_url: str = "http://localhost:8000"

    # Voice AI (Bolna) — agent prompt/extractions/webhook live in the Bolna
    # dashboard; see docs/bolna_agent_setup.md
    bolna_api_key: str = ""
    bolna_agent_id: str = ""
    bolna_api_base: str = "https://api.bolna.ai"
    bolna_from_phone_number: str = ""
    """Optional: your own/connected number. Empty = Bolna's default numbers."""
    bolna_poll_enabled: bool = True
    """Polling mode: fetch call results from Bolna's API on a timer. Needs no public URL."""
    bolna_poll_seconds: int = 20
    voice_call_timeout_minutes: int = 20
    """A call with no final status after this long is treated as failed (then retried)."""
    bolna_webhook_token: str = ""
    """Optional webhook mode. Empty = webhook disabled (requests rejected).
    If set, put it in the agent's webhook URL as ?token=<value>."""
    bolna_allowed_ips: str = ""
    """Optional comma-separated allowlist of Bolna's webhook IPs. Empty = not enforced."""
    bolna_transcript_fallback: bool = True
    """When Bolna returns no extractions at all, read the plan from the call transcript (strictly: the AE
    stated it, the assistant read it back, the AE's next words affirm it). Turn off to require extractions."""
    bolna_min_confidence: float = 0.7
    """Extractions below this confidence are treated as "not confirmed"."""

    # Plan-tier call retry policy
    voice_max_attempts: int = 3
    voice_retry_delay_minutes: int = 30
    voice_retry_delay_seconds: float | None = None
    """When set, overrides ``voice_retry_delay_minutes`` (for demos: ``5`` = call again after 5 seconds)."""
    voice_call_window_start_hour: int = 9
    """Earliest local hour (AE's timezone) we're allowed to place a call."""
    voice_call_window_end_hour: int = 19
    """Calls are not placed at or after this local hour."""
    retry_poll_seconds: int = 60
    """How often the scheduler checks for runs whose retry is due."""

    # When the AE can't be reached by phone: ask for the tier by email, escalate, then stop.
    human_email_enabled: bool = True
    """Send the "please tell us the plan tier" emails. Needs the gmail.send scope."""
    human_stop_after_hours: float = 24.0
    """No reply from the AE after this long → stop the run (it stays parked and resumable)."""
    escalation_poll_seconds: int = 60
    """How often the "AE didn't reply" timer is checked."""
    workflow_sweep_seconds: int = 30
    """How often ``pending`` workflow runs (the hand-offs between workflows) are picked up."""

    # Manual tier override endpoint (POST /onboardings/{id}/plan-tier)
    admin_api_token: str = ""

    # Rocketlane
    rocketlane_timeout_seconds: float = 180
    """How long to wait for a Rocketlane response; creating a project with a template can take a while."""
    rocketlane_enabled: bool = True         # false: skip the Rocketlane step and go straight to the Slack channel
    rocketlane_api_key: str = ""
    rocketlane_base_url: str = "https://api.rocketlane.com/api/1.0"

    # Slack (communication agent). Bot token scopes: channels:manage (or
    # groups:write for private channels), chat:write, conversations.connect:write.
    slack_bot_token: str = ""
    """xoxb-… bot token. Leave empty to skip the Slack step entirely."""
    slack_base_url: str = "https://slack.com/api"
    slack_channel_is_private: bool = True
    slack_tool_retry_attempts: int = 3
    """How many times a Slack call is tried when the failure is temporary (rate limit, 5xx, network)."""
    slack_tool_retry_base_seconds: float = 1.0
    """First wait between those tries; it doubles each time."""
    communication_agent_max_steps: int = 14
    """Upper bound on LLM turns in the Slack agent; whatever is left is completed by the standard flow."""


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached Settings instance."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
