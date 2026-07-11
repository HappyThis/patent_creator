from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.domain.disclosure import build_render_ast
from app.domain.figures import MODEL_REVIEW_IMAGE_MAX_BYTES
from app.storage.workspace_store import WorkspaceStore
from app.tools.builtin.figure import FIGURE_RULES_VERSION

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
            "rules_version": FIGURE_RULES_VERSION,
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
        {
            "type": "render_image",
            "ref": "figure:fig_000001",
            "purpose": "visual_review",
            "drawio_updated_at": figure["drawio_updated_at"],
        }
    ]
    assert "截图" in create["output"]["message"]
    assert set(create["output"]) == {"figure", "message", "attachments"}
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


def test_figure_kit_write_requires_read_timestamp_and_detects_conflict(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "create", "rules_version": FIGURE_RULES_VERSION, "title": "系统结构示意图", "drawio_xml": _sample_drawio_xml()},
    )
    assert create["status"] == "success"
    figure = create["output"]["figure"]

    missing_timestamp = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "rules_version": FIGURE_RULES_VERSION,
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
            "action": "write",
            "rules_version": FIGURE_RULES_VERSION,
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
    written = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "rules_version": FIGURE_RULES_VERSION,
            "ref": "figure:fig_000001",
            "title": "更新后的结构示意图",
            "expected_drawio_updated_at": read["output"]["figure"]["drawio_updated_at"],
            "drawio_xml": next_xml,
        },
    )

    assert written["status"] == "success"
    assert written["output"]["figure"]["title"] == "更新后的结构示意图"
    assert written["output"]["figure"]["drawio_updated_at"] != figure["drawio_updated_at"]
    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert "统一接入网关" in reread["output"]["figure"]["drawio_xml"]


def test_figure_kit_edit_requires_timestamp_and_applies_unique_replacements(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    created = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
            "title": "系统结构示意图",
            "drawio_xml": _sample_drawio_xml(),
        },
    )["output"]["figure"]

    missing_timestamp = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "edit",
            "rules_version": FIGURE_RULES_VERSION,
            "ref": "figure:fig_000001",
            "edits": [{"old_text": "统一入口", "new_text": "统一接入网关"}],
        },
    )
    assert missing_timestamp["status"] == "failed"
    assert missing_timestamp["output"]["code"] == "drawio_read_required"

    read = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    edited = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "edit",
            "rules_version": FIGURE_RULES_VERSION,
            "ref": "figure:fig_000001",
            "title": "局部编辑后的结构示意图",
            "expected_drawio_updated_at": read["output"]["figure"]["drawio_updated_at"],
            "edits": [
                {"old_text": 'value="统一入口"', "new_text": 'value="统一接入网关"'},
                {"old_text": 'x="420" y="160"', "new_text": 'x="460" y="160"'},
            ],
        },
    )

    assert edited["status"] == "success"
    assert edited["output"]["figure"]["title"] == "局部编辑后的结构示意图"
    assert edited["output"]["figure"]["drawio_updated_at"] != created["drawio_updated_at"]
    assert edited["output"]["attachments"][0]["purpose"] == "visual_review"
    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert 'value="统一接入网关"' in reread["output"]["figure"]["drawio_xml"]
    assert 'x="460" y="160"' in reread["output"]["figure"]["drawio_xml"]


def test_figure_kit_edit_requires_every_target_to_be_unique_and_is_atomic(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)
    run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
            "title": "系统结构示意图",
            "drawio_xml": _sample_drawio_xml(),
        },
    )
    read = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    original = read["output"]["figure"]
    original_revision = executor.store.figure_drawio_file(project_id, "fig_000001").parent

    missing = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "edit",
            "rules_version": FIGURE_RULES_VERSION,
            "ref": "figure:fig_000001",
            "expected_drawio_updated_at": original["drawio_updated_at"],
            "edits": [
                {"old_text": "统一入口", "new_text": "不应落盘"},
                {"old_text": "不存在的目标", "new_text": "失败"},
            ],
        },
    )
    assert missing["status"] == "failed"
    assert missing["output"]["code"] == "figure_edit_target_not_found"
    assert missing["output"]["edit_index"] == 1

    not_unique = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "edit",
            "rules_version": FIGURE_RULES_VERSION,
            "ref": "figure:fig_000001",
            "expected_drawio_updated_at": original["drawio_updated_at"],
            "edits": [{"old_text": 'parent="1"', "new_text": 'parent="2"'}],
        },
    )
    assert not_unique["status"] == "failed"
    assert not_unique["output"]["code"] == "figure_edit_target_not_unique"
    assert not_unique["output"]["match_count"] > 1

    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert reread["output"]["figure"]["drawio_updated_at"] == original["drawio_updated_at"]
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
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
            "title": "无效附图",
            "drawio_xml": "<html></html>",
        },
    )

    assert create["status"] == "failed"
    assert create["output"]["code"] == "drawio_xml_validation_failed"


def test_figure_kit_requires_rules_and_returns_rules_on_demand(tmp_path: Path, monkeypatch) -> None:
    _stub_figure_renderer(monkeypatch)
    executor, project_id = make_tool_executor(tmp_path)

    rules = run_builtin_tool(executor, project_id, "figure_kit", {"action": "rules"})
    missing_rules = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "create", "title": "系统结构示意图", "drawio_xml": _sample_drawio_xml()},
    )

    assert rules["status"] == "success"
    assert rules["output"]["rules_version"] == FIGURE_RULES_VERSION
    assert 8 <= len(rules["output"]["rules"]) <= 15
    assert any("局部修改用 edit" in item for item in rules["output"]["rules"])
    assert any("轻微审美偏好留给人工调整" in item for item in rules["output"]["rules"])
    assert not any("禁止用大外框" in item for item in rules["output"]["rules"])
    assert missing_rules["status"] == "failed"
    assert missing_rules["output"]["code"] == "figure_rules_required"


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
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
            "title": "系统结构示意图",
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
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
            "title": "错误页面",
            "drawio_xml": wrong_canvas,
        },
    )
    overflow_result = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
            "title": "节点越界",
            "drawio_xml": overflow,
        },
    )

    assert wrong_canvas_result["output"]["code"] == "drawio_canvas_invalid"
    assert overflow_result["output"]["code"] == "drawio_canvas_overflow"


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
    create = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {"action": "create", "rules_version": FIGURE_RULES_VERSION, "title": "系统结构示意图", "drawio_xml": _sample_drawio_xml()},
    )
    assert create["status"] == "success"
    read = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    original_xml = read["output"]["figure"]["drawio_xml"]
    original_timestamp = read["output"]["figure"]["drawio_updated_at"]
    modified_xml = original_xml.replace("统一入口", "失败更新不应落盘")

    should_fail["value"] = True
    write = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "write",
            "rules_version": FIGURE_RULES_VERSION,
            "ref": "figure:fig_000001",
            "title": "失败标题",
            "expected_drawio_updated_at": original_timestamp,
            "drawio_xml": modified_xml,
        },
    )

    assert write["status"] == "failed"
    assert write["output"]["code"] == "figure_render_failed"
    reread = run_builtin_tool(executor, project_id, "figure_kit", {"action": "read", "ref": "figure:fig_000001"})
    assert reread["output"]["figure"]["title"] == "系统结构示意图"
    assert reread["output"]["figure"]["drawio_updated_at"] == original_timestamp
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
    created = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
            "title": "系统结构示意图",
            "drawio_xml": _sample_drawio_xml(),
        },
    )["output"]["figure"]

    def write(title: str) -> dict:
        return executor.store.write_figure(
            project_id,
            "fig_000001",
            title=title,
            drawio_xml=_sample_drawio_xml().replace("统一入口", title),
            expected_drawio_updated_at=created["drawio_updated_at"],
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
    created = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
            "title": "系统结构示意图",
            "drawio_xml": _sample_drawio_xml(),
        },
    )["output"]["figure"]
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
        expected_drawio_updated_at=created["drawio_updated_at"],
    )

    assert result["status"] == "failed"
    assert result["output"]["code"] == "figure_storage_failed"
    assert executor.store.get_figure(project_id, "fig_000001")["title"] == "系统结构示意图"
    assert executor.store.figure_drawio_file(project_id, "fig_000001") == old_drawio_file
    assert old_drawio_file.read_text(encoding="utf-8") == old_xml
    assert old_render_file.read_bytes() == old_render
    revision_dirs = list((executor.store.figure_dir(project_id, "fig_000001") / ".revisions").glob("rev_*"))
    assert revision_dirs == [old_drawio_file.parent]


def test_figure_kit_rejects_arguments_outside_schema(tmp_path: Path) -> None:
    executor, project_id = make_tool_executor(tmp_path)

    extra_field = run_builtin_tool(executor, project_id, "figure_kit", {"action": "list", "unused": True})
    invalid_action = run_builtin_tool(executor, project_id, "figure_kit", {"action": "rename"})
    removed_update = run_builtin_tool(executor, project_id, "figure_kit", {"action": "update"})
    replace_all = run_builtin_tool(
        executor,
        project_id,
        "figure_kit",
        {
            "action": "edit",
            "ref": "figure:fig_000001",
            "rules_version": FIGURE_RULES_VERSION,
            "expected_drawio_updated_at": "timestamp",
            "edits": [{"old_text": "x", "new_text": "y", "replace_all": True}],
        },
    )

    assert extra_field["status"] == "failed"
    assert extra_field["output"]["code"] == "invalid_tool_arguments"
    assert "unused" in extra_field["output"]["message"]
    assert invalid_action["status"] == "failed"
    assert invalid_action["output"]["code"] == "invalid_tool_arguments"
    assert "action" in invalid_action["output"]["message"]
    assert removed_update["status"] == "failed"
    assert removed_update["output"]["code"] == "invalid_tool_arguments"
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
            "action": "create",
            "rules_version": FIGURE_RULES_VERSION,
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
