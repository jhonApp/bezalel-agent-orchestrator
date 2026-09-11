from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any


class LiveEventBroker:
    """In-process fan-out for the administration UI's SSE clients."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, event: dict[str, Any]) -> None:
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.put_nowait(event)
            except asyncio.QueueFull:
                # A slow browser can recover from the next dashboard refresh.
                pass

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)
