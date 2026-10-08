-- Customer Onboarding Automation — application schema.
-- Run once against a fresh database: psql <dsn> -f db/schema.sql
-- (LangGraph's own checkpoint/writes/blobs tables are created separately,
-- by AsyncPostgresSaver.setup() — see src/config/checkpointer_config.py.)

CREATE EXTENSION IF NOT EXISTS pgcrypto; -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS app;

-- 1. mailboxes: the CS team inbox being monitored
CREATE TABLE app.mailboxes (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email             TEXT NOT NULL UNIQUE,
  history_id        BIGINT,               -- Gmail sync cursor
  watch_expiration  TIMESTAMPTZ,
  last_synced_at    TIMESTAMPTZ,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. inbound_emails: every email the intake agent looked at
CREATE TABLE app.inbound_emails (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  mailbox_id        UUID NOT NULL REFERENCES app.mailboxes(id),
  gmail_message_id  TEXT NOT NULL,
  gmail_thread_id   TEXT,
  from_address      TEXT,
  subject           TEXT,
  received_at       TIMESTAMPTZ,
  body_redacted     TEXT,                 -- PII-redacted body (the raw body is never stored)
  is_deal_email     BOOLEAN,
  kind              TEXT CHECK (kind IN ('deal_closed','no_deal','escalation_reply','malicious')),
  kind_source       TEXT CHECK (kind_source IN ('thread_match','token','llm','rule')),
  classification    JSONB,                -- model, confidence, reason
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','ignored','parsed','failed')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (mailbox_id, gmail_message_id)
);

-- 3. account_executives: who to call
CREATE TABLE app.account_executives (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name           TEXT NOT NULL,
  email          TEXT NOT NULL UNIQUE,     -- matched to the email sender
  phone_e164     TEXT NOT NULL,            -- e.g. +14155550123
  timezone       TEXT NOT NULL DEFAULT 'UTC',
  slack_user_id  TEXT,
  active         BOOLEAN NOT NULL DEFAULT true
);

-- 4. plan_tiers: configuration that drives every per-tier decision
CREATE TABLE app.plan_tiers (
  code                     TEXT PRIMARY KEY CHECK (code IN ('enterprise','growth')),
  display_name             TEXT NOT NULL,
  rocketlane_template_id   TEXT NOT NULL,
  duration_days            INT  NOT NULL,     -- 30 / 14
  csm_model                TEXT NOT NULL CHECK (csm_model IN ('dedicated','pooled')),
  slack_channel_prefix     TEXT NOT NULL,     -- e.g. 'ent-' / 'growth-'
  welcome_message_template TEXT NOT NULL,
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. cs_team_members: who owns and manages each project
CREATE TABLE app.cs_team_members (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                TEXT NOT NULL,
  email               TEXT NOT NULL UNIQUE,
  role                TEXT NOT NULL CHECK (role IN ('csm','cs_manager')),
  pool                TEXT CHECK (pool IN ('enterprise','growth')),
  rocketlane_user_id  TEXT NOT NULL,
  slack_user_id       TEXT,
  active              BOOLEAN NOT NULL DEFAULT true
);

-- 6. onboardings: the central record (one per closed deal)
CREATE TABLE app.onboardings (
  id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  inbound_email_id        UUID NOT NULL UNIQUE REFERENCES app.inbound_emails(id),
  customer_name           TEXT NOT NULL,
  customer_contact_name   TEXT,
  customer_contact_email  TEXT NOT NULL,
  customer_domain         TEXT,
  ae_id                   UUID REFERENCES app.account_executives(id),
  sf_opportunity_url      TEXT,
  sf_opportunity_id       TEXT UNIQUE,          -- business-level deduplication
  extra_details           JSONB,                -- other parsed fields
  plan_tier               TEXT REFERENCES app.plan_tiers(code),   -- NULL until confirmed
  plan_tier_source        TEXT CHECK (plan_tier_source IN ('voice_call','manual_override')),
  plan_confirmed_at       TIMESTAMPTZ,
  csm_id                  UUID REFERENCES app.cs_team_members(id),     -- Project Owner
  manager_id              UUID REFERENCES app.cs_team_members(id),     -- Project Manager
  start_date              DATE,
  target_go_live_date     DATE,
  status                  TEXT NOT NULL DEFAULT 'received' CHECK (status IN (
                            'received','awaiting_tier_confirmation','tier_confirmed',
                            'project_created','channel_created','completed',
                            'needs_human_review','failed')),
  status_reason           TEXT,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON app.onboardings (status);

-- 7. voice_calls: every call attempt to the AE
CREATE TABLE app.voice_calls (
  id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  onboarding_id        UUID NOT NULL REFERENCES app.onboardings(id),
  attempt_no           INT  NOT NULL,
  provider             TEXT NOT NULL,           -- bolna / vapi / retell ...
  provider_call_id     TEXT UNIQUE,             -- links the webhook back to this row
  to_phone             TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                         ('queued','ringing','in_progress','completed','no_answer','busy','failed')),
  outcome              TEXT CONSTRAINT voice_calls_outcome_check CHECK (outcome IN
                         ('confirmed','unclear','no_answer','voicemail','failed','declined')),
  readback_confirmed   BOOLEAN NOT NULL DEFAULT false,   -- AE said "yes" to the read-back
  ended_reason         TEXT,                    -- provider status / hangup reason
  notes                TEXT,                    -- assistant's note (e.g. why tier unknown)
  detected_plan_tier   TEXT REFERENCES app.plan_tiers(code),
  confidence           NUMERIC(4,3),
  transcript           TEXT,
  recording_url        TEXT,
  duration_seconds     INT,
  raw_webhook          JSONB,
  started_at           TIMESTAMPTZ,
  ended_at             TIMESTAMPTZ,
  created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (onboarding_id, attempt_no)
);

-- 8. rocketlane_projects: link to the created project
CREATE TABLE app.rocketlane_projects (
  id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  onboarding_id          UUID NOT NULL UNIQUE REFERENCES app.onboardings(id),
  rocketlane_project_id  TEXT UNIQUE,
  template_id            TEXT NOT NULL,
  project_url            TEXT,
  owner_user_id          TEXT,                 -- Rocketlane user IDs actually set
  manager_user_id        TEXT,
  start_date             DATE,
  due_date               DATE,
  status                 TEXT NOT NULL DEFAULT 'creating'
                         CHECK (status IN ('creating','created','failed')),
  request_payload        JSONB,
  error                  TEXT,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 9. slack_channels: the shared customer channel
-- Written by the communication agent (src/agents/communication).
CREATE TABLE app.slack_channels (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  onboarding_id       UUID NOT NULL UNIQUE REFERENCES app.onboardings(id),
  slack_channel_id    TEXT UNIQUE,
  channel_name        TEXT NOT NULL,
  topic               TEXT,
  shared_invite_id    TEXT,                    -- Slack Connect invite
  invite_status       TEXT CHECK (invite_status IN ('sent','accepted','failed')),
  welcome_message_ts  TEXT,                    -- the welcome message is posted only once
  status              TEXT NOT NULL DEFAULT 'creating'
                      CHECK (status IN ('creating','created','failed')),
  error               TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 10. workflow_runs: one row per LangGraph workflow run — the link between the outside world
-- (webhooks, replies, timers) and a paused graph. The graph's own position/state lives in the
-- LangGraph checkpointer under `thread_id`; everything an outside event needs in order to FIND a
-- paused run (status, what it waits for, deadlines) is a column here.
--   intake             one per inbound email:  classify -> (deal) create the onboarding
--   tier_confirmation  one per onboarding:     call the AE, email the AE, wait for the tier
--   provisioning       one per onboarding:     create the Rocketlane project, then the Slack channel
CREATE TABLE app.workflow_runs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow          TEXT NOT NULL CHECK (workflow IN ('intake','tier_confirmation','provisioning')),
  thread_id         TEXT NOT NULL UNIQUE,      -- 'intake:<inbound_email_id>' | 'tier:<onboarding_id>' | 'provision:<onboarding_id>'
  inbound_email_id  UUID REFERENCES app.inbound_emails(id),   -- intake runs
  onboarding_id     UUID REFERENCES app.onboardings(id),      -- NULL for an intake run until a deal exists
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','waiting','stopped','succeeded','failed')),
  waiting_for       TEXT CHECK (waiting_for IN ('call_result','call_retry','email_reply')),
  current_node      TEXT,                      -- where it is parked / where it failed (resume point)
  due_at            TIMESTAMPTZ,               -- next timer: place the retry call / auto-retry a failure
  stop_at           TIMESTAMPTZ,               -- give up waiting for the AE's email reply
  outcome           TEXT,                      -- 'deal_closed' | 'no_deal' | 'tier:enterprise' | 'project+channel' ...
  attempt_count     INT NOT NULL DEFAULT 0,    -- automatic retries used (provisioning)
  last_error        TEXT,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ,
  UNIQUE (onboarding_id, workflow),
  CONSTRAINT workflow_runs_waiting_check CHECK ((status = 'waiting') = (waiting_for IS NOT NULL))
);
CREATE INDEX ON app.workflow_runs (status, due_at);
CREATE INDEX ON app.workflow_runs (waiting_for, stop_at) WHERE status = 'waiting';

-- 11. audit_events: a timeline of everything that happened
CREATE TABLE app.audit_events (
  id             BIGSERIAL PRIMARY KEY,
  onboarding_id  UUID REFERENCES app.onboardings(id),
  actor          TEXT NOT NULL,      -- intake_agent / communication_agent / webhook / human:<email>
  event_type     TEXT NOT NULL,      -- email_parsed, call_started, tier_confirmed, project_created ...
  payload        JSONB,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON app.audit_events (onboarding_id, created_at);

-- 12. escalations: one row per "please tell us the plan tier" email sent to the AE when the call
-- couldn't confirm it. Replies are matched back by the Gmail thread of the email we SENT (or the
-- token in its subject). A re-opened request adds a row (attempt_no + 1) and supersedes the old one.
CREATE TABLE app.escalations (
  id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  onboarding_id              UUID NOT NULL REFERENCES app.onboardings(id),
  workflow_run_id            UUID NOT NULL REFERENCES app.workflow_runs(id),   -- the tier run it unblocks
  attempt_no                 INT  NOT NULL DEFAULT 1,
  token                      TEXT NOT NULL,       -- 'ONB-ab12cd34' (same for every attempt of an onboarding)
  to_email                   TEXT NOT NULL,       -- the AE — the only person whose reply is accepted
  gmail_message_id           TEXT,                -- of the email we sent
  gmail_thread_id            TEXT,
  status                     TEXT NOT NULL DEFAULT 'sent'
                             CHECK (status IN ('sent','answered','superseded','failed')),
  sent_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
  answered_tier              TEXT CHECK (answered_tier IN ('enterprise','growth')),
  answered_by                TEXT,
  answered_inbound_email_id  UUID REFERENCES app.inbound_emails(id),
  answered_at                TIMESTAMPTZ,
  UNIQUE (onboarding_id, attempt_no)
);
CREATE INDEX ON app.escalations (gmail_thread_id);
CREATE INDEX ON app.escalations (token);

-- inbound_emails -> escalations (defined after both tables exist)
ALTER TABLE app.inbound_emails ADD COLUMN escalation_id UUID REFERENCES app.escalations(id);
