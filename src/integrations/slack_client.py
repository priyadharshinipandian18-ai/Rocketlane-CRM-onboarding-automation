"""Slack Web API client — creates the shared onboarding channel.

Plain ``httpx`` calls to https://api.slack.com/methods (no extra SDK).
Slack answers HTTP 200 with ``{"ok": false, "error": "..."}`` on failure, so
every call goes through ``_call`` which raises ``SlackApiError`` for that case.
"""
import asyncio
import logging

import httpx

from src.config.settings import get_settings

log = logging.getLogger(__name__)


class SlackApiError(Exception):
    def __init__(self, method: str, error: str):
        super().__init__(f"Slack {method} failed: {error}")
        self.method = method
        self.error = error


class SlackClient:
    def __init__(self):
        settings = get_settings()
        self._base_url = settings.slack_base_url
        self._token = settings.slack_bot_token
        self._private = settings.slack_channel_is_private

    async def _call(self, method: str, payload: dict, form: bool = False) -> dict:
        # A few methods (users.lookupByEmail) only accept form-encoded parameters, not a JSON body.
        body = {"data": payload} if form else {"json": payload}
        async with httpx.AsyncClient(timeout=30) as http:
            for attempt in (1, 2):
                response = await http.post(
                    f"{self._base_url}/{method}",
                    headers={"Authorization": f"Bearer {self._token}"},
                    **body,
                )
                if response.status_code == 429 and attempt == 1:
                    delay = int(response.headers.get("Retry-After", "1"))
                    log.warning("Slack rate-limited %s; retrying in %ss", method, delay)
                    await asyncio.sleep(delay)
                    continue
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    error = data.get("error", "unknown_error")
                    if error == "missing_scope":
                        # Slack says exactly which scope the token lacks (and which it has).
                        error += f" (needed: {data.get('needed')}, token has: {data.get('provided')})"
                    raise SlackApiError(method, error)
                return data
        raise SlackApiError(method, "ratelimited")

    async def create_channel(self, name: str) -> str:
        """Create the channel; returns its ID. Raises ``SlackApiError`` with
        ``error == "name_taken"`` if the name is already in use.
        """
        data = await self._call("conversations.create", {"name": name, "is_private": self._private})
        return data["channel"]["id"]

    async def set_topic(self, channel_id: str, topic: str) -> None:
        await self._call("conversations.setTopic", {"channel": channel_id, "topic": topic})

    async def invite_users(self, channel_id: str, user_ids: list[str]) -> None:
        """Add workspace members. ``force`` skips invalid IDs rather than
        failing the whole batch.
        """
        await self._call(
            "conversations.invite", {"channel": channel_id, "users": ",".join(user_ids), "force": True}
        )

    async def lookup_user_id_by_email(self, email: str) -> str | None:
        """Slack member ID for a workspace member's email (needs ``users:read.email``);
        None if there is no such member."""
        try:
            data = await self._call("users.lookupByEmail", {"email": email}, form=True)
        except SlackApiError as exc:
            if exc.error == "users_not_found":
                return None
            raise
        return data["user"]["id"]

    async def post_message(self, channel_id: str, text: str) -> str:
        """Post a mrkdwn message; returns its ``ts``."""
        data = await self._call(
            "chat.postMessage",
            {"channel": channel_id, "text": text, "mrkdwn": True, "unfurl_links": False},
        )
        return data["ts"]

    async def invite_shared(self, channel_id: str, email: str) -> str | None:
        """Slack Connect: email the customer an invite to join the channel.
        Needs a Slack Connect-enabled workspace and the
        ``conversations.connect:write`` scope. Returns the invite ID.
        """
        data = await self._call("conversations.inviteShared", {"channel": channel_id, "emails": [email]})
        return data.get("invite_id")
