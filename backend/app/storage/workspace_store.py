from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from urllib.parse import quote
from uuid import uuid4

from pydantic import ValidationError
from PIL import Image, UnidentifiedImageError

from ..core import ApiError, now_iso, generate_id
from ..drawio_config import DEFAULT_DRAWIO_EMBED_URL, normalize_drawio_embed_url
from ..domain.disclosure import build_initial_disclosure, disclosure_to_markdown
from ..domain.docx_export import DocxExportError, export_disclosure_docx
from ..domain.document_tool_results import tool_failed
from ..domain.figures import (
    FIGURE_HEIGHT,
    FIGURE_WIDTH,
    build_figure_record,
    drawio_updated_at,
    figure_summary,
    new_drawio_updated_at,
    update_figure_record,
    validate_drawio_xml,
)
from ..schemas import ProjectRecord, SessionEvent, SessionEventType, SessionSummary

DEFAULT_DISCLOSURE_TITLE = "未命名专利交底书"
CURRENT_PROJECT_POINTER = "current_project_id"
STORAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
LEGACY_IGNORED_SESSION_EVENT_TYPES = frozenset(
    {
        "technical_solution_check_result",
        "technical_solution_check_feedback",
    }
)
logger = logging.getLogger("patent_creator.workspace_store")


class WorkspaceStore:
    def __init__(
        self,
        root_dir: Path,
        git_user_name: str,
        git_user_email: str,
        drawio_embed_url: str = DEFAULT_DRAWIO_EMBED_URL,
        drawio_allow_nonlocal: bool = False,
    ) -> None:
        self.root_dir = root_dir
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.drawio_embed_url = normalize_drawio_embed_url(
            drawio_embed_url,
            allow_nonlocal=drawio_allow_nonlocal,
        )
        self.projects_dir = self.root_dir / "projects"
        self._session_event_lock = threading.Lock()
        self._figure_locks_guard = threading.Lock()
        self._figure_project_locks: dict[str, threading.RLock] = {}
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
            except (OSError, json.JSONDecodeError, ValidationError) as exc:
                logger.warning("skipping invalid project metadata path=%s error=%s", path, exc)
                continue
        projects.sort(key=lambda project: project.updated_at, reverse=True)
        return projects

    def list_projects_with_current_first(self) -> list[ProjectRecord]:
        projects = self.list_projects()
        pointer_path = self.root_dir / CURRENT_PROJECT_POINTER
        if not projects:
            if pointer_path.exists():
                pointer_path.unlink()
            return []

        if pointer_path.exists():
            current_project_id = pointer_path.read_text(encoding="utf-8").strip()
            current = next((project for project in projects if project.project_id == current_project_id), None)
            if current:
                return [current, *(project for project in projects if project.project_id != current.project_id)]

        pointer_path.write_text(projects[0].project_id + "\n", encoding="utf-8")
        return projects

    def recover_interrupted_projects(self) -> list[ProjectRecord]:
        recovered: list[ProjectRecord] = []
        for project in self.list_projects():
            if not project.is_busy and not project.running_session_id and not project.running_round_id:
                continue

            session_id = project.running_session_id
            round_id = project.running_round_id
            message_id: str | None = None
            if session_id and round_id and self.session_exists(project.project_id, session_id):
                message_id, already_marked = self.round_message_status(
                    project.project_id,
                    session_id,
                    round_id,
                    "round_interrupted_by_restart",
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
        event_type: SessionEventType,
        scope: str,
        round_id: str,
        message_id: str,
        payload: dict[str, Any],
        call_id: str | None = None,
    ) -> SessionEvent:
        with self._session_event_lock:
            path = self.session_file(project_id, session_id)
            path.parent.mkdir(exist_ok=True)
            seq = self._next_session_event_seq(path)
            event = SessionEvent(
                id=generate_id("evt"),
                ts=now_iso(),
                type=event_type,
                seq=seq,
                scope=scope,
                round_id=round_id,
                message_id=message_id,
                call_id=call_id,
                payload=payload,
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json(ensure_ascii=False) + "\n")
            return event

    def _next_session_event_seq(self, path: Path) -> int:
        if not path.exists():
            return 1

        line = _read_last_nonempty_line(path)
        if line is None:
            return 1
        try:
            return int(SessionEvent.model_validate_json(line).seq) + 1
        except (ValueError, ValidationError):
            with path.open("r", encoding="utf-8") as handle:
                return sum(1 for raw_line in handle if raw_line.strip()) + 1

    def read_session_events(self, project_id: str, session_id: str) -> list[SessionEvent]:
        return list(self.iter_session_events(project_id, session_id))

    def recent_technical_solution_enhancement_history(
        self,
        project_id: str,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        records: list[tuple[str, int, dict[str, Any]]] = []
        sessions_dir = self.project_dir(project_id) / "sessions"
        if not sessions_dir.exists():
            return []

        for path in sessions_dir.glob("*.jsonl"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        if not line.strip():
                            continue
                        event = _parse_session_event_line(line)
                        if event is None:
                            continue
                        if event.type != "technical_solution_enhancement_summary":
                            continue
                        record = event.payload.get("record")
                        if isinstance(record, dict):
                            records.append((event.ts, event.seq, record))
            except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
                logger.warning(
                    "skipping invalid enhancement history project_id=%s path=%s error=%s",
                    project_id,
                    path,
                    exc,
                )
                continue

        records.sort(key=lambda item: (item[0], item[1]))
        return [record for _, _, record in records[-max(0, limit) :]]

    def iter_session_events(self, project_id: str, session_id: str) -> Iterator[SessionEvent]:
        path = self.session_file(project_id, session_id)
        if not path.exists():
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    event = _parse_session_event_line(line)
                    if event is not None:
                        yield event

    def first_user_text(self, project_id: str, session_id: str) -> str | None:
        path = self.session_file(project_id, session_id)
        if not path.exists():
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = _parse_session_event_line(line)
                if event is None:
                    continue
                if event.type != "user_input":
                    continue
                text = event.payload.get("text")
                return text if isinstance(text, str) else None
        return None

    def delete_session(self, project_id: str, session_id: str) -> str | None:
        project = self.get_project(project_id)
        if project.is_busy or project.running_session_id or project.running_round_id:
            raise ApiError(409, "project_busy", "项目正在运行，不能删除对话。")

        path = self.session_file(project_id, session_id)
        if not path.exists():
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")

        path.unlink()
        next_session_id: str | None = None
        remaining_sessions = self.list_sessions(project_id)
        if remaining_sessions:
            next_session_id = remaining_sessions[0].session_id

        if project.active_session_id == session_id:
            project.active_session_id = next_session_id
            project.updated_at = now_iso()
            self.save_project(project)

        return next_session_id

    def round_message_status(
        self,
        project_id: str,
        session_id: str,
        round_id: str,
        marker_code: str,
    ) -> tuple[str | None, bool]:
        path = self.session_file(project_id, session_id)
        if not path.exists():
            raise ApiError(404, "session_not_found", f"session_id 不存在：{session_id}")

        user_message_id: str | None = None
        last_round_message_id: str | None = None
        already_marked = False
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = _parse_session_event_line(line)
                if event is None:
                    continue
                if event.round_id != round_id:
                    continue
                last_round_message_id = event.message_id
                if user_message_id is None and event.type == "user_input":
                    user_message_id = event.message_id
                if event.type == "agent_output" and event.payload.get("code") == marker_code:
                    already_marked = True

        return user_message_id or last_round_message_id, already_marked

    def list_sessions(self, project_id: str, active_session_id: str | None = None) -> list[SessionSummary]:
        sessions_dir = self.project_dir(project_id) / "sessions"
        if not sessions_dir.exists():
            return []

        summaries: list[SessionSummary] = []
        for path in sorted(sessions_dir.glob("*.jsonl")):
            session_id = path.stem
            try:
                summaries.append(self._session_summary_from_file(session_id, path, active_session_id))
            except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
                logger.warning(
                    "skipping invalid session log project_id=%s session_id=%s path=%s error=%s",
                    project_id,
                    session_id,
                    path,
                    exc,
                )
        summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
        return summaries

    def _session_summary_from_file(
        self,
        session_id: str,
        path: Path,
        active_session_id: str | None,
    ) -> SessionSummary:
        event_count = 0
        last_event: SessionEvent | None = None
        first_user_text: str | None = None
        title: str | None = None
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                event = _parse_session_event_line(line)
                if event is None:
                    continue
                event_count += 1
                last_event = event
                if first_user_text is None and event.type == "user_input":
                    text = event.payload.get("text")
                    first_user_text = text if isinstance(text, str) else None
                if event.type == "session_title":
                    raw_title = event.payload.get("title")
                    if isinstance(raw_title, str) and raw_title.strip():
                        title = raw_title.strip()
        if event_count == 0:
            raise ValueError("session log contains no valid current events")
        return SessionSummary(
            session_id=session_id,
            updated_at=last_event.ts if last_event else now_iso(),
            event_count=event_count,
            last_round_id=last_event.round_id if last_event else None,
            first_user_text=first_user_text,
            title=title,
            is_active=session_id == active_session_id,
        )

    def export_markdown(self, project_id: str) -> Path:
        disclosure = self.get_disclosure(project_id)
        export_path = self.project_dir(project_id) / "exports" / f"{project_id}-{uuid4().hex[:8]}.md"
        export_path.write_text(disclosure_to_markdown(disclosure), encoding="utf-8")
        return export_path

    def export_docx(self, project_id: str) -> Path:
        disclosure = self.get_disclosure(project_id)
        project_dir = self.project_dir(project_id)
        export_path = project_dir / "exports" / f"{project_id}-{uuid4().hex[:8]}.docx"
        try:
            return export_disclosure_docx(
                disclosure=disclosure,
                figures=self.list_figures(project_id),
                export_path=export_path,
                project_dir=project_dir,
            )
        except DocxExportError as exc:
            raise ApiError(500, "docx_export_failed", str(exc)) from exc

    def commit_workspace(self, project_id: str, message: str) -> tuple[bool, dict[str, str] | None]:
        workspace = self.project_dir(project_id)
        add_result = subprocess.run(
            ["git", "add", "disclosure.json", "project.json", "sessions", "assets"],
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
        if diff_result.returncode != 1:
            return False, {
                "code": "git_diff_failed",
                "message": diff_result.stderr.strip() or diff_result.stdout.strip() or "git diff 执行失败。",
            }

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
        return self.projects_dir / _validated_storage_id(project_id, "project_id")

    def project_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "project.json"

    def disclosure_file(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "disclosure.json"

    def session_file(self, project_id: str, session_id: str) -> Path:
        return self.project_dir(project_id) / "sessions" / f"{_validated_storage_id(session_id, 'session_id')}.jsonl"

    def figures_dir(self, project_id: str) -> Path:
        path = self.project_dir(project_id) / "assets" / "figures"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def list_figures(self, project_id: str) -> list[dict[str, Any]]:
        figures: list[dict[str, Any]] = []
        for path in sorted(self.figures_dir(project_id).glob("fig_*/figure.json")):
            try:
                figure = self.read_json(path)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("skipping invalid figure metadata project_id=%s path=%s error=%s", project_id, path, exc)
                continue
            if isinstance(figure.get("figure_id"), str):
                figures.append(self._normalize_figure(project_id, figure))
        figures.sort(key=lambda figure: figure.get("figure_id", ""))
        return figures

    def get_figure(self, project_id: str, figure_id: str) -> dict[str, Any] | None:
        path = self.figure_json_file(project_id, figure_id)
        if not path.exists():
            return None
        return self._normalize_figure(project_id, self.read_json(path))

    def get_figure_with_drawio(self, project_id: str, figure_id: str) -> tuple[dict[str, Any], str] | None:
        with self._figure_project_lock(project_id):
            path = self.figure_json_file(project_id, figure_id)
            if not path.exists():
                return None
            figure = self.read_json(path)
            drawio_file = self._figure_asset_file_from_record(
                project_id,
                figure_id,
                figure,
                section="source",
                fallback_name="diagram.drawio",
            )
            try:
                drawio_xml = drawio_file.read_text(encoding="utf-8")
            except OSError:
                return None
            return self._normalize_figure(project_id, figure), drawio_xml

    def create_figure(self, project_id: str, *, title: str, drawio_xml: str) -> dict[str, Any]:
        validate_result = validate_drawio_xml(drawio_xml)
        if validate_result["status"] == "failed":
            return validate_result
        with self._figure_project_lock(project_id):
            figure_id = self.next_figure_id(project_id)
            index = int(figure_id.removeprefix("fig_"))
            figure_dir = self.figure_dir(project_id, figure_id)
            asset_path = f"assets/figures/{figure_id}/figure.json"
            if figure_dir.exists() and not self.figure_json_file(project_id, figure_id).exists():
                shutil.rmtree(figure_dir)
            figure_dir.mkdir(parents=True, exist_ok=False)
            revision = self._new_figure_revision(project_id, figure_id)
            try:
                self.write_text_atomic(revision["drawio_file"], validate_result["output"]["drawio_xml"])
                render_result = self._render_drawio_file(
                    input_path=revision["drawio_file"],
                    output_path=revision["render_file"],
                )
                if render_result["status"] == "failed":
                    shutil.rmtree(figure_dir, ignore_errors=True)
                    return render_result
                figure = build_figure_record(
                    figure_id=figure_id,
                    index=index,
                    title=title,
                    source_path=revision["source_path"],
                    render_path=revision["render_path"],
                    asset_path=asset_path,
                )
                self.write_json_atomic(self.figure_json_file(project_id, figure_id), figure)
            except OSError as exc:
                shutil.rmtree(figure_dir, ignore_errors=True)
                return tool_failed("figure_storage_failed", f"附图保存失败：{exc}")
            return {
                "status": "success",
                "output": {
                    "figure": self._normalize_figure(project_id, figure),
                },
            }

    def update_figure(
        self,
        project_id: str,
        figure_id: str,
        *,
        title: str | None,
        drawio_xml: str,
        expected_drawio_updated_at: str | None,
    ) -> dict[str, Any]:
        if not expected_drawio_updated_at:
            return tool_failed("drawio_read_required", "修改 draw.io 图前必须先读取当前附图，并在 update 时带上 drawio_updated_at。")
        validate_result = validate_drawio_xml(drawio_xml)
        if validate_result["status"] == "failed":
            return validate_result
        with self._figure_project_lock(project_id):
            figure_path = self.figure_json_file(project_id, figure_id)
            if not figure_path.exists():
                return {"status": "failed", "output": {"code": "figure_not_found", "message": f"figure 不存在：{figure_id}"}}
            current = self.read_json(figure_path)
            self._cleanup_unreferenced_figure_revisions(project_id, figure_id, current)
            current_drawio_updated_at = drawio_updated_at(current)
            if expected_drawio_updated_at != current_drawio_updated_at:
                return tool_failed(
                    "drawio_conflict",
                    "当前附图已在你读取之后被修改，请重新 read 最新 draw.io XML 后再 update。",
                    current_drawio_updated_at=current_drawio_updated_at,
                )

            revision = self._new_figure_revision(project_id, figure_id)
            revision_dir = revision["drawio_file"].parent
            try:
                self.write_text_atomic(revision["drawio_file"], validate_result["output"]["drawio_xml"])
                render_result = self._render_drawio_file(
                    input_path=revision["drawio_file"],
                    output_path=revision["render_file"],
                )
                if render_result["status"] == "failed":
                    shutil.rmtree(revision_dir, ignore_errors=True)
                    return render_result
                drawio_timestamp = new_drawio_updated_at()
                updated = update_figure_record(
                    current,
                    title=title,
                    drawio_timestamp=drawio_timestamp,
                    source_path=revision["source_path"],
                    render_path=revision["render_path"],
                )
                self.write_json_atomic(figure_path, updated)
            except OSError as exc:
                shutil.rmtree(revision_dir, ignore_errors=True)
                return tool_failed("figure_storage_failed", f"附图保存失败：{exc}")

            self._remove_superseded_figure_assets(project_id, figure_id, current, updated)
            self._cleanup_unreferenced_figure_revisions(project_id, figure_id, updated)
            return {
                "status": "success",
                "output": {
                    "figure": self._normalize_figure(project_id, updated),
                },
            }

    def delete_figure(self, project_id: str, figure_id: str) -> bool:
        with self._figure_project_lock(project_id):
            path = self.figure_dir(project_id, figure_id)
            if not path.exists():
                return False
            shutil.rmtree(path)
            return True

    def next_figure_id(self, project_id: str) -> str:
        max_value = 0
        for figure in self.list_figures(project_id):
            figure_id = str(figure.get("figure_id") or "")
            if figure_id.startswith("fig_"):
                try:
                    max_value = max(max_value, int(figure_id.removeprefix("fig_")))
                except ValueError:
                    continue
        return f"fig_{max_value + 1:06d}"

    def figure_dir(self, project_id: str, figure_id: str) -> Path:
        return self.figures_dir(project_id) / _validated_storage_id(figure_id, "figure_id")

    def figure_json_file(self, project_id: str, figure_id: str) -> Path:
        return self.figure_dir(project_id, figure_id) / "figure.json"

    def figure_drawio_file(self, project_id: str, figure_id: str) -> Path:
        return self._current_figure_asset_file(
            project_id,
            figure_id,
            section="source",
            fallback_name="diagram.drawio",
        )

    def figure_render_file(self, project_id: str, figure_id: str) -> Path:
        return self._current_figure_asset_file(
            project_id,
            figure_id,
            section="render",
            fallback_name="render.png",
        )

    def read_figure_drawio_xml(self, project_id: str, figure_id: str) -> str | None:
        snapshot = self.get_figure_with_drawio(project_id, figure_id)
        return snapshot[1] if snapshot is not None else None

    def project_asset_file(self, project_id: str, asset_path: str) -> Path:
        project_root = self.project_dir(project_id).resolve()
        assets_root = (project_root / "assets").resolve()
        requested = (project_root / asset_path).resolve()
        if not requested.is_relative_to(assets_root):
            raise ApiError(404, "asset_not_found", f"asset 不存在：{asset_path}")
        return requested

    def figure_summaries(self, project_id: str) -> list[dict[str, Any]]:
        return [figure_summary(figure) for figure in self.list_figures(project_id)]

    def _new_figure_revision(self, project_id: str, figure_id: str) -> dict[str, Any]:
        revision_id = f"rev_{uuid4().hex}"
        relative_dir = Path("assets") / "figures" / figure_id / ".revisions" / revision_id
        revision_dir = self.project_dir(project_id) / relative_dir
        revision_dir.mkdir(parents=True, exist_ok=False)
        return {
            "drawio_file": revision_dir / "diagram.drawio",
            "render_file": revision_dir / "render.png",
            "source_path": (relative_dir / "diagram.drawio").as_posix(),
            "render_path": (relative_dir / "render.png").as_posix(),
        }

    def _current_figure_asset_file(
        self,
        project_id: str,
        figure_id: str,
        *,
        section: str,
        fallback_name: str,
    ) -> Path:
        figure_dir = self.figure_dir(project_id, figure_id)
        figure_path = figure_dir / "figure.json"
        if figure_path.exists():
            try:
                figure = self.read_json(figure_path)
                return self._figure_asset_file_from_record(
                    project_id,
                    figure_id,
                    figure,
                    section=section,
                    fallback_name=fallback_name,
                )
            except (OSError, json.JSONDecodeError):
                pass
        return figure_dir / fallback_name

    def _figure_asset_file_from_record(
        self,
        project_id: str,
        figure_id: str,
        figure: dict[str, Any],
        *,
        section: str,
        fallback_name: str,
    ) -> Path:
        figure_dir = self.figure_dir(project_id, figure_id)
        asset = figure.get(section)
        relative_path = asset.get("path") if isinstance(asset, dict) else None
        if isinstance(relative_path, str) and relative_path:
            resolved = (self.project_dir(project_id) / relative_path).resolve()
            if resolved.is_relative_to(figure_dir.resolve()):
                return resolved
        return figure_dir / fallback_name

    def _remove_superseded_figure_assets(
        self,
        project_id: str,
        figure_id: str,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        figure_dir = self.figure_dir(project_id, figure_id).resolve()
        current_paths = {
            str(section.get("path"))
            for section in (current.get("source"), current.get("render"))
            if isinstance(section, dict) and section.get("path")
        }
        previous_paths = {
            str(section.get("path"))
            for section in (previous.get("source"), previous.get("render"))
            if isinstance(section, dict) and section.get("path")
        }
        parent_dirs: set[Path] = set()
        for relative_path in previous_paths - current_paths:
            resolved = (self.project_dir(project_id) / relative_path).resolve()
            if not resolved.is_relative_to(figure_dir):
                continue
            try:
                resolved.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "failed to clean superseded figure asset project_id=%s figure_id=%s path=%s error=%s",
                    project_id,
                    figure_id,
                    resolved,
                    exc,
                )
                continue
            parent = resolved.parent
            while parent != figure_dir:
                parent_dirs.add(parent)
                parent = parent.parent
        for parent in sorted(parent_dirs, key=lambda path: len(path.parts), reverse=True):
            try:
                parent.rmdir()
            except OSError:
                pass

    def _cleanup_unreferenced_figure_revisions(
        self,
        project_id: str,
        figure_id: str,
        current: dict[str, Any],
    ) -> None:
        revisions_dir = self.figure_dir(project_id, figure_id) / ".revisions"
        if not revisions_dir.is_dir():
            return
        keep_dirs: set[Path] = set()
        for section in (current.get("source"), current.get("render")):
            relative_path = section.get("path") if isinstance(section, dict) else None
            if not isinstance(relative_path, str) or not relative_path:
                continue
            resolved = (self.project_dir(project_id) / relative_path).resolve()
            if resolved.is_relative_to(revisions_dir.resolve()):
                keep_dirs.add(resolved.parent)
        for revision_dir in revisions_dir.glob("rev_*"):
            if revision_dir.resolve() not in keep_dirs:
                shutil.rmtree(revision_dir, ignore_errors=True)

    @contextmanager
    def _figure_project_lock(self, project_id: str) -> Iterator[None]:
        validated_project_id = _validated_storage_id(project_id, "project_id")
        with self._figure_locks_guard:
            thread_lock = self._figure_project_locks.setdefault(validated_project_id, threading.RLock())
        with thread_lock:
            lock_path = self.project_dir(project_id) / "runtime" / "figures.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b") as lock_file:
                _lock_file(lock_file)
                try:
                    yield
                finally:
                    _unlock_file(lock_file)

    def _normalize_figure(self, project_id: str, figure: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(figure)
        render = dict(normalized.get("render") or {})
        render_path = render.get("path")
        if isinstance(render_path, str) and render_path:
            render["url"] = f"/api/projects/{quote(project_id, safe='')}/asset/{quote(render_path, safe='/')}"
        normalized["render"] = render
        return normalized

    def _render_drawio_file(self, *, input_path: Path, output_path: Path) -> dict[str, Any]:
        repo_root = Path(__file__).resolve().parents[3]
        frontend_root = repo_root / "frontend"
        script_path = frontend_root / "scripts" / "render-figure-drawio.mjs"
        try:
            result = subprocess.run(
                [
                    "node",
                    str(script_path),
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--width",
                    str(FIGURE_WIDTH),
                    "--height",
                    str(FIGURE_HEIGHT),
                    "--drawio-url",
                    self.drawio_embed_url,
                ],
                cwd=frontend_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except OSError as exc:
            return tool_failed("figure_render_failed", f"draw.io 附图渲染器启动失败：{exc}")
        except subprocess.TimeoutExpired:
            return tool_failed("figure_render_failed", "draw.io 附图渲染超时。")
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "unknown renderer error"
            return tool_failed("figure_render_failed", f"draw.io 附图渲染失败：{message}")
        if not output_path.is_file():
            return tool_failed("figure_render_failed", "draw.io 附图渲染完成但未生成 render.png。")
        dimensions = _png_dimensions(output_path)
        if dimensions != (FIGURE_WIDTH, FIGURE_HEIGHT):
            output_path.unlink(missing_ok=True)
            actual = f"{dimensions[0]}x{dimensions[1]}" if dimensions else "invalid PNG"
            return tool_failed(
                "figure_render_failed",
                f"draw.io 附图尺寸必须为 {FIGURE_WIDTH}x{FIGURE_HEIGHT}，实际为 {actual}。",
            )
        return {
            "status": "success",
            "output": {
                "path": str(output_path),
            },
        }

    @staticmethod
    def write_json(path: Path, payload: dict[str, Any]) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @staticmethod
    def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)

    @staticmethod
    def write_text_atomic(path: Path, payload: str) -> None:
        tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)

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


def _lock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                return None
            dimensions = image.size
            image.verify()
            return dimensions
    except (OSError, SyntaxError, UnidentifiedImageError):
        return None


def _make_writable_and_retry(function: Any, path: str, _exc_info: Any) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    function(path)


def _validated_storage_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not STORAGE_ID_PATTERN.fullmatch(value):
        raise ApiError(422, f"invalid_{field_name}", f"{field_name} 格式无效：{value}")
    return value


def _parse_session_event_line(line: str) -> SessionEvent | None:
    try:
        return SessionEvent.model_validate_json(line)
    except ValidationError:
        if _raw_session_event_type(line) in LEGACY_IGNORED_SESSION_EVENT_TYPES:
            return None
        raise


def _raw_session_event_type(line: str) -> str | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    event_type = payload.get("type") if isinstance(payload, dict) else None
    return event_type if isinstance(event_type, str) else None


def _read_last_nonempty_line(path: Path, chunk_size: int = 4096) -> str | None:
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            lines = buffer.splitlines()
            candidates = lines if position == 0 else lines[1:]
            for line in reversed(candidates):
                if line.strip():
                    return line.decode("utf-8")
    return None
