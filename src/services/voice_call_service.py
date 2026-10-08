import json
from typing import Any

from src.models.entities import VoiceCall
from src.models.enums import VoiceCallOutcome, VoiceCallStatus
from src.services.base_service import BaseService


class VoiceCallService(BaseService[VoiceCall]):
    def __init__(self) -> None:
        super().__init__(VoiceCall)

    async def get_attempt(self, onboarding_id: str, attempt_no: int) -> VoiceCall | None:
        return await self.find_one(onboarding_id=onboarding_id, attempt_no=attempt_no)

    async def create_attempt(self, onboarding_id: str, attempt_no: int, provider: str, to_phone: str) -> VoiceCall:
        """Insert a ``queued`` row before placing the call — or return the
        existing row for this attempt if the node is being replayed after a
        crash. ``UNIQUE (onboarding_id, attempt_no)`` makes this idempotent.
        """
        call = await self.fetch_one(
            """
            INSERT INTO app.voice_calls (onboarding_id, attempt_no, provider, to_phone)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (onboarding_id, attempt_no) DO UPDATE SET to_phone = EXCLUDED.to_phone
            RETURNING *
            """,
            (onboarding_id, attempt_no, provider, to_phone),
        )
        assert call is not None
        return call

    async def set_provider_call_id(self, call_id: str, provider_call_id: str) -> None:
        """Called right after the Voice AI API confirms the call — this is what
        the result later joins against (``provider_call_id`` is where the
        async correlation happens)."""
        await self.execute(
            "UPDATE app.voice_calls SET provider_call_id = %s, status = 'queued', started_at = now() WHERE id = %s",
            (provider_call_id, call_id),
        )

    async def mark_place_failed(self, call_id: str, error: str) -> None:
        """The provider API rejected the call (bad number, auth, quota...).
        Counts as a failed attempt; the graph schedules a retry."""
        await self.execute(
            """
            UPDATE app.voice_calls
            SET status = 'failed', outcome = 'failed', notes = %s, ended_at = now()
            WHERE id = %s
            """,
            (error[:1000], call_id),
        )

    async def get_by_provider_call_id(self, provider_call_id: str) -> VoiceCall | None:
        return await self.find_one(provider_call_id=provider_call_id)

    async def record_tier_answer(
        self,
        provider_call_id: str,
        plan: str,
        readback_confirmed: bool,
        notes: str | None,
        confidence: float | None = None,
    ) -> VoiceCall | None:
        """Store what the AE said (from the voice agent's extractions).
        ``plan='unknown'`` is stored as NULL — ``detected_plan_tier``
        references ``app.plan_tiers``."""
        detected = plan if plan in ("enterprise", "growth") else None
        return await self.fetch_one(
            """
            UPDATE app.voice_calls
            SET detected_plan_tier = %s, readback_confirmed = %s, notes = %s, confidence = %s
            WHERE provider_call_id = %s
            RETURNING *
            """,
            (detected, bool(readback_confirmed and detected), notes, confidence, provider_call_id),
        )

    async def update_status(self, provider_call_id: str, status: VoiceCallStatus | str) -> None:
        """Live status while a call is in flight. Never touches a call that has
        already been finalised."""
        await self.execute(
            "UPDATE app.voice_calls SET status = %s WHERE provider_call_id = %s AND ended_at IS NULL",
            (VoiceCallStatus(status).value, provider_call_id),
        )

    async def finalize_once(
        self,
        provider_call_id: str,
        ended_reason: str | None,
        transcript: str | None,
        recording_url: str | None,
        duration_seconds: int | None,
        raw_webhook: dict[str, Any],
    ) -> VoiceCall | None:
        """Atomically mark the call ended. Returns the row the FIRST time only;
        a repeated terminal result gets ``None`` — that's what stops the graph
        from being resumed twice for one call."""
        return await self.fetch_one(
            """
            UPDATE app.voice_calls
            SET status = 'completed', ended_reason = %s, transcript = %s, recording_url = %s,
                duration_seconds = %s, raw_webhook = %s, ended_at = now()
            WHERE provider_call_id = %s AND ended_at IS NULL
            RETURNING *
            """,
            (ended_reason, transcript, recording_url, duration_seconds, json.dumps(raw_webhook), provider_call_id),
        )

    async def set_outcome(self, call_id: str, outcome: VoiceCallOutcome | str) -> None:
        await self.execute(
            "UPDATE app.voice_calls SET outcome = %s WHERE id = %s", (VoiceCallOutcome(outcome).value, call_id)
        )

    async def list_in_flight(self, provider: str, limit: int = 50) -> list[VoiceCall]:
        """Calls we placed that haven't finished yet — what the poller checks."""
        return await self.fetch_all(
            """
            SELECT * FROM app.voice_calls
            WHERE provider = %s AND provider_call_id IS NOT NULL AND ended_at IS NULL
            ORDER BY created_at
            LIMIT %s
            """,
            (provider, limit),
        )
