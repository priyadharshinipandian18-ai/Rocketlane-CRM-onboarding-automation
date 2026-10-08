from src.agents.email_analyser.state import EmailAnalyserState


class IntakeState(EmailAnalyserState, total=False):
    """State of the intake workflow (thread ``intake:<inbound_email_id>``).

    Inherits the classifier's state (the redacted email and the classification) and
    adds the deal fields ``extract_deal`` fills in for a closed deal.
    """

    customer_name: str
    customer_contact_name: str
    customer_contact_email: str
    ae_name: str
    ae_email: str
    ae_id: str | None
    sf_opportunity_id: str | None
    deal_summary: str
