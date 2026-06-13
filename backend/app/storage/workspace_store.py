from __future__ import annotations

import json
import os
import shutil
import subprocess
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core import ApiError, now_iso, generate_id
from ..domain.disclosure import build_initial_disclosure, disclosure_to_markdown
from ..schemas import InnovationKernelRecord, ProjectRecord, SessionEvent, SessionSummary

DEFAULT_PROJECT_TITLE = "一种图像检测方法"
DEFAULT_DISCLOSURE_TITLE = "未命名专利交底书"
CURRENT_PROJECT_POINTER = "current_project_id"


class WorkspaceStore:
    def __init__(self, root_dir: Path, git_user_name: str, git_user_email: str) -> None:
        self.root_dir = root_dir
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.projects_dir = self.root_dir / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def create_project(self, project_name: str, disclosure_title: str | None = None) -> ProjectRecord:
        project_id = generate_id("proj")
        return self.create_project_with_id(project_id, project_name, disclosure_title)

    def create_project_with_id(
        self,
        project_id: str,
        project_name: str,
        disclosure_title: str | None = None,
    ) -> ProjectRecord:
        workspace = self.project_dir(project_id)
        workspace.mkdir(parents=True, exist_ok=False)
        for name in ("sessions", "assets", "exports", "runtime"):
            (workspace / name).mkdir(exist_ok=True)

        created_at = now_iso()
        project = ProjectRecord(
            project_id=project_id,
            title=project_name,
            created_at=created_at,
            updated_at=created_at,
        )
        disclosure = build_initial_disclosure(disclosure_title if disclosure_title is not None else project_name)

        self.write_json(self.project_file(project_id), project.model_dump())
        self.write_json(self.disclosure_file(project_id), disclosure)
        (workspace / ".gitignore").write_text("runtime/\nexports/\n", encoding="utf-8")
        self._init_workspace_git(workspace)
        return project

    def delete_project(self, project_id: str) -> str | None:
        project = self.get_project(project_id)
        if project.is_busy or project.running_session_id or project.running_round_id:
            raise ApiError(409, "project_busy", "项目正在运行，不能删除。")

        shutil.rmtree(self.project_dir(project_id), onerror=_make_writable_and_retry)

        pointer_path = self.root_dir / CURRENT_PROJECT_POINTER
        next_project_id: str | None = None
        projects = self.list_projects()
        if projects:
            next_project_id = projects[0].project_id

        if pointer_path.exists():
            current_project_id = pointer_path.read_text(encoding="utf-8").strip()
            if current_project_id == project_id:
                if next_project_id:
                    pointer_path.write_text(next_project_id + "\n", encoding="utf-8")
                else:
                    pointer_path.unlink()

        return next_project_id

    def rename_project(self, project_id: str, project_name: str) -> ProjectRecord:
        project = self.get_project(project_id)
        if project.is_busy or project.running_session_id or project.running_round_id:
            raise ApiError(409, "project_busy", "项目正在运行，不能重命名。")
        project.title = project_name
        project.updated_at = now_iso()
        return self.save_project(project)

    def list_projects(self) -> list[ProjectRecord]:
        projects: list[ProjectRecord] = []
        for path in self.projects_dir.glob("*/project.json"):
            try:
                projects.append(ProjectRecord.model_validate(self.read_json(path)))
            except Exception:
                continue
        projects.sort(key=lambda project: project.updated_at, reverse=True)
        return projects

    def list_projects_with_current_first(self) -> list[ProjectRecord]:
        current = self.ensure_current_project()
        projects = self.list_projects()
        return [current, *(project for project in projects if project.project_id != current.project_id)]

    def recover_interrupted_projects(self) -> list[ProjectRecord]:
        recovered: list[ProjectRecord] = []
        for project in self.list_projects():
            if not project.is_busy and not project.running_session_id and not project.running_round_id:
                continue

            session_id = project.running_session_id
            round_id = project.running_round_id
            message_id: str | None = None
            if session_id and round_id and self.session_exists(project.project_id, session_id):
                events = self.read_session_events(project.project_id, session_id)
                round_events = [event for event in events if event.round_id == round_id]
                user_event = next((event for event in round_events if event.type == "user_input"), None)
                anchor_event = user_event or (round_events[-1] if round_events else None)
                message_id = anchor_event.message_id if anchor_event else None
                already_marked = any(
                    event.type == "agent_output"
                    and event.round_id == round_id
                    and event.payload.get("code") == "round_interrupted_by_restart"
                    for event in events
                )
                if not already_marked:
                    self.append_session_event(
                        project.project_id,
                        session_id,
                        event_type="agent_output",
                        scope="main",
                        round_id=round_id,
                        message_id=message_id or generate_id("msg"),
                        payload={
                            "text": "上一次任务因后端重启而中断，已标记为失败。你可以继续发送消息或重新发起任务。",
                            "status": "failed",
                            "code": "round_interrupted_by_restart",
                        },
                    )

            project.running_session_id = None
            project.running_round_id = None
            project.is_busy = False
            project.updated_at = now_iso()
            self.save_project(project)
            recovered.append(project)
        return recovered

    def ensure_current_project(self) -> ProjectRecord:
        pointer_path = self.root_dir / CURRENT_PROJECT_POINTER
        if pointer_path.exists():
            project_id = pointer_path.read_text(encoding="utf-8").strip()
            if project_id:
                try:
                    return self.get_project(project_id)
                except ApiError:
                    pass

        projects = self.list_projects()
        current = (
            projects[0]
            if projects
            else self.create_project(DEFAULT_PROJECT_TITLE, disclosure_title=DEFAULT_PROJECT_TITLE)
        )
        pointer_path.write_text(current.project_id + "\n", encoding="utf-8")
        return current

    def get_project(self, project_id: str) -> ProjectRecord:
        path = self.project_file(project_id)
        if not path.exists():
            raise ApiError(404, "project_not_found", f"project_id 不存在：{project_id}")
        return ProjectRecord.model_validate(self.read_json(path))

    def save_project(self, project: ProjectRecord) -> ProjectRecord:
        self.write_json(self.project_file(project.project_id), project.model_dump())
        return project

    def get_disclosure(self, project_id: str) -> dict[str, Any]:
        path = self.disclosure_file(project_id)
        if not path.exists():
            raise ApiError(404, "document_not_found", f"disclosure.json 不存在：{project_id}")
        return self.read_json(path)

    def save_disclosure(self, project_id: str, disclosure: dict[str, Any]) -> None:
        self.write_json_atomic(self.disclosure_file(project_id), disclosure)

    def session_exists(self, project_id: str, session_id: str) -> bool:
        return self.session_file(project_id, session_id).exists()

    def append_session_event(
        self,
        project_id: str,
        session_id: str,
        *,
        event_type: str,
        scope: str,
        round_id: str,
        message_id: str,
        payload: dict[str, Any],
        call_id: str | None = None,
        parent_call_id: str | None = None,
    ) -> SessionEvent:
        path = self.session_file(project_id, session_id)
        path.parent.mkdir(exist_ok=True)
        seq = 1
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                seq = sum(1 for _ in handle) + 1
        event = SessionEvent(
            id=generate_id("evt"),
            ts=now_iso(),
            type=event_type,
            seq=seq,
            scope=scope,
            round_id=round_id,
            message_id=message_id,
            call_id=call_id,
            parent_call_id=parent_call_id,
            payload=payload,
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json(ensure_ascii=False) + "\n")
        return event

    def read_session_events(self, project_id: str, session_id: str) -> list[SessionEvent]:
        path = self.session_file(project_id, session_id)
        if not path.exists():
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")
        events: list[SessionEvent] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(SessionEvent.model_validate_json(line))
        return events

    def get_innovation_kernel(self, project_id: str, session_id: str) -> InnovationKernelRecord | None:
        if not self.session_exists(project_id, session_id):
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")
        path = self.innovation_kernel_file(project_id, session_id)
        if not path.exists():
            return None
        return InnovationKernelRecord.model_validate(self.read_json(path))

    def save_innovation_kernel(
        self,
        project_id: str,
        session_id: str,
        *,
        kernel_markdown: str,
        source: str,
    ) -> InnovationKernelRecord:
        if not self.session_exists(project_id, session_id):
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")
        record = InnovationKernelRecord(
            exists=True,
            kernel_markdown=kernel_markdown,
            updated_at=now_iso(),
            source=source,
        )
        self.write_json_atomic(self.innovation_kernel_file(project_id, session_id), record.model_dump())
        return record

    def list_sessions(self, project_id: str, active_session_id: str | None = None) -> list[SessionSummary]:
        sessions_dir = self.project_dir(project_id) / "sessions"
        if not sessions_dir.exists():
            return []

        summaries: list[SessionSummary] = []
        for path in sorted(sessions_dir.glob("*.jsonl")):
            session_id = path.stem
            events = self.read_session_events(project_id, session_id)
            last_event = events[-1] if events else None
            first_user_event = next((event for event in events if event.type == "user_input"), None)
            summaries.append(
                SessionSummary(
                    session_id=session_id,
                    updated_at=last_event.ts if last_event else now_iso(),
                    event_count=len(events),
                    last_round_id=last_event.round_id if last_event else None,
                    first_user_text=first_user_event.payload.get("text") if first_user_event else None,
                    is_active=session_id == active_session_id,
                )
            )
        summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
        return summaries

    def export_markdown(self, project_id: str) -> Path:
        disclosure = self.get_disclosure(project_id)
        export_path = self.project_dir(project_id) / "exports" / f"{project_id}-{uuid4().hex[:8]}.md"
        export_path.write_text(disclosure_to_markdown(disclosure), encoding="utf-8")
        return export_path

    def commit_workspace(self, project_id: str, message: str) -> tuple[bool, dict[str, str] | None]:
        workspace = self.project_dir(project_id)
        add_result = subprocess.run(
            ["git", "add", "disclosure.json", "project.json", "sessions"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        if add_result.returncode != 0:
            return False, {"code": "git_add_failed", "message": add_result.stderr.strip() or "git add 执行失败。"}

        diff_result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        if diff_result.returncode == 0:
            return True, None

        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_result.returncode != 0:
            return False, {
                "code": "git_commit_failed",
                "message": commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit 执行失败。",
            }
        return True, None

    def project_dir(self, project_id: str) -> Path:
        return self.projects_dir / project_id

    def project_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def disclosure_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "disclosure.json"

    def session_file(self, project_id: str, session_id: str) -> Path:
        return self.project_dir(project_id) / "sessions" / f"{session_id}.jsonl"

    def innovation_kernel_file(self, project_id: str, session_id: str) -> Path:
        return self.project_dir(project_id) / "sessions" / f"{session_id}.innovation_kernel.json"

    @staticmethod
    def write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    @staticmethod
    def read_json(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def _init_workspace_git(self, workspace: Path) -> None:
        commands = [
            ["git", "init"],
            ["git", "config", "user.name", self.git_user_name],
            ["git", "config", "user.email", self.git_user_email],
            ["git", "add", "."],
            ["git", "commit", "-m", "init disclosure workspace"],
        ]
        for command in commands:
            subprocess.run(command, cwd=workspace, capture_output=True, text=True, check=False)


def _make_writable_and_retry(function: Any, path: str, _exc_info: Any) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    function(path)
