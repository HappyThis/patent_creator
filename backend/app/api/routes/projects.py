from __future__ import annotations

from fastapi import APIRouter

from ...schemas import ContextUsageSummary, ProjectListResponse, ProjectSummary
from ...services.app_services import AppServices


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

    @router.get("/projects/{project_id}", response_model=ProjectSummary)
    async def get_project(project_id: str) -> ProjectSummary:
        return build_project_summary(project_id)

    return router
