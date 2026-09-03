"""Per-user board views, held in memory and refreshed from Postgres.

`board_state` stays global and shared: it caches *facts about the world*
(how many seats a trek has on a date), which are identical for everyone. Only
the selection is per user — which treks, in what order, over how many days.

Keeping the views in memory has a useful side effect: a database blip does not
take the board down. The sweeper and every connected viewer keep working from
the last known registry; only writes fail until Postgres returns.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config


@dataclass(frozen=True)
class Fav:
    trek_id: int
    name: str
    district_id: int


@dataclass(frozen=True)
class Watch:
    trek_id: int
    name: str
    district_id: int
    date: str            # ISO YYYY-MM-DD


@dataclass(frozen=True)
class UserView:
    user_id: int
    favourites: tuple = ()
    watch: tuple = ()
    window_days: int = config.WINDOW_DAYS_DEFAULT
    access_until: datetime | None = None
    is_admin: bool = False
    version: int = 0

    @property
    def has_access(self) -> bool:
        if self.is_admin:
            return True
        return (self.access_until is not None
                and self.access_until > datetime.now(timezone.utc))


_lock = threading.Lock()
_views: dict[int, UserView] = {}
_versions: dict[int, int] = {}
_loaded = False


def loaded() -> bool:
    return _loaded


def replace_all(rows: list[dict]) -> None:
    """Swap in a freshly loaded set of views. Per-user version counters are
    bumped only where something actually changed, so unchanged users keep
    their cached SSE payload."""
    global _loaded
    with _lock:
        new: dict[int, UserView] = {}
        for r in rows:
            uid = r["user_id"]
            prev = _views.get(uid)
            candidate = UserView(
                user_id=uid,
                favourites=tuple(Fav(**f) for f in r["favourites"]),
                watch=tuple(Watch(**w) for w in r["watch"]),
                window_days=r["window_days"],
                access_until=r["access_until"],
                is_admin=r["is_admin"],
                version=_versions.get(uid, 0),
            )
            if prev is None or _differs(prev, candidate):
                bumped = _versions.get(uid, 0) + 1
                _versions[uid] = bumped
                candidate = _replace_version(candidate, bumped)
            new[uid] = candidate
        _views.clear()
        _views.update(new)
        _loaded = True


def _differs(a: UserView, b: UserView) -> bool:
    return (a.favourites != b.favourites or a.watch != b.watch
            or a.window_days != b.window_days or a.access_until != b.access_until
            or a.is_admin != b.is_admin)


def _replace_version(v: UserView, version: int) -> UserView:
    return UserView(user_id=v.user_id, favourites=v.favourites, watch=v.watch,
                    window_days=v.window_days, access_until=v.access_until,
                    is_admin=v.is_admin, version=version)


def put(view_row: dict) -> None:
    """Insert or update one user's view (after that user edits something)."""
    uid = view_row["user_id"]
    with _lock:
        bumped = _versions.get(uid, 0) + 1
        _versions[uid] = bumped
        _views[uid] = UserView(
            user_id=uid,
            favourites=tuple(Fav(**f) for f in view_row["favourites"]),
            watch=tuple(Watch(**w) for w in view_row["watch"]),
            window_days=view_row["window_days"],
            access_until=view_row["access_until"],
            is_admin=view_row["is_admin"],
            version=bumped,
        )


def get(user_id: int) -> UserView | None:
    """In-memory read. Never touches the database — safe to call from the SSE
    loop, which must never hold a pooled connection."""
    with _lock:
        return _views.get(user_id)


def get_or_empty(user_id: int) -> UserView:
    return get(user_id) or UserView(user_id=user_id)


def active() -> list[UserView]:
    """Views worth sweeping: users whose access is current. A lapsed customer's
    treks drop out of the sweep, so portal load scales with paying users."""
    with _lock:
        return [v for v in _views.values() if v.has_access]


def all_views() -> list[UserView]:
    with _lock:
        return list(_views.values())
