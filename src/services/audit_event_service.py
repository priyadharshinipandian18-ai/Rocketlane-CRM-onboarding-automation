from typing import Any

from src.models.entities import AuditEvent
from src.services.base_service import BaseService


class AuditEventService(BaseService[AuditEvent]):
    def __init__(self) -> None:
        super().__init__(AuditEvent)

    async def log_event(
        self, onboarding_id: str | None, actor: str, event_type: str, payload: dict[str, Any] | None = None
    ) -> None:
        await self.save(AuditEvent(onboarding_id=onboarding_id, actor=actor, event_type=event_type, payload=payload))

    async def list_for_onboarding(self, onboarding_id: str) -> list[AuditEvent]:
        return await self.find_all(order_by="id", onboarding_id=onboarding_id)
