"""Gmail OAuth scope handling: reading mail must keep working with a token that was granted before
``gmail.send`` existed; only *sending* needs that permission."""
import json
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

from types import SimpleNamespace

import pytest

from src.gmail import auth

OLD_TOKEN_SCOPES = list(auth.READ_SCOPES)                    # a token from before the escalation emails
NEW_TOKEN_SCOPES = [*auth.READ_SCOPES, auth.SEND_SCOPE]


@pytest.fixture
def token(tmp_path, monkeypatch):
    def make(scopes):
        path = tmp_path / "token.json"
        path.write_text(json.dumps({"token": "t", "refresh_token": "r", "scopes": scopes}), encoding="utf-8")
        monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(
            gmail_token_path=str(path), gmail_credentials_path=str(tmp_path / "credentials.json")))
        loaded = {}

        def fake_from_file(_path, scopes):
            loaded["scopes"] = list(scopes)
            return SimpleNamespace(valid=True)

        monkeypatch.setattr(auth.Credentials, "from_authorized_user_file", staticmethod(fake_from_file))
        monkeypatch.setattr(auth, "build", lambda *a, **k: "gmail-service")
        return loaded

    return make


def test_reading_works_with_a_token_granted_before_gmail_send_existed(token):
    loaded = token(OLD_TOKEN_SCOPES)
    assert auth.get_gmail_service() == "gmail-service"
    assert loaded["scopes"] == sorted(OLD_TOKEN_SCOPES)          # refreshes never ask for more than was granted


def test_sending_with_such_a_token_says_exactly_what_to_do(token):
    token(OLD_TOKEN_SCOPES)
    with pytest.raises(auth.GmailAuthError) as error:
        auth.get_gmail_service(send=True)
    message = str(error.value)
    assert auth.SEND_SCOPE in message and "register_gmail_watch.py" in message and "HUMAN_EMAIL_ENABLED" in message


def test_sending_works_once_the_permission_was_granted(token):
    token(NEW_TOKEN_SCOPES)
    assert auth.get_gmail_service(send=True) == "gmail-service"


def test_a_token_without_read_permission_is_refused_in_the_app(token):
    token([auth.SEND_SCOPE])
    with pytest.raises(auth.GmailAuthError):
        auth.get_gmail_service()


def test_the_email_service_asks_for_the_send_permission_and_nothing_else_does(monkeypatch):
    from src.services import email_service

    built = []
    monkeypatch.setattr(email_service, "GmailClient", lambda **kwargs: built.append(kwargs) or SimpleNamespace(
        send_message=lambda *a: {"id": "m", "threadId": "t"}))
    import asyncio

    asyncio.run(email_service.EmailService().send("ae@x.co", "s", "b"))
    assert built == [{"send": True}]
