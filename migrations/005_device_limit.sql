-- Forced sign-outs: the device limit, a password reset, "sign out other
-- devices", or an admin.
--
-- Two jobs. A device whose session was ended can be told *why* ("your account
-- was signed in on a Pixel 7 at 10:42") rather than dropped at a bare login
-- form, which matters because the usual reason is someone else using the
-- account. And the device-limit rows, counted per user, are how /admin spots an
-- account being shared.
--
-- A voluntary logout is not recorded: that device deleted its own cookie.

CREATE TABLE IF NOT EXISTS session_revocations (
  id           bigserial   PRIMARY KEY,
  token_hash   bytea       NOT NULL,             -- the ended session's hash
  user_id      bigint      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reason       text        NOT NULL
               CHECK (reason IN ('device_limit', 'password_reset',
                                 'signed_out_remotely', 'admin')),
  revoked_at   timestamptz NOT NULL DEFAULT now(),
  ended_device text,                             -- label of the device signed out
  by_device    text,                             -- label of the device that caused it
  by_ip        inet
);

CREATE INDEX IF NOT EXISTS session_revocations_token_idx
  ON session_revocations (token_hash);
CREATE INDEX IF NOT EXISTS session_revocations_user_idx
  ON session_revocations (user_id, revoked_at DESC);
