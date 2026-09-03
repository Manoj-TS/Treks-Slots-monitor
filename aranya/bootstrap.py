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
    # Connect + migrate before loading. A database failure is logged and
    # degrades to the JSON files rather than preventing startup — the board is
    # public data and must keep serving.
    storage.init_storage()
    storage.load_all(config.OWNER_USER_ID)
    threading.Thread(target=discovery.discovery_loop, daemon=True).start()
    threading.Thread(target=sweeper.supervised_worker, daemon=True).start()
