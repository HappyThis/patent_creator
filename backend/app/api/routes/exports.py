from __future__ import annotations

from fastapi import APIRouter

from ...schemas import ExportResponse
from ...services.app_services import AppServices


def create_export_router(services: AppServices) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/export/markdown", response_model=ExportResponse)
    async def export_markdown(project_id: str) -> ExportResponse:
        services.store.get_project(project_id)
        export_path = services.store.export_markdown(project_id)
        return ExportResponse(path=str(export_path.resolve()))

    return router
