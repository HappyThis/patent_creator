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


def _sample_figure_html(text: str = "任务接收") -> str:
    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><style>
html,body{{margin:0;background:#fff;}}
#diagram{{width:1500px;height:900px;box-sizing:border-box;padding:80px;font-family:Arial,sans-serif;color:#111;}}
.box{{border:2px solid #111;padding:28px;display:inline-block;}}
</style></head>
<body><div id="diagram"><div class="box">{text}</div></div></body>
</html>"""


def _stub_figure_renderer(monkeypatch) -> None:
    def render(self: WorkspaceStore, project_id: str, figure_id: str) -> dict:
        output_path = self.figure_render_file(project_id, figure_id)
        output_path.write_bytes(PNG_BYTES)
        return {"status": "success", "output": {"path": str(output_path)}}

    monkeypatch.setattr(WorkspaceStore, "_render_html_figure", render)


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
    html = _sample_figure_html()
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "title": "系统结构示意图",
            "html": html,
        },
    )

    assert create["status"] == "success"
    figure = create["output"]["figure"]
    assert figure["figure_id"] == "fig_000001"
    assert figure["ref"] == "figure:fig_000001"
    assert figure["markdown_ref"] == "[图1](figure:fig_000001)"
    assert figure["caption"] == "图1 系统结构示意图"
    assert figure["source"] == {
        "type": "html",
        "path": "assets/figures/fig_000001/diagram.html",
        "width": 1500,
        "height": 900,
    }
    assert figure["render"]["path"] == "assets/figures/fig_000001/render.png"
    assert figure["render"]["url"] == f"/api/projects/{project_id}/asset/assets/figures/fig_000001/render.png"
    assert figure["html"].strip() == html
    assert (executor.store.project_dir(project_id) / "assets" / "figures" / "fig_000001" / "figure.json").exists()
    assert (executor.store.project_dir(project_id) / "assets" / "figures" / "fig_000001" / "diagram.html").exists()
    assert (executor.store.project_dir(project_id) / "assets" / "figures" / "fig_000001" / "render.png").exists()

    listed = run_builtin_tool(executor, project_id, "figure_kit", {"action": "list"})
    assert listed["status"] == "success"
    assert listed["output"]["figures"][0]["markdown_ref"] == "[图1](figure:fig_000001)"

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


def test_figure_kit_rejects_unsafe_html(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "title": "外链附图",
            "html": '<!doctype html><html><body><div id="diagram"><script>alert(1)</script></div></body></html>',
        },
    )

    assert create["status"] == "failed"
    assert create["output"]["code"] == "figure_html_unsafe"


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
    html = _sample_figure_html("读取内核")
    figure = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "title": "代理执行流程示意图",
            "html": html,
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
    assert render_ast["figures"][0]["source"]["path"] == "assets/figures/fig_000001/diagram.html"
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
