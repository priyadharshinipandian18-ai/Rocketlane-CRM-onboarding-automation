from src.models.entities import PlanTier
from src.services.base_service import BaseService


class PlanTierService(BaseService[PlanTier]):
    def __init__(self) -> None:
        super().__init__(PlanTier)

    async def get_by_code(self, code: str) -> PlanTier | None:
        """The template / timeline / CSM model / Slack tone for a confirmed
        tier — all from one row, keyed by the confirmed ``code``. The LLM never
        chooses a template; it only ever produces ``code``, and this lookup
        does the rest.
        """
        return await self.get_by_id(code)
