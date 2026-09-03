"""A shared token bucket for outbound requests to the government portal.

Without this, portal load scales with the number of paying customers, which is
exactly the wrong thing to have happen as the product succeeds. With it, load
is a constant you set (SWEEP_RPS) and adding customers just makes a full sweep
take longer, which is acceptable: bookings open at a publicly known fixed time,
so this is a board, not a race.
"""

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int):
        self.rate = max(0.1, float(rate_per_sec))
        self.burst = max(1, int(burst))
        self._tokens = float(self.burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self, timeout: float | None = None) -> bool:
        """Block until a token is free. Returns False if `timeout` elapsed."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                shortfall = (1.0 - self._tokens) / self.rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                shortfall = min(shortfall, remaining)
            time.sleep(min(max(shortfall, 0.01), 1.0))
