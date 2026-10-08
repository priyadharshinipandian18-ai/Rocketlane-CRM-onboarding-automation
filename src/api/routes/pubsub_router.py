"""``POST /pubsub/push?token=...`` — Pub/Sub push-subscription endpoint.

An alternative to the Cloud Function / the pull subscriber: point a *push*
subscription on the Gmail topic here. Disabled unless ``PUBSUB_PUSH_TOKEN`` is
set. A 2xx acknowledges the message; an error makes Pub/Sub redeliver it.
"""
from typing import Any, Optional

from fastapi import APIRouter, Body, Query

from src.api.dependencies import token_ok
from src.api.exceptions import AppException, ErrorCode
from src.config.settings import get_settings
from src.models.dto.response import PubSubAckResponse
from src.services.gmail_intake_service import GmailIntakeService

router = APIRouter(prefix="/pubsub", tags=["pubsub"])


@router.post("/push", response_model=PubSubAckResponse)
async def pubsub_push(envelope: dict[str, Any] = Body(...), token: Optional[str] = Query(default=None)) -> PubSubAckResponse:
    if not token_ok(get_settings().pubsub_push_token, token):
        raise AppException(ErrorCode.UNAUTHORIZED, "invalid push token")
    if "message" not in envelope or "data" not in envelope["message"]:
        raise AppException(ErrorCode.BAD_REQUEST, "not a Pub/Sub push envelope")
    await GmailIntakeService().handle_pubsub_envelope(envelope)
    return PubSubAckResponse()
