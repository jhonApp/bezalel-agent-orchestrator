from __future__ import annotations

from wake_listener.activity import ActivityTracker


def test_new_tracker_is_idle_once_the_timeout_elapses():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])

    now[0] = 100.0

    assert tracker.is_idle(idle_timeout_seconds=50)
    assert not tracker.is_idle(idle_timeout_seconds=200)


def test_mark_active_resets_the_idle_clock():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    now[0] = 100.0
    tracker.mark_active()

    now[0] = 110.0

    assert not tracker.is_idle(idle_timeout_seconds=50)


def test_an_open_stream_prevents_idle_regardless_of_elapsed_time():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    tracker.stream_opened()

    now[0] = 10_000.0

    assert not tracker.is_idle(idle_timeout_seconds=1)
    assert tracker.open_stream_count == 1


def test_closing_the_last_stream_lets_the_idle_clock_run_again():
    now = [0.0]
    tracker = ActivityTracker(clock=lambda: now[0])
    tracker.stream_opened()
    now[0] = 10.0
    tracker.stream_closed()

    now[0] = 10_100.0

    assert tracker.is_idle(idle_timeout_seconds=50)
    assert tracker.open_stream_count == 0


def test_stream_closed_never_goes_negative_if_called_without_a_matching_open():
    tracker = ActivityTracker(clock=lambda: 0.0)

    tracker.stream_closed()

    assert tracker.open_stream_count == 0
