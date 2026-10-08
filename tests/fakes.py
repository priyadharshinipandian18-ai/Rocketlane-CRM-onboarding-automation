"""Test doubles shared by several test modules."""
from src.services.email_service import SentEmail


class FakeEmailService:
    """Stands in for ``EmailService``: records every email instead of sending it.

    ``sent`` is class-level so a test can read what *any* service instance sent;
    ``fail`` makes every send raise, to exercise the "email failed" path.
    """

    sent: list[dict] = []
    fail: bool = False

    @classmethod
    def reset(cls) -> None:
        cls.sent, cls.fail = [], False

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def send(self, to, subject, body, thread_id=None):
        if FakeEmailService.fail:
            raise RuntimeError("gmail is down")
        FakeEmailService.sent.append({"to": to, "subject": subject, "body": body, "thread_id": thread_id})
        return SentEmail(message_id=f"m{len(FakeEmailService.sent)}", thread_id="thread-1")

    @classmethod
    def to(cls, address: str) -> list[dict]:
        return [m for m in cls.sent if m["to"] == address]
