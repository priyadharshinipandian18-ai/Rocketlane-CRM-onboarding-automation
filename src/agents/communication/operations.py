"""The Slack side effects of the communication agent: each one idempotent, retried when the failure is
temporary, and reporting problems as ``OpError`` instead of raw exceptions.

Both the agent's tools (``tools.py``) and the deterministic completion in the finalizer (``nodes.py``)
call these, so there is exactly one implementation of "create the channel", "post the welcome", and so on.
Every operation writes to / checks ``app.slack_channels`` first, so repeating it (a retry, a rerun, the LLM
calling a tool twice) never creates a second channel, welcome message or invite.
"""
import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from src.agents.communication.channel_plan import build_channel_plan
from src.config.settings import get_settings
from src.integrations.slack_client import SlackApiError, SlackClient
from src.models.dto.comms_context import OnboardingCommsContext
from src.models.enums import SlackInviteStatus
from src.services import OnboardingService, SlackChannelService

log = logging.getLogger(__name__)

ROLES = ("csm", "manager", "ae")
TRANSIENT_SLACK_ERRORS = {
    "ratelimited", "rate_limited", "internal_error", "service_unavailable", "fatal_error", "request_timeout", "timeout",
}
_CHANNEL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")


class OpError(Exception):
    """An operation failed. ``code`` is Slack's error code (or ours); ``retryable`` says whether trying
    again could help (temporary failures have already been retried before this is raised)."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, SlackApiError):
        return exc.error.split(" ")[0]                  # "missing_scope (needed: …)" → "missing_scope"
    return type(exc).__name__


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, SlackApiError):
        return _error_code(exc) in TRANSIENT_SLACK_ERRORS
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500 or exc.response.status_code == 429
    return isinstance(exc, httpx.TransportError)


def _describe(exc: BaseException) -> str:
    return exc.error if isinstance(exc, SlackApiError) else f"{type(exc).__name__}: {exc}".rstrip(": ")


async def with_retry(label: str, call):
    """Run ``await call()``; temporary failures are retried with a doubling wait, anything else is raised
    as an ``OpError`` straight away (a permanent failure fails the same way every time)."""
    settings = get_settings()
    attempts = max(1, settings.slack_tool_retry_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:
            transient = _is_transient(exc)
            if transient and attempt < attempts:
                delay = settings.slack_tool_retry_base_seconds * 2 ** (attempt - 1)
                log.warning("Slack %s failed (%s) — retry %d/%d in %.1fs", label, _describe(exc), attempt, attempts - 1, delay)
                await _sleep(delay)
                continue
            raise OpError(_error_code(exc), f"{label}: {_describe(exc)}", retryable=transient) from exc


def validate_channel_name(name: str, prefix: str) -> str:
    """The model proposes a channel name; only a Slack-safe name carrying the tier's prefix is accepted."""
    cleaned = (name or "").strip().lower().lstrip("#")
    if not _CHANNEL_NAME.match(cleaned):
        raise OpError("invalid_name", f"'{name}' is not a valid channel name (lowercase letters, digits, - and _ only, max 80)")
    if prefix and not cleaned.startswith(prefix.lower()):
        raise OpError("invalid_name", f"the channel name must start with '{prefix}'")
    return cleaned


@dataclass
class TeamResult:
    user_ids: dict[str, str] = field(default_factory=dict)      # role → Slack member ID
    warnings: list[str] = field(default_factory=list)


@dataclass
class ChannelResult:
    channel_id: str
    name: str
    reused: bool = False


@dataclass
class InviteResult:
    status: str                       # "sent" | "failed"
    error: str | None = None
    already: bool = False


class ChannelOps:
    """All Slack work for one onboarding."""

    def __init__(self, onboarding_id: str) -> None:
        self.onboarding_id = onboarding_id
        self.channels = SlackChannelService()
        self.slack = SlackClient()
        self._ctx: OnboardingCommsContext | None = None
        self._loaded = False

    async def context(self) -> OnboardingCommsContext | None:
        if not self._loaded:
            self._ctx = await OnboardingService().get_comms_context(self.onboarding_id)
            self._loaded = True
        return self._ctx

    async def _need_context(self) -> OnboardingCommsContext:
        ctx = await self.context()
        if ctx is None:
            raise OpError("no_context", "this onboarding has no confirmed plan tier to build the channel from")
        return ctx

    async def _need_channel(self) -> str:
        row = await self.channels.get_by_onboarding(self.onboarding_id)
        if row is None or not row.slack_channel_id:
            raise OpError("no_channel", "the Slack channel does not exist yet: create it first")
        return row.slack_channel_id

    # ── team ────────────────────────────────────────────────────────────────

    async def resolve_team(self) -> TeamResult:
        """Slack IDs for the CSM, manager and AE: the ID stored in the database, else a lookup by email.
        Best effort: someone who can't be found is reported, never a reason to stop."""
        ctx = await self._need_context()
        result = TeamResult()
        for role in ROLES:
            stored = getattr(ctx, f"{role}_slack_user_id", None)
            email = getattr(ctx, f"{role}_email", None)
            if stored:
                result.user_ids[role] = stored
                continue
            if not email:
                continue
            try:
                user_id = await with_retry(f"look up {role} in Slack", lambda e=email: self.slack.lookup_user_id_by_email(e))
            except OpError as exc:
                log.warning("Slack lookup of %s <%s> failed: %s", role, email, exc)
                result.warnings.append(f"Could not look up {role} {email} in Slack ({exc.code}).")
                continue
            if user_id:
                result.user_ids[role] = user_id
                setattr(ctx, f"{role}_slack_user_id", user_id)       # so the template welcome can mention them too
            else:
                log.warning("No Slack member with email %s (%s)", email, role)
        if "csm" not in result.user_ids:
            result.warnings.append(
                "The CSM could not be added to the channel: no slack_user_id in app.cs_team_members and no Slack member "
                "found for their email."
            )
        return result

    async def invite_team(self, user_ids: list[str]) -> int:
        """Add internal people; returns how many were requested. ``already_in_channel`` is fine."""
        channel_id = await self._need_channel()
        unique = list(dict.fromkeys(user_ids))
        if not unique:
            return 0
        try:
            await with_retry("invite the internal team", lambda: self.slack.invite_users(channel_id, unique))
        except OpError as exc:
            if exc.code != "already_in_channel":
                raise
        return len(unique)

    # ── channel ─────────────────────────────────────────────────────────────

    async def create_channel(self, name: str) -> ChannelResult:
        """Create the channel, or return the one an earlier attempt made. The ``slack_channels`` row is written
        before the API call, so a crash in between cannot lead to a second channel."""
        existing = await self.channels.get_by_onboarding(self.onboarding_id)
        if existing and existing.slack_channel_id:
            return ChannelResult(existing.slack_channel_id, existing.channel_name, reused=True)

        ctx = await self._need_context()
        name = validate_channel_name(name, ctx.slack_channel_prefix)
        if not existing:
            await self.channels.create_pending(self.onboarding_id, name, build_channel_plan(ctx, self.onboarding_id).topic)
        channel_id = await with_retry("create the channel", lambda: self.slack.create_channel(name))
        await self.channels.mark_created(self.onboarding_id, channel_id, name)
        log.info("Slack channel created | name=%s id=%s", name, channel_id)
        return ChannelResult(channel_id, name)

    async def create_channel_with_fallback(self, candidates: list[str]) -> ChannelResult:
        """Deterministic version used by the finalizer: the preferred name, then the fallback if it is taken."""
        last: OpError | None = None
        for name in candidates:
            try:
                return await self.create_channel(name)
            except OpError as exc:
                last = exc
                if exc.code != "name_taken":
                    break
        raise last or OpError("unknown_error", "no channel name to try")

    async def set_topic(self, topic: str) -> None:
        channel_id = await self._need_channel()
        await with_retry("set the topic", lambda: self.slack.set_topic(channel_id, topic[:250]))

    # ── messages and invites ────────────────────────────────────────────────

    async def post_welcome(self, text: str) -> str:
        """Post the welcome once. ``welcome_message_ts`` is the guard against a duplicate on re-runs."""
        row = await self.channels.get_by_onboarding(self.onboarding_id)
        if row is None or not row.slack_channel_id:
            raise OpError("no_channel", "the Slack channel does not exist yet: create it first")
        if row.welcome_message_ts:
            return "already_posted"
        ts = await with_retry("post the welcome message", lambda: self.slack.post_message(row.slack_channel_id, text))
        await self.channels.set_welcome_ts(self.onboarding_id, ts)
        return "posted"

    async def invite_customer(self) -> InviteResult:
        """Slack Connect invite to the customer contact. The address comes from the database, never from the LLM.
        A failure is recorded, flagged in the channel for the CSM, and returned (not raised)."""
        row = await self.channels.get_by_onboarding(self.onboarding_id)
        if row is None or not row.slack_channel_id:
            raise OpError("no_channel", "the Slack channel does not exist yet: create it first")
        if row.invite_status == SlackInviteStatus.SENT.value:
            return InviteResult("sent", already=True)
        ctx = await self._need_context()
        email = ctx.customer_contact_email
        try:
            invite_id = await with_retry("send the Slack Connect invite", lambda: self.slack.invite_shared(row.slack_channel_id, email))
        except OpError as exc:
            log.warning("Slack Connect invite to %s failed: %s", email, exc)
            await self.channels.set_invite(self.onboarding_id, None, SlackInviteStatus.FAILED)
            try:
                await self.slack.post_message(
                    row.slack_channel_id,
                    f":warning: Couldn't send the Slack Connect invite to {email} ({exc.code}). "
                    "CSM: please invite them manually from the channel's *Add people* menu.",
                )
            except Exception:
                log.exception("Could not post the manual-invite notice either")
            return InviteResult("failed", error=exc.code)
        await self.channels.set_invite(self.onboarding_id, invite_id, SlackInviteStatus.SENT)
        return InviteResult("sent")
