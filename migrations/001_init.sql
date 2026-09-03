-- Accounts + per-user board preferences.
--
-- Single-tenant today: every row belongs to the seeded owner (users.id = 1).
-- The account columns (password_hash, access_until, is_admin, ...) exist from
-- the start so that going multi-tenant is a data change, not a schema change.
--
-- Deliberately NOT using citext for email: it needs CREATE EXTENSION, which is
-- one more thing that can fail on a shared database. A unique index on
-- lower(email) gives the same case-insensitive guarantee with no extension.

CREATE TABLE IF NOT EXISTS users (
  id             bigserial   PRIMARY KEY,
  email          text        NOT NULL,
  email_verified boolean     NOT NULL DEFAULT false,
  name           text,
  password_hash  text,                      -- NULL = no password (OAuth-only later)
  access_until   timestamptz,               -- NULL = never paid; the paywall check
  is_admin       boolean     NOT NULL DEFAULT false,
  status         text        NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'disabled')),
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_login_at  timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx ON users (lower(email));

-- The board rows. `position` preserves the user's manual ordering.
-- district_name is deliberately absent: it is derived from district_id at
-- render time (config.district_name), so it can't drift.
CREATE TABLE IF NOT EXISTS user_favourites (
  user_id     bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  trek_id     integer     NOT NULL,
  name        text        NOT NULL,
  district_id integer     NOT NULL,
  position    integer     NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, trek_id)
);

CREATE INDEX IF NOT EXISTS user_favourites_order_idx ON user_favourites (user_id, position);

-- Pinned trek+date combinations outside the weekend columns.
CREATE TABLE IF NOT EXISTS user_watch (
  user_id     bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  trek_id     integer     NOT NULL,
  watch_date  date        NOT NULL,
  name        text        NOT NULL,
  district_id integer     NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, trek_id, watch_date)
);

CREATE INDEX IF NOT EXISTS user_watch_date_idx ON user_watch (watch_date);

-- Per-user view settings. Sweep cadence is NOT here: it is global and belongs
-- in app_settings, because one user must not be able to set the poll rate
-- against the government portal on everyone else's behalf.
CREATE TABLE IF NOT EXISTS user_settings (
  user_id     bigint      PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  window_days integer     NOT NULL DEFAULT 30 CHECK (window_days BETWEEN 1 AND 60),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Operator-managed trek catalog (the source of district_id, without which a
-- trek cannot be swept). Global, not per-user.
CREATE TABLE IF NOT EXISTS trek_configs (
  trek_id             integer     PRIMARY KEY,
  name                text        NOT NULL,
  district_id         integer     NOT NULL,
  timeslot_mapping_id integer,
  timeslot_id         integer,
  updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS app_settings (
  key        text        PRIMARY KEY,
  value      jsonb       NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
