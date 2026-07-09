from __future__ import annotations

import json
from pathlib import Path

from app.domain.disclosure import build_render_ast
from app.storage.workspace_store import WorkspaceStore

from helpers import make_tool_executor, run_builtin_tool, section_id_by_title

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?"
    b"\x00\x05\xfe\x02\xfeA\xd7S\x84\x00\x00\x00\x00IEND\xaeB`\x82"
)

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
    drawio_xml = _sample_drawio_xml()
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "title": "系统结构示意图",
            "drawio_xml": drawio_xml,
        },
    )

    assert create["status"] == "success"
    figure = create["output"]["figure"]
    assert figure["figure_id"] == "fig_000001"
    assert figure["ref"] == "figure:fig_000001"
    assert figure["markdown_ref"] == "[图1](figure:fig_000001)"
    assert figure["caption"] == "图1 系统结构示意图"
    assert figure["drawio_updated_at"]
    assert create["output"]["attachments"] == [
        {"type": "render_image", "ref": "figure:fig_000001", "purpose": "visual_review"}
    ]
    assert "截图" in create["output"]["message"]
    assert set(create["output"]) == {"figure", "message", "attachments"}
    figure_dir = executor.store.project_dir(project_id) / "assets" / "figures" / "fig_000001"
    assert (figure_dir / "figure.json").exists()
    assert (figure_dir / "diagram.drawio").exists()
    assert not (figure_dir / "diagram.html").exists()
    assert not (figure_dir / "geometry.json").exists()
    assert not (figure_dir / "geometry_report.json").exists()
    assert (figure_dir / "render.png").read_bytes() == PNG_BYTES

    stored = executor.store.get_figure(project_id, "fig_000001")
    assert stored is not None
    assert stored["source"]["type"] == "drawio"
    assert stored["source"]["path"] == "assets/figures/fig_000001/diagram.drawio"
    assert stored["render"]["path"] == "assets/figures/fig_000001/render.png"
    assert stored["render"]["url"] == f"/api/projects/{project_id}/asset/assets/figures/fig_000001/render.png"

    read = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert read["status"] == "success"
    assert "<mxGraphModel" in read["output"]["figure"]["drawio_xml"]
    assert "统一入口" in read["output"]["figure"]["drawio_xml"]
    assert read["output"]["figure"]["drawio_updated_at"] == figure["drawio_updated_at"]

    listed = run_builtin_tool(executor, project_id, "figure_kit", {"action": "list"})
    assert listed["status"] == "success"
    assert listed["output"]["figures"][0]["markdown_ref"] == "[图1](figure:fig_000001)"
    assert "drawio_updated_at" in listed["output"]["figures"][0]

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
    assert checked["output"]["ok"] is False
    assert {item["code"] for item in checked["output"]["errors"]} == {"figure_label_mismatch"}
    assert {item["code"] for item in checked["output"]["warnings"]} == {"figure_not_displayed_in_appendix"}


def test_figure_kit_update_requires_read_timestamp_and_detects_conflict(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "create", "title": "系统结构示意图", "drawio_xml": _sample_drawio_xml()},
    )
    assert create["status"] == "success"
    figure = create["output"]["figure"]

    missing_timestamp = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "drawio_xml": _sample_drawio_xml(),
        },
    )
    assert missing_timestamp["status"] == "failed"
    assert missing_timestamp["output"]["code"] == "drawio_read_required"

    conflict = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "expected_drawio_updated_at": "2000-01-01T00:00:00+00:00",
            "drawio_xml": _sample_drawio_xml(),
        },
    )
    assert conflict["status"] == "failed"
    assert conflict["output"]["code"] == "drawio_conflict"
    assert conflict["output"]["current_drawio_updated_at"] == figure["drawio_updated_at"]

    read = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    next_xml = read["output"]["figure"]["drawio_xml"].replace("统一入口", "统一接入网关")
    updated = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "title": "更新后的结构示意图",
            "expected_drawio_updated_at": read["output"]["figure"]["drawio_updated_at"],
            "drawio_xml": next_xml,
        },
    )

    assert updated["status"] == "success"
    assert updated["output"]["figure"]["title"] == "更新后的结构示意图"
    assert updated["output"]["figure"]["drawio_updated_at"] != figure["drawio_updated_at"]
    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert "统一接入网关" in reread["output"]["figure"]["drawio_xml"]


def test_figure_kit_rejects_invalid_drawio_xml(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)

    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "title": "无效附图",
            "drawio_xml": "<html></html>",
        },
    )

    assert create["status"] == "failed"
    assert create["output"]["code"] == "drawio_xml_validation_failed"


def test_figure_update_render_failure_leaves_current_drawio_xml_unchanged(tmp_path: Path, monkeypatch) -> None:
    should_fail = {"value": False}

    def render(self: WorkspaceStore, *, input_path: Path, output_path: Path) -> dict:
        assert input_path.exists()
        if should_fail["value"]:
            return {"status": "failed", "output": {"code": "figure_render_failed", "message": "renderer failed"}}
        output_path.write_bytes(PNG_BYTES)
        return {"status": "success", "output": {"path": str(output_path)}}

    monkeypatch.setattr(WorkspaceStore, "_render_drawio_file", render)
    executor, project_id = make_tool_executor(tmp_path)
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "create", "title": "系统结构示意图", "drawio_xml": _sample_drawio_xml()},
    )
    assert create["status"] == "success"
    read = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    original_xml = read["output"]["figure"]["drawio_xml"]
    original_timestamp = read["output"]["figure"]["drawio_updated_at"]
    modified_xml = original_xml.replace("统一入口", "失败更新不应落盘")

    should_fail["value"] = True
    update = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "update",
            "ref": "figure:fig_000001",
            "title": "失败标题",
            "expected_drawio_updated_at": original_timestamp,
            "drawio_xml": modified_xml,
        },
    )

    assert update["status"] == "failed"
    assert update["output"]["code"] == "figure_render_failed"
    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert reread["output"]["figure"]["title"] == "系统结构示意图"
    assert reread["output"]["figure"]["drawio_updated_at"] == original_timestamp
    assert reread["output"]["figure"]["drawio_xml"] == original_xml
    assert executor.store.figure_render_file(project_id, "fig_000001").read_bytes() == PNG_BYTES


def test_figure_kit_rejects_arguments_outside_schema(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)

    extra_field = run_builtin_tool(executor, project_id, "figure_kit", {"action": "list", "unused": True})
    invalid_action = run_builtin_tool(executor, project_id, "figure_kit", {"action": "rename"})

    assert extra_field["status"] == "failed"
    assert extra_field["output"]["code"] == "invalid_tool_arguments"
    assert "unused" in extra_field["output"]["message"]
    assert invalid_action["status"] == "failed"
    assert invalid_action["output"]["code"] == "invalid_tool_arguments"
    assert "action" in invalid_action["output"]["message"]


def test_figure_block_only_allowed_in_appendix_and_renders_with_asset(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    figure = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "title": "代理执行流程示意图",
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
    assert render_ast["figures"][0]["source"]["path"] == "assets/figures/fig_000001/diagram.drawio"
    assert render_ast["figures"][0]["render"]["url"] == f"/api/projects/{project_id}/asset/assets/figures/fig_000001/render.png"


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
