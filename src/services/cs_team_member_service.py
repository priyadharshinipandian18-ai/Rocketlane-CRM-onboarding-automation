from src.models.entities import CsTeamMember
from src.services.base_service import BaseService


class CsTeamMemberService(BaseService[CsTeamMember]):
    def __init__(self) -> None:
        super().__init__(CsTeamMember)

    async def list_active_managers(self) -> list[CsTeamMember]:
        """Everyone a stalled onboarding is escalated to."""
        return await self.find_all(order_by="name", role="cs_manager", active=True)

    async def get_owner_and_manager(self, pool_name: str) -> tuple[CsTeamMember | None, CsTeamMember | None]:
        """Pick the Project Owner (a CSM) and Project Manager for a tier's pool
        ('enterprise' = dedicated, 'growth' = pooled).

        Rocketlane's own escalation automation notifies the Project Manager
        after 1 day overdue and the Project Owner after 4 days — both only
        fire correctly if the project is created with both set.
        """
        csm = await self.fetch_one(
            """
            SELECT * FROM app.cs_team_members
            WHERE role = 'csm' AND pool = %s AND active
            ORDER BY random() LIMIT 1
            """,
            (pool_name,),
        )
        manager = await self.fetch_one(
            """
            SELECT * FROM app.cs_team_members
            WHERE role = 'cs_manager' AND (pool = %s OR pool IS NULL) AND active
            ORDER BY random() LIMIT 1
            """,
            (pool_name,),
        )
        return csm, manager
