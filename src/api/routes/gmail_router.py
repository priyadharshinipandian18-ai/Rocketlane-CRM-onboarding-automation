from fastapi import APIRouter, Depends

from src.api.dependencies import require_admin_token
from src.models.dto.response import GmailWatchResponse
from src.services.gmail_watch_service import GmailWatchService

router = APIRouter(prefix="/gmail", tags=["gmail"], dependencies=[Depends(require_admin_token)])


@router.post("/watch", response_model=GmailWatchResponse)
async def register_watch() -> GmailWatchResponse:
    """Register or renew Gmail push notifications (they expire after 7 days —
    call this on a schedule, e.g. Cloud Scheduler)."""
    return await GmailWatchService().register()
