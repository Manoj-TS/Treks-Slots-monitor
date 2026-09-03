"""Startup sequencing: load persisted state, then launch the background threads."""

import threading

from . import config, discovery, storage, sweeper

_started = False


def start_background():
    """Load data + launch the discovery/polling threads. Safe to call once."""
    global _started
    if _started:
        return
    _started = True
    # A database failure here is logged, not fatal: the sweeper retries the
    # connection and reloads views on its own cycle, so a Postgres blip during
    # a deploy doesn't stop the process from coming up.
    storage.init_storage()
    storage.reload_views()
    threading.Thread(target=discovery.discovery_loop, daemon=True).start()
    threading.Thread(target=sweeper.supervised_worker, daemon=True).start()
