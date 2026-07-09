from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ...core import ApiError, now_iso
from ...domain.disclosure import build_outline_items, build_render_ast, find_block, find_section
from ...domain.figures import figure_summary
from ...schemas import OutlineResponse, RenderResponse
from ...services.app_services import AppServices


class FigureDrawioSaveRequest(BaseModel):
    expected_drawio_updated_at: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=120)
    drawio_xml: str = Field(min_length=1)


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

    @router.get("/figures/{figure_id}/drawio")
    async def get_figure_drawio(project_id: str, figure_id: str) -> dict:
        figure = services.store.get_figure(project_id, figure_id)
        if figure is None:
            raise ApiError(404, "figure_not_found", f"figure 不存在：{figure_id}")
        drawio_xml = services.store.read_figure_drawio_xml(project_id, figure_id)
        if drawio_xml is None:
            raise ApiError(404, "figure_drawio_not_found", f"figure draw.io XML 不存在：{figure_id}")
        payload = figure_summary(figure)
        payload["drawio_xml"] = drawio_xml
        return {"figure": payload}

    @router.put("/figures/{figure_id}/drawio")
    async def save_figure_drawio(project_id: str, figure_id: str, payload: FigureDrawioSaveRequest) -> dict:
        result = services.store.update_figure(
            project_id,
            figure_id,
            title=payload.title.strip() if payload.title is not None else None,
            drawio_xml=payload.drawio_xml,
            expected_drawio_updated_at=payload.expected_drawio_updated_at,
        )
        if result.get("status") == "failed":
            _raise_figure_drawio_error(result["output"])
        project = services.store.get_project(project_id)
        project.updated_at = now_iso()
        services.store.save_project(project)
        figure = result["output"]["figure"]
        summary = figure_summary(figure)
        render = figure.get("render") if isinstance(figure.get("render"), dict) else {}
        summary["render_url"] = render.get("url")
        return {"figure": summary}

    return router


def _raise_figure_drawio_error(output: dict[str, Any]) -> None:
    code = str(output.get("code") or "figure_drawio_error")
    message = str(output.get("message") or "附图 draw.io XML 保存失败。")
    if code == "figure_not_found":
        raise ApiError(404, code, message)
    if code == "drawio_conflict":
        raise ApiError(409, code, message)
    if code in {"drawio_xml_validation_failed", "drawio_read_required", "drawio_xml_required"}:
        raise ApiError(422, code, message)
    raise ApiError(500, code, message)
