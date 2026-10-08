from src.agents.communication.state import CommunicationState


class ProvisioningState(CommunicationState, total=False):
    """State of the provisioning workflow (thread ``provision:<onboarding_id>``).

    Inherits the communication agent's state (identity, ``plan_tier``, the customer
    contact, ``rocketlane_project_id`` and the ``slack_*`` keys). The input is
    tiny — ``onboarding_id`` and ``plan_tier`` plus the customer's name and
    contact; everything else the nodes read from the database.
    """

    customer_name: str
