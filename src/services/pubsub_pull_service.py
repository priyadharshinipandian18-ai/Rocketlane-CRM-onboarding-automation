"""Local-dev stand-in for the deployed Cloud Function.

A push-triggered Cloud Function can't be invoked locally without deploying it
or exposing a public endpoint for the Pub/Sub topic to push to. This pulls from
the same subscription instead — same handling (``GmailIntakeService``),
different transport.

Pub/Sub invokes callbacks on its own thread pool, so the async handling is
submitted to one event loop with ``run_coroutine_threadsafe``: inside the app
that is the server's loop (``start``); as a standalone process it is a loop
running in a background thread (``run_blocking``).

Failure handling (so a persistent fault doesn't become an endless stack-trace loop):

* a notification only says "something changed" — the real work is a diff from the stored
  history cursor — so nothing is lost by *not* retrying one that can never succeed;
* **transient** failures (network, Google 5xx/429, a missing Gmail permission) are ``nack``-ed
  *after a backoff* (5s doubling up to 5 min), so redelivery doesn't spin;
* **permanent** failures (a bug, a malformed notification) are logged and ``ack``-ed;
* logging is throttled by ``FailureTracker``: full detail for the first occurrence, then a
  one-line summary every few minutes, then a "recovered" line.
"""
import asyncio
import json
import logging
import sys
import threading

from google.cloud import pubsub_v1

from src.config.settings import get_settings
from src.services.gmail_intake_service import GmailIntakeService
from src.utils.errors import is_transient
from src.utils.failure_tracker import FailureTracker

log = logging.getLogger(__name__)


class PubSubPullService:
    def __init__(self, intake: GmailIntakeService | None = None, failures: FailureTracker | None = None) -> None:
        self.intake = intake or GmailIntakeService()
        self.failures = failures or FailureTracker("Pub/Sub pull", log)
        self._subscriber: pubsub_v1.SubscriberClient | None = None
        self._future = None
        self._stopping = threading.Event()

    def start(self, loop: asyncio.AbstractEventLoop, auto_restart: bool = True) -> None:
        """Begin pulling; returns immediately. Handling runs on ``loop``.

        ``subscribe()`` only *starts* a background stream — if Google then refuses it (bad credentials,
        a missing subscription or permission, the network), nothing is raised here. With
        ``auto_restart`` (in-app mode) that is caught, logged, and the stream is re-created after a backoff.
        """
        settings = get_settings()
        self._stopping.clear()
        self._subscriber = pubsub_v1.SubscriberClient()
        subscription_path = self._subscriber.subscription_path(settings.gcp_project_id, settings.pubsub_subscription)

        log.info("Listening on %s", subscription_path)
        # One message at a time: handlers read and advance the mailbox's history cursor.
        self._future = self._subscriber.subscribe(
            subscription_path,
            callback=lambda message: self.process(message, loop),
            flow_control=pubsub_v1.types.FlowControl(max_messages=1),
        )
        if auto_restart:
            self._future.add_done_callback(lambda future: self._stream_ended(future, loop))

    def _stream_ended(self, future, loop: asyncio.AbstractEventLoop) -> None:
        """The streaming pull stopped on its own. Say why (once, via the failure tracker) and restart it."""
        if self._stopping.is_set():
            return                                  # we asked for this (``stop()``)
        try:
            future.result()
            reason: BaseException = RuntimeError("the Pub/Sub stream ended without reporting an error")
        except BaseException as exc:                # noqa: BLE001 — a cancelled future raises a BaseException
            reason = exc
        delay = self.failures.failed(reason, "streaming pull")
        log.error("Pub/Sub streaming pull stopped, so NO emails are being received; restarting in %.0fs", delay)
        self._schedule_restart(loop, delay)

    def _schedule_restart(self, loop: asyncio.AbstractEventLoop, delay: float) -> None:
        timer = threading.Timer(delay, self._restart, args=(loop,))
        timer.daemon = True
        timer.start()

    def _restart(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._stopping.is_set():
            return
        try:
            if self._subscriber is not None:
                self._subscriber.close()
                self._subscriber = None
            self.start(loop)
        except Exception as exc:
            delay = self.failures.failed(exc, "restarting streaming pull")
            self._schedule_restart(loop, delay)

    def process(self, message, loop: asyncio.AbstractEventLoop) -> None:
        """Handle one Pub/Sub message: ack it, or nack it after a backoff. Never raises."""
        context = f"message {getattr(message, 'message_id', '?')}"
        try:
            notification = json.loads(message.data.decode("utf-8"))
            asyncio.run_coroutine_threadsafe(self.intake.handle_gmail_notification(notification), loop).result()
        except Exception as exc:
            delay = self.failures.failed(exc, context)
            if is_transient(exc):
                self._stopping.wait(delay)         # back off, so the redelivery doesn't spin
                message.nack()
            else:
                log.error("Dropping %s: this error will not go away by retrying (%s)", context, type(exc).__name__)
                message.ack()
            return
        self.failures.succeeded()
        message.ack()

    def stop(self) -> None:
        self._stopping.set()
        if self._future is not None:
            self._future.cancel()
            self._future = None
        if self._subscriber is not None:
            self._subscriber.close()
            self._subscriber = None

    def run_blocking(self) -> None:
        """Standalone mode (``scripts/run_pubsub_pull_locally.py``): own loop, runs until Ctrl+C."""
        # psycopg's async mode can't run on Windows' default ProactorEventLoop.
        loop = asyncio.SelectorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        self.start(loop, auto_restart=False)        # here a dead stream should fail loudly: ``result()`` raises it
        try:
            self._future.result()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
