from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from typing import Any


class LiveEventBroker:
    """In-process fan-out for the administration UI's SSE clients.

    `/events` has no history of its own — a browser tab opened or refreshed while an agent is
    mid-run would otherwise see nothing until the next event happens to fire, even though that
    agent's card in Manage Agents is very much alive. Keeping a small buffer of recently
    published events and replaying it to each new subscriber closes that gap.
    """

    def __init__(self, recent_buffer_size: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._recent: deque[dict[str, Any]] = deque(maxlen=recent_buffer_size)

    async def publish(self, event: dict[str, Any]) -> None:
        self._recent.append(event)
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.put_nowait(event)
            except asyncio.QueueFull:
                # A slow browser can recover from the next dashboard refresh.
                pass

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        # The replay buffer can hold more than the subscriber queue's own capacity — keep only
        # the most recent ones that actually fit, rather than raising QueueFull mid-replay.
        for event in list(self._recent)[-queue.maxsize:]:
            queue.put_nowait(event)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
