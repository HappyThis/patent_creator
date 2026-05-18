from __future__ import annotations

from pathlib import Path

from app.services import AppServices

from helpers import ScriptedLLMClient, make_settings


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
