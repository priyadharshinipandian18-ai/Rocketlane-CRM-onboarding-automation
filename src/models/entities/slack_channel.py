from datetime import datetime

from src.models.base_model import BaseModel
from src.models.enums import SlackChannelStatus, SlackInviteStatus


class SlackChannel(BaseModel):
    """The shared customer channel. ``welcome_message_ts`` guarantees the
    welcome message is posted only once."""

    __table_name__ = "slack_channels"

    id: str | None = None
    onboarding_id: str
    slack_channel_id: str | None = None
    channel_name: str
    topic: str | None = None
    shared_invite_id: str | None = None
    invite_status: SlackInviteStatus | None = None
    welcome_message_ts: str | None = None
    status: SlackChannelStatus = SlackChannelStatus.CREATING
    error: str | None = None
    created_at: datetime | None = None
