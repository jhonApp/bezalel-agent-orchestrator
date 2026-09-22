from __future__ import annotations

import time
from typing import Callable


class ActivityTracker:
    """Tracks when the backend was last used, so the idle loop knows when it is
    safe to stop it. An open SSE stream always counts as active for its whole
    duration — the idle timer must never fire while a client is still watching
    live events, even if no new bytes have been sent in a while.
    """

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._last_activity = self._clock()
        self._open_streams = 0

    def mark_active(self) -> None:
        self._last_activity = self._clock()

    def stream_opened(self) -> None:
        self._open_streams += 1
        self.mark_active()

    def stream_closed(self) -> None:
        self._open_streams = max(0, self._open_streams - 1)
        self.mark_active()

    def idle_seconds(self) -> float:
        return self._clock() - self._last_activity

    def is_idle(self, idle_timeout_seconds: float) -> bool:
        return self._open_streams == 0 and self.idle_seconds() >= idle_timeout_seconds

    @property
    def open_stream_count(self) -> int:
        return self._open_streams
