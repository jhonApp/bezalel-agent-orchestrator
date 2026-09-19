from __future__ import annotations

import asyncio

import pytest

from observability.live_events import LiveEventBroker


async def _next(events, timeout: float = 2.0):
    return await asyncio.wait_for(anext(events), timeout=timeout)


@pytest.mark.asyncio
async def test_a_new_subscriber_immediately_receives_recently_published_events() -> None:
    """A browser tab opened or refreshed mid-execution must not see a blank activity feed —
    /events has no history otherwise, so a late/reconnecting subscriber would miss the
    agent.started + every agent.stream line published before it connected."""
    broker = LiveEventBroker()
    await broker.publish({"type": "agent.started", "agent": "frontend", "task_id": "T001"})
    await broker.publish({"type": "agent.stream", "agent": "frontend", "raw": "inspecting repository"})

    events = broker.subscribe()
    first = await _next(events)
    second = await _next(events)

    assert first == {"type": "agent.started", "agent": "frontend", "task_id": "T001"}
    assert second == {"type": "agent.stream", "agent": "frontend", "raw": "inspecting repository"}


@pytest.mark.asyncio
async def test_replay_buffer_is_capped_and_drops_the_oldest_events() -> None:
    broker = LiveEventBroker(recent_buffer_size=3)
    for i in range(5):
        await broker.publish({"type": "agent.stream", "raw": "line " + str(i)})

    events = broker.subscribe()
    replayed = [await _next(events) for _ in range(3)]

    assert [e["raw"] for e in replayed] == ["line 2", "line 3", "line 4"]


@pytest.mark.asyncio
async def test_subscriber_still_receives_events_published_after_it_connects() -> None:
    broker = LiveEventBroker()
    events = broker.subscribe()
    receive_task = asyncio.create_task(_next(events))
    await asyncio.sleep(0)  # let the async generator start running and register its queue
    await broker.publish({"type": "agent.stream", "raw": "after connect"})

    received = await receive_task

    assert received == {"type": "agent.stream", "raw": "after connect"}
