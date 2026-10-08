# Customer Onboarding Automation

Deal-intake agent, structured the same way as agents in `rappit-ai-companion` (a `state.py` /
`models.py` / `node.py` / `graph.py` split, async nodes, `with_structured_output` for
classification, a Postgres-backed checkpointer) — but **fully independent**: no dependency on
`corelibraries-python` or anything else under `ailibraries/`.

## What it does

> Agent 1 — Intake & Routing: monitors an AE's inbox for deal-closed emails; parses customer +
> AE details; since the email never carries the plan tier, places a phone call to the AE to
> confirm Enterprise vs. Growth; then creates the Rocketlane onboarding project with the right
> template, timeline, owner and manager.

```
CS inbox ◄── AE emails a deal  /  the AE replies to our escalation email
   │
   │ Gmail watch() → Pub/Sub → decode notification → mailbox cursor → Gmail history
   ▼
read body → redact PII → app.inbound_emails (insert-or-skip: dedupes replays)
   │
   ▼
┌─ INTAKE workflow   thread  intake:<inbound_email_id>   (one email, runs to completion) ──────────┐
│   classify:  reply to our escalation?  (Gmail thread / subject token — never the LLM)            │
│              → no_deal (not from an AE) · malicious (prompt injection) · deal_closed · no_deal   │
│   deal_closed ──► extract_deal: create app.onboardings, queue the tier workflow                  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
   │ deal_closed                                         │ escalation_reply (AE only, one clear word)
   ▼                                                     │
┌─ TIER_CONFIRMATION workflow   thread  tier:<onboarding_id> ───────────────────────┐   resumes
│  place_plan_tier_call ─► await_plan_tier_result   (waiting: call_result)          │◄───────┘
│     ├─ AE confirms on the call ─────────────────────────────┐                     │
│     ├─ no answer / unclear ─► schedule_call_retry ─► wait_for_call_retry (call_retry timer)
│     └─ attempts exhausted / no phone ─► request_ae_reply   (EMAILS the AE: reply ENTERPRISE or GROWTH)
│                                         ─► await_ae_reply  (waiting: email_reply, stop_at +24h)
│                                         ─► confirm_tier ◄──────────────────────────┘
│  confirm_tier: queue the provisioning run as `pending`                              │
└────────────────────────────────────────────────────────────────────────────────────┘
   │ pending → started right away (and by the sweeper if the app died in between)
   ▼
┌─ PROVISIONING workflow   thread  provision:<onboarding_id> ────────────────────────┐
│  create_rocketlane_project ─► communication_agent (shared Slack channel) ─► END      │
│  a failure stops AT that node (status failed); POST /onboardings/{id}/retry resumes  │
└────────────────────────────────────────────────────────────────────────────────────┘
```

Each part is its own LangGraph graph with its own **thread** and its own row in
`app.workflow_runs`, so every part has its own status, state and resume point (see
[Workflows, threads and resume](#workflows-threads-and-resume)). A graph genuinely **pauses** (via
`interrupt()`) while it waits — for the call result, a retry time, or the AE's email — and a
**Postgres-backed checkpointer** stores exactly where it paused under its `thread_id`.

## Workflows, threads and resume

| Workflow | Thread id | Pauses (`waiting_for`) | Ends by |
|---|---|---|---|
| `intake` | `intake:<inbound_email_id>` | never | classifying the email (`deal_closed` / `no_deal` / `escalation_reply` / `malicious`) |
| `tier_confirmation` | `tier:<onboarding_id>` | `call_result`, `call_retry` (timer), `email_reply` (the AE) | writing `onboardings.plan_tier`, then queuing `provisioning` |
| `provisioning` | `provision:<onboarding_id>` | never — a failure stops *at that node* | Rocketlane project + Slack channel |

**Two stores, two jobs.** The LangGraph checkpointer holds *where each graph is* plus a small working
state, under the `thread_id`. `app.workflow_runs` (one row per run: `workflow`, `thread_id`, `status` ∈
`pending|running|waiting|stopped|succeeded|failed`, `waiting_for`, `current_node`, `due_at`, `stop_at`,
`outcome`) holds what an outside event needs in order to **find** a paused run. The business facts live
in the `app.*` tables, and the nodes re-read them — checkpoints stay small and never contain the raw
email (only a PII-redacted copy, also kept in `inbound_emails.body_redacted`).

**How each event resumes a run**

| Event | Found through | Resumes |
|---|---|---|
| Call finished (poller / webhook) | `voice_calls.provider_call_id` → onboarding → tier run `waiting` for `call_result` | `tier:<id>` |
| Call retry due | tier run `waiting` for `call_retry`, `due_at <= now` | `tier:<id>` |
| **AE replies to the escalation email** | the classifier matches the Gmail thread of the email we sent (else the `[ONB-…]` subject token) → `app.escalations` → tier run | `tier:<id>`, which then queues `provision:<id>` — so the flow continues **from create_rocketlane_project** |
| Nobody answers the escalation within `HUMAN_STOP_AFTER_HOURS` (24h) | `workflow_runs.stop_at` | the run becomes `stopped` — still parked, nothing lost; a late reply or `POST /onboardings/{id}/plan-tier` continues it, `POST /onboardings/{id}/resume` asks the AE again |
| Rocketlane / Slack step failed | `workflow_runs.status = failed`, `current_node` | `POST /onboardings/{id}/retry` re-runs from the failing node (manual) |

An AE's reply never goes through the LLM: only the AE the escalation was sent to may answer, and only
with exactly one of ENTERPRISE / GROWTH (a negation or both words is "unclear" and gets a "please answer
with one word" email, at most twice).

**Database changes.** `db/schema.sql` is the canonical schema. To bring an *existing development*
database to this version run `db/schema_changes_workflow_split.sql` once (it **discards in-flight run
state** — it is meant for test data).

## Persistent storage: Postgres, all of it

Both the application's own state (`db/schema.sql`, 11 tables) and LangGraph's checkpoint state
live in the **same Postgres database** — no JSON files, no SQLite, nothing else. See
`src/models/entities/` (one class per table) and `src/services/` (one service per table) for the
data layer, and `src/config/checkpointer_config.py` / `src/config/database_config.py` for how one
connection pool backs both.

| Table | Why it exists |
|---|---|
| `mailboxes` | Gmail sync cursor (`history_id`) + `watch_expiration`, so the scheduled renewal job knows when to re-register `watch()` |
| `inbound_emails` | Every email looked at. `UNIQUE (mailbox_id, gmail_message_id)` is what makes a duplicate Pub/Sub notification a no-op instead of a second onboarding |
| `account_executives` | AE phone + timezone — the deal email has neither |
| `plan_tiers` | Template ID / timeline / CSM model / Slack tone, one row per confirmed tier — **the LLM never chooses a template**, it only ever produces `enterprise`/`growth` and this table does the rest |
| `cs_team_members` | Who becomes the Rocketlane Project Owner (CSM) and Project Manager — Rocketlane's own escalation automation needs both set correctly |
| `onboardings` | The central record, one per closed deal. `UNIQUE sf_opportunity_id` catches an AE re-sending the same deal in a second email (Gmail message-ID dedup alone wouldn't) |
| `voice_calls` | Every call attempt. `provider_call_id` is how the async webhook finds its way back to the right onboarding |
| `rocketlane_projects` | A `creating` row is written *before* calling the API — if the process crashes in between, a retry checks this row instead of risking a duplicate project |
| `slack_channels` | The shared customer channel (communication agent). A `creating` row is written before the Slack call; `welcome_message_ts` guarantees the welcome is posted once |
| `workflow_runs` | `thread_id` ↔ `onboarding_id`, so the webhook handler knows which paused graph run to resume |
| `audit_events` | Append-only timeline — every node logs here |

## A LangGraph gotcha this design avoids (verified empirically)

When a graph resumes after `interrupt()`, LangGraph **re-runs the node containing that
`interrupt()` call from the top**. Anything with a side effect *before* the `interrupt()` call in
the same node re-runs too. Tested directly against `langgraph`:

```
# interrupt() in the SAME node as the side effect:
side-effect executed, count= 1   # initial run, pauses at interrupt()
side-effect executed, count= 2   # resume — side effect replayed!
total side-effect calls: 2

# side effect and interrupt() split into two separate nodes:
side-effect executed, count= 1   # initial run
                                  # resume — side-effect node is NOT replayed
total side-effect calls: 1
```

That's why "place the call" (`place_plan_tier_call_node`) and "wait for the result"
(`await_plan_tier_result_node`) are two separate nodes, not one — see the docstring in
`src/agents/tier_confirmation/nodes.py`.

## External integrations

| Integration | Why | Built against |
|---|---|---|
| **Gmail API** (OAuth2) | Read the AE's deal-notification email | [Gmail API](https://developers.google.com/gmail/api) — `watch()` for push notifications, `history.list` for the delta, `messages.get` for content |
| **Google Cloud Pub/Sub** | Transport for Gmail's push notifications | Gmail's `watch()` publishes `{emailAddress, historyId}` to a topic you own |
| **Groq** (hosted Llama, API key) | Classify the email and parse the deal (structured output) | `langchain-groq`, `GROQ_API_KEY`, model `GROQ_MODEL` (default `llama-3.3-70b-versatile`). Gemini stays available: set `LLM_PROVIDER=gemini` + `GOOGLE_API_KEY` (`langchain-google-genai`) — see `src/config/model_factory.py` |
| **Bolna** (Voice AI) | Calls the AE (Indian numbers supported natively) to confirm Enterprise vs Growth | [`POST /call`](https://www.bolna.ai/docs/guides/outbound/making-outgoing-calls) with `user_data`, [execution webhooks](https://www.bolna.ai/docs/post-call/execution-payload), [`GET /executions/{id}`](https://www.bolna.ai/docs/api-reference/executions/get_execution), agent extractions. Swappable — the graph only depends on `send_call()` in `src/integrations/voice_ai_client.py` |
| **Rocketlane** | Creates the onboarding project | [`POST /projects`](https://developer.rocketlane.com/reference/create-project), `api-key` header |

## Setup

### 1. Postgres

```bash
docker run -d -p 5432:5432 --name postgres \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres postgres
docker exec postgres psql -U postgres -c "CREATE DATABASE onboarding_automation;"
psql postgresql://postgres:postgres@127.0.0.1:5432/onboarding_automation -f db/schema.sql
```

The LangGraph checkpointer creates its own tables on first run (`AsyncPostgresSaver.setup()`) —
no separate migration needed for those.

Seed the reference tables for your org (not covered by any script here — insert directly):
`app.account_executives`, `app.plan_tiers`, `app.cs_team_members`.

### 2. Gmail API + Pub/Sub

Same as before: enable the Gmail API + Pub/Sub API, create a topic + subscription, grant
`gmail-api-push@system.gserviceaccount.com` publish rights on the topic, create an OAuth
"Desktop app" client (`credentials.json`). See inline comments in `.env.example`.

### 3. LLM (Groq / Gemini), Bolna, Rocketlane

`GROQ_API_KEY` (or `LLM_PROVIDER=gemini` + `GOOGLE_API_KEY`), `BOLNA_API_KEY`, `ROCKETLANE_API_KEY` —
see `.env.example`. The app checks the chosen LLM provider at startup and tells you what's missing.

Bolna (free $5 credit on signup; calls Indian numbers from a +91 number) also needs a one-time
agent setup in its dashboard — prompt, extractions and webhook URL. Follow
**[`docs/bolna_agent_setup.md`](docs/bolna_agent_setup.md)**, then set:
- `BOLNA_AGENT_ID` — the agent's id.

Call results are fetched by **polling** Bolna's API (default), so the app needs **no public URL or
tunnel** — only outbound HTTPS to `api.bolna.ai`. A webhook mode is available as an option
(`BOLNA_WEBHOOK_TOKEN` + a public URL); see `docs/bolna_agent_setup.md`.

On a trial account Bolna only calls **verified numbers** — verify your own number in the dashboard
and put it in `app.account_executives.phone_e164` to play the AE.

If you applied an older `db/schema.sql`, run `db/schema_changes_workflow_split.sql` once (see
[Workflows, threads and resume](#workflows-threads-and-resume)).

**Sending the escalation emails** needs the Gmail `gmail.send` scope. After upgrading, delete
`token.json` and run `python scripts/register_gmail_watch.py` once to approve it (or set
`HUMAN_EMAIL_ENABLED=false` to skip the emails and answer through the API).

### 4. Install

```bash
python -m venv .venv
source .venv/Scripts/activate   # PowerShell: .venv\Scripts\Activate.ps1

pip install poetry
poetry install

cp .env.example .env   # then fill in the values above
```

### 5. First Gmail OAuth run + watch registration

```bash
python scripts/register_gmail_watch.py        # or, with the app running: POST /gmail/watch
```

Re-run this on a schedule (watch registrations expire after 7 days) — e.g. Cloud Scheduler
calling `POST /gmail/watch` with the `x-admin-token` header.

### 6. Run it

**App** — runs the call-result poller, the retry scheduler, the REST API and the optional Bolna
webhook (no tunnel needed in the default polling mode):
```bash
python main.py                          # picks the right event loop on Windows
uvicorn main:app --port 8000 --reload   # alternative
```

**Pub/Sub intake** — any one of:
- **Local dev, in the app** (pull subscription, no deploy needed): set `PUBSUB_PULL_ENABLED=true`
  and the app pulls Gmail notifications itself — one process runs everything.
- **Local dev, standalone**: `python scripts/run_pubsub_pull_locally.py`
- **Push subscription → the app**: point a push subscription at
  `POST /pubsub/push?token=<PUBSUB_PUSH_TOKEN>`
- **Deployed** (push-triggered Cloud Function):
  ```bash
  gcloud functions deploy deal-intake-pubsub \
    --gen2 --runtime=python312 --region=<region> \
    --source=. --entry-point=handle_pubsub_message \
    --trigger-topic=<GMAIL_WATCH_TOPIC>
  ```

## Project layout

The layering follows `rappit-ai-companion`: **models → services → routers**, with the agents'
`TypedDict` states alongside.

```
main.py                        # FastAPI app: lifespan (background workers) + routers
db/schema.sql                  # all 12 app.* tables (canonical)
db/schema_changes_workflow_split.sql  # one-off: bring an existing dev database to this schema
docs/bolna_agent_setup.md      # one-time Bolna agent setup (prompt, extractions, webhook)
functions/pubsub_intake.py     # Cloud Function entrypoint (Pub/Sub trigger)
scripts/                       # thin CLIs over the services — no logic of their own
├── register_gmail_watch.py    #   -> GmailWatchService
└── run_pubsub_pull_locally.py #   -> PubSubPullService
src/
├── models/                    # everything that is data
│   ├── base_model.py           # BaseModel(pydantic.BaseModel): table metadata + from_row()
│   ├── entities/               # one class per app.* table (Onboarding, VoiceCall, ...)
│   ├── enums/                  # PlanTierCode, OnboardingStatus, WorkflowRunStatus, ...
│   └── dto/                    # request.py / response.py (API bodies), comms_context.py (read model)
├── services/                  # everything that does work — classes, no module-level functions
│   ├── base_service.py         # BaseService[T]: typed CRUD over the pool; guards column names
│   ├── *_service.py            # one per table (OnboardingService, VoiceCallService, ...)
│   ├── call_result_service.py  # a finished call: record, decide outcome, resume (poller + webhook)
│   ├── retry_service.py        # resume runs whose retry call is due
│   ├── gmail_intake_service.py # notification -> dedupe -> AE-sender gate -> run the graph
│   ├── gmail_watch_service.py  # register / renew Gmail push notifications
│   └── pubsub_pull_service.py  # local-dev Pub/Sub puller
├── api/
│   ├── routes/                 # health / webhook / onboarding / gmail / pubsub routers
│   ├── register_routes.py      # includes every router
│   ├── dependencies.py         # shared-secret checks (admin token, webhook token)
│   ├── constants.py            # header names
│   └── exceptions/             # AppException + handler -> {"error": {"code", "message"}}
├── state/                     # reducers.py + base_state.py (reusable TypedDict building blocks)
├── agents/
│   ├── commons/models.py       # NodeReport — shared by every agent
│   ├── deal_intake/            # root graph: state.py (OnboardingState), models.py, node.py, graph.py
│   ├── email_analyser/         # subgraph: gate in front of deal parsing (+ guardrails.py)
│   └── communication/          # subgraph: shared Slack channel (+ channel_plan.py)
├── config/                    # settings, database_config (pool), model_factory, checkpointer_config
├── scheduler/                 # the two timers (call poller, retry scheduler) — they only call services
├── gmail/                     # OAuth2 + watch()/history.list/messages.get
└── integrations/              # Bolna, Rocketlane and Slack API clients
```

**State classes** are `TypedDict`s. `OnboardingState` *inherits* `EmailAnalyserState` and
`CommunicationState` (which build on `state/base_state.py`), so a subgraph's keys flow back to the
parent graph and each key is declared exactly once.

**HTTP API** (operator routes need the `x-admin-token` header = `ADMIN_API_TOKEN`):

| Route | Purpose |
|---|---|
| `GET /health` | liveness |
| `GET /onboardings`, `GET /onboardings/{id}` | list (filter `?status=`) / read onboardings |
| `POST /onboardings/{id}/plan-tier` | CS manager sets the tier by hand |
| `POST /gmail/watch` | register / renew Gmail push notifications |
| `POST /webhooks/bolna?token=` | optional Bolna result webhook |
| `POST /pubsub/push?token=` | optional Pub/Sub push endpoint |

## Adding a new agent

Same convention as `rappit-ai-companion` / the other starters in this workspace: a new folder
under `src/agents/<name>/` with `state.py`, `models.py`, `node.py`, `graph.py`. See
`deal_intake/` as the reference, including how it pairs a side-effecting node with a
side-effect-free `interrupt()` node for any future step that needs a human/external
in-the-loop pause. `email_analyser/` and `communication/` are subgraphs added as nodes of the
deal-intake graph.

## Shared Slack channel (communication agent / "Agent 2")

Runs right after `create_rocketlane_project` (only if a project was actually created):
`prepare_channel → create_channel → configure_channel → invite_team → post_welcome →
invite_customer → finalize_channel` (`src/agents/communication/`).

- **Setup**: create a Slack app, add a bot token with scopes `channels:manage` (`groups:write` for
  private channels — the default, see `SLACK_CHANNEL_IS_PRIVATE`), `chat:write` and
  `conversations.connect:write`, then set `SLACK_BOT_TOKEN`. Slack Connect (the customer invite)
  needs a paid workspace with Slack Connect enabled. With no token the step is skipped.
- **Personalised by tier** (all from `app.plan_tiers` + the onboarding, never the LLM): channel
  name = `slack_channel_prefix` + customer slug (a taken name falls back to `…-<id6>`); topic = tier,
  customer, N-day plan, go-live date, dedicated CSM vs pooled CS lead; welcome message =
  `plan_tiers.welcome_message_template`, or a built-in default if blank/malformed.
- **Template placeholders**: `{customer_name} {contact_name} {tier_name} {duration_days}
  {start_date} {go_live_date} {csm_name} {csm_mention} {csm_intro} {manager_mention} {ae_mention}
  {phases} {project_url} {project_line}`. Unknown placeholders are left as written.
- **Who is added**: CSM, CS manager and AE if their `slack_user_id` is set; the customer contact
  is invited by email via Slack Connect.
- **Failures never fail the onboarding** (the Rocketlane project already exists): any problem —
  no channel, no CSM Slack ID, Slack Connect invite refused — sets `needs_human_review` with the
  reason, and a refused customer invite posts a "please invite manually" note in the channel.

**Not built yet** (from the discovery call): scheduling the kickoff call (the welcome message only
says the CSM will share options), a monitor that escalates tasks blocked for 3+ days (today only
Rocketlane's own overdue automation and the CS-manager mention in the welcome message), enforcing
that Data Migration is customer-verified before it can be closed (the welcome message sets that
expectation, nothing blocks the task), and the end-of-onboarding handoff summary.

## Assumptions to revisit against your real setup

- **Rocketlane fields**: `customer`, `owner`, and `sources[].templateId` are populated from
  verified API docs, but your Rocketlane workspace's exact template/customer/field conventions
  may need adjusting (`src/integrations/rocketlane_client.py`).
- **Voice AI provider**: Bolna, because it calls Indian numbers natively on a free trial (Vapi's
  free numbers can't call outside the US, and an Indian-capable Twilio number costs money). The
  provider sits behind `send_call()` + the webhook, so swapping it doesn't touch the graph or DB.
- **CSM/manager assignment**: `CsTeamMemberService.get_owner_and_manager` picks any active, matching
  row at random — replace with real round-robin/workload-based assignment if that matters.
- **Gmail body parsing**: uses `format=metadata` + `snippet`, not full MIME body parsing — fine
  for classification, not for anything needing the complete message text.
- **Retry policy**: up to `VOICE_MAX_ATTEMPTS` (3) calls, `VOICE_RETRY_DELAY_MINUTES` (30) apart,
  only on weekdays between `VOICE_CALL_WINDOW_START_HOUR`–`VOICE_CALL_WINDOW_END_HOUR` in the AE's
  timezone. After that the onboarding goes to `needs_human_review`.
- **Human fallback**: the tier hand-off to a human is still a log warning + audit event (the Slack
  agent only creates the customer channel, it doesn't DM the CS team), and the CS manager sets the
  tier with the manual endpoint below.

## AE phone confirmation (Bolna)

```
place_plan_tier_call ──► POST /call (agent_id, AE phone, user_data: customer/AE names)
                          Bolna agent: "Is Acme on the Enterprise plan or the Growth plan?"
                                       "Just to confirm, Acme is on Enterprise, correct?"  "Yes"
await_plan_tier_result (paused)

call-result poller, every BOLNA_POLL_SECONDS ──► GET /executions/{id} for each unfinished call
     still running   → voice_calls.status updated (ringing, in_progress…)
     finished        → extractions: plan_tier + readback_confirmed (+ confidence)
                       → finalise once → resume the paused graph
     stuck > VOICE_CALL_TIMEOUT_MINUTES → treated as failed → normal retry path
```

- **Polling, not webhooks, by default.** The app pulls results from Bolna's API instead of waiting
  for Bolna to push them, so it works behind a corporate firewall with no tunnel or public URL, and
  there is no unsigned inbound webhook to secure. The trade-off is up to `BOLNA_POLL_SECONDS` (20s)
  of extra latency, which doesn't matter here. The webhook (`POST /webhooks/bolna?token=…`) is still
  available for lower latency; both paths share `CallResultService.apply_finished_execution`, and a
  call is processed exactly once even if both see it.
- **The tier is never guessed.** It's only accepted from a `completed` call whose extractions say the
  AE confirmed the read-back (`readback_confirmed = yes`) with confidence ≥ `BOLNA_MIN_CONFIDENCE`.
  "Not sure", wrong person, voicemail, busy and no answer all lead to a retry.
- **Exactly-once processing.** `VoiceCallService.finalize_once` only returns the row the first time,
  and only a run in `waiting_for_call` is resumed.
- **No double dialing.** Placing the call and waiting for it are separate nodes, and each attempt is
  a unique `(onboarding_id, attempt_no)` row that's reused if the node is replayed.
- **Retries** are parked in `workflow_runs` (`status='waiting_for_retry'`, `next_retry_at`). The
  retry scheduler claims due rows with `FOR UPDATE SKIP LOCKED` and resumes them.

**Manual tier** (after retries are exhausted, or the AE has no phone on file):
```bash
curl -X POST http://localhost:8000/onboardings/<onboarding_id>/plan-tier \
  -H "x-admin-token: $ADMIN_API_TOKEN" -H "content-type: application/json" \
  -d '{"plan_tier": "growth", "reviewer": "priya@novacrm.com"}'
```

## Tests

```bash
pytest
```

Unit tests (`test_call_policy.py`, `test_bolna_helpers.py`, `test_state_shape`) need nothing.

`test_voice_flow_integration.py` runs the whole call flow end to end against a **real Postgres**
(app tables + checkpointer), sending Bolna-shaped webhooks through the real FastAPI app, with only
Gemini, Bolna and Rocketlane faked. It creates and drops its own temporary database on the server
from `TEST_POSTGRES_ADMIN_URI` (or `POSTGRES_URI`), and skips if no server is reachable:
```bash
TEST_POSTGRES_ADMIN_URI=postgresql://postgres:postgres@127.0.0.1:5432/postgres pytest
```
