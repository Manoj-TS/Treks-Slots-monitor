-- Payment ledger.
--
-- users.access_until stays the authoritative answer to "can this account see
-- the board" — it is read on every SSE wakeup, so it must be a field read, not
-- an aggregate. This table is the immutable audit trail behind it: every row
-- records access_before/access_after, so access_until is fully reconstructable.
--
-- `product` exists from the start so a season pass, or a manual grant, is a new
-- value rather than a migration written under deadline pressure.
-- `applied_at` is the idempotency latch: granting is a conditional UPDATE on
-- `applied_at IS NULL`, so a duplicate webhook cannot grant twice.

CREATE TABLE IF NOT EXISTS payments (
  id                  bigserial   PRIMARY KEY,
  user_id             bigint      NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  product             text        NOT NULL DEFAULT 'access_30d',
  razorpay_order_id   text        NOT NULL UNIQUE,
  razorpay_payment_id text        UNIQUE,
  amount_paise        integer     NOT NULL,
  currency            text        NOT NULL DEFAULT 'INR',
  status              text        NOT NULL DEFAULT 'created'
                      CHECK (status IN ('created', 'paid', 'failed', 'refunded')),
  days_granted        integer     NOT NULL DEFAULT 30,
  access_before       timestamptz,
  access_after        timestamptz,
  applied_at          timestamptz,
  notes               jsonb       NOT NULL DEFAULT '{}',
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payments_user_idx    ON payments (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS payments_pending_idx ON payments (created_at) WHERE status = 'created';

-- Razorpay delivers webhooks at-least-once and can retry out of order. Rows
-- here dedupe on the provider's event id before any money logic runs.
CREATE TABLE IF NOT EXISTS webhook_events (
  id           bigserial   PRIMARY KEY,
  event_id     text        NOT NULL UNIQUE,
  event_type   text        NOT NULL,
  payload      jsonb       NOT NULL,
  received_at  timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  error        text
);
