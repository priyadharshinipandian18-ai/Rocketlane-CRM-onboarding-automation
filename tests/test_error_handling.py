"""Failure handling: throttled logging, backoff, transient vs permanent errors, and isolation of one
bad message — so a persistent fault is one clear log line, not an endless stack-trace loop."""
import asyncio
import json
import logging
import os
import threading

os.environ.setdefault("GOOGLE_API_KEY", "test-key-not-used")

from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from src.gmail.auth import GmailAuthError
from src.models.entities import InboundEmail, Mailbox
from src.scheduler.periodic import run_periodic
from src.services.gmail_intake_service import GmailIntakeService
from src.services.pubsub_pull_service import PubSubPullService
from src.utils.errors import is_transient
from src.utils.failure_tracker import FailureTracker


def http_error(status):
    return HttpError(SimpleNamespace(status=status, reason="x"), b"{}")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def error_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


# ── FailureTracker ─────────────────────────────────────────────────────────

def test_the_first_failure_logs_full_detail_and_repeats_are_quiet(caplog):
    clock = Clock()
    tracker = FailureTracker("Thing", clock=clock, repeat_after=300)
    with caplog.at_level(logging.INFO):
        for _ in range(50):
            tracker.failed(GmailAuthError("token lacks gmail.send"))
            clock.now += 1

    errors = error_records(caplog)
    assert len(errors) == 1 and errors[0].exc_info is not None            # one traceback for 50 identical failures
    assert "token lacks gmail.send" in errors[0].getMessage()
    assert tracker.consecutive == 50


def test_a_one_line_summary_appears_every_repeat_interval(caplog):
    clock = Clock()
    tracker = FailureTracker("Thing", clock=clock, repeat_after=300)
    with caplog.at_level(logging.INFO):
        tracker.failed(RuntimeError("down"))
        clock.now += 100
        tracker.failed(RuntimeError("down"))
        clock.now += 250                                                    # 350s since the last log
        tracker.failed(RuntimeError("down"))

    errors = error_records(caplog)
    assert len(errors) == 2 and "still failing" in errors[1].getMessage() and errors[1].exc_info is None
    assert "3 consecutive" in errors[1].getMessage() and "1 similar errors not logged" in errors[1].getMessage()


def test_a_different_error_is_logged_in_full_again(caplog):
    tracker = FailureTracker("Thing", clock=Clock())
    with caplog.at_level(logging.INFO):
        tracker.failed(RuntimeError("down"))
        tracker.failed(ValueError("a new problem"))
    assert len(error_records(caplog)) == 2 and all(r.exc_info for r in error_records(caplog))


def test_recovery_is_announced_once_and_resets_everything(caplog):
    tracker = FailureTracker("Thing", clock=Clock())
    with caplog.at_level(logging.INFO):
        tracker.failed(RuntimeError("down"))
        tracker.succeeded()
        tracker.succeeded()
    assert [r.getMessage() for r in caplog.records if "recovered" in r.getMessage()] == ["Thing recovered after 1 consecutive failure(s)"]
    assert tracker.consecutive == 0 and tracker.backoff() == 0


def test_backoff_doubles_and_is_capped():
    tracker = FailureTracker("Thing", clock=Clock(), base_backoff=5, max_backoff=60)
    assert tracker.backoff() == 0
    assert [tracker.failed(RuntimeError("x")) for _ in range(6)] == [5, 10, 20, 40, 60, 60]


def test_context_is_part_of_the_log_line(caplog):
    FailureTracker("Pub/Sub pull", clock=Clock()).failed(RuntimeError("boom"), context="message 42")
    assert "Pub/Sub pull [message 42] failed: boom" in error_records(caplog)[0].getMessage()


# ── which errors are worth retrying ────────────────────────────────────────

@pytest.mark.parametrize("exc, transient", [
    (GmailAuthError("missing scope"), True),
    (http_error(503), True), (http_error(429), True), (http_error(403), True), (http_error(401), True),
    (http_error(400), False), (http_error(404), False),
    (ConnectionError("reset"), True), (TimeoutError(), True),
    (type("RefreshError", (Exception,), {})("expired"), True),
    (ValueError("bad data"), False), (KeyError("sender"), False), (AttributeError("bug"), False),
])
def test_transient_versus_permanent(exc, transient):
    assert is_transient(exc) is transient


# ── the timer loop ─────────────────────────────────────────────────────────

async def test_a_failing_tick_does_not_kill_the_loop_and_logs_once(caplog):
    stop, calls = asyncio.Event(), []

    async def tick():
        calls.append(1)
        if len(calls) <= 3:
            raise RuntimeError("database down")
        stop.set()

    tracker = FailureTracker("Timer", clock=Clock(), base_backoff=0, max_backoff=0)
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(run_periodic("Timer", stop, 0.001, tick, failures=tracker), timeout=5)

    assert len(calls) == 4
    assert len(error_records(caplog)) == 1                                  # three identical failures, one traceback
    assert any("recovered after 3 consecutive failure(s)" in r.getMessage() for r in caplog.records)


async def test_the_wait_between_ticks_grows_while_failing():
    stop, waits = asyncio.Event(), []
    real_wait_for = asyncio.wait_for

    async def spy(awaitable, timeout):
        waits.append(timeout)
        if len(waits) >= 3:
            stop.set()
        return await real_wait_for(awaitable, timeout=0.001)

    async def tick():
        raise RuntimeError("down")

    asyncio.wait_for, original = spy, asyncio.wait_for
    try:
        tracker = FailureTracker("Timer", clock=Clock(), base_backoff=0.5, max_backoff=10)
        await run_periodic("Timer", stop, 0.1, tick, failures=tracker)
    finally:
        asyncio.wait_for = original
    assert waits[:3] == [0.5, 1.0, 2.0]                                     # never shorter than the interval, then doubling


# ── the Pub/Sub puller ─────────────────────────────────────────────────────

class FakeMessage:
    def __init__(self, data):
        self.data, self.message_id, self.state = data, "m-1", []

    def ack(self):
        self.state.append("ack")

    def nack(self):
        self.state.append("nack")


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)


def puller(error=None):
    handled = []

    class Intake:
        async def handle_gmail_notification(self, notification):
            handled.append(notification)
            if error:
                raise error

    service = PubSubPullService(intake=Intake(), failures=FailureTracker("Pub/Sub pull", clock=Clock(), base_backoff=0, max_backoff=0))
    return service, handled


NOTIFICATION = json.dumps({"emailAddress": "cs@x.co", "historyId": "1"}).encode()


def test_a_good_message_is_acked(loop_thread):
    service, handled = puller()
    message = FakeMessage(NOTIFICATION)
    service.process(message, loop_thread)
    assert message.state == ["ack"] and handled == [{"emailAddress": "cs@x.co", "historyId": "1"}]


def test_a_transient_failure_is_nacked_for_redelivery_not_dropped(loop_thread):
    service, _ = puller(GmailAuthError("token lacks gmail.send"))
    message = FakeMessage(NOTIFICATION)
    service.process(message, loop_thread)
    assert message.state == ["nack"]


def test_a_permanent_failure_is_acked_so_it_cannot_loop_forever(loop_thread, caplog):
    service, _ = puller(ValueError("a bug that retrying will not fix"))
    message = FakeMessage(NOTIFICATION)
    with caplog.at_level(logging.INFO):
        service.process(message, loop_thread)
    assert message.state == ["ack"]
    assert any("Dropping message m-1" in r.getMessage() for r in caplog.records)


def test_a_malformed_notification_is_dropped(loop_thread):
    service, handled = puller()
    message = FakeMessage(b"not json at all")
    service.process(message, loop_thread)
    assert message.state == ["ack"] and handled == []


def test_a_persistent_fault_logs_one_traceback_across_many_redeliveries(loop_thread, caplog):
    service, _ = puller(GmailAuthError("token lacks gmail.send"))
    with caplog.at_level(logging.INFO):
        for _ in range(25):
            service.process(FakeMessage(NOTIFICATION), loop_thread)
    with_traceback = [r for r in error_records(caplog) if r.exc_info]
    assert len(with_traceback) == 1 and service.failures.consecutive == 25


def test_the_puller_recovers_and_says_so(loop_thread, caplog):
    service, _ = puller(GmailAuthError("down"))
    with caplog.at_level(logging.INFO):
        service.process(FakeMessage(NOTIFICATION), loop_thread)
        service.intake.handle_gmail_notification = lambda n: asyncio.sleep(0)      # the fault is fixed
        ok = FakeMessage(NOTIFICATION)
        service.process(ok, loop_thread)
    assert ok.state == ["ack"] and any("recovered" in r.getMessage() for r in caplog.records)


class DeadFuture:
    def __init__(self, error=None):
        self.error = error

    def result(self):
        if self.error:
            raise self.error


def test_a_stream_that_dies_silently_is_logged_and_restarted_with_a_backoff(monkeypatch, caplog):
    """subscribe() raises nothing when Google refuses the stream — the failure only shows up on the future."""
    scheduled = []

    class FakeTimer:
        def __init__(self, delay, fn, args=()):
            scheduled.append((delay, fn, args))
            self.daemon = False

        def start(self):
            pass

    monkeypatch.setattr("src.services.pubsub_pull_service.threading.Timer", FakeTimer)
    service, _ = puller()
    loop = object()
    with caplog.at_level(logging.INFO):
        service._stream_ended(DeadFuture(PermissionError("403 caller lacks pubsub.subscriptions.consume")), loop)
        service._stream_ended(DeadFuture(PermissionError("403 caller lacks pubsub.subscriptions.consume")), loop)

    errors = error_records(caplog)
    assert any("403 caller lacks" in r.getMessage() and r.exc_info for r in errors)                 # the real reason, with a traceback
    assert sum("NO emails are being received" in r.getMessage() for r in errors) == 2
    assert [fn.__name__ for _, fn, _ in scheduled] == ["_restart", "_restart"] and all(args == (loop,) for _, _, args in scheduled)
    assert len([r for r in errors if r.exc_info]) == 1                                              # one traceback for the same fault


def test_a_stream_we_stopped_ourselves_is_not_restarted(monkeypatch):
    scheduled = []
    monkeypatch.setattr("src.services.pubsub_pull_service.threading.Timer", lambda *a, **k: scheduled.append(a))
    service, _ = puller()
    service._stopping.set()
    service._stream_ended(DeadFuture(), object())
    assert scheduled == []


def test_a_failed_restart_is_retried_later(monkeypatch):
    scheduled = []
    service, _ = puller()
    monkeypatch.setattr(service, "start", lambda loop: (_ for _ in ()).throw(ConnectionError("no network")))
    monkeypatch.setattr(service, "_schedule_restart", lambda loop, delay: scheduled.append(delay))
    service._restart(object())
    assert len(scheduled) == 1


# ── one bad message must not block the others ──────────────────────────────

class FakeGmail:
    def __init__(self, messages, get_error=None):
        self.messages, self.get_error = messages, get_error

    def list_new_message_ids(self, _history_id):
        return list(self.messages), "200"

    def get_message(self, message_id):
        if self.get_error:
            raise self.get_error
        return {"id": message_id, "sender": "alex@rappit.io", "subject": f"s-{message_id}", "received_at": "",
                "snippet": "x", "labels": ["INBOX"], "thread_id": "T"}

    def get_message_text(self, _id):
        return "body"


def intake(gmail, *, insert_fails_for=()):
    record = SimpleNamespace(processed=[], cursor=[], failed=[])

    class Mailboxes:
        async def get_or_create(self, _email):
            return Mailbox(id="mb1", email="cs@x.co", history_id=100)

        async def update_cursor(self, mailbox_id, history_id, watch_expiration=None):
            record.cursor.append(history_id)

    class InboundEmails:
        async def try_insert(self, **kwargs):
            if kwargs["gmail_message_id"] in insert_fails_for:
                raise ValueError("odd data in this one message")
            return InboundEmail(id=f"e-{kwargs['gmail_message_id']}", mailbox_id="mb1", gmail_message_id=kwargs["gmail_message_id"])

        async def find_one(self, **kwargs):
            return InboundEmail(id=f"e-{kwargs['gmail_message_id']}", mailbox_id="mb1", gmail_message_id=kwargs["gmail_message_id"])

        async def update_classification(self, email_id, is_deal_email, classification, status, **_):
            record.failed.append((email_id, classification, str(status)))

    class Orchestrator:
        async def process_inbound_email(self, inbound_id, email):
            record.processed.append(inbound_id)
            return {}

    service = GmailIntakeService(
        mailboxes=Mailboxes(), inbound_emails=InboundEmails(), gmail_client_factory=lambda: gmail,
        orchestrator=Orchestrator(), tier_replies=SimpleNamespace(),
    )
    return service, record


NOTIFICATION_DICT = {"emailAddress": "cs@x.co", "historyId": "200"}


async def test_a_poison_message_is_skipped_recorded_and_does_not_block_the_rest(caplog):
    service, record = intake(FakeGmail(["m1", "m2", "m3"]), insert_fails_for={"m2"})
    with caplog.at_level(logging.INFO):
        await service.handle_gmail_notification(NOTIFICATION_DICT)

    assert record.processed == ["e-m1", "e-m3"]                            # the messages around it were handled
    assert record.cursor == [200]                                          # and the cursor moved on: no redelivery loop
    assert any("Skipping message m2" in r.getMessage() for r in error_records(caplog))
    assert record.failed[0][0] == "e-m2" and "ValueError" in record.failed[0][1]["error"]


async def test_a_transient_failure_fails_the_notification_and_keeps_the_cursor(caplog):
    service, record = intake(FakeGmail(["m1", "m2"], get_error=http_error(503)))
    with pytest.raises(HttpError):
        await service.handle_gmail_notification(NOTIFICATION_DICT)
    assert record.cursor == [] and record.processed == []                  # nothing lost: it will be retried


async def test_a_missing_gmail_permission_is_not_swallowed(caplog):
    service, record = intake(FakeGmail(["m1"], get_error=GmailAuthError("token lacks gmail.send")))
    with pytest.raises(GmailAuthError):
        await service.handle_gmail_notification(NOTIFICATION_DICT)
    assert record.cursor == []
