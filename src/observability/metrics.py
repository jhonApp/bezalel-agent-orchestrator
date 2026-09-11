from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator


class Metrics:
    def __init__(self):
        self.timings: dict[str, float] = {}
        self.counters: dict[str, int] = {}

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            self.timings[name] = self.timings.get(name, 0.0) + (time.perf_counter() - started)

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount
