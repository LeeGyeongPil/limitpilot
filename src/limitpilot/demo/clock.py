"""Virtual clock for the practice tour: 15 minutes pass between ticks in no time.

The decision code reads ``time.time()`` for cooldowns, freshness and reset countdowns.
The tour swaps that one function for a clock it can move forward, so the real code sees
the same timeline it would see over an hour of real use.
"""

import time


class VirtualClock:
    def __init__(self, start: float):
        self.now = float(start)
        self._real_time = None

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def install(self) -> None:
        if self._real_time is None:
            self._real_time = time.time
            time.time = self.time

    def restore(self) -> None:
        if self._real_time is not None:
            time.time = self._real_time
            self._real_time = None

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, *exc):
        self.restore()
        return False
