"""Personalise the shared Slack channel from the plan tier + customer details.

Pure functions — no I/O. The *tier* decides everything (name prefix, timeline,
CSM model, message template) via the ``app.plan_tiers`` row, so an Enterprise
customer can never receive Growth wording or vice-versa.
"""
import re
import unicodedata
from datetime import date

from src.models.base_model import BaseModel
from src.models.dto.comms_context import OnboardingCommsContext
from src.models.enums import CsmModel
from src.prompts import load_prompt

SLACK_NAME_MAX = 80
SLACK_TOPIC_MAX = 250

PHASES = ("Kickoff", "Data Migration", "Configuration", "Go-Live")

DEFAULT_WELCOME_TEMPLATE = load_prompt("communication/default_welcome_template")
"""The standard welcome message (used when the LLM's isn't, and when a tier has no template of its own). Its text lives
in ``src/prompts/communication/default_welcome_template.md``."""


def slugify(name: str) -> str:
    """Slack channel-name safe: lowercase ``a-z0-9`` and hyphens only."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or "customer"


def channel_name_candidates(prefix: str, customer_name: str, onboarding_id: str) -> list[str]:
    """Preferred name, then a deterministic fallback for when it's taken
    (e.g. a second deal for the same customer). The fallback derives from the
    onboarding ID so a retried run picks the same name.
    """
    base = f"{prefix}{slugify(customer_name)}".lower()
    primary = base[:SLACK_NAME_MAX].rstrip("-")
    suffix = f"-{onboarding_id.replace('-', '')[:6]}"
    fallback = base[: SLACK_NAME_MAX - len(suffix)].rstrip("-") + suffix
    return [primary, fallback]


def _fmt_date(d: date | None) -> str:
    return f"{d:%B} {d.day}, {d.year}" if d else "to be confirmed"


def _mention(name: str | None, slack_user_id: str | None, fallback: str) -> str:
    if slack_user_id:
        return f"<@{slack_user_id}>"
    return name or fallback


class _SafeDict(dict):
    """Unknown ``{placeholders}`` stay as written instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class ChannelPlan(BaseModel):
    name_candidates: list[str]
    topic: str
    welcome_text: str
    internal_user_ids: list[str]
    missing_csm_slack_id: bool


def build_channel_plan(ctx: OnboardingCommsContext, onboarding_id: str) -> ChannelPlan:
    dedicated = ctx.csm_model == CsmModel.DEDICATED
    csm = _mention(ctx.csm_name, ctx.csm_slack_user_id, "your CSM")
    manager = _mention(ctx.manager_name, ctx.manager_slack_user_id, "our CS manager")
    ae = _mention(ctx.ae_name, ctx.ae_slack_user_id, "your account executive")
    go_live = ctx.target_go_live_date

    if dedicated:
        csm_intro = f"{csm} is your dedicated CSM for the whole onboarding."
    else:
        csm_intro = f"{csm} leads your onboarding, backed by our pooled CS team."
    csm_label = "Dedicated CSM" if dedicated else "CS lead"

    topic = (
        f"{ctx.tier_name} onboarding · {ctx.customer_name} · {ctx.duration_days}-day plan · "
        f"Go-live target {_fmt_date(go_live)} · {csm_label}: {ctx.csm_name or 'TBC'}"
    )[:SLACK_TOPIC_MAX]

    values = _SafeDict(
        customer_name=ctx.customer_name,
        contact_name=ctx.customer_contact_name or "there",
        tier_name=ctx.tier_name,
        duration_days=ctx.duration_days,
        start_date=_fmt_date(ctx.start_date),
        go_live_date=_fmt_date(go_live),
        csm_name=ctx.csm_name or "your CSM",
        csm_mention=csm,
        csm_intro=csm_intro,
        manager_mention=manager,
        ae_mention=ae,
        phases="\n".join(f"{i}. {phase}" for i, phase in enumerate(PHASES, start=1)),
        project_url=ctx.project_url or "",
        project_line=f"\nTrack every task in your project: {ctx.project_url}" if ctx.project_url else "",
    )
    template = (ctx.welcome_message_template or "").strip() or DEFAULT_WELCOME_TEMPLATE
    try:
        welcome = template.format_map(values)
    except (ValueError, IndexError, AttributeError):
        # Malformed template in the DB — fall back rather than skip the welcome.
        welcome = DEFAULT_WELCOME_TEMPLATE.format_map(values)

    internal = [uid for uid in (ctx.csm_slack_user_id, ctx.manager_slack_user_id, ctx.ae_slack_user_id) if uid]
    return ChannelPlan(
        name_candidates=channel_name_candidates(ctx.slack_channel_prefix, ctx.customer_name, onboarding_id),
        topic=topic,
        welcome_text=welcome,
        internal_user_ids=list(dict.fromkeys(internal)),
        missing_csm_slack_id=not ctx.csm_slack_user_id,
    )
