from src.models.base_model import BaseModel


class AccountExecutive(BaseModel):
    """Who to call. The deal email has neither phone nor timezone, so both come from here."""

    __table_name__ = "account_executives"

    id: str | None = None
    name: str
    email: str
    phone_e164: str
    timezone: str = "UTC"
    slack_user_id: str | None = None
    active: bool = True
