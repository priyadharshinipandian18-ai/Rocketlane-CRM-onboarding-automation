-- Schema changes for the three-workflow split (intake / tier_confirmation / provisioning).
--
-- Run ONCE against an existing development database:
--     psql <dsn> -f db/schema_changes_workflow_split.sql
-- Fresh databases don't need this: db/schema.sql already contains everything below.
--
-- THIS DISCARDS IN-FLIGHT RUN STATE (workflow_runs, tier_requests) — it is meant for test data.
-- onboardings, voice_calls, rocketlane_projects, slack_channels and audit_events are kept, but any
-- onboarding that was mid-flight has no run any more and will not resume.

BEGIN;

-- 1. inbound_emails: the redacted body, and how each email was classified
ALTER TABLE app.inbound_emails
  ADD COLUMN IF NOT EXISTS body_redacted TEXT,
  ADD COLUMN IF NOT EXISTS kind TEXT
    CHECK (kind IN ('deal_closed','no_deal','escalation_reply','malicious')),
  ADD COLUMN IF NOT EXISTS kind_source TEXT
    CHECK (kind_source IN ('thread_match','token','llm','rule'));

-- 2. replace the old single-thread run table and the old tier_requests table
--    (the escalations bits are dropped first so the script is also safe to run a second time)
ALTER TABLE app.inbound_emails DROP COLUMN IF EXISTS escalation_id;
DROP TABLE IF EXISTS app.escalations;
DROP TABLE IF EXISTS app.tier_requests;
DROP TABLE IF EXISTS app.workflow_runs CASCADE;

CREATE TABLE app.workflow_runs (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow          TEXT NOT NULL CHECK (workflow IN ('intake','tier_confirmation','provisioning')),
  thread_id         TEXT NOT NULL UNIQUE,
  inbound_email_id  UUID REFERENCES app.inbound_emails(id),
  onboarding_id     UUID REFERENCES app.onboardings(id),
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','waiting','stopped','succeeded','failed')),
  waiting_for       TEXT CHECK (waiting_for IN ('call_result','call_retry','email_reply')),
  current_node      TEXT,
  due_at            TIMESTAMPTZ,
  stop_at           TIMESTAMPTZ,
  outcome           TEXT,
  attempt_count     INT NOT NULL DEFAULT 0,
  last_error        TEXT,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at       TIMESTAMPTZ,
  UNIQUE (onboarding_id, workflow),
  CONSTRAINT workflow_runs_waiting_check CHECK ((status = 'waiting') = (waiting_for IS NOT NULL))
);
CREATE INDEX ON app.workflow_runs (status, due_at);
CREATE INDEX ON app.workflow_runs (waiting_for, stop_at) WHERE status = 'waiting';

CREATE TABLE app.escalations (
  id                         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  onboarding_id              UUID NOT NULL REFERENCES app.onboardings(id),
  workflow_run_id            UUID NOT NULL REFERENCES app.workflow_runs(id),
  attempt_no                 INT  NOT NULL DEFAULT 1,
  token                      TEXT NOT NULL,
  to_email                   TEXT NOT NULL,
  gmail_message_id           TEXT,
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

ALTER TABLE app.inbound_emails
  ADD COLUMN IF NOT EXISTS escalation_id UUID REFERENCES app.escalations(id);

COMMIT;

-- Optional clean-up of the LangGraph checkpoints of the old single-thread runs ('onboarding-<id>'),
-- which nothing refers to any more. Harmless to leave; uncomment to clear them:
-- TRUNCATE checkpoint_writes, checkpoint_blobs, checkpoints;
