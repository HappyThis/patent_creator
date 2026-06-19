from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ...schemas import ExportResponse
from ...services.app_services import AppServices


def create_export_router(services: AppServices) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/export/markdown", response_model=ExportResponse)
    async def export_markdown(project_id: str) -> ExportResponse:
        services.store.get_project(project_id)
        export_path = services.store.export_markdown(project_id)
        return ExportResponse(path=str(export_path.resolve()))

    @router.post("/export/docx", response_model=ExportResponse)
    async def export_docx(project_id: str) -> ExportResponse:
        services.store.get_project(project_id)
        export_path = services.store.export_docx(project_id)
        return ExportResponse(path=str(export_path.resolve()))

    @router.post("/export/docx/download")
    async def download_docx(project_id: str) -> FileResponse:
        services.store.get_project(project_id)
        export_path = services.store.export_docx(project_id).resolve()
        return FileResponse(
            path=export_path,
            filename=export_path.name,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"X-Export-Path": str(export_path)},
        )

    return router
