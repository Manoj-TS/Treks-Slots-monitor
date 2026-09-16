-- Portal health: incidents, and the pages that caused them.
--
-- Detection itself is in memory (aranya/health.py) so it keeps working when
-- the database doesn't. These tables are the record: what went wrong and when,
-- whether anyone was told, and a copy of the page the scraper could not read —
-- the thing a human needs in order to fix a changed selector.

CREATE TABLE IF NOT EXISTS health_events (
  id          bigserial   PRIMARY KEY,
  kind        text        NOT NULL,
  started_at  timestamptz NOT NULL,
  alerted_at  timestamptz,
  ended_at    timestamptz,
  detail      text
);

CREATE INDEX IF NOT EXISTS health_events_started_idx ON health_events (started_at DESC);

-- Response bodies are third-party HTML. They are only ever served back as
-- text/plain under a sandbox CSP (admin_routes.health_sample), never rendered.
CREATE TABLE IF NOT EXISTS health_samples (
  id          bigserial   PRIMARY KEY,
  kind        text        NOT NULL,
  outcome     text        NOT NULL,
  http_status integer,
  url         text,
  trek_id     integer,
  cell_date   date,
  note        text,
  headers     jsonb,
  body        text,
  body_bytes  integer,
  captured_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS health_samples_captured_idx ON health_samples (captured_at DESC);
