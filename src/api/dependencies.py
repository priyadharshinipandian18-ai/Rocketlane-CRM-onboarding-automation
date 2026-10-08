"""Shared FastAPI dependencies — shared-secret checks used by several routers."""
import hmac
from typing import Optional

from fastapi import Header, Request

from src.api.constants import X_ADMIN_TOKEN, X_FORWARDED_FOR
from src.api.exceptions import AppException, ErrorCode
from src.config.settings import get_settings


def token_ok(expected: str, *candidates: Optional[str]) -> bool:
    """Constant-time check that fails closed when the secret isn't configured."""
    if not expected:
        return False
    return any(c and hmac.compare_digest(c, expected) for c in candidates)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get(X_FORWARDED_FOR)
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


async def require_admin_token(x_admin_token: Optional[str] = Header(default=None, alias=X_ADMIN_TOKEN)) -> None:
    if not token_ok(get_settings().admin_api_token, x_admin_token):
        raise AppException(ErrorCode.UNAUTHORIZED, "invalid admin token")
