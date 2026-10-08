from typing import Literal

from src.state.base_state import IdentityState, NodeReportState


class TierConfirmationState(IdentityState, NodeReportState, total=False):
    """State of the tier-confirmation workflow (thread ``tier:<onboarding_id>``).

    Deliberately small: who we're confirming for, the call bookkeeping, and the
    answer. Everything else is re-read from the database by the nodes.
    """

    # ── The deal (copied from the onboarding when the run starts) ────────
    customer_name: str
    customer_contact_name: str
    customer_contact_email: str
    ae_name: str
    ae_email: str
    ae_id: str | None

    # ── The call to the AE ────────────────────────────────────────────
    call_attempts: int
    """Calls actually placed so far (a deferred-to-business-hours slot doesn't count)."""
    call_placement: Literal["placed", "deferred", "failed", "no_phone"]
    """Result of the most recent place_plan_tier_call run — drives routing."""
    voice_call_id: str | None
    """Bolna execution id of the most recent attempt."""
    call_outcome: Literal["confirmed", "unclear", "no_answer", "voicemail", "failed"] | None
    next_call_at: str | None
    """ISO timestamp of the next scheduled attempt, if waiting."""

    # ── The answer ────────────────────────────────────────────────────
    plan_tier: Literal["enterprise", "growth"] | None
    plan_tier_source: Literal["voice_call", "manual_override"] | None
