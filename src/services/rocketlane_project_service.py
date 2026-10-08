import json
from datetime import date
from typing import Any

from src.models.entities import RocketlaneProject
from src.services.base_service import BaseService


class RocketlaneProjectService(BaseService[RocketlaneProject]):
    def __init__(self) -> None:
        super().__init__(RocketlaneProject)

    async def get_existing(self, onboarding_id: str) -> RocketlaneProject | None:
        """Checked before creating a project — if a ``creating`` row already
        exists (the process crashed between writing it and getting
        Rocketlane's response), the caller must not blindly create a second
        project."""
        return await self.find_one(onboarding_id=onboarding_id)

    async def create_pending(self, onboarding_id: str, template_id: str, request_payload: dict[str, Any]) -> RocketlaneProject:
        created = await self.fetch_one(
            """
            INSERT INTO app.rocketlane_projects (onboarding_id, template_id, request_payload, status)
            VALUES (%s, %s, %s, 'creating')
            RETURNING *
            """,
            (onboarding_id, template_id, json.dumps(request_payload)),
        )
        assert created is not None
        return created

    async def save_project_id(self, onboarding_id: str, rocketlane_project_id: str, project_url: str | None) -> None:
        """Step 1 done: the (empty) project exists. The row stays ``creating`` until the template is
        imported, so a retry after a failed import reuses this project instead of making another."""
        await self.execute(
            """
            UPDATE app.rocketlane_projects
            SET rocketlane_project_id = %s, project_url = %s, status = 'creating', error = NULL
            WHERE onboarding_id = %s
            """,
            (rocketlane_project_id, project_url, onboarding_id),
        )

    async def mark_created(
        self,
        onboarding_id: str,
        rocketlane_project_id: str,
        project_url: str | None,
        owner_user_id: str | None,
        manager_user_id: str | None,
        start_date: date,
        due_date: date,
    ) -> None:
        await self.execute(
            """
            UPDATE app.rocketlane_projects
            SET status = 'created', rocketlane_project_id = %s, project_url = %s,
                owner_user_id = %s, manager_user_id = %s, start_date = %s, due_date = %s
            WHERE onboarding_id = %s
            """,
            (rocketlane_project_id, project_url, owner_user_id, manager_user_id, start_date, due_date, onboarding_id),
        )

    async def note_error(self, onboarding_id: str, error: str) -> None:
        """Record a problem on a project that exists and stays ``created`` (e.g. its template was not imported)."""
        await self.execute(
            "UPDATE app.rocketlane_projects SET error = %s WHERE onboarding_id = %s", (error[:1000], onboarding_id)
        )

    async def mark_failed(self, onboarding_id: str, error: str) -> None:
        # status 'failed' with a project id means "created, template import failed" — see the node.
        await self.execute(
            "UPDATE app.rocketlane_projects SET status = 'failed', error = %s WHERE onboarding_id = %s",
            (error, onboarding_id),
        )
