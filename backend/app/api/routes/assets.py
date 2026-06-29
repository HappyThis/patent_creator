from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ...core import ApiError
from ...services.app_services import AppServices


def create_asset_router(services: AppServices) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/asset/{asset_path:path}")
    async def get_asset(project_id: str, asset_path: str) -> FileResponse:
        path = services.store.project_asset_file(project_id, asset_path)
        if not path.is_file() or path.suffix.lower() != ".png":
            raise ApiError(404, "asset_not_found", f"asset 不存在：{asset_path}")
        return FileResponse(path=path, media_type="image/png")

    return router
