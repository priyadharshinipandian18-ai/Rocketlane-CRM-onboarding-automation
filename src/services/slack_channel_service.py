from src.models.entities import SlackChannel
from src.models.enums import SlackInviteStatus
from src.services.base_service import BaseService


class SlackChannelService(BaseService[SlackChannel]):
    def __init__(self) -> None:
        super().__init__(SlackChannel)

    async def get_by_onboarding(self, onboarding_id: str) -> SlackChannel | None:
        """Checked before creating a channel — a ``slack_channel_id`` means it
        already exists (reuse it); a ``welcome_message_ts`` means the welcome
        was already posted (never post it twice)."""
        return await self.find_one(onboarding_id=onboarding_id)

    async def create_pending(self, onboarding_id: str, channel_name: str, topic: str) -> SlackChannel:
        return await self.save(SlackChannel(onboarding_id=onboarding_id, channel_name=channel_name, topic=topic))

    async def mark_created(self, onboarding_id: str, slack_channel_id: str, channel_name: str) -> None:
        await self.execute(
            """
            UPDATE app.slack_channels
            SET status = 'created', slack_channel_id = %s, channel_name = %s, error = NULL
            WHERE onboarding_id = %s
            """,
            (slack_channel_id, channel_name, onboarding_id),
        )

    async def set_invite(self, onboarding_id: str, shared_invite_id: str | None, invite_status: SlackInviteStatus | str) -> None:
        await self.execute(
            "UPDATE app.slack_channels SET shared_invite_id = %s, invite_status = %s WHERE onboarding_id = %s",
            (shared_invite_id, SlackInviteStatus(invite_status).value, onboarding_id),
        )

    async def set_welcome_ts(self, onboarding_id: str, welcome_message_ts: str) -> None:
        await self.execute(
            "UPDATE app.slack_channels SET welcome_message_ts = %s WHERE onboarding_id = %s",
            (welcome_message_ts, onboarding_id),
        )

    async def mark_failed(self, onboarding_id: str, error: str) -> None:
        await self.execute(
            "UPDATE app.slack_channels SET status = 'failed', error = %s WHERE onboarding_id = %s",
            (error, onboarding_id),
        )
