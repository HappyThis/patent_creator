from __future__ import annotations

import json
import subprocess
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
import pytest

from app.domain.disclosure import build_initial_disclosure
from app.domain import docx_export
from app.domain.docx_export import DocxExportError, export_disclosure_docx


def test_docx_export_renders_headings_without_word_outline_markers(tmp_path: Path) -> None:
    disclosure = build_initial_disclosure("heading export")

    output_path = export_disclosure_docx(
        disclosure=disclosure,
        figures=[],
        export_path=tmp_path / "heading-export.docx",
        project_dir=tmp_path,
    )

    document = Document(str(output_path))
    heading = next(paragraph for paragraph in document.paragraphs if paragraph.text.startswith("1. "))
    assert heading.style is not None
    assert heading.style.name == "Normal"
    assert heading.runs[0].bold is True
    assert heading.runs[0].font.color.rgb == docx_export.RGBColor(0x0D, 0x16, 0x24)


def test_docx_export_uses_songti_for_chinese_and_times_for_latin(tmp_path: Path) -> None:
    disclosure = build_initial_disclosure("font export")
    disclosure["sections"][0]["blocks"] = [
        {
            "id": "blk_font_test",
            "type": "paragraph",
            "text": "中文 ABC 123",
        }
    ]

    output_path = export_disclosure_docx(
        disclosure=disclosure,
        figures=[],
        export_path=tmp_path / "font-export.docx",
        project_dir=tmp_path,
    )

    document = Document(str(output_path))
    paragraph = next(paragraph for paragraph in document.paragraphs if "中文 ABC 123" in paragraph.text)
    r_pr = paragraph.runs[0]._element.rPr
    assert r_pr is not None
    r_fonts = r_pr.rFonts
    assert r_fonts.get(qn("w:ascii")) == "Times New Roman"
    assert r_fonts.get(qn("w:hAnsi")) == "Times New Roman"
    assert r_fonts.get(qn("w:eastAsia")) == "宋体"


def test_docx_export_leaves_empty_sections_blank(tmp_path: Path) -> None:
    disclosure = build_initial_disclosure("empty export")

    output_path = export_disclosure_docx(
        disclosure=disclosure,
        figures=[],
        export_path=tmp_path / "empty-export.docx",
        project_dir=tmp_path,
    )

    document = Document(str(output_path))
    assert "内容待补充" not in [paragraph.text for paragraph in document.paragraphs]


def test_docx_export_centers_block_formulas_across_full_line(tmp_path: Path) -> None:
    disclosure = build_initial_disclosure("formula export")
    disclosure["sections"][0]["blocks"].append(
        {
            "id": "blk_formula_test",
            "type": "formula",
            "latex": r"U=\sum_{i=1}^{n}\alpha_i L_i+\beta G",
        }
    )

    output_path = export_disclosure_docx(
        disclosure=disclosure,
        figures=[],
        export_path=tmp_path / "formula-export.docx",
        project_dir=tmp_path,
    )

    document = Document(str(output_path))
    formula_table = document.tables[0]
    assert len(formula_table.columns) == 3
    assert formula_table.cell(0, 0).text == ""
    assert formula_table.cell(0, 2).text == "(1)"
    formula_cell_pr = formula_table.cell(0, 1)._tc.tcPr
    assert formula_cell_pr is not None
    formula_cell_width = int(formula_cell_pr.tcW.w)
    assert formula_cell_width == int(docx_export.FORMULA_BODY_COL_IN * 1440)
    assert docx_export.FORMULA_MAX_WIDTH_IN < docx_export.FORMULA_BODY_COL_IN


def test_docx_export_renders_all_inline_math_as_images(tmp_path: Path) -> None:
    disclosure = build_initial_disclosure("inline math export")
    disclosure["sections"][0]["blocks"] = [
        {
            "id": "blk_inline_math_test",
            "type": "paragraph",
            "text": "condition $U > T$ and length $L_i$",
        }
    ]

    output_path = export_disclosure_docx(
        disclosure=disclosure,
        figures=[],
        export_path=tmp_path / "inline-math-export.docx",
        project_dir=tmp_path,
    )

    document = Document(str(output_path))
    paragraph = next(paragraph for paragraph in document.paragraphs if "condition" in paragraph.text)
    assert paragraph.text == "condition  and length "
    assert len(document.inline_shapes) == 2
    assert not any(run.font.name == "Cambria Math" for run in paragraph.runs)
    assert not list((tmp_path / "exports").glob("docx-assets-*"))


def test_docx_export_cleans_asset_directory_when_renderer_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    disclosure = build_initial_disclosure("asset failure export")
    disclosure["sections"][0]["blocks"] = [
        {
            "id": "blk_inline_math_test",
            "type": "paragraph",
            "text": "condition $U > T$",
        }
    ]

    def fail_renderer(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args="", returncode=1, stdout="", stderr="renderer failed")

    monkeypatch.setattr(docx_export.subprocess, "run", fail_renderer)

    with pytest.raises(DocxExportError, match="renderer failed"):
        export_disclosure_docx(
            disclosure=disclosure,
            figures=[],
            export_path=tmp_path / "failed-export.docx",
            project_dir=tmp_path,
        )

    assert not list((tmp_path / "exports").glob("docx-assets-*"))


def test_docx_export_rejects_renderer_manifest_missing_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disclosure = build_initial_disclosure("missing asset export")
    disclosure["sections"][0]["blocks"] = [
        {
            "id": "blk_inline_math_test",
            "type": "paragraph",
            "text": "condition $U > T$",
        }
    ]

    def write_incomplete_manifest(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        manifest_path = Path(args[args.index("--manifest") + 1])
        manifest_path.write_text('{"assets": {}}\n', encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docx_export.subprocess, "run", write_incomplete_manifest)

    with pytest.raises(DocxExportError, match="did not return asset"):
        export_disclosure_docx(
            disclosure=disclosure,
            figures=[],
            export_path=tmp_path / "missing-asset-export.docx",
            project_dir=tmp_path,
        )

    assert not list((tmp_path / "exports").glob("docx-assets-*"))


def test_docx_export_rejects_renderer_manifest_missing_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disclosure = build_initial_disclosure("missing file export")
    disclosure["sections"][0]["blocks"] = [
        {
            "id": "blk_inline_math_test",
            "type": "paragraph",
            "text": "condition $U > T$",
        }
    ]

    def write_missing_file_manifest(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        input_path = Path(args[args.index("--input") + 1])
        output_dir = Path(args[args.index("--output") + 1])
        manifest_path = Path(args[args.index("--manifest") + 1])
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        asset_id = payload["items"][0]["id"]
        manifest_path.write_text(
            json.dumps(
                {
                    "assets": {
                        asset_id: {
                            "id": asset_id,
                            "kind": "inline_formula",
                            "path": str(output_dir / "does-not-exist.png"),
                            "width_px": 20,
                            "height_px": 10,
                        }
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docx_export.subprocess, "run", write_missing_file_manifest)

    with pytest.raises(DocxExportError, match="missing file"):
        export_disclosure_docx(
            disclosure=disclosure,
            figures=[],
            export_path=tmp_path / "missing-file-export.docx",
            project_dir=tmp_path,
        )

    assert not list((tmp_path / "exports").glob("docx-assets-*"))


def test_docx_export_rejects_renderer_manifest_paths_outside_output_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disclosure = build_initial_disclosure("outside asset export")
    disclosure["sections"][0]["blocks"] = [
        {
            "id": "blk_inline_math_test",
            "type": "paragraph",
            "text": "condition $U > T$",
        }
    ]
    outside_asset = tmp_path / "outside.png"
    outside_asset.write_bytes(b"not a real png")

    def write_outside_file_manifest(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        input_path = Path(args[args.index("--input") + 1])
        manifest_path = Path(args[args.index("--manifest") + 1])
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        asset_id = payload["items"][0]["id"]
        manifest_path.write_text(
            json.dumps(
                {
                    "assets": {
                        asset_id: {
                            "id": asset_id,
                            "kind": "inline_formula",
                            "path": str(outside_asset),
                            "width_px": 20,
                            "height_px": 10,
                        }
                    }
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(docx_export.subprocess, "run", write_outside_file_manifest)

    with pytest.raises(DocxExportError, match="out-of-directory path"):
        export_disclosure_docx(
            disclosure=disclosure,
            figures=[],
            export_path=tmp_path / "outside-asset-export.docx",
            project_dir=tmp_path,
        )

    assert not list((tmp_path / "exports").glob("docx-assets-*"))


def test_docx_export_cleans_asset_directory_when_document_build_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disclosure = build_initial_disclosure("document failure export")
    disclosure["sections"][0]["blocks"] = [
        {
            "id": "blk_inline_math_test",
            "type": "paragraph",
            "text": "condition $U > T$",
        }
    ]

    def fail_render_section(*args: object, **kwargs: object) -> None:
        raise RuntimeError("document build failed")

    monkeypatch.setattr(docx_export, "render_section", fail_render_section)

    with pytest.raises(RuntimeError, match="document build failed"):
        export_disclosure_docx(
            disclosure=disclosure,
            figures=[],
            export_path=tmp_path / "failed-document-export.docx",
            project_dir=tmp_path,
        )

    assert not list((tmp_path / "exports").glob("docx-assets-*"))


def test_docx_export_keeps_tables_editable(tmp_path: Path) -> None:
    disclosure = build_initial_disclosure("table export")
    disclosure["sections"][0]["blocks"].append(
        {
            "id": "blk_900001",
            "type": "table",
            "columns": ["模块", "作用"],
            "rows": [["前端交互", "展示预览"], ["文档领域", "维护结构化正文"]],
        }
    )

    output_path = export_disclosure_docx(
        disclosure=disclosure,
        figures=[],
        export_path=tmp_path / "table-export.docx",
        project_dir=tmp_path,
    )

    document = Document(str(output_path))
    assert len(document.tables) == 1
    table = document.tables[0]
    assert table.cell(0, 0).text == "模块"
    assert table.cell(0, 1).text == "作用"
    assert table.cell(1, 0).text == "前端交互"
    assert table.cell(2, 1).text == "维护结构化正文"
