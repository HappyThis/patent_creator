from __future__ import annotations

from fastapi import APIRouter

from ...schemas import CreateProjectRequest, ProjectSummary
from ...services.app_services import AppServices


def create_project_router(services: AppServices) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.post("/projects", response_model=ProjectSummary)
    async def create_project(payload: CreateProjectRequest) -> ProjectSummary:
        project = services.store.create_project(payload.title.strip())
        return ProjectSummary.model_validate(project.model_dump())

    @router.get("/projects/{project_id}", response_model=ProjectSummary)
    async def get_project(project_id: str) -> ProjectSummary:
        project = services.store.get_project(project_id)
        return ProjectSummary.model_validate(project.model_dump())

    return router
