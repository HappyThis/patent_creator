from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import ApiError
from app.services import AppServices

from helpers import ScriptedLLMClient, make_settings


def test_workspace_store_rejects_path_traversal_storage_ids(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id

    for action, expected_code in (
        (lambda: services.store.get_project("../outside"), "invalid_project_id"),
        (lambda: services.store.session_exists(project_id, "../outside"), "invalid_session_id"),
        (lambda: services.store.figure_json_file(project_id, "../outside"), "invalid_figure_id"),
    ):
        with pytest.raises(ApiError) as exc_info:
            action()
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == expected_code


def test_append_session_event_uses_last_event_seq_with_trailing_blank_lines(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_seq"
    first = services.store.append_session_event(
        project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id="round_1",
        message_id="msg_1",
        payload={"text": "第一条"},
    )
    assert first.seq == 1
    services.store.session_file(project_id, session_id).write_text(
        services.store.session_file(project_id, session_id).read_text(encoding="utf-8") + "\n\n",
        encoding="utf-8",
    )

    second = services.store.append_session_event(
        project_id,
        session_id,
        event_type="agent_output",
        scope="main",
        round_id="round_1",
        message_id="msg_1",
        payload={"text": "第二条"},
    )

    assert second.seq == 2


def test_read_session_events_ignores_removed_parent_call_id_field(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_legacy_parent"
    services.store.session_file(project_id, session_id).write_text(
        json.dumps(
            {
                "id": "evt_legacy",
                "ts": "2026-06-19T00:00:00Z",
                "type": "tool_call",
                "seq": 1,
                "scope": "main",
                "round_id": "round_1",
                "message_id": "msg_1",
                "call_id": "call_1",
                "parent_call_id": "call_parent",
                "payload": {"tool": "file_read", "arguments": {}},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    event = services.store.read_session_events(project_id, session_id)[0]

    assert event.call_id == "call_1"
    assert "parent_call_id" not in event.model_dump()


def test_recover_interrupted_project_marks_round_failed_and_unlocks_project(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_interrupted"
    round_id = "round_interrupted"
    message_id = "msg_interrupted"
    services.store.append_session_event(
        project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id=round_id,
        message_id=message_id,
        payload={"text": "开始长任务"},
    )
    project = services.store.get_project(project_id)
    project.active_session_id = session_id
    project.running_session_id = session_id
    project.running_round_id = round_id
    project.is_busy = True
    services.store.save_project(project)

    recovered = services.store.recover_interrupted_projects()

    assert [item.project_id for item in recovered] == [project_id]
    unlocked = services.store.get_project(project_id)
    assert unlocked.is_busy is False
    assert unlocked.running_session_id is None
    assert unlocked.running_round_id is None

    events = services.store.read_session_events(project_id, session_id)
    failure_event = events[-1]
    assert failure_event.type == "agent_output"
    assert failure_event.round_id == round_id
    assert failure_event.message_id == message_id
    assert failure_event.payload["code"] == "round_interrupted_by_restart"
    assert "后端重启" in failure_event.payload["text"]

    recovered_again = services.store.recover_interrupted_projects()
    assert recovered_again == []
    assert len(services.store.read_session_events(project_id, session_id)) == len(events)

def test_recover_interrupted_project_does_not_infer_missing_running_round(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_stale"
    services.store.append_session_event(
        project_id,
        session_id,
        event_type="user_input",
        scope="main",
        round_id="round_stale",
        message_id="msg_stale",
        payload={"text": "开始长任务"},
    )
    project = services.store.get_project(project_id)
    project.active_session_id = session_id
    project.running_session_id = None
    project.running_round_id = None
    project.is_busy = True
    services.store.save_project(project)

    recovered = services.store.recover_interrupted_projects()

    assert [item.project_id for item in recovered] == [project_id]
    unlocked = services.store.get_project(project_id)
    assert unlocked.is_busy is False
    assert unlocked.running_session_id is None
    assert unlocked.running_round_id is None
    events = services.store.read_session_events(project_id, session_id)
    assert len(events) == 1


def test_list_sessions_skips_invalid_session_log(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    valid_session_id = "sess_valid"
    invalid_session_id = "sess_invalid"
    services.store.append_session_event(
        project_id,
        valid_session_id,
        event_type="user_input",
        scope="main",
        round_id="round_valid",
        message_id="msg_valid",
        payload={"text": "有效会话"},
    )
    services.store.session_file(project_id, invalid_session_id).write_text(
        "{not valid json}\n",
        encoding="utf-8",
    )

    sessions = services.store.list_sessions(project_id)

    assert [session.session_id for session in sessions] == [valid_session_id]
