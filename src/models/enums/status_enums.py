from enum import Enum


class InboundEmailStatus(str, Enum):
    PENDING = "pending"
    IGNORED = "ignored"
    PARSED = "parsed"
    FAILED = "failed"


class OnboardingStatus(str, Enum):
    RECEIVED = "received"
    AWAITING_TIER_CONFIRMATION = "awaiting_tier_confirmation"
    TIER_CONFIRMED = "tier_confirmed"
    PROJECT_CREATED = "project_created"
    CHANNEL_CREATED = "channel_created"
    COMPLETED = "completed"
    NEEDS_HUMAN_REVIEW = "needs_human_review"
    FAILED = "failed"


class VoiceCallStatus(str, Enum):
    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"


class VoiceCallOutcome(str, Enum):
    CONFIRMED = "confirmed"
    UNCLEAR = "unclear"
    NO_ANSWER = "no_answer"
    VOICEMAIL = "voicemail"
    FAILED = "failed"
    DECLINED = "declined"


class RocketlaneProjectStatus(str, Enum):
    CREATING = "creating"
    CREATED = "created"
    FAILED = "failed"


class SlackChannelStatus(str, Enum):
    CREATING = "creating"
    CREATED = "created"
    FAILED = "failed"


class SlackInviteStatus(str, Enum):
    SENT = "sent"
    ACCEPTED = "accepted"
    FAILED = "failed"


class EscalationStatus(str, Enum):
    SENT = "sent"
    ANSWERED = "answered"
    SUPERSEDED = "superseded"
    """Replaced by a newer attempt (the request was re-opened)."""
    FAILED = "failed"


class WorkflowName(str, Enum):
    INTAKE = "intake"
    """One per inbound email: classify it; for a closed deal, create the onboarding."""
    TIER_CONFIRMATION = "tier_confirmation"
    """One per onboarding: call the AE, email the AE, wait for the plan tier."""
    PROVISIONING = "provisioning"
    """One per onboarding: create the Rocketlane project, then the Slack channel."""


class WorkflowRunStatus(str, Enum):
    PENDING = "pending"
    """Created, not started yet (the hand-off between two workflows)."""
    RUNNING = "running"
    WAITING = "waiting"
    """Paused at an ``interrupt()``; ``waiting_for`` says why."""
    STOPPED = "stopped"
    """Gave up waiting for the AE's reply; still parked in the checkpointer and resumable."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    """Raised at ``current_node``; ``POST /onboardings/{id}/retry`` re-runs from there."""


class WaitingFor(str, Enum):
    CALL_RESULT = "call_result"
    CALL_RETRY = "call_retry"
    EMAIL_REPLY = "email_reply"


class EmailKind(str, Enum):
    DEAL_CLOSED = "deal_closed"
    NO_DEAL = "no_deal"
    ESCALATION_REPLY = "escalation_reply"
    MALICIOUS = "malicious"


class EmailKindSource(str, Enum):
    THREAD_MATCH = "thread_match"
    """The email is in the Gmail thread of an escalation we sent."""
    TOKEN = "token"
    """Its subject carries an escalation token (``[ONB-…]``)."""
    LLM = "llm"
    RULE = "rule"
