# Code deep dive: scheduler, SDK vs API, and the full technical flow

This explains *how* the code works, with function and file names. For a shorter overview, see [CODE_FLOW.md](CODE_FLOW.md) and [TECHNICAL_DOCUMENT.md](TECHNICAL_DOCUMENT.md).

---

## 0. Where the workflow starts, and how it flows

Nothing in this app is started by a button or a cron job. **A workflow starts when a new email lands in the CS inbox.** Everything after that is the chain below.

```
Gmail inbox
   │ (1) Gmail "watch" publishes a tiny notification {emailAddress, historyId} to a Pub/Sub topic
   ▼
PubSubPullService.process()                    src/services/pubsub_pull_service.py   (Google SDK thread)
   │ (2) run_coroutine_threadsafe(...)
   ▼
GmailIntakeService.handle_gmail_notification() src/services/gmail_intake_service.py
   │ (3) diff history cursor → new message ids → fetch → redact PII → store row
   ▼
WorkflowOrchestrator.process_inbound_email()   src/services/workflow_orchestrator.py
   │ (4) create workflow_runs row + start the INTAKE graph       thread  intake:<inbound_email_id>
   ▼
INTAKE graph ── extract_deal ──▶ onboardings row + workflow_runs(tier, pending)
   │ (5) start_pending() claims it and starts the TIER graph      thread  tier:<onboarding_id>
   ▼
TIER graph … … … … … … … … … … confirm_tier ──▶ workflow_runs(provision, pending)
   │ (6) start_pending() again → PROVISIONING graph               thread  provision:<onboarding_id>
   ▼
Rocketlane project (optional) → Slack channel
```

### Step 1: The trigger. A Gmail notification

`scripts/register_gmail_watch.py` calls Gmail's `users().watch(...)` once, pointing at a Pub/Sub topic. From then on, every change to the mailbox publishes a message that carries **only** the mailbox address and a `historyId`, not the email. `main.py` starts `PubSubPullService` (when `PUBSUB_PULL_ENABLED=true`), which keeps a streaming pull open on the subscription:

```python
self._future = self._subscriber.subscribe(
    subscription_path,
    callback=lambda message: self.process(message, loop),
    flow_control=pubsub_v1.types.FlowControl(max_messages=1),   # one notification at a time
)
```

`process()` runs on Google's thread, so it hands the async work to the app's event loop and blocks until it finishes:

```python
notification = json.loads(message.data.decode("utf-8"))
asyncio.run_coroutine_threadsafe(self.intake.handle_gmail_notification(notification), loop).result()
...
message.ack()        # only after the work is done; failures nack (transient) or ack-and-drop (permanent)
```

The other two ways in end in the same function: `POST /pubsub/push` (push delivery) and `functions/pubsub_intake.py` (Cloud Function). Only the transport differs.

### Step 2: Turn the notification into stored emails (`GmailIntakeService`)

```python
mailbox = await self.mailboxes.get_or_create(mailbox_email)
message_ids, latest_history_id = await asyncio.to_thread(client.list_new_message_ids, str(mailbox.history_id))
for message_id in message_ids:
    await self._handle_message(client, mailbox.id, message_id)
await self.mailboxes.update_cursor(mailbox.id, int(latest_history_id))
```

- The notification only says "something changed". The real list of new messages comes from a **diff against the stored `history_id` cursor** (`mailboxes` table). That is why a lost or repeated notification is harmless.
- The first notification ever received just stores the cursor. An expired cursor is re-anchored.
- `_process_message` then, per message: skips a message that is gone (404) or one we sent ourselves (`SENT` and not `INBOX`), reads the body, **redacts PII** (`redact_pii`), and calls `InboundEmailService.try_insert`, which has a unique key so a duplicate returns `None` and is skipped. **The raw body is never stored.**
- Failure policy: a transient error (network, Google 5xx/429) is re-raised so the notification is retried and the cursor stays put; a permanent error is logged, recorded on the email row as `failed`, and skipped so it cannot block the mailbox.

### Step 3: Start the intake workflow (`WorkflowOrchestrator.process_inbound_email`)

This is the exact point where the first LangGraph run begins:

```python
thread_id = f"intake:{inbound_email_id}"
run = await self.workflow_runs.create(WorkflowName.INTAKE, thread_id,
                                      inbound_email_id=inbound_email_id, status="running")
graph = self.graph_for("intake")
values = await self._execute(run, graph, lambda: graph.start(thread_id, {
    "inbound_email_id": inbound_email_id, "sender": email["sender"],
    "subject": email["subject"], "body": email.get("body", ""),
    "gmail_thread_id": email.get("gmail_thread_id"),
}))
```

- `workflow_runs.create` is idempotent per `thread_id` (`ON CONFLICT DO NOTHING`), so a replay returns the same run.
- `graph.start` is `WorkflowGraph.start`: `ainvoke(state, config={"configurable": {"thread_id": thread_id}})`. The **thread ID is the key under which the Postgres checkpointer saves this run's state**.
- `_execute` wraps the call so it never raises: success updates `workflow_runs`, an exception marks the run `failed` at the node that raised.

### Step 4: Inside the intake graph (`src/agents/intake/graph.py`)

```python
graph.add_node("email_analyser", build_email_analyser_graph())   # a subgraph
graph.add_node("extract_deal", extract_deal_node)
graph.add_edge(START, "email_analyser")
graph.add_conditional_edges("email_analyser", route_after_classification,
                            {"extract_deal": "extract_deal", "__end__": END})
graph.add_edge("extract_deal", END)
```

```
START → email_analyser ─┬─ kind = deal_closed → extract_deal → END
                        └─ anything else (no_deal / malicious / escalation_reply) → END
```

Inside `email_analyser`: `fetch_email` → `pii_redactor` → `detect_escalation_reply` (matches an open escalation by Gmail thread ID, then by the `[ONB-xxxxxxxx]` subject token, **no LLM**; a match ends the graph classified as `escalation_reply`) → `sender_gate` (unknown sender → `no_deal` by rule) → `injection_detector` → `analyze_deal_status` (LLM verdict) → `fact_check_validator` (hard checks on that verdict).

`route_after_classification` is one line: `"extract_deal" if state.get("kind") == "deal_closed" else "__end__"`.

`extract_deal_node` is where the deal becomes data:

```python
extraction = await _invoke_with_retries(_get_deal_parser_chain(), {...})   # prompt | llm.with_structured_output(DealEmailExtraction)
ae, ae_email, ae_name = await resolve_ae(extraction, state["sender"])
onboarding = await onboardings.create(onboarding_id=..., customer_name=..., sf_opportunity_id=..., extra_details={"ae_name": ..., "ae_email": ...})
...
await WorkflowRunService().create(WorkflowName.TIER_CONFIRMATION, f"tier:{onboarding_id}",
                                  onboarding_id=onboarding_id, status=WorkflowRunStatus.PENDING)
```

The intake graph **never starts the tier graph itself**. It only writes a `pending` row, the hand-off, so a crash right here cannot lose the deal.

### Step 5: Hand-off. `start_pending()` starts the tier workflow

Back in `process_inbound_email`:

```python
if values.get("kind") == EmailKind.DEAL_CLOSED.value and onboarding_id:
    await self.start_pending()
```

```python
async def start_pending(self, workflow=None) -> int:
    for _ in range(MAX_HANDOFF_ROUNDS):
        runs = await self.workflow_runs.claim_pending(workflow)   # pending → running, FOR UPDATE SKIP LOCKED
        if not runs: break
        for run in runs:
            await self._start(run)
```

`_start` rebuilds a **small initial state from the database** (`_tier_input`: onboarding ID, customer, contact, AE name/email, `call_attempts: 0`) and calls `graph.start(run.thread_id, state)`. If the process dies between "intake wrote pending" and "tier started", the **workflow sweeper** loop calls `start_pending()` on a timer and picks it up.

### Step 6: Inside the tier graph (`src/agents/tier_confirmation/graph.py`)

```
START → place_plan_tier_call ─┬─ placed ────────► await_plan_tier_result   (PAUSE: call result)
                              ├─ deferred ──────► wait_for_call_retry      (PAUSE: timer)
                              ├─ failed ────────► schedule_call_retry / request_ae_reply
                              └─ no phone ──────► request_ae_reply
await_plan_tier_result ─┬─ confirmed ─────────────► confirm_tier → END
                        ├─ not confirmed, tries left ─► schedule_call_retry → wait_for_call_retry → place_plan_tier_call
                        └─ tries exhausted ───────────► request_ae_reply → await_ae_reply (PAUSE: AE email) → confirm_tier
```

Three nodes pause with `interrupt()`: `await_plan_tier_result`, `wait_for_call_retry` and `await_ae_reply`. Before pausing, the node *before* each of them writes the wake-up condition into `workflow_runs`:

```python
await workflow_runs.mark_waiting(onboarding_id, TIER, WaitingFor.CALL_RESULT, current_node="await_plan_tier_result")
```

When the graph hits `interrupt()`, LangGraph saves the checkpoint and returns. `_execute` sees `snapshot.next` is non-empty and leaves the `waiting` row alone. **The process now holds nothing for this run**: no thread, no timer, no memory. It can wait seconds or days, and survive a restart.

### Step 7: The run wakes up

Something outside finds the row and continues the same thread:

| What happened | Who notices | How it continues |
|---|---|---|
| The call finished | `call_poll_loop` (or `POST /webhooks/bolna`) | `claim_waiting(CALL_RESULT)` → `orchestrator.resume(run, {"outcome", "plan_tier"})` |
| A retry is due | `retry_loop` | `claim_due_call_retries()` → `resume(run, {"action": "retry"})` |
| The AE replied by email | A new Gmail notification (Step 1 again) | `TierReplyService.deliver_reply` → `claim_for_tier_answer` → `resume(run, {"plan_tier", ...})` |
| The AE stayed silent | `tier_escalation_loop` | Marks the run `stopped`; a late reply or `POST /onboardings/{id}/resume` still continues it |

`resume` is `graph.resume(thread_id, payload)`, which is `ainvoke(Command(resume=payload), config)`. LangGraph reloads the checkpoint for that thread and **re-enters the paused node; `interrupt()` now returns `payload`**. This is why nodes with `interrupt()` have no side effects before it.

### Step 8: Hand-off to provisioning

`confirm_tier` records the final tier and inserts `workflow_runs(provision:<id>, pending)`. `resume` ends with `await self.start_pending()`, which starts it:

```python
async def resume(self, run, payload):
    graph = self.graph_for(run.workflow)
    await self._execute(run, graph, lambda: graph.resume(run.thread_id, payload))
    await self.start_pending()      # a finished tier run queued its provisioning run
```

### Step 9: Provisioning (`src/agents/provisioning/graph.py`)

```python
if get_settings().rocketlane_enabled:
    graph.add_edge(START, "create_rocketlane_project")
    graph.add_edge("create_rocketlane_project", "communication_agent")
else:
    graph.add_edge(START, "assign_team")
    graph.add_edge("assign_team", "communication_agent")
graph.add_edge("communication_agent", END)
```

It never pauses. A failure raises, `_execute` marks the run `failed` at that node, and `POST /onboardings/{id}/retry` runs `claim_failed` then `graph.rerun(thread_id)` (`ainvoke(None, config)`), which continues from the last good checkpoint.

### The one idea that ties it together

> **Each workflow is a LangGraph thread. The graph position lives in the Postgres checkpoint. The reason it is waiting and the time it should wake live in `workflow_runs`. Workflows hand off to each other by inserting a `pending` row, never by calling each other directly.**

That is why the app survives restarts, why duplicate events are harmless, and why a failed step can be retried without redoing earlier steps. The sections below go deeper on each part.

---

## 1. SDK or raw API? One answer per integration

| Integration | How it is called | Library | Where |
|---|---|---|---|
| **Gmail** (read, history, watch, send) | **Google's SDK**: `build("gmail", "v1", credentials=…)` then `service.users().messages().get(...).execute()` | `google-api-python-client`, `google-auth-oauthlib` | `src/gmail/auth.py`, `src/gmail/client.py` |
| **Pub/Sub** (Gmail notifications) | **Google's SDK**: `pubsub_v1.SubscriberClient().subscribe(...)` | `google-cloud-pubsub` | `src/services/pubsub_pull_service.py` |
| **Bolna** (voice call) | **Raw REST** with `httpx`: `POST /call`, `GET /executions/{id}`, header `Authorization: Bearer` | `httpx` | `src/integrations/voice_ai_client.py` |
| **Rocketlane** | **Raw REST** with `httpx`: `POST /projects`, `POST /projects/{id}/import-template`, header `api-key` | `httpx` | `src/integrations/rocketlane_client.py` |
| **Slack** | **Raw REST** with `httpx`: `conversations.create`, `chat.postMessage`, `users.lookupByEmail`, … with a bot token | `httpx` | `src/integrations/slack_client.py` |
| **LLM** (Groq / Gemini) | **LangChain wrappers**: `ChatGroq` / `ChatGoogleGenerativeAI` | `langchain-groq`, `langchain-google-genai` | `src/config/model_factory.py` |
| **Postgres** | **psycopg 3** driver (async) | `psycopg[binary,pool]` | `src/config/database_config.py`, `src/services/base_service.py` |

Why the split: Google has no light REST alternative worth using (OAuth refresh and the Pub/Sub streaming protocol are handled by their SDKs), while Bolna, Rocketlane and Slack are simple JSON-over-HTTPS APIs, so a thin `httpx` client is clearer and fully mockable in tests (`httpx.MockTransport`) with no extra dependency.

**Sync SDK inside an async app.** The Google API client is **synchronous**. Every call to it is wrapped in `asyncio.to_thread(...)` (see `gmail_intake_service.py`, `email_service.py`, `gmail_watch_service.py`) so it never blocks the event loop.

**Slack quirks** (in `_call`): Slack answers HTTP 200 with `{"ok": false, "error": …}` on failure, so the client raises `SlackApiError` for that; one retry on HTTP 429 using `Retry-After`; `users.lookupByEmail` needs form-encoded parameters rather than JSON.

---

## 2. How the scheduler is implemented

### 2.1 There is no scheduler library

The app does **not** use APScheduler, Celery, cron or a message broker. The "scheduler" is **four plain `asyncio` tasks** started in FastAPI's lifespan (`main.py`):

```python
background_loops = [
    asyncio.create_task(retry_loop(stop)),            # places retry calls when due
    asyncio.create_task(call_poll_loop(stop)),        # fetches finished-call results from Bolna
    asyncio.create_task(tier_escalation_loop(stop)),  # AE didn't reply in time: stop the run
    asyncio.create_task(workflow_sweeper_loop(stop)), # starts pending hand-offs
]
```

They share one `asyncio.Event` called `stop`. On shutdown, `stop.set()` and `await asyncio.gather(*background_loops)` end them cleanly.

### 2.2 The one loop they all use: `src/scheduler/periodic.py`

```python
async def run_periodic(name, stop, interval, tick, failures=None):
    failures = failures or FailureTracker(name, ...)
    while not stop.is_set():
        wait = interval
        try:
            await tick()                          # the work
        except Exception as exc:
            wait = max(interval, failures.failed(exc))   # back off while it keeps failing
        else:
            failures.succeeded()
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)   # sleep, but wake at once on shutdown
        except asyncio.TimeoutError:
            pass
```

Key properties:

- **A tick that raises never kills the loop.** The exception is handed to `FailureTracker` (`src/utils/failure_tracker.py`): full traceback the first time, then a one-line summary every few minutes, then a "recovered" line. The wait before the next tick grows (exponential backoff) while it keeps failing, so a Bolna outage does not flood the log.
- **Sleeping with `wait_for(stop.wait(), timeout)`** instead of `asyncio.sleep` means shutdown is immediate, not delayed by up to one interval.
- Each loop file is just a few lines: it reads its interval from settings and passes a lambda that calls a service method.

### 2.3 The four loops

| Loop (file) | Interval setting | What a tick calls | What it does |
|---|---|---|---|
| `call_result_poller.py` | `BOLNA_POLL_SECONDS` (off if `BOLNA_POLL_ENABLED=false`) | `CallResultService.poll_in_flight_once()` | Lists in-flight `voice_calls`, calls Bolna `GET /executions/{id}` for each, and for finished calls records the result and resumes the paused tier run |
| `retry_runner.py` | `RETRY_POLL_SECONDS` | `RetryService.run_due_once()` | Claims tier runs `waiting / call_retry` whose `due_at <= now()` and resumes each with `{"action": "retry"}` |
| `tier_escalation.py` | `ESCALATION_POLL_SECONDS` | `TierEscalationService.run_due_once()` | Claims tier runs `waiting / email_reply` whose `stop_at <= now()` and marks them `stopped` (nothing is lost) |
| `workflow_sweeper.py` | `WORKFLOW_SWEEP_SECONDS` | `WorkflowOrchestrator.start_pending()` | Starts any `pending` run, a safety net for a crash between two workflows |

A fifth background activity, the Gmail Pub/Sub puller, is **not** one of these loops: it runs on a Google SDK thread (Section 2.6).

### 2.4 The database is the schedule

A LangGraph run cannot "sleep for 30 minutes". Instead, when a node needs to wait, it **writes the wake-up condition into `app.workflow_runs`** and then pauses with `interrupt()`:

```python
await workflow_runs.mark_waiting(onboarding_id, TIER, WaitingFor.CALL_RETRY,
                                 current_node="wait_for_call_retry", due_at=retry_at)
...
interrupt({"type": "awaiting_call_retry", ...})
```

`workflow_runs` columns that drive scheduling: `status` (`pending`, `running`, `waiting`, `stopped`, `failed`, `succeeded`), `waiting_for` (`call_result`, `call_retry`, `email_reply`), `due_at` (retry time) and `stop_at` (give-up time). A loop's tick is just "find rows whose time has come and continue them".

### 2.5 Claiming a run safely: conditional `UPDATE`

Every state change is a **single conditional `UPDATE … RETURNING`** in `WorkflowRunService`, so two racing actors can never both take the same run. Example, due retries:

```sql
UPDATE app.workflow_runs
SET status = 'running', waiting_for = NULL, due_at = NULL, stop_at = NULL, updated_at = now()
WHERE id IN (
    SELECT id FROM app.workflow_runs
    WHERE workflow = 'tier_confirmation' AND status = 'waiting'
      AND waiting_for = 'call_retry' AND due_at <= now()
    ORDER BY due_at LIMIT %s
    FOR UPDATE SKIP LOCKED
)
RETURNING *
```

- `status = 'waiting' → 'running'` is the claim. Whoever's `UPDATE` returns the row owns it; everyone else gets nothing.
- `FOR UPDATE SKIP LOCKED` makes this safe with **several app instances**: each takes different rows instead of blocking or double-processing.
- The same pattern backs `claim_pending`, `claim_waiting(onboarding_id, workflow, waiting_for)` (used by the call-result path), `claim_for_tier_answer` (waiting for an email reply *or* `stopped`), `claim_due_stops` and `claim_failed` (manual retry).
- `claim_waiting` requires the run to be waiting for *exactly* the given reason, so a late or stray event for a run that has moved on is ignored.

### 2.6 Two ways a paused run wakes up

| Kind | Examples | Mechanism |
|---|---|---|
| **Timer-driven** (loops above) | Call retry due; AE-reply deadline; pending hand-off; polling Bolna | A loop tick finds due rows in `workflow_runs` |
| **Event-driven** | A Gmail notification; the AE's email reply; Bolna's webhook | An HTTP route or the Pub/Sub thread calls the service directly |

**The Pub/Sub puller** (`PubSubPullService`) is event-driven and lives on Google's thread pool:

- `subscriber.subscribe(path, callback=…, flow_control=FlowControl(max_messages=1))` starts a background **streaming pull**. `max_messages=1` handles one notification at a time, because handlers read and advance the mailbox's `history_id` cursor.
- The callback runs on a Google thread, so it hands the async work to the app's event loop with `asyncio.run_coroutine_threadsafe(...).result()`.
- `process()` never raises: success → `ack`; transient failure (network, Google 5xx/429) → back off, then `nack`; permanent failure → log and `ack` (retrying could never succeed).
- If Google ends the stream on its own, `_stream_ended` logs the reason and `threading.Timer` restarts it after a backoff (`auto_restart=True` in-app).

### 2.7 Idempotency: why duplicate wake-ups are harmless

The poller and the Bolna webhook can both see the same finished call. `VoiceCallService.finalize_once(execution_id, …)` lets exactly one caller through (the other gets `None`), and only a run `waiting / call_result` can be claimed. Similarly, `place_plan_tier_call_node` uses `UNIQUE (onboarding_id, attempt_no)` plus a stored provider call ID so a replayed node never dials twice.

---

## 3. LangGraph mechanics used throughout

- **One graph per workflow, one thread per run.** Thread IDs: `intake:<inbound_email_id>`, `tier:<onboarding_id>`, `provision:<onboarding_id>`.
- **`WorkflowGraph`** (`src/agents/workflow_graph.py`) wraps a graph builder, compiles it lazily with the Postgres checkpointer (`AsyncPostgresSaver`), and offers:
  - `start(thread_id, state)` → `ainvoke(state, config={"configurable": {"thread_id": …}})`
  - `resume(thread_id, payload)` → `ainvoke(Command(resume=payload), …)`; `payload` becomes the return value of the paused node's `interrupt()`
  - `rerun(thread_id)` → `ainvoke(None, …)`; LangGraph re-runs the node that raised, from the last good checkpoint
  - `snapshot(thread_id)` → `aget_state(…)`; `snapshot.next` is empty once the run is finished
- **`interrupt()` rules the code follows:** a node that calls `interrupt()` has *no side effects before it*, because on resume the node re-executes from its top; the dial-out happens in a separate node (`place_plan_tier_call`) that never calls `interrupt()`.
- **State** is a `TypedDict` per workflow (`IntakeState`, `TierConfirmationState`, `ProvisioningState`), kept deliberately small, with no email text in tier or provisioning checkpoints.
- **Two stores, two jobs:** the checkpoint answers "*where is this graph and what is in its state*"; `workflow_runs` answers "*how does an outside event find this run, and when should it wake*".

---

## 4. The full code flow with technical explanation

### Step 0: Startup (`main.py`)

1. `load_dotenv`, logging setup, noisy library loggers set to WARNING.
2. FastAPI `lifespan`: `check_llm_configuration()` (fails fast on a missing key or a bad model ID), create the four loop tasks, optionally `PubSubPullService().start(loop)`.
3. On Windows, `python main.py` selects `WindowsSelectorEventLoopPolicy` because psycopg's async mode cannot run on the Proactor loop.

### Step 1: A notification arrives (`GmailIntakeService.handle_gmail_notification`)

1. Pub/Sub delivers `{emailAddress, historyId}` (pull thread, `POST /pubsub/push`, or the Cloud Function).
2. `list_new_message_ids(history_id)` (Gmail SDK, in `to_thread`) diffs the mailbox cursor stored in `mailboxes`.
3. `_handle_message(message_id)`, isolated per message:
   - 404 (message gone) or `SENT` label (our own mail) → skip;
   - fetch body → `redact_pii` → `InboundEmailService.try_insert` (unique key, so a duplicate is skipped). The raw body is never stored.
4. If the email is new: `WorkflowOrchestrator.process_inbound_email(inbound_email_id, {...})`.
5. Reply emails are routed to `TierReplyService.deliver_reply` after the classifier flags them (Step 5).

### Step 2: Intake workflow (`WorkflowOrchestrator.process_inbound_email`)

1. `WorkflowRunService.create(INTAKE, "intake:<id>")` is idempotent per `thread_id` (`ON CONFLICT DO NOTHING`).
2. `_execute(run, graph, lambda: graph.start(thread_id, {...}))`. `_execute` never raises: an exception becomes `workflow_runs.status='failed'` with `current_node` taken from the checkpoint (`_next_node`).
3. The email-analyser subgraph runs in order: `fetch_email` → `pii_redactor` → `detect_escalation_reply` (thread ID, then `[ONB-xxxxxxxx]` token; no LLM) → `sender_gate` → `injection_detector` → `analyze_deal_status` → `fact_check_validator`.
4. For `deal_closed`, `extract_deal_node`:
   - the LLM chain (`prompt | llm.with_structured_output(DealEmailExtraction)`) with `_invoke_with_retries`;
   - `resolve_ae` picks the AE (known AE by extracted email, then by `From`, then fallbacks);
   - `OnboardingService.create` (unique `sf_opportunity_id`; same customer → duplicate ignored; different customer → onboarded without the ID);
   - inserts a `workflow_runs` row `tier:<onboarding_id>` with status **`pending`**, the hand-off.
5. Back in the orchestrator: `start_pending()`.

### Step 3: Tier confirmation (`start_pending` → `_start` → `_tier_input`)

1. `claim_pending` moves `pending → running` (with `SKIP LOCKED`); `_tier_input` rebuilds the small initial state from the database.
2. **`place_plan_tier_call_node`**: look up the AE; no phone row → `call_placement="no_phone"`. Outside the AE's calling window (`next_call_time`) → `mark_waiting(CALL_RETRY, due_at=…)`. Otherwise `voice_calls.create_attempt` (unique per attempt) → `BolnaVoiceClient.send_call` (`POST /call`) → store the execution ID → `mark_waiting(CALL_RESULT)`.
3. **`route_after_place`** sends it to `await_plan_tier_result` (placed), `wait_for_call_retry` (deferred), `request_ae_reply` (no phone), or the retry policy (failed).
4. **`await_plan_tier_result_node`** calls `interrupt()`. The graph stops; the checkpoint is saved; `_execute` sees `snapshot.next` is non-empty and leaves the `waiting` row alone.

### Step 4: The call result arrives (timer path)

`call_poll_loop` → `CallResultService.poll_in_flight_once()`:

1. `voice_calls.list_in_flight(PROVIDER)`; for each, `BolnaVoiceClient.get_execution(id)` (`GET /executions/{id}`).
2. Not terminal → update `voice_calls.status`; if older than `VOICE_CALL_TIMEOUT_MINUTES`, treat it as failed so the retry path takes over instead of waiting forever.
3. Terminal → `apply_finished_execution`:
   - `finalize_once` (exactly one caller proceeds);
   - `parse_extraction(extracted_data)`; if Bolna returned none and `BOLNA_TRANSCRIPT_FALLBACK` is on, `parse_transcript` (AE stated a tier, assistant read back the same tier, next user turn affirmative, no negation);
   - `decide_outcome` → `confirmed` / `unclear` / `no_answer` / `voicemail` / `failed`;
   - record on `voice_calls`, audit `call_finished`;
   - `claim_waiting(onboarding_id, TIER, CALL_RESULT)`.
4. `orchestrator.resume(run, result)` → `graph.resume(...)` → `Command(resume=result)`. The paused node's `interrupt()` returns `result`.
5. `await_plan_tier_result_node` continues: **confirmed** → `set_plan_tier(source=voice_call)`; otherwise records "not confirmed".
6. `route_after_call`: confirmed → `confirm_tier`; attempts left → `schedule_call_retry`; else → `request_ae_reply`.

The webhook path (`POST /webhooks/bolna`) calls the same `apply_finished_execution` after re-fetching the execution from Bolna (webhook bodies are not signed, so they are never trusted directly).

### Step 5: Retry and the email fallback

- **`schedule_call_retry_node`** computes the next time (`voice_retry_delay_minutes`, respecting the AE's window), `mark_waiting(CALL_RETRY, due_at)`; **`wait_for_call_retry_node`** pauses with `interrupt()`. `retry_loop` resumes it with `{"action": "retry"}` when due, and the graph returns to `place_plan_tier_call`.
- **`request_ae_reply_node`**: `mark_waiting(EMAIL_REPLY, stop_at=now+HUMAN_STOP_AFTER_HOURS)`, then `HumanTierNotifier.open_request` creates an `escalations` row and sends the email through `EmailService` (Gmail SDK, in `to_thread`; the sent message's `thread_id` and a `[ONB-xxxxxxxx]` token are stored). `await_ae_reply_node` pauses.
- **The AE replies:** the Gmail notification flows through Step 1; `detect_escalation_reply` matches it; `TierReplyService.deliver_reply` verifies the sender equals `escalation.to_email`, parses it with `parse_tier` (exactly one clear word), claims the run with `claim_for_tier_answer` (works for `waiting` or `stopped`) and calls `orchestrator.resume(run, {"plan_tier", "reviewer", "inbound_email_id"})` on the **same thread**.
- **Silence:** `tier_escalation_loop` marks the run `stopped` at `stop_at`. The checkpoint is untouched, so a late reply or `POST /onboardings/{id}/resume` continues it.

### Step 6: Confirmation and hand-off

`confirm_tier` finishes the tier graph and queues `provision:<onboarding_id>` as `pending`. `resume` calls `start_pending()` straight away; the sweeper does the same later if the process crashed in between. `_outcome` records `tier:<plan_tier>` on the tier run.

### Step 7: Provisioning (`build_provisioning_graph`)

- **`ROCKETLANE_ENABLED=true`:** `create_rocketlane_project` → `communication_agent`.
  1. Read the tier row (`plan_tiers`) and choose the CSM and manager (`CsTeamMemberService.get_owner_and_manager`: random active CSM of the tier's pool, and a manager).
  2. `rocketlane_projects` row `creating` (written before any API call).
  3. `RocketlaneClient.create_project` (`POST /projects`, no template) → `save_project_id`.
  4. `RocketlaneClient.import_template` (`POST /projects/{id}/import-template`, body `[{templateId, startDate}]`) → `mark_created`, `set_team_and_dates`, status `project_created`.
  5. A failure is recorded and raised as `ProvisioningError`, so the run stops at this node. A retry reuses the saved project ID and re-runs only the failed step.
- **`ROCKETLANE_ENABLED=false`:** `assign_team` (CSM, manager, dates only) → `communication_agent`.
- **Slack subgraph:** `prepare_channel` (joined read model; Slack IDs resolved by email when not stored) → `create_channel` (row first; deterministic fallback name if taken) → `configure_channel` → `invite_team` → `post_welcome` → `invite_customer` (Slack Connect) → `finalize`, or `channel_failed`.

### Step 8: Bookkeeping after every run (`_execute`)

| Graph outcome | `workflow_runs` result |
|---|---|
| Finished (`snapshot.next` empty) | `succeeded` + `outcome` (e.g. `tier:growth`, `project+channel`, `channel`) |
| Paused at `interrupt()` | Left as the `waiting` row the pausing node wrote |
| Exception | `failed` with `last_error` and `current_node` |

`POST /onboardings/{id}/retry` → `claim_failed` (`failed → running`, `attempt_count + 1`) → `graph.rerun(thread_id)`.

---

## 5. One-page timeline

```
Gmail ─Pub/Sub SDK thread─▶ handle_gmail_notification ─▶ intake graph ─▶ onboardings + workflow_runs(tier, pending)
                                                                                │ start_pending (or sweeper tick)
                                                                                ▼
                                               tier graph: place call (Bolna POST /call) ─ interrupt() ─ checkpoint saved
                                                                                │
        call_poll_loop tick ─ GET /executions/{id} ─ apply_finished_execution ─ claim_waiting ─ resume(Command)
             confirmed ─────────────────────────────────────────────────────────┤
             not confirmed ─ schedule_call_retry ─ due_at ─ retry_loop tick ────┘ (call again)
             attempts used ─ request_ae_reply (email via Gmail SDK) ─ interrupt() ─ stop_at
                  AE reply ─ Gmail notification ─ deliver_reply ─ claim_for_tier_answer ─ resume(Command)
                  silence ─ tier_escalation_loop tick ─ stopped (still resumable)
                                                                                ▼ confirm_tier ─ workflow_runs(provision, pending)
                                               provisioning graph: Rocketlane (create, import) ─ Slack channel ─ succeeded
```

---

## 6. Design trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| asyncio tasks + DB rows instead of Celery/APScheduler | No extra infrastructure; schedule survives restarts because it lives in Postgres | Polling latency (up to one interval); every instance runs its own loops, which is safe only because of the atomic claims |
| Polling Bolna as well as the webhook | Needs no public URL | Slightly later than a webhook; a poll per in-flight call |
| Raw `httpx` for Bolna / Rocketlane / Slack | Small, explicit, trivially mocked | You handle API quirks (Slack `ok:false`, form-encoded lookup) yourself |
| Two stores (checkpoint + `workflow_runs`) | Events can find a run without reading checkpoints; the graph stays resumable | Must keep them consistent; `_execute` is the single place that does it |
| `interrupt()` for every wait | A run can wait days with no process alive | Nodes with `interrupt()` must have no side effects before it |
