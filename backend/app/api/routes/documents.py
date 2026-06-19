from __future__ import annotations

from fastapi import APIRouter, Query

from ...core import ApiError
from ...domain.disclosure import build_outline_items, build_render_ast, find_block, find_section
from ...schemas import OutlineResponse, RenderResponse
from ...services.app_services import AppServices


def create_document_router(services: AppServices) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/outline", response_model=OutlineResponse)
    async def get_outline(project_id: str) -> OutlineResponse:
        disclosure = services.store.get_disclosure(project_id)
        return OutlineResponse(sections=build_outline_items(disclosure["sections"]))

    @router.get("/render", response_model=RenderResponse)
    async def get_render(
        project_id: str,
        focus_section_id: str | None = Query(default=None),
        focus_block_id: str | None = Query(default=None),
    ) -> RenderResponse:
        project = services.store.get_project(project_id)
        disclosure = services.store.get_disclosure(project_id)

        active_section_id = focus_section_id
        active_block_id = focus_block_id
        if focus_section_id and not find_section(disclosure["sections"], focus_section_id):
            raise ApiError(404, "section_not_found", f"section_id 不存在：{focus_section_id}")
        if focus_block_id:
            block_match = find_block(disclosure["sections"], focus_block_id)
            if not block_match:
                raise ApiError(404, "block_not_found", f"block_id 不存在：{focus_block_id}")
            active_section_id = block_match[0]["id"]

        return RenderResponse(
            render_ast=build_render_ast(disclosure, figures=services.store.list_figures(project_id)),
            active_section_id=active_section_id,
            active_block_id=active_block_id,
            updated_at=project.updated_at,
        )

    @router.get("/document")
    async def get_document(project_id: str) -> dict:
        return services.store.get_disclosure(project_id)

    return router
