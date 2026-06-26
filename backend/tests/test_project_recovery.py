from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import time

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


def test_append_session_event_serializes_concurrent_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_concurrent"
    original_next_seq = services.store._next_session_event_seq

    def slow_next_seq(path: Path) -> int:
        seq = original_next_seq(path)
        time.sleep(0.01)
        return seq

    monkeypatch.setattr(services.store, "_next_session_event_seq", slow_next_seq)

    def append_event(index: int) -> int:
        event = services.store.append_session_event(
            project_id,
            session_id,
            event_type="agent_output",
            scope="main",
            round_id="round_1",
            message_id=f"msg_{index}",
            payload={"text": f"第 {index} 条"},
        )
        return event.seq

    with ThreadPoolExecutor(max_workers=8) as executor:
        returned_seq = list(executor.map(append_event, range(12)))

    events = services.store.read_session_events(project_id, session_id)
    assert sorted(returned_seq) == list(range(1, 13))
    assert [event.seq for event in events] == list(range(1, 13))


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


def test_session_log_readers_skip_legacy_technical_solution_check_events(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_legacy_check"
    lines = [
        {
            "id": "evt_1",
            "ts": "2026-06-19T00:00:00Z",
            "type": "user_input",
            "seq": 1,
            "scope": "main",
            "round_id": "round_1",
            "message_id": "msg_1",
            "call_id": None,
            "payload": {"text": "写技术方案"},
        },
        {
            "id": "evt_2",
            "ts": "2026-06-19T00:00:01Z",
            "type": "technical_solution_check_feedback",
            "seq": 2,
            "scope": "main",
            "round_id": "round_1",
            "message_id": "msg_1",
            "call_id": None,
            "payload": {"text": "旧质量门禁反馈"},
        },
        {
            "id": "evt_3",
            "ts": "2026-06-19T00:00:02Z",
            "type": "agent_output",
            "seq": 3,
            "scope": "main",
            "round_id": "round_1",
            "message_id": "msg_1",
            "call_id": None,
            "payload": {"text": "已完成"},
        },
        {
            "id": "evt_4",
            "ts": "2026-06-19T00:00:03Z",
            "type": "technical_solution_check_result",
            "seq": 4,
            "scope": "main",
            "round_id": "round_1",
            "message_id": "msg_1",
            "call_id": None,
            "payload": {"review_markdown": "旧结果"},
        },
    ]
    services.store.session_file(project_id, session_id).write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        encoding="utf-8",
    )

    events = services.store.read_session_events(project_id, session_id)
    sessions = services.store.list_sessions(project_id)

    assert [event.type for event in events] == ["user_input", "agent_output"]
    assert services.store.first_user_text(project_id, session_id) == "写技术方案"
    assert sessions[0].session_id == session_id
    assert sessions[0].event_count == 2
    assert sessions[0].last_round_id == "round_1"
    assert sessions[0].updated_at == "2026-06-19T00:00:02Z"


def test_list_sessions_skips_logs_with_only_legacy_technical_solution_check_events(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    session_id = "sess_legacy_only"
    lines = [
        {
            "id": "evt_1",
            "ts": "2026-06-19T00:00:01Z",
            "type": "technical_solution_check_feedback",
            "seq": 1,
            "scope": "main",
            "round_id": "round_1",
            "message_id": "msg_1",
            "call_id": None,
            "payload": {"text": "旧质量门禁反馈"},
        },
        {
            "id": "evt_2",
            "ts": "2026-06-19T00:00:02Z",
            "type": "technical_solution_check_result",
            "seq": 2,
            "scope": "main",
            "round_id": "round_1",
            "message_id": "msg_1",
            "call_id": None,
            "payload": {"review_markdown": "旧结果"},
        },
    ]
    services.store.session_file(project_id, session_id).write_text(
        "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines),
        encoding="utf-8",
    )

    assert services.store.read_session_events(project_id, session_id) == []
    assert services.store.list_sessions(project_id) == []


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


def test_commit_workspace_stops_when_cached_diff_command_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(tmp_path)
    services = AppServices(settings, llm_client=ScriptedLLMClient([]))
    project_id = services.store.create_project("测试项目").project_id
    commands: list[list[str]] = []

    def fake_run(
        command: list[str],
        *,
        cwd: Path,
        capture_output: bool,
        text: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        _ = (cwd, capture_output, text, check)
        commands.append(command)
        if command[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(command, 128, stdout="", stderr="fatal: not a git repository")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    committed, error = services.store.commit_workspace(project_id, "update disclosure")

    assert committed is False
    assert error == {"code": "git_diff_failed", "message": "fatal: not a git repository"}
    assert commands == [
        ["git", "add", "disclosure.json", "project.json", "sessions", "assets"],
        ["git", "diff", "--cached", "--quiet"],
    ]
