# Code flow: start to end

How one deal email travels through the code, from the inbox to a Rocketlane project and a Slack channel, including the path where the AE does not answer the call.

There are three workflows. Each is its own LangGraph graph with its own thread and its own row in `app.workflow_runs`.

| Workflow | Thread id | Pauses (`waiting_for`) |
|---|---|---|
| `intake` | `intake:<inbound_email_id>` | never |
| `tier_confirmation` | `tier:<onboarding_id>` | `call_result`, `call_retry`, `email_reply` |
| `provisioning` | `provision:<onboarding_id>` | never (a failure stops at the failing node) |

The LangGraph checkpointer (Postgres) stores *where each graph is* under its thread id. `workflow_runs` stores what an outside event needs to *find* a paused run: status, what it waits for, deadlines.

---

## 0. Startup — `main.py`

`python main.py` builds the FastAPI app and registers the routers (`src/api/register_routes.py`). The lifespan starts the background timers:

- call-result poller (`src/scheduler/call_result_poller.py`)
- call retry (`src/scheduler/retry_runner.py`)
- AE-reply timeout (`src/scheduler/tier_escalation.py`)
- workflow sweeper (`src/scheduler/workflow_sweeper.py`)
- optionally the Pub/Sub puller, when `PUBSUB_PULL_ENABLED=true`

---

## 1. An email arrives — `src/services/gmail_intake_service.py`

Entry point: `GmailIntakeService.handle_gmail_notification`. It is called by Pub/Sub (pull, push, or the Cloud Function).

1. Diff the mailbox's `history_id` cursor against Gmail to get the new message ids.
2. For each message, `_handle_message`:
   - skip messages that were deleted (404) or that we sent ourselves (`SENT` label);
   - read the full body and run `redact_pii` — the raw body is never stored or checkpointed;
   - `InboundEmailService.try_insert` records the row in `inbound_emails` (with `body_redacted` and `gmail_thread_id`); a duplicate is skipped here.
3. Call `WorkflowOrchestrator.process_inbound_email`.

---

## 2. Intake workflow — `src/agents/intake/graph.py` (thread `intake:<inbound_email_id>`)

`src/services/workflow_orchestrator.py` creates a `workflow_runs` row and runs the graph. It begins with the classifier subgraph, `src/agents/email_analyser/graph.py`:

| Node | What it does |
|---|---|
| `fetch_email`, `pii_redactor` | Compose the text and redact it a second time |
| `detect_escalation_reply` | Match the email to an `escalations` row by Gmail thread, then by `[ONB-…]` subject token. No LLM. Match → `kind = escalation_reply` → `handle_escalation_reply` → END |
| `sender_gate` | Sender not in `account_executives` → `no_deal` (rule) → `archive_skip` → END |
| `injection_detector` | Prompt injection → `malicious` → `log_and_reject` → END |
| `analyze_deal_status`, `fact_check_validator` | LLM verdict, then hard checks. Confirmed → `deal_closed`; otherwise `no_deal` |

For `deal_closed`, `extract_deal` (`src/agents/intake/nodes.py`):

- extracts the customer and AE details;
- creates the `onboardings` row (AE name and email are kept in `extra_details`);
- inserts a `workflow_runs` row for `tier:<onboarding_id>` with status `pending`.

Back in `process_inbound_email`: a deal calls `start_pending()`; an escalation reply is handled in step 5.

---

## 3. Tier workflow — `src/agents/tier_confirmation/graph.py` (thread `tier:<onboarding_id>`)

`start_pending` claims the pending run and builds the input from the database (`_tier_input`). Then:

1. **`place_plan_tier_call`** looks the AE up by email.
   - No row (no phone) → `request_ae_reply` (step 4).
   - Outside call hours → run marked `waiting / call_retry` with `due_at`; `wait_for_call_retry` pauses.
   - Otherwise it calls Bolna and marks the run `waiting / call_result`.
2. **`await_plan_tier_result`** pauses at `interrupt()`. `src/services/call_result_service.py` resumes it, from the poller or `POST /webhooks/bolna`:
   - finalize the call once, decide the outcome, claim the run (`claim_waiting`), call `orchestrator.resume`;
   - **confirmed** → set `plan_tier` → `confirm_tier`;
   - **not confirmed, tries left** → `schedule_call_retry` marks `waiting / call_retry`; `src/services/retry_service.py` resumes it when `due_at` passes;
   - **tries exhausted** → `request_ae_reply`.

---

## 4. The AE doesn't answer — the escalation email

`request_ae_reply`:

- marks the run `waiting / email_reply` with `stop_at` (+24h);
- calls `HumanTierNotifier.open_request` (`src/services/human_tier_notifier.py`): creates an `escalations` row, sends "reply ENTERPRISE or GROWTH" through `EmailService` (Gmail API), and stores the sent email's `gmail_thread_id` and token;
- a failed send is audited and never raises.

`await_ae_reply` then pauses. The graph state sits in the Postgres checkpointer under `tier:<id>`.

---

## 5. The AE replies — back through step 1

1. The Gmail notification arrives; the classifier's `detect_escalation_reply` matches it → `kind = escalation_reply`.
2. `GmailIntakeService._deliver_reply` calls `TierReplyService.deliver_reply` (`src/services/tier_reply_service.py`):
   - the sender must equal `escalation.to_email`, otherwise it is rejected;
   - `parse_tier` (`src/agents/tier_confirmation/tier_reply.py`) needs exactly one clear word; an unclear reply gets a "one word please" nudge (at most twice);
   - `claim_for_tier_answer` claims the run (it works whether the run is `waiting` or `stopped`);
   - `orchestrator.resume(run, {plan_tier, reviewer, inbound_email_id})` continues the **same thread**.
3. `await_ae_reply` wakes, sets `plan_tier`, marks the escalation answered. `confirm_tier` then inserts `provision:<id>` as `pending`.

---

## 6. Provisioning — `src/agents/provisioning/graph.py` (thread `provision:<onboarding_id>`)

After a resume the orchestrator calls `start_pending()` again. The sweeper does the same, which covers a crash between the two workflows.

1. **`create_rocketlane_project`** reads the tier row and the CSM, writes a `creating` row, and calls Rocketlane. On failure it records the error and **raises**, so the run stops at this node.
2. **`communication_agent`** (the Slack subgraph) runs next: prepare → create channel → set topic → invite the team → post the welcome → invite the customer → finalize.

After every run, the orchestrator's `_execute` writes the result to `workflow_runs`: `succeeded` plus an outcome, `failed` plus `current_node`, or it leaves the `waiting` row that the pausing node wrote.

---

## 7. Timers and operator paths

**Stop timer** — `src/services/tier_escalation_service.py`: when `stop_at` passes with no reply, the run becomes `stopped`. Nothing is lost, and a late reply still resumes it.

**API** — `src/api/routes/onboarding_router.py` (all need the `x-admin-token` header):

| Route | Purpose |
|---|---|
| `GET /onboardings/{id}/runs` | Status, `waiting_for` and `current_node` of each workflow |
| `POST /onboardings/{id}/plan-tier` | An operator sets the tier by hand |
| `POST /onboardings/{id}/resume` | With a tier: apply it. With no body: ask the AE again |
| `POST /onboardings/{id}/retry` | Re-run a failed workflow from the node that failed |

---

## Compact picture

```
email → intake → ┬ no_deal / malicious → archive
                 ├ escalation_reply ────────────────────────────┐ resume tier:<id>
                 └ deal_closed → tier workflow                  │
                       call AE ── confirmed ──────────┐         │
                       │ no answer → retry → email AE → wait ◄──┘
                       └──────────────────────────────┴→ confirm_tier
                                                          ↓ (pending → started)
                                         provisioning: Rocketlane → Slack channel
```
