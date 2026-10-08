# Customer Onboarding Automation: technical document

A multi-agent service that turns a "deal closed" email from an Account Executive (AE) into a fully set-up customer onboarding: the AE is phoned to confirm the plan tier, a Rocketlane project is created from the tier's template, and a shared Slack channel is opened with the customer and the CS team.

For the step-by-step path of one email through the code, see [CODE_FLOW.md](CODE_FLOW.md). This document covers the architecture, the code layout, the data model, the libraries, configuration, testing and operations.

---

## 1. What the system does

```
CS inbox (Gmail)
   │  Pub/Sub notification
   ▼
INTAKE workflow          classify the email: closed deal / not a deal / malicious / AE reply
   │ closed deal
   ▼
TIER_CONFIRMATION        call the AE (Bolna) → Enterprise or Growth
   │  no answer? retry → then email the AE; their reply resumes the same run
   ▼
PROVISIONING             Rocketlane: create project + import template   (can be switched off)
                         Slack: create shared channel, invite team + customer, post welcome
```

Design principles:

- **The LLM never decides anything that matters.** The plan tier comes from the AE (a call or an email reply), parsed with strict rules. The Rocketlane template, the CSM and manager, and the channel prefix all come from database rows (`plan_tiers`, `cs_team_members`).
- **Nothing is lost on a failure.** Each workflow is a LangGraph graph with a Postgres checkpoint. A failure stops the run at the failing node; a retry continues from there.
- **Every external side effect is written down first** (`creating` rows) so a crash or retry never creates a duplicate project or channel.
- **Personal data is redacted before storage.** The raw email body is never stored or checkpointed.

---

## 2. Technology stack

| Concern | Choice |
|---|---|
| Language / runtime | Python 3.12+ (developed on 3.13), Poetry for dependencies |
| Web / API | FastAPI served by Uvicorn |
| Agent orchestration | LangGraph (graphs, subgraphs, `interrupt()`, Postgres checkpointer) |
| LLM | Groq (Llama) by default, Google Gemini switchable, through LangChain chat models |
| Database | PostgreSQL, accessed with psycopg 3 (async, connection pool) |
| Email | Gmail API with Pub/Sub notifications (pull, push or Cloud Function) |
| Voice | Bolna (outbound phone calls to the AE) |
| Project management | Rocketlane REST API |
| Chat | Slack Web API (bot token) |
| Settings | pydantic-settings, `.env` file |
| Tests | pytest + pytest-asyncio |

---

## 3. Libraries

Declared in [pyproject.toml](../pyproject.toml).

### Runtime dependencies

| Library | Version | Used for |
|---|---|---|
| `fastapi` | ^0.129 | The HTTP API: health, Bolna webhook, Pub/Sub push, Gmail admin and the onboarding operator routes |
| `uvicorn` | ^0.34 | ASGI server that runs the FastAPI app (`python main.py`) |
| `pydantic-settings` | ^2.6 | `Settings` class in `src/config/settings.py`: typed config from environment / `.env` with validators |
| `pydantic` (via the above) | v2 | Base for every entity, DTO and structured LLM output (`src/models/base_model.py`) |
| `python-dotenv` | ^1.2 | Loading `.env` |
| `langgraph` | ^1.2 | `StateGraph`, subgraphs, `interrupt()` / `Command(resume=…)`, compiled graphs with a checkpointer |
| `langgraph-checkpoint-postgres` | ^3.0 | `AsyncPostgresSaver`: stores each graph's state per `thread_id` in Postgres |
| `langchain` / `langchain-core` | ^1.3 / ^1.4 | Prompt templates, `with_structured_output`, runnable chains |
| `langchain-groq` | ^1.1 | `ChatGroq`: the default LLM (Llama models on Groq) |
| `langchain-google-genai` | ^4.2 | `ChatGoogleGenerativeAI`: Gemini, selected with `LLM_PROVIDER=gemini` |
| `psycopg[binary,pool]` | ^3.3 | Async Postgres driver (`dict_row`, autocommit) and `AsyncConnectionPool` |
| `httpx` | ^0.28 | Async HTTP client for Bolna, Rocketlane and Slack (and the in-test `MockTransport`) |
| `google-api-python-client` | ^2.149 | Gmail API (read messages, history, watch, send) |
| `google-auth-oauthlib` | ^1.2 | OAuth flow and `token.json` for Gmail |
| `google-auth-httplib2` | ^0.2 | Transport that the Google API client uses |
| `google-cloud-pubsub` | ^2.27 | Streaming pull subscriber for Gmail notifications |
| `functions-framework` | ^3.8 | Runs `functions/pubsub_intake.py` as a Cloud Function (alternative to pull) |

### Development dependencies

| Library | Used for |
|---|---|
| `pytest` ^7 | Test runner |
| `pytest-asyncio` ^0.23 | `asyncio_mode = "auto"`: async tests need no decorator |

### Standard library worth knowing

`asyncio` (single event loop plus a loop thread for Pub/Sub), `threading.Timer` (stream auto-restart), `email` (parsing `From` headers, RFC 2822 dates), `uuid`, `re` (strict tier parsing, PII redaction), `functools.cache`.

---

## 4. Repository layout

```
main.py                     FastAPI app, lifespan (startup checks, timers, Pub/Sub puller)
db/
  schema.sql                canonical schema (12 tables)
  schema_changes_workflow_split.sql   one-off migration for an older dev database
docs/                       CODE_FLOW.md, this file, bolna_agent_setup.md
functions/pubsub_intake.py  Cloud Function entry (push alternative)
scripts/                    register_gmail_watch.py, run_pubsub_pull_locally.py, test_bolna_call.py
src/
  config/                   settings, model factory, database pool, checkpointer
  models/                   base_model, entities (one per table), enums, dto
  services/                 one service per table + orchestration and integration services
  agents/                   the LangGraph graphs (one package per agent / workflow)
  integrations/             Bolna, Rocketlane, Slack HTTP clients
  prompts/                  every LLM prompt as a text file, plus load_prompt() (no prompt text lives in code)
  gmail/                    OAuth and the Gmail client
  scheduler/                background timers
  api/                      routers, dependencies, exception handlers
  state/ utils/             shared state reducers, FailureTracker, error helpers
tests/                      pytest suite
```

The layering is **models → services → routers**:

- **Models** (`src/models`): pydantic entities that mirror tables (`__table_name__`), enums, and request/response DTOs. `BaseModel` uses `use_enum_values` and converts UUIDs to `str` in `from_row`.
- **Services** (`src/services`): all SQL lives here. `BaseService[T]` gives generic `find_one`, `create`, `update`, `fetch_one`, `execute` over the pool. Business services extend it.
- **Routers** (`src/api/routes`): thin HTTP handlers that call services.
- **Agents** (`src/agents`): the graphs. Their states are `TypedDict`s; nodes call services and clients.

---

## 5. Code walkthrough

### 5.1 Startup: `main.py`

The FastAPI lifespan:

1. validates the LLM configuration (`check_llm_configuration`), so a wrong model ID fails at startup rather than on the first email;
2. starts the background loops (Section 5.9);
3. optionally starts the Pub/Sub puller when `PUBSUB_PULL_ENABLED=true`;
4. on Windows, `python main.py` sets the Selector event-loop policy, because psycopg's async mode does not work on the default Proactor loop.

### 5.2 Configuration: `src/config`

- `settings.py`: one `Settings` object (`get_settings()`, cached). Notable groups: LLM (`llm_provider`, Groq / Gemini model IDs, attempts and timeouts), Gmail and Pub/Sub, Bolna, Rocketlane (`rocketlane_enabled`, key, base URL), Slack (`slack_bot_token`, `slack_channel_is_private`), human-fallback timing (`human_stop_after_hours`, `escalation_poll_seconds`), `require_ae_sender`, `admin_api_token`. A model validator checks only the chosen provider's model ID.
- `model_factory.py`: `LlmModelFactory` builds `ChatGroq` or `ChatGoogleGenerativeAI`. `get_deal_parser_llm()` is what the graphs use.
- `database_config.py`: the shared `AsyncConnectionPool`.
- `checkpointer_config.py`: the `AsyncPostgresSaver` the graphs compile with.

### 5.3 Gmail intake: `src/services/gmail_intake_service.py`

`handle_gmail_notification` is the single entry point for Pub/Sub (pull, push, Cloud Function):

1. Diff the stored `history_id` (table `mailboxes`) against Gmail to find new message IDs.
2. For each message (`_handle_message`, isolated so one bad message cannot block the rest): skip deleted (404) and self-sent (`SENT` label) mail, fetch the body, **redact PII**, and insert an `inbound_emails` row. A duplicate message is skipped by a unique key.
3. Hand off to `WorkflowOrchestrator.process_inbound_email`.

Supporting pieces: `gmail_watch_service.py` (registers the Gmail `watch`), `pubsub_pull_service.py` (a streaming-pull subscriber on its own thread, with auto-restart when the stream ends), `src/gmail/auth.py` (scopes: read always, `gmail.send` only when sending) and `src/gmail/client.py`.

Failure handling follows one pattern (`src/utils/failure_tracker.py`, `src/utils/errors.py`): classify an error as transient or permanent, throttle repeated log lines, back off, and nack only after the backoff so a poisoned message does not spin.

### 5.4 Workflow orchestration: `src/services/workflow_orchestrator.py`, `src/agents/workflow_graph.py`

There are three workflows, each a separate graph with a `thread_id` and a row in `workflow_runs`:

| Workflow | Thread id | Pauses for |
|---|---|---|
| `intake` | `intake:<inbound_email_id>` | never |
| `tier_confirmation` | `tier:<onboarding_id>` | `call_result`, `call_retry`, `email_reply` |
| `provisioning` | `provision:<onboarding_id>` | never (a failure stops at the failing node) |

- `WorkflowGraph` wraps a graph builder and offers `start`, `resume`, `rerun` (continue a failed run with `ainvoke(None)`) and `snapshot`. It compiles lazily with the Postgres checkpointer.
- `WorkflowOrchestrator` is the only place that runs graphs. `_execute` writes the result back to `workflow_runs`: `succeeded` plus an outcome, `failed` plus `current_node`, or it leaves the `waiting` row that the pausing node wrote.
- **Division of storage:** the LangGraph checkpoint stores *where each graph is*; `workflow_runs` stores what an outside event (a call result, a retry timer, an email reply) needs to *find* a paused run: status, `waiting_for`, `due_at`, `stop_at`.
- Hand-off between workflows is a `pending` row; `start_pending()` claims and starts it. The sweeper repeats this, which covers a crash between two workflows.

### 5.5 Intake agent: `src/agents/intake`, `src/agents/email_analyser`

The classifier subgraph (`email_analyser/graph.py`) has two stages. **Guardrails first** (cheap, deterministic, no LLM), then classification:

1. `fetch_email` composes the text. Then **one `guardrail` node** (`email_guardrail.py`, built with `create_guardrail_node` from `agents/commons/guardrail`, the same pattern as rappit-ai-companion) runs, in order: PII redaction, then three rules. It writes a single `guardrail_check` (blocked? which rule? which category?) and `route_after_guardrail` sends a blocked email to the node for its category (`not_an_ae` → `archive_skip`; `prompt_injection` and `phishing` → `log_and_reject`). The first rule that blocks wins. A rule that raises (a database error) fails the run at this node instead of letting an unchecked email through.
2. `sender_gate` (rule): when `REQUIRE_AE_SENDER` is on, a sender not in `account_executives` is `no_deal`.
3. `injection_detector` (rule): prompt-injection patterns (`guardrails.py`).
4. `phishing_detector` (rule): credential requests, new-bank-details / gift-card / wire lures, links to bare IPs or with embedded credentials, punycode, a spoofed display name. One hard signal is enough; soft signals (account threats, pressure, shortened links, "click here") only count in pairs. A hit is `malicious`, logged and rejected by `log_and_reject`.
5. `detect_escalation_reply`: match the email to an `escalations` row by Gmail thread ID, then by the `[ONB-xxxxxxxx]` subject token. No LLM. A match ends in `handle_escalation_reply`.
6. `analyze_deal_status` + `fact_check_validator`: the LLM gives a verdict, then hard checks confirm it.

Because the guardrails run before reply detection, a reply from an address that is not a known AE is stopped by the sender gate (unless `REQUIRE_AE_SENDER=false`).

`extract_deal_node` then extracts customer and AE details (structured output into `DealEmailExtraction`), resolves the AE (`resolve_ae`: known AE by extracted email, then by the `From` address, then fall back to the extracted values), creates the `onboardings` row and queues the tier workflow as `pending`.

Duplicates: the same Salesforce opportunity ID for the *same customer* (`same_customer` ignores case, punctuation and a legal suffix like Inc/Corp) is a re-send and is ignored. The same ID under a *different* customer is onboarded anyway, with the ID dropped and the conflict noted in `extra_details`.

### 5.6 Tier confirmation: `src/agents/tier_confirmation`

Nodes: `place_plan_tier_call`, `await_plan_tier_result`, `schedule_call_retry`, `wait_for_call_retry`, `request_ae_reply`, `await_ae_reply`, `confirm_tier`.

- **Calling:** `place_plan_tier_call` looks the AE up by email. No phone row → straight to the email fallback. Outside call hours (`call_policy.py`) → wait for the next window. Otherwise it places a Bolna call (`integrations/voice_ai_client.py`) and parks the run as `waiting / call_result`.
- **Getting the answer:** `await_plan_tier_result` pauses at `interrupt()`. `CallResultService` resumes it (`call_result_poller` or `POST /webhooks/bolna`), after finalizing the call once and deciding the outcome.
  - Tier from Bolna's structured `extracted_data` when configured.
  - Otherwise a strict transcript fallback: the AE stated a tier, the assistant read the *same* tier back, the next user turn is affirmative, and there is no negation. Anything else is "unclear" and is never guessed.
- **No answer:** retries up to the configured limit (`retry_service.py`), then `request_ae_reply` emails the AE through `HumanTierNotifier` and `EmailService`, creating an `escalations` row and storing the sent thread ID and token.
- **AE reply:** `TierReplyService.deliver_reply` checks the sender equals `escalation.to_email`, parses the body with `tier_reply.parse_tier` (exactly one clear word; unclear replies get a "one word please" nudge, at most twice) and resumes the same thread.
- **Silence:** `TierEscalationService` marks a run `stopped` after `stop_at`; a late reply, or `POST /onboardings/{id}/resume`, still continues it.
- `confirm_tier` records `plan_tier` and its source (`voice_call`, `email_reply` or `operator`), then queues provisioning.

### 5.7 Provisioning: `src/agents/provisioning`, `src/agents/communication`

`build_provisioning_graph` reads `ROCKETLANE_ENABLED`:

```
ROCKETLANE_ENABLED=true  :  START → create_rocketlane_project → communication_agent → END
ROCKETLANE_ENABLED=false :  START → assign_team              → communication_agent → END
```

**`create_rocketlane_project_node`** works in two Rocketlane calls (`integrations/rocketlane_client.py`):

1. `create_project`: `POST /projects` with name, customer, owner (the CSM's email), dates and the customer contact. No template.
2. `import_template`: `POST /projects/{projectId}/import-template` with `[{templateId, startDate}]`.

Idempotency: a `creating` row is written before step 1; the project ID is saved as soon as step 1 succeeds; the row becomes `created` only after step 2. A retry after a failed import reuses the saved project ID and only re-runs the import. Any failure is recorded and raised, so the run stops at this node.

**`assign_team_node`** (when Rocketlane is off) does only the part the Slack step depends on: pick the CSM and manager for the tier, and set `csm_id`, `manager_id` and the dates on the onboarding.

**The Slack communication agent** (`agents/communication`) is a **ReAct agent**: `prepare → agent ⇄ tools → finalize`.

- **`prepare`** loads one joined read model (`OnboardingService.get_comms_context`) and starts the model's conversation: a stable system prompt (`src/prompts/communication/agent_system.md`, assembled by `context.py`) plus a small `<facts>` brief (names, plan, dates, the channel names to use, the project link). The brief contains **no emails and no Slack IDs**, and customer-supplied text is fenced as data.
- **`agent`** asks the tool-calling LLM (`get_agent_llm()`, Groq or Gemini) for the next step. Model failures are retried (`LLM_ATTEMPTS`); if the model stays unavailable, the agent steps aside.
- **`tools`** (`tools.py`, a LangGraph `ToolNode`): `lookup_team_in_slack`, `create_slack_channel`, `set_channel_topic`, `invite_internal_team`, `post_welcome_message`, `invite_customer_to_channel`. The onboarding comes from the graph state (injected, never chosen by the model), people and the customer's address come from the database. Results come back as short `ERROR (temporary…)` / `ERROR (permanent…)` messages the model can act on.
- **Tool retry** (`operations.with_retry`): temporary Slack failures (rate limit, 5xx, network) are retried with a doubling wait (`SLACK_TOOL_RETRY_ATTEMPTS`, `SLACK_TOOL_RETRY_BASE_SECONDS`); permanent ones (missing scope, restricted action) fail immediately.
- **The welcome message is written by the LLM** and checked by guardrails (`welcome.py`) before it is posted: no `@channel`, no invented mentions, no links except the onboarding project, sane length. It refers to teammates as `{{csm}}`, `{{manager}}`, `{{ae}}`, and the tool swaps in real Slack mentions, so the model never handles Slack IDs. A rejected draft is explained to the model so it can fix it.
- **`finalize`** is the safety net and bookkeeper: it completes any step the agent did not attempt (the standard welcome template stands in for the LLM's), then decides the outcome from the database, not from the model's claims. If the LLM is down, the whole setup still completes this way. The loop is bounded (`COMMUNICATION_AGENT_MAX_STEPS`).
- Crash safety is in the operations, so it holds whoever calls them: a `slack_channels` row is written before the API call, an existing channel is reused, the welcome is posted once (`welcome_message_ts`), and the Slack Connect invite is sent once.
- The agent's conversation lives only inside the subgraph (its input and output schemas are `CommunicationState`), so the provisioning checkpoint does not grow with it.
- With no `SLACK_BOT_TOKEN`, the whole step is skipped with a warning.

### 5.8 Integrations: `src/integrations`

| Client | API | Notes |
|---|---|---|
| `voice_ai_client.py` (`BolnaVoiceClient`) | Bolna calls and executions | `parse_extraction`, `parse_transcript`, `decide_outcome` |
| `rocketlane_client.py` (`RocketlaneClient`) | `https://api.rocketlane.com/api/1.0`, header `api-key` | Errors carry Rocketlane's explanation; network failures name the host |
| `slack_client.py` (`SlackClient`) | `https://slack.com/api` | HTTP 200 with `ok:false` is raised as `SlackApiError`; `missing_scope` reports the needed scope; one retry on a 429 |

Slack scopes needed: `groups:write` (or `channels:manage` for public channels), `chat:write`, `conversations.connect:write`, `users:read.email`.

### 5.9 Background timers: `src/scheduler`

`periodic.py` provides the loop helper; the loops are:

| Loop | Job |
|---|---|
| `call_result_poller` | Polls Bolna for finished calls and resumes `waiting / call_result` runs |
| `retry_runner` | Resumes `waiting / call_retry` runs when `due_at` passes |
| `tier_escalation` | Marks runs `stopped` when `stop_at` passes with no AE reply |
| `workflow_sweeper` | Starts `pending` runs and retries lost hand-offs |

### 5.10 HTTP API: `src/api`

| Route | Purpose |
|---|---|
| `GET /health` | Liveness |
| `POST /webhooks/bolna` | Bolna call-finished callback (the execution is re-fetched before it is trusted) |
| `POST /pubsub/push` | Gmail notification by push (token-protected) |
| `/gmail/...` | Gmail watch administration |
| `GET /onboardings`, `GET /onboardings/{id}`, `GET /onboardings/{id}/runs` | Inspection |
| `POST /onboardings/{id}/plan-tier` | An operator sets the tier |
| `POST /onboardings/{id}/resume` | Apply a tier, or ask the AE again |
| `POST /onboardings/{id}/retry` | Re-run a failed workflow from the failed node |

The operator routes require the `x-admin-token` header. Errors map to HTTP responses in `api/exceptions`.

---

## 6. Data model: `db/schema.sql`

Twelve tables in the `app` schema:

| Table | Purpose |
|---|---|
| `mailboxes` | Watched inbox and the Gmail `history_id` cursor |
| `inbound_emails` | Every received email (redacted body, status, kind, Gmail thread ID) |
| `account_executives` | Known AEs: name, email, phone (E.164), Slack ID |
| `plan_tiers` | Per tier: Rocketlane template ID, duration, CSM model, Slack prefix, welcome template |
| `cs_team_members` | CSMs and managers: role, pool, Rocketlane user ID, Slack ID |
| `onboardings` | The central record: customer, AE, tier and its source, CSM, manager, dates, status |
| `voice_calls` | Each Bolna call: outcome, detected tier, read-back confirmation |
| `escalations` | AE email fallback: thread ID, token, status |
| `workflow_runs` | One row per workflow run: status, `waiting_for`, `current_node`, `due_at`, `stop_at`, attempts |
| `rocketlane_projects` | Project link: `creating` / `created` / `failed` |
| `slack_channels` | Channel, invite status, welcome timestamp |
| `audit_events` | Append-only trail of what happened and who did it |

LangGraph also creates its own checkpoint tables the first time the checkpointer starts.

Seed data you must provide before a real run: `plan_tiers` (two rows, `enterprise` and `growth`), `cs_team_members` (at least one active CSM per pool and a manager), `account_executives` (with a real phone), and one `mailboxes` row.

---

## 7. Configuration and running

Copy `.env.example` to `.env`. The main groups:

| Group | Keys |
|---|---|
| Database | `POSTGRES_URI` |
| LLM | `LLM_PROVIDER` (`groq` or `gemini`), `GROQ_API_KEY`, `GROQ_MODEL`, `GOOGLE_API_KEY`, `GEMINI_MODEL` |
| Gmail / Pub/Sub | `GMAIL_CREDENTIALS_PATH`, `GMAIL_TOKEN_PATH`, `PUBSUB_PULL_ENABLED`, topic and subscription names |
| Bolna | API key, agent ID, from-number, `BOLNA_TRANSCRIPT_FALLBACK` |
| Rocketlane | `ROCKETLANE_ENABLED`, `ROCKETLANE_API_KEY`, `ROCKETLANE_BASE_URL` |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_IS_PRIVATE` |
| Behaviour | `REQUIRE_AE_SENDER`, `HUMAN_EMAIL_ENABLED`, `HUMAN_STOP_AFTER_HOURS`, `ADMIN_API_TOKEN` |

Run (Windows / PowerShell):

```
python -m poetry install
# apply db/schema.sql to your Postgres, then seed plan_tiers, cs_team_members, account_executives, mailboxes
python -m poetry run python scripts/register_gmail_watch.py     # once: registers the Gmail watch (interactive OAuth)
python -m poetry run python main.py                              # with PUBSUB_PULL_ENABLED=true
```

Run only one process that pulls from Pub/Sub; do not run the pull script alongside `main.py`.

---

## 8. Testing

```
python -m poetry run pytest tests -q -rs
```

- The suite sets its own environment in `tests/conftest.py` (LLM provider and models, `ROCKETLANE_ENABLED=true`), blocks any real Gmail use, and resets the app singletons between tests.
- Tests that need Postgres create a throwaway database on your server; they are skipped, with the reason shown by `-rs`, if it is unreachable. Set `TEST_POSTGRES_ADMIN_URI` if it is not on the default address.
- Coverage by area:

| Area | Files |
|---|---|
| End-to-end flows (call and email fallback, provisioning failure and retry) | `test_voice_flow_integration.py`, `test_human_tier_flow.py` |
| Intake and classification | `test_email_analyser.py`, `test_gmail_intake_service.py`, `test_intake_ae_resolution.py`, `test_intake_duplicates.py` |
| Voice and tier parsing | `test_bolna_transcript.py`, `test_bolna_helpers.py`, `test_tier_reply_parsing.py`, `test_call_policy.py` |
| Slack and Rocketlane | `test_communication_agent.py`, `test_integration_clients.py` |
| Graph structure and state | `test_deal_intake_graph.py`, `test_checkpoint_serde.py` |
| Config, auth and errors | `test_settings.py`, `test_model_factory.py`, `test_gmail_auth.py`, `test_error_handling.py` |
| Models, services and API | `test_models_and_services.py`, `test_api_routers.py` |

---

## 9. Operations and troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| A run is `failed` at a node | Fix the cause, then `POST /onboardings/{id}/retry`; it resumes at that node |
| Run `waiting / email_reply` for too long | It becomes `stopped` after `HUMAN_STOP_AFTER_HOURS`; a late reply or `POST /onboardings/{id}/resume` continues it |
| Call answered but nothing happens | Check the poller log; with no Bolna extractions the transcript fallback decides, and an unclear transcript is retried, never guessed |
| `missing_scope` from Slack | Add the scope the log names, **reinstall the app**, put the new token in `.env` |
| Slack channel not visible | A private channel is visible only to its members; set `slack_user_id` or the person's email on the CSM row so they are invited, or use the Slack admin view |
| Rocketlane `ConnectTimeout` | Network path to `api.rocketlane.com` (VPN, proxy, firewall); set `ROCKETLANE_ENABLED=false` to skip it meanwhile |
| FK violation on `voice_calls.detected_plan_tier` | `plan_tiers` is empty: seed the two tier rows |
| `psycopg` loop error on Windows | Must run on the Selector event loop; `main.py` and the scripts set it |

Security notes: secrets live only in `.env` and `token.json` (keep both out of source control); the Bolna webhook re-fetches the execution before trusting it; operator routes need `ADMIN_API_TOKEN`; the LLM output is validated, and the AE's reply is accepted only from the address the escalation was sent to.

---

## 10. Known limitations

- Retries of failed workflows are manual (`POST /onboardings/{id}/retry`), by design for this demo.
- `CallResultService` finalizes a call before saving its results; a failure in between leaves the call finalized and the run waiting. A fix (reopen the call on failure) is not yet implemented.
- There is no endpoint to reprocess emails that failed intake.
- The Slack Connect customer invite needs a Slack plan and workspace settings that allow it; a failure is flagged in the channel, not fatal.
- Migrations are plain SQL files; there is no migration tool.
