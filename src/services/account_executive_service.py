from src.models.entities import AccountExecutive
from src.services.base_service import BaseService


class AccountExecutiveService(BaseService[AccountExecutive]):
    def __init__(self) -> None:
        super().__init__(AccountExecutive)

    async def get_by_email(self, email: str) -> AccountExecutive | None:
        """Match on the sender's address — more reliable than a name parsed
        from the email body. Case-insensitive; inactive AEs never match."""
        return await self.fetch_one(
            "SELECT * FROM app.account_executives WHERE lower(email) = lower(%s) AND active",
            (email,),
        )
