from datetime import date

from src.models.base_model import BaseModel
from src.models.enums import CsmModel


class OnboardingCommsContext(BaseModel):
    """Read model: everything the communication agent personalises the Slack
    channel with — the onboarding, its tier's configuration, the CSM /
    manager / AE (with Slack IDs) and the Rocketlane project link, joined in
    one query (``OnboardingService.get_comms_context``). Not a table.
    """

    id: str
    customer_name: str
    customer_contact_name: str | None = None
    customer_contact_email: str
    start_date: date | None = None
    target_go_live_date: date | None = None
    tier_name: str
    duration_days: int
    csm_model: CsmModel
    slack_channel_prefix: str
    welcome_message_template: str = ""
    csm_name: str | None = None
    csm_email: str | None = None
    csm_slack_user_id: str | None = None
    manager_name: str | None = None
    manager_email: str | None = None
    manager_slack_user_id: str | None = None
    ae_name: str | None = None
    ae_email: str | None = None
    ae_slack_user_id: str | None = None
    project_url: str | None = None
