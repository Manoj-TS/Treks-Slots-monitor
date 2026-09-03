"""Postgres access: a small connection pool and a migration runner.

No ORM on purpose — this is a handful of tables and hand-written queries, and
SQLAlchemy + Alembic would be a larger surface than the data layer itself.

Hard rule: never hold a pooled connection inside the SSE generator. Those
connections live for the length of a viewer's session; with a small pool, a
single leaked connection per stream would deadlock the whole app. Everything
the stream needs is already in memory.
"""

import pathlib
import threading

from psycopg_pool import ConnectionPool

from . import config

# Arbitrary but fixed: two app instances racing to migrate must pick the same key.
_MIGRATION_LOCK_KEY = 8_244_113_097_001

_pool: ConnectionPool | None = None
_lock = threading.Lock()

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def init(dsn: str | None = None, timeout: float = 10.0) -> None:
    """Open the pool and prove the database is actually reachable.

    Raises if it is not — callers decide whether that is fatal (it is not; see
    storage.init_storage, which falls back to the legacy JSON files).
    """
    global _pool
    dsn = dsn or config.DATABASE_URL
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    with _lock:
        if _pool is not None:
            return
        pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=10,
                              timeout=5.0, open=False)
        pool.open()
        try:
            # Raises PoolTimeout if no connection can be established.
            pool.wait(timeout=timeout)
        except Exception:
            pool.close()
            raise
        _pool = pool


def close() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def connection():
    """Context manager yielding a connection. Commits on clean exit, rolls back
    on exception (psycopg3 pool semantics)."""
    if _pool is None:
        raise RuntimeError("db.init() has not been called")
    return _pool.connection()


def migrate() -> list[str]:
    """Apply any migrations/*.sql not yet recorded. Returns the ones applied.

    Held under a transaction-scoped advisory lock so a second process (there
    should not be one — see the Dockerfile's single-process note) cannot apply
    the same migration concurrently. The lock releases automatically with the
    transaction, so a crash mid-migration cannot leave it stuck.
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied: list[str] = []
    with connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for path in files:
            if path.name in done:
                continue
            # No parameters here, so psycopg uses the simple query protocol and
            # the file may contain multiple statements.
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.name,))
            applied.append(path.name)
            print(f"[DB] applied migration {path.name}")
    return applied
