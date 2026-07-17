from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.domain.disclosure import build_render_ast
from app.domain.figures import MODEL_REVIEW_IMAGE_MAX_BYTES
from app.storage.workspace_store import WorkspaceStore
from app.tools.builtin.figure import MAX_FIGURE_ATTEMPTS_PER_ROUND

from helpers import make_tool_executor, make_tool_runtime_context, run_builtin_tool, section_id_by_title

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfeA\xd7S\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _review(
    attempt: int,
    successful_renders: int,
    *,
    consecutive_failures: int = 0,
    stable_version_available: bool = True,
) -> dict:
    return {
        "attempt": attempt,
        "limit": MAX_FIGURE_ATTEMPTS_PER_ROUND,
        "remaining": MAX_FIGURE_ATTEMPTS_PER_ROUND - attempt,
        "successful_renders": successful_renders,
        "consecutive_failures": consecutive_failures,
        "stable_version_available": stable_version_available,
    }

def _stub_figure_renderer(monkeypatch) -> None:
    def render(self: WorkspaceStore, *, input_path: Path, output_path: Path) -> dict:
        assert input_path.name == "diagram.drawio" or input_path.name.startswith(".diagram.")
        output_path.write_bytes(PNG_BYTES)
        return {"status": "success", "output": {"path": str(output_path)}}

    monkeypatch.setattr(WorkspaceStore, "_render_drawio_file", render)


def _sample_drawio_xml(title: str = "系统结构示意图") -> str:
    return f"""<mxfile host="embed.diagrams.net">
  <diagram name="{title}">
    <mxGraphModel page="1" pageWidth="1500" pageHeight="900">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="gateway" value="统一入口" style="rounded=0;whiteSpace=wrap;html=1;strokeColor=#111111;fillColor=#ffffff;" vertex="1" parent="1">
          <mxGeometry x="120" y="160" width="180" height="80" as="geometry" />
        </mxCell>
        <mxCell id="runtime" value="运行时" style="rounded=0;whiteSpace=wrap;html=1;strokeColor=#111111;fillColor=#ffffff;" vertex="1" parent="1">
          <mxGeometry x="420" y="160" width="180" height="80" as="geometry" />
        </mxCell>
        <mxCell id="edge-gateway-runtime" value="调用" style="endArrow=block;html=1;rounded=0;strokeColor=#111111;" edge="1" parent="1" source="gateway" target="runtime">
          <mxGeometry relative="1" as="geometry" />
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>"""


def test_disclosure_v3_initial_structure(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    disclosure = executor.store.get_disclosure(project_id)

    assert disclosure["meta"]["schema_version"] == "v3.3"
    assert "id_counters" not in disclosure["meta"]
    assert "title" not in disclosure["meta"]
    assert disclosure["sections"][0]["title"] == {"id": "blk_000001", "type": "title", "text": "发明名称"}
    assert [section["title"]["text"] for section in disclosure["sections"]] == [
        "发明名称",
        "技术领域",
        "背景技术",
        "现有技术及其缺陷",
        "要解决的技术问题",
        "技术方案",
        "具体实施方式",
        "关键创新点及权利要求建议",
        "附录",
    ]
    assert disclosure["sections"][-1]["title"]["text"] == "附录"
    assert disclosure["sections"][0]["blocks"][0] == {
        "id": "blk_000002",
        "type": "paragraph",
        "text": "一种图像检测方法",
    }
    assert "type" not in disclosure["sections"][0]
    assert "children" not in disclosure["sections"][0]


def test_disclosure_edit_supports_formula_block(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "技术方案")

    inserted = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "formula", "latex": r"\\frac{a+b}{c}"},
        },
    )
    assert inserted["status"] == "success"
    block_id = inserted["output"]["primary_block_id"]

    read = run_builtin_tool(
        executor,
        project_id,
        "disclosure_read_section",
        {"section_id": section, "block_ids": [block_id]},
    )
    assert read["status"] == "success"
    assert read["output"]["section"]["blocks"][0]["type"] == "formula"
    assert read["output"]["section"]["blocks"][0]["latex"] == r"\\frac{a+b}{c}"

    search = run_builtin_tool(executor, project_id, "disclosure_search", {"query": r"\\frac{a+b}{c}"})
    assert search["status"] == "success"
    assert search["output"]["matches"][0]["locator"]["block_id"] == block_id


def test_figure_kit_creates_listable_figure_and_checks_references(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context()
    drawio_xml = _sample_drawio_xml()
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示统一入口与策略解析模块之间的结构关系。",
            "drawio_xml": drawio_xml,
        },
        runtime_context=context,
    )

    assert create["status"] == "success"
    figure = create["output"]["figure"]
    assert figure["figure_id"] == "fig_000001"
    assert figure["ref"] == "figure:fig_000001"
    assert figure["markdown_ref"] == "[图1](figure:fig_000001)"
    assert set(figure) == {"figure_id", "ref", "label", "title", "markdown_ref"}
    assert create["output"]["attachments"] == [
        {
            "type": "render_image",
            "ref": "figure:fig_000001",
            "purpose": "visual_review",
            "drawio_updated_at": context.figure_drawio_versions["fig_000001"],
        }
    ]
    assert "截图" in create["output"]["message"]
    assert "自动检查不覆盖技术语义和最终视觉质量" in create["output"]["message"]
    assert "请结合 warnings 和截图判断是否需要修改" in create["output"]["message"]
    assert create["output"]["review"] == _review(1, 1)
    assert create["output"]["warnings"] == []
    assert create["output"]["normalization"]["applied"] is True
    assert "mxGraphModel.pageScale=1" in create["output"]["normalization"]["fields"]
    assert set(create["output"]) == {"figure", "message", "attachments", "warnings", "normalization", "review"}
    figure_dir = executor.store.project_dir(project_id) / "assets" / "figures" / "fig_000001"
    assert (figure_dir / "figure.json").exists()
    assert executor.store.figure_drawio_file(project_id, "fig_000001").exists()
    assert not (figure_dir / "diagram.html").exists()
    assert not (figure_dir / "geometry.json").exists()
    assert not (figure_dir / "geometry_report.json").exists()
    assert executor.store.figure_render_file(project_id, "fig_000001").read_bytes() == PNG_BYTES

    stored = executor.store.get_figure(project_id, "fig_000001")
    assert stored is not None
    assert stored["source"]["type"] == "drawio"
    assert stored["source"]["path"].startswith("assets/figures/fig_000001/.revisions/rev_")
    assert stored["source"]["path"].endswith("/diagram.drawio")
    assert stored["render"]["path"].startswith("assets/figures/fig_000001/.revisions/rev_")
    assert stored["render"]["path"].endswith("/render.png")
    assert stored["render"]["url"] == f"/api/projects/{project_id}/asset/{stored['render']['path']}"

    read = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "read", "ref": "figure:fig_000001"},
        runtime_context=context,
    )
    assert read["status"] == "success"
    assert "<mxGraphModel" in read["output"]["figure"]["drawio_xml"]
    assert "统一入口" in read["output"]["figure"]["drawio_xml"]
    assert "drawio_updated_at" not in read["output"]["figure"]
    assert read["output"]["attachments"] == create["output"]["attachments"]
    assert "read 未重新渲染，也不占渲染次数" in read["output"]["message"]
    assert "以最终正文重新核对技术关系覆盖" in read["output"]["message"]
    assert context.figure_review_states["fig_000001"] == {
        "attempts": 1,
        "successful_renders": 1,
        "consecutive_failures": 0,
    }

    listed = run_builtin_tool(executor, project_id, "figure_kit", {"action": "list"})
    assert listed["status"] == "success"
    assert listed["output"]["figures"][0]["markdown_ref"] == "[图1](figure:fig_000001)"
    assert set(listed["output"]["figures"][0]) == {"figure_id", "ref", "label", "title", "markdown_ref"}

    technical_solution = section_id_by_title(executor, project_id, "技术方案")
    inserted_ref = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": technical_solution,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": "如[图2](figure:fig_000001)所示，系统包括任务接收和策略解析。"},
        },
    )
    assert inserted_ref["status"] == "success"

    checked = run_builtin_tool(executor, project_id, "figure_kit", {"action": "check"})
    assert checked["status"] == "success"
    assert set(checked["output"]) == {"ok", "errors", "warnings", "scope_notice"}
    assert "不判断附图是否完整、正确地覆盖最终正文的技术关系" in checked["output"]["scope_notice"]
    assert "仍需根据最终正文进行语义复核" in checked["output"]["scope_notice"]
    assert checked["output"]["ok"] is False
    assert {item["code"] for item in checked["output"]["errors"]} == {"figure_label_mismatch"}
    assert {item["code"] for item in checked["output"]["warnings"]} == {"figure_not_displayed_in_appendix"}


def test_figure_read_reports_missing_screenshot_and_check_detects_missing_assets(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    created = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "write", "title": "资源检查", "reason": "验证资源缺失提示。", "drawio_xml": _sample_drawio_xml()},
    )
    assert created["status"] == "success"

    executor.store.figure_render_file(project_id, "fig_000001").unlink()
    read = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert read["status"] == "success"
    assert read["output"]["attachments"] == []
    assert "未附加视觉复盘图片" in read["output"]["message"]
    assert "不要声称已经查看截图" in read["output"]["message"]

    executor.store.figure_drawio_file(project_id, "fig_000001").unlink()
    checked = run_builtin_tool(executor, project_id, "figure_kit", {"action": "check"})
    assert checked["status"] == "success"
    assert checked["output"]["ok"] is False
    assert {item["code"] for item in checked["output"]["errors"]} == {
        "figure_source_missing",
        "figure_render_missing",
    }


def test_figure_kit_write_tracks_read_version_internally_and_detects_conflict(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context()
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示系统模块关系。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=context,
    )
    assert create["status"] == "success"

    fresh_context = make_tool_runtime_context("round_without_read")
    missing_read = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "ref": "figure:fig_000001",
            "reason": "整体重构系统结构。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=fresh_context,
    )
    assert missing_read["status"] == "failed"
    assert missing_read["output"]["code"] == "drawio_read_required"

    read = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "read", "ref": "figure:fig_000001"},
        runtime_context=fresh_context,
    )
    stale_version = fresh_context.figure_drawio_versions["fig_000001"]
    concurrent = executor.store.write_figure(
        project_id,
        "fig_000001",
        title="用户并发修改",
        drawio_xml=read["output"]["figure"]["drawio_xml"].replace("统一入口", "用户修改"),
        expected_drawio_updated_at=stale_version,
    )
    assert concurrent["status"] == "success"

    conflict = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "ref": "figure:fig_000001",
            "title": "更新后的结构示意图",
            "reason": "整体重构系统结构。",
            "drawio_xml": read["output"]["figure"]["drawio_xml"].replace("统一入口", "统一接入网关"),
        },
        runtime_context=fresh_context,
    )
    assert conflict["status"] == "failed"
    assert conflict["output"]["code"] == "drawio_conflict"

    current = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "read", "ref": "figure:fig_000001"},
        runtime_context=fresh_context,
    )
    next_xml = current["output"]["figure"]["drawio_xml"].replace("用户修改", "统一接入网关")
    written = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "ref": "figure:fig_000001",
            "title": "更新后的结构示意图",
            "reason": "将并发修改后的入口名称统一为接入网关。",
            "drawio_xml": next_xml,
        },
        runtime_context=fresh_context,
    )
    assert written["status"] == "success"
    assert written["output"]["figure"]["title"] == "更新后的结构示意图"
    assert written["output"]["review"] == _review(3, 1)
    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert "统一接入网关" in reread["output"]["figure"]["drawio_xml"]


def test_figure_kit_update_requires_read_and_applies_unique_replacements(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context()
    created = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示系统结构。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=context,
    )["output"]["figure"]

    missing_read_context = make_tool_runtime_context("round_without_read")
    missing_read = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "reason": "修正入口名称。",
            "edits": [{"old_text": "统一入口", "new_text": "统一接入网关"}],
        },
        runtime_context=missing_read_context,
    )
    assert missing_read["status"] == "failed"
    assert missing_read["output"]["code"] == "drawio_read_required"

    run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "read", "ref": "figure:fig_000001"},
        runtime_context=missing_read_context,
    )
    updated = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "title": "局部编辑后的结构示意图",
            "reason": "修正入口名称并调整节点位置，避免与连线拥挤。",
            "edits": [
                {"old_text": 'value="统一入口"', "new_text": 'value="统一接入网关"'},
                {"old_text": 'x="420" y="160"', "new_text": 'x="460" y="160"'},
            ],
        },
        runtime_context=missing_read_context,
    )

    assert updated["status"] == "success"
    assert updated["output"]["figure"]["title"] == "局部编辑后的结构示意图"
    assert "drawio_updated_at" not in created
    assert updated["output"]["attachments"][0]["purpose"] == "visual_review"
    assert updated["output"]["review"] == _review(2, 1)
    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert 'value="统一接入网关"' in reread["output"]["figure"]["drawio_xml"]
    assert 'x="460" y="160"' in reread["output"]["figure"]["drawio_xml"]


def test_figure_kit_update_requires_every_target_to_be_unique_and_is_atomic(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context()
    run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示系统结构。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=context,
    )
    read = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "read", "ref": "figure:fig_000001"},
        runtime_context=context,
    )
    original = read["output"]["figure"]
    original_timestamp = context.figure_drawio_versions["fig_000001"]
    original_revision = executor.store.figure_drawio_file(project_id, "fig_000001").parent

    missing = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "reason": "批量修正名称。",
            "edits": [
                {"old_text": "统一入口", "new_text": "不应落盘"},
                {"old_text": "不存在的目标", "new_text": "失败"},
            ],
        },
        runtime_context=context,
    )
    assert missing["status"] == "failed"
    assert missing["output"]["code"] == "figure_update_target_not_found"
    assert missing["output"]["update_index"] == 1

    not_unique = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "reason": "修正父节点引用。",
            "edits": [{"old_text": 'parent="1"', "new_text": 'parent="2"'}],
        },
        runtime_context=context,
    )
    assert not_unique["status"] == "failed"
    assert not_unique["output"]["code"] == "figure_update_target_not_unique"
    assert not_unique["output"]["match_count"] > 1

    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert executor.store.get_figure(project_id, "fig_000001")["source"]["updated_at"] == original_timestamp
    assert reread["output"]["figure"]["drawio_xml"] == original["drawio_xml"]
    revision_dirs = list((executor.store.figure_dir(project_id, "fig_000001") / ".revisions").glob("rev_*"))
    assert revision_dirs == [original_revision]


def test_figure_kit_rejects_invalid_drawio_xml(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)

    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "无效附图",
            "reason": "展示无效附图校验。",
            "drawio_xml": "<html></html>",
        },
    )

    assert create["status"] == "failed"
    assert create["output"]["code"] == "drawio_xml_format_invalid"


def test_figure_kit_requires_reason_and_has_no_rules_action(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)

    rules = run_builtin_tool(executor, project_id, "figure_kit", {"action": "rules"})
    missing_reason = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "write", "title": "系统结构示意图", "drawio_xml": _sample_drawio_xml()},
    )

    assert rules["status"] == "failed"
    assert rules["output"]["code"] == "invalid_tool_arguments"
    assert missing_reason["status"] == "failed"
    assert missing_reason["output"]["code"] == "figure_reason_required"


def test_figure_kit_reports_when_visual_attachment_is_too_large(tmp_path: Path, monkeypatch) -> None:
    def render(self: WorkspaceStore, *, input_path: Path, output_path: Path) -> dict:
        output_path.write_bytes(b"x" * (MODEL_REVIEW_IMAGE_MAX_BYTES + 1))
        return {"status": "success", "output": {"path": str(output_path)}}

    monkeypatch.setattr(WorkspaceStore, "_render_drawio_file", render)
    executor, project_id = make_tool_executor(tmp_path)
    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示系统结构。",
            "drawio_xml": _sample_drawio_xml(),
        },
    )

    assert result["status"] == "success"
    assert result["output"]["attachments"] == []
    assert "视觉复盘附件未附加" in result["output"]["message"]
    assert "不要声称已经看过截图" in result["output"]["message"]


def test_figure_kit_rejects_wrong_canvas_and_overflow(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    wrong_canvas = _sample_drawio_xml().replace('pageWidth="1500"', 'pageWidth="1200"')
    overflow = _sample_drawio_xml().replace('x="420"', 'x="1400"')

    wrong_canvas_result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "错误页面",
            "reason": "验证错误画布。",
            "drawio_xml": wrong_canvas,
        },
    )
    overflow_result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "节点越界",
            "reason": "验证节点越界。",
            "drawio_xml": overflow,
        },
    )

    assert wrong_canvas_result["output"]["code"] == "drawio_canvas_invalid"
    assert overflow_result["output"]["code"] == "drawio_canvas_overflow"


def test_figure_kit_only_accepts_canonical_mxfile_structure(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    graph_model = _sample_drawio_xml().split("<mxGraphModel", 1)[1].split("</mxGraphModel>", 1)[0]
    bare_xml = "<mxGraphModel" + graph_model + "</mxGraphModel>"

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "裸模型",
            "reason": "验证只接受工具示例中的唯一 XML 结构。",
            "drawio_xml": bare_xml,
        },
    )

    assert result["status"] == "failed"
    assert result["output"]["code"] == "drawio_xml_format_invalid"
    assert "不再接受裸 mxGraphModel" in result["output"]["message"]


def test_figure_kit_returns_all_hard_validation_errors_at_once(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    invalid = (
        _sample_drawio_xml()
        .replace('pageWidth="1500"', 'pageWidth="1200"')
        .replace('width="180" height="80"', 'width="-1" height="80"', 1)
        .replace(' source="gateway" target="runtime"', "")
        .replace('<mxCell id="runtime"', '<mxCell id="gateway"', 1)
    )

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "多错误图",
            "reason": "验证渲染前一次返回全部硬错误。",
            "drawio_xml": invalid,
        },
    )

    assert result["status"] == "failed"
    codes = {item["code"] for item in result["output"]["errors"]}
    assert {
        "drawio_cell_id_duplicate",
        "drawio_canvas_invalid",
        "drawio_semantic_edge_dangling",
    }.issubset(codes)
    assert len(result["output"]["errors"]) >= 3
    assert "已一次返回全部失败点" in result["output"]["message"]


def test_figure_kit_rejects_dangling_arrows_and_only_allows_explicitly_arrowless_auxiliary_edges(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    dangling = _sample_drawio_xml().replace(' source="gateway" target="runtime"', "")
    rejected = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "悬空连线",
            "reason": "验证普通语义连线不能悬空。",
            "drawio_xml": dangling,
        },
    )

    arrowed_auxiliary = dangling.replace(
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;",
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;edgeRole=auxiliary;",
    )
    arrowed_rejected = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "错误辅助箭头",
            "reason": "验证带箭头的辅助线不能绕过节点连接检查。",
            "drawio_xml": arrowed_auxiliary,
        },
    )
    arrowless_auxiliary = arrowed_auxiliary.replace("endArrow=block", "endArrow=none")
    accepted = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "辅助时间轴",
            "reason": "验证明确无箭头的非语义辅助线可以悬空。",
            "drawio_xml": arrowless_auxiliary,
        },
    )

    assert rejected["status"] == "failed"
    assert rejected["output"]["code"] == "drawio_semantic_edge_dangling"
    assert "edgeRole=auxiliary" in rejected["output"]["message"]
    assert arrowed_rejected["status"] == "failed"
    assert arrowed_rejected["output"]["code"] == "drawio_semantic_edge_dangling"
    assert accepted["status"] == "success"


def test_figure_kit_rejects_invalid_semantic_edge_references(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    invalid_reference = _sample_drawio_xml().replace('source="gateway"', 'source="missing-node"')

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "无效连线引用",
            "reason": "验证语义连线必须引用真实节点。",
            "drawio_xml": invalid_reference,
        },
    )

    assert result["status"] == "failed"
    assert result["output"]["code"] == "drawio_semantic_edge_reference_invalid"
    assert "missing-node" in result["output"]["message"]


def test_figure_kit_warns_when_first_waypoint_does_not_leave_anchor_orthogonally(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    invalid_route = _sample_drawio_xml().replace(
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;",
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;exitX=0.25;exitY=0.75;entryX=0;entryY=0.5;",
    ).replace(
        '<mxGeometry relative="1" as="geometry" />',
        '<mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="80" y="300"/><mxPoint x="380" y="200"/></Array></mxGeometry>',
    )

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "高风险端点走线",
            "reason": "验证显式锚点后的第一段必须从节点水平或垂直离开。",
            "drawio_xml": invalid_route,
        },
    )

    assert result["status"] == "success"
    route_warning = next(
        item for item in result["output"]["warnings"] if item["code"] == "drawio_semantic_edge_route_invalid"
    )
    assert route_warning["cell_id"] == "edge-gateway-runtime"
    assert "沿边界法向" in route_warning["message"]


def test_figure_kit_accepts_semantic_edge_with_orthogonal_endpoint_segments(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    valid_route = _sample_drawio_xml().replace(
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;",
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;exitX=0.5;exitY=1;entryX=0;entryY=0.5;",
    ).replace(
        '<mxGeometry relative="1" as="geometry" />',
        '<mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="210" y="300"/><mxPoint x="380" y="200"/></Array></mxGeometry>',
    )

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "有效端点走线",
            "reason": "验证连线首尾沿节点边界法向离开和进入。",
            "drawio_xml": valid_route,
        },
    )

    assert result["status"] == "success"
    assert "硬检查已通过" in result["output"]["message"]
    assert "自动检查不覆盖技术语义和最终视觉质量" in result["output"]["message"]


def test_figure_kit_warns_when_labeled_outer_route_is_too_close_to_page_edge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    risky_route = _sample_drawio_xml().replace(
        '<mxGeometry relative="1" as="geometry" />',
        '<mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="1450" y="720"/><mxPoint x="1450" y="100"/></Array></mxGeometry>',
    )

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "临近页面边界的带标签反馈线",
            "reason": "验证外绕线标签可能触发 Draw.io 横向扩页时会提前给出可操作警告。",
            "drawio_xml": risky_route,
        },
    )

    assert result["status"] == "success"
    warning = next(
        item for item in result["output"]["warnings"] if item["code"] == "drawio_edge_label_page_margin"
    )
    assert warning["cell_id"] == "edge-gateway-runtime"
    assert "距右侧页面边界仅约 50px" in warning["message"]
    assert "1500x450" in warning["message"]


def test_figure_kit_warns_for_4_to_12px_explicit_edge_segment(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    short_segment = _sample_drawio_xml().replace(
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;",
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;exitX=1;exitY=0.5;entryX=0;entryY=0.5;",
    ).replace(
        '<mxGeometry relative="1" as="geometry" />',
        '<mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="310" y="200"/><mxPoint x="380" y="200"/></Array></mxGeometry>',
    )

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "短折返走线",
            "reason": "验证过短显式线段会被拦截，避免形成微小阶梯或小折断。",
            "drawio_xml": short_segment,
        },
    )

    assert result["status"] == "success"
    short_warning = next(
        item for item in result["output"]["warnings"] if item["code"] == "drawio_semantic_edge_segment_too_short"
    )
    assert "只有约 10px" in short_warning["message"]
    assert "短线警告范围" in short_warning["message"]


def test_figure_kit_rejects_explicit_edge_segment_shorter_than_4px(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    short_segment = _sample_drawio_xml().replace(
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;",
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;exitX=1;exitY=0.5;entryX=0;entryY=0.5;",
    ).replace(
        '<mxGeometry relative="1" as="geometry" />',
        '<mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="302" y="200"/><mxPoint x="380" y="200"/></Array></mxGeometry>',
    )

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "接近零长度走线",
            "reason": "验证小于四像素的显式线段会阻断渲染。",
            "drawio_xml": short_segment,
        },
    )

    assert result["status"] == "failed"
    assert result["output"]["code"] == "drawio_semantic_edge_segment_invalid"
    assert any(item["code"] == "drawio_semantic_edge_segment_invalid" for item in result["output"]["errors"])


def test_figure_kit_warns_for_overlapping_explicit_semantic_edges(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    overlapping = """<mxfile host="embed.diagrams.net">
  <diagram name="重叠分支">
    <mxGraphModel page="1" pageWidth="1500" pageHeight="900">
      <root>
        <mxCell id="0" />
        <mxCell id="1" parent="0" />
        <mxCell id="decision" value="判断" vertex="1" parent="1">
          <mxGeometry x="120" y="160" width="180" height="80" as="geometry" />
        </mxCell>
        <mxCell id="normal" value="正常分支" vertex="1" parent="1">
          <mxGeometry x="420" y="160" width="180" height="80" as="geometry" />
        </mxCell>
        <mxCell id="fallback" value="降级分支" vertex="1" parent="1">
          <mxGeometry x="420" y="360" width="180" height="80" as="geometry" />
        </mxCell>
        <mxCell id="edge-normal" style="edgeStyle=orthogonalEdgeStyle;exitX=1;exitY=0.5;entryX=0;entryY=0.5;" edge="1" parent="1" source="decision" target="normal">
          <mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="350" y="200" /></Array></mxGeometry>
        </mxCell>
        <mxCell id="edge-fallback" style="edgeStyle=orthogonalEdgeStyle;exitX=1;exitY=0.5;entryX=0;entryY=0.5;" edge="1" parent="1" source="decision" target="fallback">
          <mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="350" y="200" /><mxPoint x="350" y="400" /></Array></mxGeometry>
        </mxCell>
      </root>
    </mxGraphModel>
  </diagram>
</mxfile>"""

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "重叠分支",
            "reason": "验证两个分支共享一段出线时会在渲染前被拒绝。",
            "drawio_xml": overlapping,
        },
    )

    assert result["status"] == "success"
    overlap_warning = next(
        item for item in result["output"]["warnings"] if item["code"] == "drawio_semantic_edge_overlap"
    )
    assert "edge-normal 与 edge-fallback" in overlap_warning["message"]
    assert "约 50px" in overlap_warning["message"]
    assert "从半空开始" in overlap_warning["message"]


def test_figure_kit_warns_for_overlapping_waypoint_segments_without_explicit_anchors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    overlapping = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" vertex="1" parent="1"><mxGeometry x="10" y="100" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="b" vertex="1" parent="1"><mxGeometry x="500" y="100" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="c" vertex="1" parent="1"><mxGeometry x="500" y="400" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="e1" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="200" y="200"/><mxPoint x="400" y="200"/></Array></mxGeometry></mxCell>
<mxCell id="e2" edge="1" parent="1" source="a" target="c"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="200" y="200"/><mxPoint x="400" y="200"/><mxPoint x="400" y="430"/></Array></mxGeometry></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "write", "title": "无锚点重叠", "reason": "验证 waypoint 重叠。", "drawio_xml": overlapping},
    )

    assert result["status"] == "success"
    overlap_warning = next(
        item for item in result["output"]["warnings"] if item["code"] == "drawio_semantic_edge_overlap"
    )
    assert "约 200px" in overlap_warning["message"]


def test_figure_kit_does_not_apply_rectangular_endpoint_math_to_rhombus(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    rhombus = _sample_drawio_xml().replace(
        'id="gateway" value="统一入口" style="',
        'id="gateway" value="统一入口" style="shape=rhombus;',
    ).replace(
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;",
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;exitX=0.8;exitY=0.7;",
    ).replace(
        '<mxGeometry relative="1" as="geometry" />',
        '<mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="330" y="260"/></Array></mxGeometry>',
    )

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "write", "title": "菱形端点", "reason": "验证非矩形端点不使用包围盒硬判。", "drawio_xml": rhombus},
    )

    assert result["status"] == "success"


def test_figure_kit_rejects_rhombus_anchor_that_floats_on_bounding_box(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    floating_anchor = _sample_drawio_xml().replace(
        'id="gateway" value="统一入口" style="',
        'id="gateway" value="统一入口" style="shape=rhombus;',
    ).replace(
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;",
        "endArrow=block;html=1;rounded=0;strokeColor=#111111;exitX=1;exitY=0.15;",
    )

    result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "菱形悬空锚点",
            "reason": "验证菱形包围盒上的离边锚点会在渲染前被拒绝。",
            "drawio_xml": floating_anchor,
        },
    )

    assert result["status"] == "failed"
    assert result["output"]["code"] == "drawio_semantic_edge_anchor_invalid"
    assert "不在菱形节点 gateway 的实际边界上" in result["output"]["message"]
    assert "|X-0.5|+|Y-0.5|=0.5" in result["output"]["message"]


def test_figure_update_rejects_legacy_invalid_xml_even_for_text_only_edit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context()
    created = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "write", "title": "旧图", "reason": "创建测试图。", "drawio_xml": _sample_drawio_xml()},
        runtime_context=context,
    )
    assert created["status"] == "success"
    drawio_file = executor.store.figure_drawio_file(project_id, "fig_000001")
    legacy_xml = drawio_file.read_text(encoding="utf-8").replace(' source="gateway" target="runtime"', "")
    drawio_file.write_text(legacy_xml, encoding="utf-8")

    run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "read", "ref": "figure:fig_000001"},
        runtime_context=context,
    )
    text_edit = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "reason": "只修改旧图文字，不改变既有几何。",
            "edits": [{"old_text": "统一入口", "new_text": "统一接入入口"}],
        },
        runtime_context=context,
    )
    assert text_edit["status"] == "failed"
    assert text_edit["output"]["code"] == "drawio_semantic_edge_dangling"
    assert text_edit["output"]["stable_version_preserved"] is True
    assert "统一接入入口" not in drawio_file.read_text(encoding="utf-8")

    geometry_edit = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "reason": "改变旧图几何时必须先修复既有硬错误。",
            "edits": [{"old_text": 'x="120" y="160"', "new_text": 'x="130" y="160"'}],
        },
        runtime_context=context,
    )
    assert geometry_edit["status"] == "failed"
    assert geometry_edit["output"]["code"] == "drawio_semantic_edge_dangling"
    assert geometry_edit["output"]["review"] == _review(3, 1, consecutive_failures=2)


def test_figure_write_render_failure_leaves_current_drawio_xml_unchanged(tmp_path: Path, monkeypatch) -> None:
    should_fail = {"value": False}

    def render(self: WorkspaceStore, *, input_path: Path, output_path: Path) -> dict:
        assert input_path.exists()
        if should_fail["value"]:
            return {"status": "failed", "output": {"code": "figure_render_failed", "message": "renderer failed"}}
        output_path.write_bytes(PNG_BYTES)
        return {"status": "success", "output": {"path": str(output_path)}}

    monkeypatch.setattr(WorkspaceStore, "_render_drawio_file", render)
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context()
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示系统结构。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=context,
    )
    assert create["status"] == "success"
    read = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "read", "ref": "figure:fig_000001"},
        runtime_context=context,
    )
    original_xml = read["output"]["figure"]["drawio_xml"]
    original_timestamp = context.figure_drawio_versions["fig_000001"]
    modified_xml = original_xml.replace("统一入口", "失败更新不应落盘")

    should_fail["value"] = True
    write = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "ref": "figure:fig_000001",
            "title": "失败标题",
            "reason": "整体更新系统结构。",
            "drawio_xml": modified_xml,
        },
        runtime_context=context,
    )

    assert write["status"] == "failed"
    assert write["output"]["code"] == "figure_render_failed"
    assert context.figure_review_states["fig_000001"] == {
        "attempts": 2,
        "successful_renders": 1,
        "consecutive_failures": 1,
    }
    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert reread["output"]["figure"]["title"] == "系统结构示意图"
    assert executor.store.get_figure(project_id, "fig_000001")["source"]["updated_at"] == original_timestamp
    assert reread["output"]["figure"]["drawio_xml"] == original_xml
    assert executor.store.figure_render_file(project_id, "fig_000001").read_bytes() == PNG_BYTES


def test_concurrent_figure_writes_allow_only_one_timestamp_winner(tmp_path: Path, monkeypatch) -> None:
    first_render_started = threading.Event()
    allow_first_render = threading.Event()

    def render(self: WorkspaceStore, *, input_path: Path, output_path: Path) -> dict:
        if "更新 A" in input_path.read_text(encoding="utf-8"):
            first_render_started.set()
            assert allow_first_render.wait(timeout=5)
        output_path.write_bytes(PNG_BYTES)
        return {"status": "success", "output": {"path": str(output_path)}}

    monkeypatch.setattr(WorkspaceStore, "_render_drawio_file", render)
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context()
    run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示系统结构。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=context,
    )
    created_timestamp = context.figure_drawio_versions["fig_000001"]

    def write(title: str) -> dict:
        return executor.store.write_figure(
            project_id,
            "fig_000001",
            title=title,
            drawio_xml=_sample_drawio_xml().replace("统一入口", title),
            expected_drawio_updated_at=created_timestamp,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, "更新 A")
        assert first_render_started.wait(timeout=5)
        second = pool.submit(write, "更新 B")
        allow_first_render.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert sorted(result["status"] for result in results) == ["failed", "success"]
    failed = next(result for result in results if result["status"] == "failed")
    assert failed["output"]["code"] == "drawio_conflict"


def test_figure_metadata_failure_keeps_previous_revision(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context()
    run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示系统结构。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=context,
    )
    created_timestamp = context.figure_drawio_versions["fig_000001"]
    old_drawio_file = executor.store.figure_drawio_file(project_id, "fig_000001")
    old_render_file = executor.store.figure_render_file(project_id, "fig_000001")
    old_xml = old_drawio_file.read_text(encoding="utf-8")
    old_render = old_render_file.read_bytes()

    def fail_metadata_write(path: Path, payload: dict) -> None:
        raise OSError("metadata unavailable")

    monkeypatch.setattr(executor.store, "write_json_atomic", fail_metadata_write)
    result = executor.store.write_figure(
        project_id,
        "fig_000001",
        title="不应提交",
        drawio_xml=_sample_drawio_xml().replace("统一入口", "不应提交"),
        expected_drawio_updated_at=created_timestamp,
    )

    assert result["status"] == "failed"
    assert result["output"]["code"] == "figure_storage_failed"
    assert executor.store.get_figure(project_id, "fig_000001")["title"] == "系统结构示意图"
    assert executor.store.figure_drawio_file(project_id, "fig_000001") == old_drawio_file
    assert old_drawio_file.read_text(encoding="utf-8") == old_xml
    assert old_render_file.read_bytes() == old_render
    revision_dirs = list((executor.store.figure_dir(project_id, "fig_000001") / ".revisions").glob("rev_*"))
    assert revision_dirs == [old_drawio_file.parent]


def test_figure_kit_limits_all_write_update_attempts_per_figure_and_resets_next_round(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    first_round = make_tool_runtime_context("round_1")
    created = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "系统结构示意图",
            "reason": "展示系统结构。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=first_round,
    )
    current_name = "统一入口"
    updates: list[dict] = []
    for index in range(2, MAX_FIGURE_ATTEMPTS_PER_ROUND + 1):
        next_name = f"统一入口-{index}"
        updates.append(
            run_builtin_tool(
                executor,
                project_id,
                "figure_kit",
                {
                    "action": "update",
                    "ref": "figure:fig_000001",
                    "reason": f"执行第 {index} 次成功渲染以验证安全上限。",
                    "edits": [{"old_text": current_name, "new_text": next_name}],
                },
                runtime_context=first_round,
            )
        )
        current_name = next_name
    rejected = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "reason": "尝试超过八次渲染。",
            "edits": [{"old_text": current_name, "new_text": "不应落盘"}],
        },
        runtime_context=first_round,
    )

    assert created["output"]["review"] == _review(1, 1)
    assert [item["output"]["review"]["attempt"] for item in updates] == list(
        range(2, MAX_FIGURE_ATTEMPTS_PER_ROUND + 1)
    )
    assert all(item["status"] == "success" for item in updates)
    assert rejected["status"] == "failed"
    assert rejected["output"]["code"] == "figure_attempt_limit_reached"
    assert rejected["output"]["review"] == _review(
        MAX_FIGURE_ATTEMPTS_PER_ROUND,
        MAX_FIGURE_ATTEMPTS_PER_ROUND,
    )
    assert rejected["output"]["stable_version_preserved"] is True
    assert "不应落盘" not in executor.store.read_figure_drawio_xml(project_id, "fig_000001")

    second_round = make_tool_runtime_context("round_2")
    run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "read", "ref": "figure:fig_000001"},
        runtime_context=second_round,
    )
    resumed = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "reason": "用户在新请求中继续调整入口名称。",
            "edits": [{"old_text": current_name, "new_text": "统一接入网关"}],
        },
        runtime_context=second_round,
    )
    assert resumed["status"] == "success"
    assert resumed["output"]["review"] == _review(1, 1)


def test_figure_kit_counts_preflight_failures_toward_attempt_limit(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    context = make_tool_runtime_context("round_failures")
    dangling = _sample_drawio_xml().replace(' source="gateway" target="runtime"', "")

    failures = [
        run_builtin_tool(
            executor,
            project_id,
            "figure_kit",
            {
                "action": "write",
                "title": "失败重试",
                "reason": f"第 {attempt} 次验证预检失败也计入上限。",
                "drawio_xml": dangling,
            },
            runtime_context=context,
        )
        for attempt in range(1, MAX_FIGURE_ATTEMPTS_PER_ROUND + 1)
    ]
    rejected = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "不应创建",
            "reason": "尝试超过上限。",
            "drawio_xml": _sample_drawio_xml(),
        },
        runtime_context=context,
    )

    assert all(item["status"] == "failed" for item in failures)
    assert [item["output"]["review"]["attempt"] for item in failures] == list(
        range(1, MAX_FIGURE_ATTEMPTS_PER_ROUND + 1)
    )
    assert failures[-1]["output"]["review"] == _review(
        MAX_FIGURE_ATTEMPTS_PER_ROUND,
        0,
        consecutive_failures=MAX_FIGURE_ATTEMPTS_PER_ROUND,
        stable_version_available=False,
    )
    assert rejected["output"]["code"] == "figure_attempt_limit_reached"
    assert rejected["output"]["stable_version_preserved"] is False
    assert executor.store.list_figures(project_id) == []


def test_figure_kit_rejects_arguments_outside_schema(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)

    extra_field = run_builtin_tool(executor, project_id, "figure_kit", {"action": "list", "unused": True})
    invalid_action = run_builtin_tool(executor, project_id, "figure_kit", {"action": "rename"})
    removed_create = run_builtin_tool(executor, project_id, "figure_kit", {"action": "create"})
    removed_edit = run_builtin_tool(executor, project_id, "figure_kit", {"action": "edit"})
    replace_all = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "reason": "修正 XML。",
            "edits": [{"old_text": "x", "new_text": "y", "replace_all": True}],
        },
    )

    assert extra_field["status"] == "failed"
    assert extra_field["output"]["code"] == "invalid_tool_arguments"
    assert "unused" in extra_field["output"]["message"]
    assert invalid_action["status"] == "failed"
    assert invalid_action["output"]["code"] == "invalid_tool_arguments"
    assert "action" in invalid_action["output"]["message"]
    assert removed_create["status"] == "failed"
    assert removed_create["output"]["code"] == "invalid_tool_arguments"
    assert removed_edit["status"] == "failed"
    assert removed_edit["output"]["code"] == "invalid_tool_arguments"
    assert replace_all["status"] == "failed"
    assert replace_all["output"]["code"] == "invalid_tool_arguments"
    assert "replace_all" in replace_all["output"]["message"]


def test_figure_block_only_allowed_in_appendix_and_renders_with_asset(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    figure = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "title": "代理执行流程示意图",
            "reason": "展示代理执行流程。",
            "drawio_xml": _sample_drawio_xml("代理执行流程示意图"),
        },
    )["output"]["figure"]

    technical_solution = section_id_by_title(executor, project_id, "技术方案")
    rejected = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": technical_solution,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "figure", "figure_id": figure["figure_id"]},
        },
    )
    assert rejected["status"] == "failed"
    assert rejected["output"]["code"] == "figure_block_outside_appendix"

    appendix = section_id_by_title(executor, project_id, "附录")
    inserted = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": appendix,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "figure", "figure_id": figure["figure_id"]},
        },
    )
    assert inserted["status"] == "success"

    render_ast = build_render_ast(
        executor.store.get_disclosure(project_id),
        figures=executor.store.list_figures(project_id),
    )
    appendix_node = next(node for node in render_ast["children"] if node["title"] == "附录")
    assert appendix_node["children"][0]["type"] == "figure"
    assert render_ast["figures"][0]["label"] == "图1"
    assert render_ast["figures"][0]["source"]["type"] == "drawio"
    assert render_ast["figures"][0]["source"]["path"].startswith("assets/figures/fig_000001/.revisions/rev_")
    assert render_ast["figures"][0]["render"]["url"].startswith(
        f"/api/projects/{project_id}/asset/assets/figures/fig_000001/.revisions/rev_"
    )


def test_disclosure_outline_search_and_read_section(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    technical_problem_id = section_id_by_title(executor, project_id, "要解决的技术问题")

    edit_result = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": technical_problem_id,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": "低算力终端上的实时检测时延较高。"},
        },
    )
    assert edit_result["status"] == "success"
    block_id = edit_result["output"]["primary_block_id"]

    outline = run_builtin_tool(executor, project_id, "disclosure_outline", {"limit": 5, "offset": 0})
    assert outline["status"] == "success"
    assert outline["output"]["returned"] == 5
    assert outline["output"]["truncated"] is True
    first_item = outline["output"]["items"][0]
    assert first_item["kind"] == "section"
    assert first_item["title"]["locator"]["block_type"] == "title"

    search = run_builtin_tool(executor, project_id, "disclosure_search", {"query": "低算力"})
    assert search["status"] == "success"
    assert search["output"]["matches"][0]["locator"]["block_id"] == block_id
    assert search["output"]["matches"][0]["locator"]["section_id"] == technical_problem_id

    read = run_builtin_tool(
        executor,
        project_id,
        "disclosure_read_section",
        {"section_id": technical_problem_id, "block_ids": [block_id]},
    )
    assert read["status"] == "success"
    assert read["output"]["section"]["blocks"][0]["text"] == "低算力终端上的实时检测时延较高。"
    assert read["output"]["section"]["blocks"][0]["locator"]["index"] == 1


def test_disclosure_search_supports_regex_and_pagination(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "背景技术")
    for text in ["Alpha 创新内核", "beta 创新内核", "其他内容"]:
        result = run_builtin_tool(
            executor,
            project_id,
            "disclosure_edit",
            {
                "section_id": section,
                "operation": "insert_block",
                "position": {"mode": "end"},
                "block": {"type": "paragraph", "text": text},
            },
        )
        assert result["status"] == "success"

    search = run_builtin_tool(
        executor,
        project_id,
        "disclosure_search",
        {"query": "alpha|BETA", "regex": True, "limit": 1, "offset": 0},
    )
    assert search["status"] == "success"
    assert search["output"]["returned"] == 1
    assert search["output"]["total"] == 2
    assert search["output"]["truncated"] is True
    assert search["output"]["next_offset"] == 1


def test_disclosure_read_section_paginates_title_and_direct_blocks(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "技术方案")
    for text in ["第一段", "第二段"]:
        assert run_builtin_tool(
            executor,
            project_id,
            "disclosure_edit",
            {
                "section_id": section,
                "operation": "insert_block",
                "position": {"mode": "end"},
                "block": {"type": "paragraph", "text": text},
            },
        )["status"] == "success"

    first_page = run_builtin_tool(executor, project_id, "disclosure_read_section", {"section_id": section, "limit": 2})
    assert first_page["status"] == "success"
    assert first_page["output"]["returned"] == 2
    assert first_page["output"]["total"] == 3
    assert first_page["output"]["section"]["blocks"][0]["type"] == "title"
    assert first_page["output"]["section"]["blocks"][1]["text"] == "第一段"
    assert first_page["output"]["next_offset"] == 2

    second_page = run_builtin_tool(
        executor,
        project_id,
        "disclosure_read_section",
        {"section_id": section, "limit": 2, "offset": first_page["output"]["next_offset"]},
    )
    assert second_page["status"] == "success"
    assert second_page["output"]["section"]["blocks"][0]["text"] == "第二段"
    assert second_page["output"]["truncated"] is False


def test_disclosure_read_section_returns_writing_guide_for_empty_top_level_fixed_section(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "技术方案")

    read = run_builtin_tool(executor, project_id, "disclosure_read_section", {"section_id": section})

    assert read["status"] == "success"
    assert read["output"]["writing_guide_markdown"].startswith("## 技术方案写作要领")
    assert "专利代理人员" in read["output"]["writing_guide_markdown"]
    assert "不要求固定写作顺序" in read["output"]["writing_guide_markdown"]
    assert "避免只写“系统根据状态进行处理”" in read["output"]["writing_guide_markdown"]
    assert "两个以上独立机制" in read["output"]["writing_guide_markdown"]
    assert "附图可自由表达结构、边界、流向、状态、依赖或协同关系" in read["output"]["writing_guide_markdown"]
    assert "不要求套用固定图型" in read["output"]["writing_guide_markdown"]
    assert "流程图" not in read["output"]["writing_guide_markdown"]
    assert "架构图" not in read["output"]["writing_guide_markdown"]
    assert "时序图" not in read["output"]["writing_guide_markdown"]


def test_disclosure_read_section_omits_writing_guide_for_filled_or_child_section(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "技术方案")

    inserted_block = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": "系统先接收任务条目，再创建隔离工作空间。"},
        },
    )
    assert inserted_block["status"] == "success"

    filled = run_builtin_tool(executor, project_id, "disclosure_read_section", {"section_id": section})
    assert filled["status"] == "success"
    assert "writing_guide_markdown" not in filled["output"]

    child = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_section",
            "position": {"mode": "end"},
            "section": {"title": "调度流程"},
        },
    )
    assert child["status"] == "success"

    child_read = run_builtin_tool(
        executor,
        project_id,
        "disclosure_read_section",
        {"section_id": child["output"]["primary_section_id"]},
    )
    assert child_read["status"] == "success"
    assert "writing_guide_markdown" not in child_read["output"]


def test_disclosure_edit_block_and_section_operations(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "技术方案")

    inserted = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_section",
            "position": {"mode": "end"},
            "section": {"title": "创新内核门禁机制"},
        },
    )
    assert inserted["status"] == "success"
    child_section_id = inserted["output"]["primary_section_id"]
    child_title_block_id = inserted["output"]["primary_block_id"]

    renamed = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": child_section_id,
            "operation": "replace_block",
            "block_id": child_title_block_id,
            "block": {"type": "title", "text": "创新内核前置门禁机制"},
        },
    )
    assert renamed["status"] == "success"

    deleted = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {"section_id": section, "operation": "delete_section", "target_section_id": child_section_id},
    )
    assert deleted["status"] == "success"
    disclosure = executor.store.get_disclosure(project_id)
    parent = next(item for item in disclosure["sections"] if item["id"] == section)
    assert parent["sections"] == []


def test_disclosure_edit_rejects_cross_section_block_operation(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    background = section_id_by_title(executor, project_id, "背景技术")
    solution = section_id_by_title(executor, project_id, "技术方案")
    inserted = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": background,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": "背景段落。"},
        },
    )
    block_id = inserted["output"]["primary_block_id"]

    rejected = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": solution,
            "operation": "replace_block",
            "block_id": block_id,
            "block": {"type": "paragraph", "text": "不应跨 section 替换。"},
        },
    )
    assert rejected["status"] == "failed"
    assert rejected["output"]["code"] == "block_not_found"


def test_disclosure_edit_rejects_title_delete_and_before_title_insert(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "技术方案")
    read = run_builtin_tool(executor, project_id, "disclosure_read_section", {"section_id": section, "limit": 1})
    title_block_id = read["output"]["section"]["title"]["locator"]["block_id"]

    delete_title = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {"section_id": section, "operation": "delete_block", "block_id": title_block_id},
    )
    assert delete_title["status"] == "failed"

    before_title = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": {"mode": "before", "block_id": title_block_id},
            "block": {"type": "paragraph", "text": "不允许插入。"},
        },
    )
    assert before_title["status"] == "failed"


def test_disclosure_edit_rejects_overlong_single_edit(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "要解决的技术问题")

    edit_result = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": "超" * 1501},
        },
    )

    assert edit_result["status"] == "failed"
    assert edit_result["output"]["code"] == "edit_too_large"
    assert "不能超过 1500 字" in edit_result["output"]["message"]


def test_disclosure_edit_normalizes_stringified_json_containers(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "要解决的技术问题")

    appended = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": json.dumps({"mode": "end"}, ensure_ascii=False),
            "block": json.dumps({"type": "paragraph", "text": "字符串化 block 会被还原。"}, ensure_ascii=False),
        },
    )
    assert appended["status"] == "success"

    disclosure = executor.store.get_disclosure(project_id)
    problem = next(item for item in disclosure["sections"] if item["id"] == section)
    assert problem["blocks"][0]["text"] == "字符串化 block 会被还原。"


def test_disclosure_edit_do_not_parse_text_as_json(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    section = section_id_by_title(executor, project_id, "要解决的技术问题")
    json_like_text = '{"保留为正文": true}'

    result = run_builtin_tool(
        executor,
        project_id,
        "disclosure_edit",
        {
            "section_id": section,
            "operation": "insert_block",
            "position": {"mode": "end"},
            "block": {"type": "paragraph", "text": json_like_text},
        },
    )

    assert result["status"] == "success"
    disclosure = executor.store.get_disclosure(project_id)
    problem = next(item for item in disclosure["sections"] if item["id"] == section)
    assert problem["blocks"][0]["text"] == json_like_text


def test_disclosure_edit_rejects_legacy_document_tool_names(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    result = run_builtin_tool(executor, project_id, "document_append_block", {})
    assert result["status"] == "failed"
    assert result["output"]["code"] == "unsupported_tool"
