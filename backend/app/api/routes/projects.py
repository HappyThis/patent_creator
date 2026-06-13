from __future__ import annotations

from fastapi import APIRouter

from ...core import ApiError
from ...schemas import (
    ContextUsageSummary,
    ProjectCreateRequest,
    ProjectDeleteResponse,
    ProjectListResponse,
    ProjectSummary,
    ProjectUpdateRequest,
)
from ...services.app_services import AppServices
from ...storage.workspace_store import DEFAULT_DISCLOSURE_TITLE


def create_project_router(services: AppServices) -> APIRouter:
    router = APIRouter(prefix="/api")

    def build_project_summary(project_id: str) -> ProjectSummary:
        project = services.store.get_project(project_id)
        summary = ProjectSummary.model_validate(project.model_dump())
        if project.active_session_id:
            usage = services.context_manager.context_usage(project_id, project.active_session_id)
            summary.active_session_context = ContextUsageSummary.model_validate(usage.model_dump()) if usage else None
        return summary

    @router.get("/projects", response_model=ProjectListResponse)
    async def list_projects() -> ProjectListResponse:
        projects = services.store.list_projects_with_current_first()
        return ProjectListResponse(
            projects=[build_project_summary(project.project_id) for project in projects]
        )

    @router.post("/projects", response_model=ProjectSummary)
    async def create_project(payload: ProjectCreateRequest) -> ProjectSummary:
        project_name = payload.project_name.strip()
        if not project_name:
            raise ApiError(422, "invalid_project_name", "项目名不能为空。")
        disclosure_title = payload.disclosure_title.strip() if payload.disclosure_title else None
        project = services.store.create_project(
            project_name,
            disclosure_title=disclosure_title or DEFAULT_DISCLOSURE_TITLE,
        )
        return build_project_summary(project.project_id)

    @router.get("/projects/{project_id}", response_model=ProjectSummary)
    async def get_project(project_id: str) -> ProjectSummary:
        return build_project_summary(project_id)

    @router.patch("/projects/{project_id}", response_model=ProjectSummary)
    async def rename_project(project_id: str, payload: ProjectUpdateRequest) -> ProjectSummary:
        project_name = payload.project_name.strip()
        if not project_name:
            raise ApiError(422, "invalid_project_name", "项目名不能为空。")
        project = services.store.rename_project(project_id, project_name)
        return build_project_summary(project.project_id)

    @router.delete("/projects/{project_id}", response_model=ProjectDeleteResponse)
    async def delete_project(project_id: str) -> ProjectDeleteResponse:
        next_project_id = services.store.delete_project(project_id)
        return ProjectDeleteResponse(deleted=True, project_id=project_id, next_project_id=next_project_id)

    return router
