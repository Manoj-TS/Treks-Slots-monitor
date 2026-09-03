"""Startup sequencing: load persisted state, then launch the background threads."""

import threading

from . import discovery, storage, sweeper

_started = False


def start_background():
    """Load data + launch the discovery/polling threads. Safe to call once."""
    global _started
    if _started:
        return
    _started = True
    storage.load_all_from_disk()
    threading.Thread(target=discovery.discovery_loop, daemon=True).start()
    threading.Thread(target=sweeper.supervised_worker, daemon=True).start()
