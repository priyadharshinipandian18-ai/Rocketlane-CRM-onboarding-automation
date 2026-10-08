from src.agents.tier_confirmation.tier_reply import extract_token
from src.models.entities import Escalation
from src.models.enums import EmailKindSource, PlanTierCode
from src.services.base_service import BaseService


class EscalationService(BaseService[Escalation]):
    def __init__(self) -> None:
        super().__init__(Escalation)

    async def create_attempt(self, onboarding_id: str, workflow_run_id: str, token: str, to_email: str) -> Escalation:
        """Record a new request to the AE. Any earlier one still ``sent`` is superseded
        (a re-opened request), so there is only ever one live attempt."""
        await self.execute(
            "UPDATE app.escalations SET status = 'superseded' WHERE onboarding_id = %s AND status = 'sent'",
            (onboarding_id,),
        )
        created = await self.fetch_one(
            """
            INSERT INTO app.escalations (onboarding_id, workflow_run_id, attempt_no, token, to_email)
            SELECT %s, %s, COALESCE(MAX(attempt_no), 0) + 1, %s, %s
            FROM app.escalations WHERE onboarding_id = %s
            RETURNING *
            """,
            (onboarding_id, workflow_run_id, token, to_email, onboarding_id),
        )
        assert created is not None
        return created

    async def set_sent(self, escalation_id: str, gmail_message_id: str | None, gmail_thread_id: str | None) -> None:
        await self.execute(
            "UPDATE app.escalations SET gmail_message_id = %s, gmail_thread_id = %s WHERE id = %s",
            (gmail_message_id, gmail_thread_id, escalation_id),
        )

    async def mark_failed(self, escalation_id: str) -> None:
        await self.execute("UPDATE app.escalations SET status = 'failed' WHERE id = %s", (escalation_id,))

    async def latest_for(self, onboarding_id: str) -> Escalation | None:
        found = await self.find_all(order_by="-attempt_no", limit=1, onboarding_id=onboarding_id)
        return found[0] if found else None

    async def match_reply(self, gmail_thread_id: str | None, subject: str | None) -> tuple[Escalation, EmailKindSource] | None:
        """Which escalation (if any) is this email a reply to?

        Strongest evidence first: the Gmail thread of the email *we* sent, then the
        ``[ONB-…]`` token in the subject. Deterministic — an LLM never decides this.
        """
        if gmail_thread_id:
            by_thread = await self.fetch_one(
                "SELECT * FROM app.escalations WHERE gmail_thread_id = %s ORDER BY attempt_no DESC LIMIT 1",
                (gmail_thread_id,),
            )
            if by_thread is not None:
                return by_thread, EmailKindSource.THREAD_MATCH
        token = extract_token(subject)
        if token:
            by_token = await self.fetch_one(
                "SELECT * FROM app.escalations WHERE token = %s ORDER BY attempt_no DESC LIMIT 1", (token,)
            )
            if by_token is not None:
                return by_token, EmailKindSource.TOKEN
        return None

    async def mark_answered(
        self, onboarding_id: str, tier: PlanTierCode | str, answered_by: str, inbound_email_id: str | None = None
    ) -> None:
        """Whichever path answered (the AE's reply or the API) closes the live request."""
        await self.execute(
            """
            UPDATE app.escalations
            SET status = 'answered', answered_tier = %s, answered_by = %s,
                answered_inbound_email_id = %s, answered_at = now()
            WHERE onboarding_id = %s AND status = 'sent'
            """,
            (PlanTierCode(tier).value, answered_by, inbound_email_id, onboarding_id),
        )
