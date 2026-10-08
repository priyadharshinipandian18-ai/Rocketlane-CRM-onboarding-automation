"""Rocketlane client — turns a confirmed plan tier into an onboarding project.

Two calls, in this order (the provisioning node makes both):

1. ``create_project``   — ``POST /projects`` (https://developer.rocketlane.com/reference/create-project):
   auth via an ``api-key`` header, requiring ``projectName``, ``customer`` and ``owner``. The node creates the
   project empty, which is fast. (Given a ``template_id`` it could apply the template in the same call via
   ``sources``, but that is slow enough for Rocketlane's gateway to time out.)
2. ``import_template``  — ``POST /projects/{projectId}/import-template``
   (https://developer.rocketlane.com/reference/import-template): adds the template to the project from step 1;
   the body is a JSON **array** of ``{templateId, startDate, prefix?}``.
"""
import logging
from datetime import date

import httpx

from src.config.settings import get_settings

log = logging.getLogger(__name__)


class RocketlaneError(RuntimeError):
    """A Rocketlane call was refused; the message carries Rocketlane's own explanation."""


def _template_id(value: str | int) -> int | str:
    # Rocketlane documents templateId as an integer; plan_tiers.rocketlane_template_id is TEXT
    # (for flexibility against future non-numeric IDs) — cast when it's numeric.
    return int(value) if str(value).isdigit() else value


class RocketlaneClient:
    def __init__(self):
        settings = get_settings()
        self._base_url = settings.rocketlane_base_url
        self._api_key = settings.rocketlane_api_key
        self._timeout = settings.rocketlane_timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"accept": "application/json", "api-key": self._api_key, "content-type": "application/json"}

    async def _post(self, path: str, body) -> dict:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self._timeout, connect=20)) as http:
                response = await http.post(url, headers=self._headers(), json=body)
        except httpx.ReadTimeout as exc:
            # The request went out and Rocketlane may well have acted on it (creating a project with a
            # template is slow): we just don't know. Say so, so nobody retries blindly.
            raise RocketlaneError(
                f"POST {path}: no response from {httpx.URL(url).host} within {self._timeout:g}s (ReadTimeout). "
                "Rocketlane may STILL have processed the request: look for the project in Rocketlane and delete "
                "it before retrying, or a duplicate is created"
            ) from exc
        except httpx.TransportError as exc:
            # httpx's own message is empty for timeouts — say what failed and where.
            raise RocketlaneError(
                f"POST {path}: could not reach {httpx.URL(url).host} ({type(exc).__name__}) — check internet/VPN/proxy/firewall"
            ) from exc
        if response.status_code in (502, 503, 504):
            # A gateway in front of Rocketlane gave up waiting; the work behind it may well have completed.
            raise RocketlaneError(
                f"POST {path} -> {response.status_code} gateway error (Rocketlane took too long to answer). Rocketlane may "
                "STILL have processed the request: look for the project in Rocketlane and delete it before retrying, or a "
                "duplicate is created"
            )
        if response.status_code >= 400:
            # Rocketlane explains what it didn't like in the body; the bare "400 Bad Request" doesn't.
            raise RocketlaneError(f"POST {path} -> {response.status_code}: {response.text[:500]}")
        return response.json()

    @staticmethod
    def _owner(email: str, user_id: str | int | None) -> dict:
        """The project owner: the email, plus Rocketlane's numeric ``userId`` when we have it
        (``cs_team_members.rocketlane_user_id``; a non-numeric placeholder is not sent)."""
        owner: dict = {"emailId": email}
        if user_id is not None and str(user_id).isdigit():
            owner["userId"] = int(user_id)
        return owner

    async def create_project(
        self,
        customer_name: str,
        customer_contact_email: str,
        owner_email: str,
        start_date: date,
        due_date: date,
        template_id: str | int | None = None,
        owner_user_id: str | int | None = None,
        external_reference_id: str | None = None,
    ) -> dict:
        """Create the project. ``owner_email`` comes from the DB (``cs_team_members``), never from
        the LLM. With a ``template_id`` (from ``plan_tiers``) the template is applied in the same
        call, through ``sources``; without one the project is created empty and ``import_template``
        can add it afterwards. Returns Rocketlane's response (includes the new ``projectId``)."""
        body = {
            "projectName": f"{customer_name} Onboarding",
            "customer": {"companyName": customer_name},
            "autoCreateCompany": True,      # a new customer has no Rocketlane company yet: create it
            "owner": self._owner(owner_email, owner_user_id),
            "startDate": start_date.isoformat(),
            "dueDate": due_date.isoformat(),
            "teamMembers": {"customers": [{"emailId": customer_contact_email}]},
        }
        if external_reference_id:
            body["externalReferenceId"] = external_reference_id     # our onboarding id: ties the project back to us
        if template_id is not None:
            body["sources"] = [{"templateId": _template_id(template_id), "startDate": start_date.isoformat()}]
        data = await self._post("/projects", body)
        log.info("Rocketlane project created | projectId=%s customer=%s template=%s",
                 data.get("projectId"), customer_name, template_id)
        return data

    async def import_template(self, project_id: str | int, template_id: str | int, start_date: date) -> dict:
        """Import the tier's onboarding template (its phases and tasks) into an existing project.
        ``template_id`` comes from ``plan_tiers`` — the LLM never chooses a template."""
        body = [{"templateId": _template_id(template_id), "startDate": start_date.isoformat()}]
        data = await self._post(f"/projects/{project_id}/import-template", body)
        log.info("Rocketlane template imported | projectId=%s templateId=%s", project_id, template_id)
        return data
