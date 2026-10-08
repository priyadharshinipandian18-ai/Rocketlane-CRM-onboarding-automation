from enum import Enum


class ErrorCode(str, Enum):
    BAD_REQUEST = "bad_request"
    CONFIG_MISSING = "config_missing"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UPSTREAM_ERROR = "upstream_error"


_DEFAULT_STATUS = {
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.CONFIG_MISSING: 500,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.UPSTREAM_ERROR: 502,
}


class AppException(Exception):
    """Raised by services and routers; ``register_exception_handlers`` turns it
    into ``{"error": {"code": ..., "message": ...}}`` with the right HTTP status."""

    def __init__(self, code: ErrorCode, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code or _DEFAULT_STATUS[code]
