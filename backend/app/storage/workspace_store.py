from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import stat
import threading
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote
from uuid import uuid4

from pydantic import ValidationError

from ..core import ApiError, now_iso, generate_id
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
    def __init__(self, root_dir: Path, git_user_name: str, git_user_email: str) -> None:
        self.root_dir = root_dir
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.projects_dir = self.root_dir / "projects"
        self._session_event_lock = threading.Lock()
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

    def create_figure(self, project_id: str, *, title: str, drawio_xml: str) -> dict[str, Any]:
        figure_id = self.next_figure_id(project_id)
        index = int(figure_id.removeprefix("fig_"))
        figure_dir = self.figure_dir(project_id, figure_id)
        source_path = f"assets/figures/{figure_id}/diagram.drawio"
        render_path = f"assets/figures/{figure_id}/render.png"
        asset_path = f"assets/figures/{figure_id}/figure.json"
        validate_result = validate_drawio_xml(drawio_xml)
        if validate_result["status"] == "failed":
            return validate_result
        if figure_dir.exists() and not self.figure_json_file(project_id, figure_id).exists():
            shutil.rmtree(figure_dir)
        figure_dir.mkdir(parents=True, exist_ok=False)
        self.write_text_atomic(self.figure_drawio_file(project_id, figure_id), validate_result["output"]["drawio_xml"])
        render_result = self._render_drawio_file(
            input_path=self.figure_drawio_file(project_id, figure_id),
            output_path=self.figure_render_file(project_id, figure_id),
        )
        if render_result["status"] == "failed":
            shutil.rmtree(figure_dir, ignore_errors=True)
            return render_result
        figure = build_figure_record(
            figure_id=figure_id,
            index=index,
            title=title,
            source_path=source_path,
            render_path=render_path,
            asset_path=asset_path,
        )
        self.write_json_atomic(self.figure_json_file(project_id, figure_id), figure)
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
        figure_path = self.figure_json_file(project_id, figure_id)
        if not figure_path.exists():
            return {"status": "failed", "output": {"code": "figure_not_found", "message": f"figure 不存在：{figure_id}"}}
        current = self.read_json(figure_path)
        if not expected_drawio_updated_at:
            return tool_failed("drawio_read_required", "修改 draw.io 图前必须先读取当前附图，并在 update 时带上 drawio_updated_at。")
        current_drawio_updated_at = drawio_updated_at(current)
        if expected_drawio_updated_at != current_drawio_updated_at:
            return tool_failed(
                "drawio_conflict",
                "当前附图已在你读取之后被修改，请重新 read 最新 draw.io XML 后再 update。",
                current_drawio_updated_at=current_drawio_updated_at,
            )
        validate_result = validate_drawio_xml(drawio_xml)
        if validate_result["status"] == "failed":
            return validate_result
        drawio_file = self.figure_drawio_file(project_id, figure_id)
        render_file = self.figure_render_file(project_id, figure_id)
        tmp_drawio_file = drawio_file.with_name(f".diagram.{uuid4().hex}.drawio")
        tmp_render_file = render_file.with_name(f".render.{uuid4().hex}.png")
        tmp_drawio_file.write_text(validate_result["output"]["drawio_xml"], encoding="utf-8")
        render_result = self._render_drawio_file(input_path=tmp_drawio_file, output_path=tmp_render_file)
        if render_result["status"] == "failed":
            tmp_drawio_file.unlink(missing_ok=True)
            tmp_render_file.unlink(missing_ok=True)
            return render_result
        drawio_timestamp = new_drawio_updated_at()
        tmp_drawio_file.replace(drawio_file)
        tmp_render_file.replace(render_file)
        updated = update_figure_record(current, title=title, drawio_timestamp=drawio_timestamp)
        self.write_json_atomic(figure_path, updated)
        return {
            "status": "success",
            "output": {
                "figure": self._normalize_figure(project_id, updated),
            },
        }

    def delete_figure(self, project_id: str, figure_id: str) -> bool:
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
        return self.figure_dir(project_id, figure_id) / "diagram.drawio"

    def figure_render_file(self, project_id: str, figure_id: str) -> Path:
        return self.figure_dir(project_id, figure_id) / "render.png"

    def read_figure_drawio_xml(self, project_id: str, figure_id: str) -> str | None:
        path = self.figure_drawio_file(project_id, figure_id)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("skipping invalid figure drawio xml project_id=%s figure_id=%s error=%s", project_id, figure_id, exc)
            return None

    def project_asset_file(self, project_id: str, asset_path: str) -> Path:
        project_root = self.project_dir(project_id).resolve()
        assets_root = (project_root / "assets").resolve()
        requested = (project_root / asset_path).resolve()
        if not requested.is_relative_to(assets_root):
            raise ApiError(404, "asset_not_found", f"asset 不存在：{asset_path}")
        return requested

    def figure_summaries(self, project_id: str) -> list[dict[str, Any]]:
        return [figure_summary(figure) for figure in self.list_figures(project_id)]

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
                ],
                cwd=frontend_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=45,
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
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)

    @staticmethod
    def write_text_atomic(path: Path, payload: str) -> None:
        tmp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
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
