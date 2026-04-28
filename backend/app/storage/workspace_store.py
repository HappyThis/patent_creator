from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..core import ApiError, now_iso, generate_id
from ..domain.disclosure import build_initial_disclosure, disclosure_to_markdown
from ..schemas import ProjectRecord, SessionEvent, SessionSummary


class WorkspaceStore:
    def __init__(self, root_dir: Path, git_user_name: str, git_user_email: str) -> None:
        self.root_dir = root_dir
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.projects_dir = self.root_dir / "projects"
        self.projects_dir.mkdir(parents=True, exist_ok=True)

    def create_project(self, title: str) -> ProjectRecord:
        project_id = generate_id("proj")
        workspace = self.project_dir(project_id)
        workspace.mkdir(parents=True, exist_ok=False)
        for name in ("sessions", "assets", "exports", "runtime"):
            (workspace / name).mkdir(exist_ok=True)

        created_at = now_iso()
        project = ProjectRecord(
            project_id=project_id,
            title=title,
            created_at=created_at,
            updated_at=created_at,
        )
        disclosure = build_initial_disclosure(title)

        self.write_json(self.project_file(project_id), project.model_dump())
        self.write_json(self.disclosure_file(project_id), disclosure)
        (workspace / ".gitignore").write_text("runtime/\nexports/\n", encoding="utf-8")
        self._init_workspace_git(workspace)
        return project

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

    def list_sessions(self, project_id: str, active_session_id: str | None = None) -> list[SessionSummary]:
        sessions_dir = self.project_dir(project_id) / "sessions"
        if not sessions_dir.exists():
            return []

        summaries: list[SessionSummary] = []
        for path in sorted(sessions_dir.glob("*.jsonl")):
            session_id = path.stem
            events = self.read_session_events(project_id, session_id)
            last_event = events[-1] if events else None
            latest_user_event = next((event for event in reversed(events) if event.type == "user_input"), None)
            summaries.append(
                SessionSummary(
                    session_id=session_id,
                    updated_at=last_event.ts if last_event else now_iso(),
                    event_count=len(events),
                    last_round_id=last_event.round_id if last_event else None,
                    latest_user_text=latest_user_event.payload.get("text") if latest_user_event else None,
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
