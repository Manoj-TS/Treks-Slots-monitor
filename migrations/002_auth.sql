-- Sessions, email/reset tokens, and linked OAuth identities.
--
-- Sessions are server-side rather than Flask's signed cookie, for three
-- reasons: instant revocation (refunds, abuse, "log out everywhere" after a
-- password reset), a 30-day access product implying 30-day sessions (a leaked
-- signed cookie would then be a 30-day compromise with no recourse), and so
-- that rotating FLASK_SECRET_KEY doesn't log everybody out.
--
-- Only hashes are stored. A dump of these tables yields no usable credential.

CREATE TABLE IF NOT EXISTS sessions (
  token_hash   bytea       PRIMARY KEY,          -- sha256 of the cookie value
  user_id      bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token   text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  ip           inet,
  user_agent   text
);

CREATE INDEX IF NOT EXISTS sessions_user_idx    ON sessions (user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions (expires_at);

-- Single-use, expiring tokens for email verification and password reset.
CREATE TABLE IF NOT EXISTS auth_tokens (
  token_hash bytea       PRIMARY KEY,            -- sha256 of the emailed token
  user_id    bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  purpose    text        NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
  expires_at timestamptz NOT NULL,
  used_at    timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS auth_tokens_user_purpose_idx
  ON auth_tokens (user_id, purpose) WHERE used_at IS NULL;

-- Google (and later, others). Keyed on the provider's immutable subject id,
-- never on email — an account's email address can change.
CREATE TABLE IF NOT EXISTS oauth_identities (
  id            bigserial   PRIMARY KEY,
  user_id       bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider      text        NOT NULL CHECK (provider IN ('google')),
  subject       text        NOT NULL,
  email_at_link text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, subject)
);

CREATE INDEX IF NOT EXISTS oauth_identities_user_idx ON oauth_identities (user_id);

-- Repair: the owner row seeded by the previous release used access_until =
-- 'infinity'. Postgres accepts it, but psycopg cannot load it into a Python
-- datetime (max year 9999) and raises DataError on every read of that row.
-- is_admin already bypasses the paywall, so NULL is the correct value.
UPDATE users SET access_until = NULL WHERE access_until = 'infinity';
