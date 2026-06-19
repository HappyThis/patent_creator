from __future__ import annotations

from pathlib import Path

from docx import Document

from app.domain.disclosure import build_initial_disclosure
from app.domain.docx_export import export_disclosure_docx


def test_docx_export_renders_headings_without_word_outline_markers(tmp_path: Path) -> None:
    disclosure = build_initial_disclosure("heading export")

    output_path = export_disclosure_docx(
        disclosure=disclosure,
        figures=[],
        export_path=tmp_path / "heading-export.docx",
        project_dir=tmp_path,
    )

    document = Document(output_path)
    heading = next(paragraph for paragraph in document.paragraphs if paragraph.text.startswith("1. "))
    assert heading.style.name == "Normal"
    assert heading.runs[0].bold is True


def test_docx_export_leaves_empty_sections_blank(tmp_path: Path) -> None:
    disclosure = build_initial_disclosure("empty export")

    output_path = export_disclosure_docx(
        disclosure=disclosure,
        figures=[],
        export_path=tmp_path / "empty-export.docx",
        project_dir=tmp_path,
    )

    document = Document(output_path)
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

    document = Document(output_path)
    formula_table = document.tables[0]
    assert len(formula_table.columns) == 3
    assert formula_table.cell(0, 0).text == ""
    assert formula_table.cell(0, 2).text == "(1)"


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

    document = Document(output_path)
    paragraph = next(paragraph for paragraph in document.paragraphs if "condition" in paragraph.text)
    assert paragraph.text == "condition  and length "
    assert len(document.inline_shapes) == 2
    assert not any(run.font.name == "Cambria Math" for run in paragraph.runs)


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

    document = Document(output_path)
    assert len(document.tables) == 1
    table = document.tables[0]
    assert table.cell(0, 0).text == "模块"
    assert table.cell(0, 1).text == "作用"
    assert table.cell(1, 0).text == "前端交互"
    assert table.cell(2, 1).text == "维护结构化正文"
