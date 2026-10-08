import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.api.exceptions.service_exceptions import AppException

log = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def _app_exception_handler(_request: Request, exc: AppException) -> JSONResponse:
        if exc.status_code >= 500:
            log.error("%s: %s", exc.code.value, exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code.value, "message": exc.message}},
        )
