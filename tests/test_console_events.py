from __future__ import annotations

from orchestrator.main import format_console_event


def test_formats_agent_started() -> None:
    line = format_console_event({
        "type": "agent.started", "agent": "frontend", "task_id": "T001",
        "description": "Implement the button",
    })
    assert line == ">> frontend T001 started: Implement the button"


def test_formats_agent_finished() -> None:
    line = format_console_event({
        "type": "agent.finished", "agent": "frontend", "task_id": "T001", "status": "completed",
    })
    assert line == "<< frontend T001 finished: completed"


def test_formats_agent_stream_line_with_raw_text() -> None:
    line = format_console_event({
        "type": "agent.stream", "agent": "frontend", "task_id": "T001",
        "stream": "stdout", "parsed": None, "raw": "inspecting repository",
    })
    assert line == "   [frontend] inspecting repository"


def test_agent_stream_with_blank_raw_is_skipped() -> None:
    line = format_console_event({
        "type": "agent.stream", "agent": "frontend", "task_id": "T001",
        "stream": "stdout", "parsed": None, "raw": "   ",
    })
    assert line is None


def test_formats_state_updated_node_transition() -> None:
    line = format_console_event({
        "type": "state.updated", "node": "create_plan", "event": "plan.created",
    })
    assert line == "-- create_plan: plan.created"


def test_state_updated_without_event_name_is_skipped() -> None:
    line = format_console_event({"type": "state.updated", "node": "create_plan", "event": None})
    assert line is None


def test_unknown_event_type_is_skipped() -> None:
    assert format_console_event({"type": "something.else"}) is None
