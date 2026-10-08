"""Quiet, useful logging for work that fails repeatedly.

A persistent fault (Gmail permission missing, database down, an API outage) makes every
retry fail the same way. Logging a full stack trace each time buries the one line that
matters. ``FailureTracker`` logs:

* the **first** failure (and any *different* failure) with the full traceback;
* then only a one-line "still failing" summary at most every ``repeat_after`` seconds;
* a "recovered" line when a success follows failures;

and tells the caller how long to **back off** (exponential, capped) so a retry loop doesn't spin.
"""
import logging
import time
from typing import Callable


class FailureTracker:
    def __init__(
        self,
        name: str,
        logger: logging.Logger | None = None,
        repeat_after: float = 300.0,
        base_backoff: float = 5.0,
        max_backoff: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._log = logger or logging.getLogger(__name__)
        self._repeat_after = repeat_after
        self._base, self._max = base_backoff, max_backoff
        self._clock = clock
        self.consecutive = 0
        self._signature: tuple[str, str] | None = None
        self._last_logged = 0.0
        self._suppressed = 0

    @staticmethod
    def _signature_of(exc: BaseException) -> tuple[str, str]:
        return type(exc).__name__, str(exc)[:200]

    def failed(self, exc: BaseException, context: str = "") -> float:
        """Record a failure, log it sensibly, and return the seconds to back off before retrying."""
        self.consecutive += 1
        signature = self._signature_of(exc)
        now = self._clock()
        where = f"{self.name}{f' [{context}]' if context else ''}"

        if signature != self._signature or self.consecutive == 1:
            self._log.error("%s failed: %s", where, exc, exc_info=exc)       # full detail, once per distinct fault
            self._signature, self._last_logged, self._suppressed = signature, now, 0
        elif now - self._last_logged >= self._repeat_after:
            self._log.error(
                "%s is still failing (%d consecutive failures; %d similar errors not logged): %s",
                where, self.consecutive, self._suppressed, exc,
            )
            self._last_logged, self._suppressed = now, 0
        else:
            self._suppressed += 1
        return self.backoff()

    def succeeded(self) -> None:
        if self.consecutive:
            self._log.info("%s recovered after %d consecutive failure(s)", self.name, self.consecutive)
        self.consecutive, self._signature, self._suppressed = 0, None, 0

    def backoff(self) -> float:
        """0 when healthy; otherwise base * 2^(failures-1), capped."""
        if not self.consecutive:
            return 0.0
        return min(self._max, self._base * (2 ** (self.consecutive - 1)))
