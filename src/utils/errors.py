"""Which failures are worth retrying later, and which will fail the same way every time."""
import socket

from googleapiclient.errors import HttpError

from src.gmail.auth import GmailAuthError


def is_transient(exc: BaseException) -> bool:
    """``True`` for failures that may well succeed later: network trouble, Google 429/5xx, a missing
    OAuth permission or an expired credential (a person can fix those, after which the retry works).

    Anything else — a bug, bad data in one message — will fail identically on every retry, so
    retrying it only produces noise and blocks the messages behind it.
    """
    if isinstance(exc, GmailAuthError):
        return True
    if isinstance(exc, HttpError):
        return exc.resp.status in (401, 403, 408, 429) or exc.resp.status >= 500
    if isinstance(exc, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
        return True
    return type(exc).__name__ in {"RefreshError", "TransportError", "PoolTimeout", "OperationalError"}
