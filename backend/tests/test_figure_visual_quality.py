from __future__ import annotations

from app.domain.figures import validate_drawio_xml


def _warning_codes(result: dict) -> set[str]:
    assert result["status"] == "success"
    return {item["code"] for item in result["output"]["warnings"]}


def test_visual_defaults_are_auto_filled_without_overriding_explicit_choices() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" value="输入" vertex="1" parent="1"><mxGeometry x="100" y="200" width="180" height="80" as="geometry"/></mxCell>
<mxCell id="b" value="处理" style="strokeWidth=2;" vertex="1" parent="1"><mxGeometry x="500" y="200" width="180" height="80" as="geometry"/></mxCell>
<mxCell id="e1" value="调用" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert result["status"] == "success"
    normalized_xml = result["output"]["drawio_xml"]
    normalized_fields = result["output"]["normalized_fields"]
    assert "fontFamily=Helvetica" in normalized_xml
    assert "strokeWidth=1.4" in normalized_xml
    assert "edgeStyle=orthogonalEdgeStyle" in normalized_xml
    assert "endArrow=block" in normalized_xml
    assert "endFill=1" in normalized_xml
    assert "labelBackgroundColor=#ffffff" in normalized_xml
    assert "strokeWidth=2;" in normalized_xml
    assert any("mxCell.style.fontFamily=Helvetica" in field for field in normalized_fields)
    assert any("mxCell.style.strokeWidth=1.4" in field for field in normalized_fields)


def test_crossing_semantic_edges_are_reported_before_render() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" value="A" vertex="1" parent="1"><mxGeometry x="100" y="270" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="b" value="B" vertex="1" parent="1"><mxGeometry x="600" y="270" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="c" value="C" vertex="1" parent="1"><mxGeometry x="370" y="80" width="60" height="60" as="geometry"/></mxCell>
<mxCell id="d" value="D" vertex="1" parent="1"><mxGeometry x="370" y="500" width="60" height="60" as="geometry"/></mxCell>
<mxCell id="e1" style="exitX=1;exitY=0.5;entryX=0;entryY=0.5;" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="300" y="300"/><mxPoint x="500" y="300"/></Array></mxGeometry></mxCell>
<mxCell id="e2" style="exitX=0.5;exitY=1;entryX=0.5;entryY=0;" edge="1" parent="1" source="c" target="d"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="400" y="220"/><mxPoint x="400" y="420"/></Array></mxGeometry></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert "drawio_semantic_edge_crossing" in _warning_codes(result)
    crossing = next(item for item in result["output"]["warnings"] if item["code"] == "drawio_semantic_edge_crossing")
    assert "(400, 300)" in crossing["message"]
    assert crossing["related_cell_ids"] == ["e2"]


def test_edge_crossing_an_unrelated_node_is_reported_before_render() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" value="A" vertex="1" parent="1"><mxGeometry x="100" y="270" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="b" value="B" vertex="1" parent="1"><mxGeometry x="600" y="270" width="100" height="60" as="geometry"/></mxCell>
<mxCell id="blocker" value="无关节点" vertex="1" parent="1"><mxGeometry x="350" y="250" width="100" height="100" as="geometry"/></mxCell>
<mxCell id="e1" style="exitX=1;exitY=0.5;entryX=0;entryY=0.5;" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="300" y="300"/><mxPoint x="500" y="300"/></Array></mxGeometry></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert "drawio_edge_crosses_vertex" in _warning_codes(result)
    crossing = next(item for item in result["output"]["warnings"] if item["code"] == "drawio_edge_crosses_vertex")
    assert crossing["cell_id"] == "e1"
    assert crossing["related_cell_ids"] == ["blocker"]


def test_visual_lint_reports_overlapping_nodes_and_missing_text_hierarchy() -> None:
    nodes = "\n".join(
        f'<mxCell id="n{i}" value="节点{i}" vertex="1" parent="1"><mxGeometry x="{100 + i * 120}" y="200" width="150" height="70" as="geometry"/></mxCell>'
        for i in range(6)
    )
    xml = f"""<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>{nodes}
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)
    codes = _warning_codes(result)

    assert "drawio_vertex_overlap" in codes
    assert "drawio_visual_hierarchy_missing" in codes


def test_top_level_shapes_near_page_edge_receive_safe_margin_warning() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="near" value="贴边主体" vertex="1" parent="1"><mxGeometry x="20" y="100" width="200" height="80" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert "drawio_visual_safe_margin" in _warning_codes(result)


def test_sibling_background_panel_is_not_reported_as_overlap_or_crossed_node() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="panel" value="处理分区" vertex="1" parent="1"><mxGeometry x="80" y="100" width="900" height="420" as="geometry"/></mxCell>
<mxCell id="a" value="输入" vertex="1" parent="1"><mxGeometry x="180" y="260" width="180" height="80" as="geometry"/></mxCell>
<mxCell id="b" value="输出" vertex="1" parent="1"><mxGeometry x="680" y="260" width="180" height="80" as="geometry"/></mxCell>
<mxCell id="e1" style="exitX=1;exitY=0.5;entryX=0;entryY=0.5;" edge="1" parent="1" source="a" target="b"><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="440" y="300"/><mxPoint x="600" y="300"/></Array></mxGeometry></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)
    codes = _warning_codes(result)

    assert "drawio_vertex_overlap" not in codes
    assert "drawio_edge_crosses_vertex" not in codes
    assert 'id="panel"' in result["output"]["drawio_xml"]
    panel_xml = result["output"]["drawio_xml"].split('id="panel"', 1)[1].split("</mxCell>", 1)[0]
    assert "strokeWidth=1;" in panel_xml
    assert "fillColor=#f7f7f7;" in panel_xml


def test_four_typographic_levels_match_the_recommended_visual_hierarchy() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="title" value="标题" style="text;fontSize=18;" vertex="1" parent="1"><mxGeometry x="70" y="60" width="400" height="40" as="geometry"/></mxCell>
<mxCell id="panel" value="分区" style="fontSize=15;" vertex="1" parent="1"><mxGeometry x="70" y="130" width="900" height="360" as="geometry"/></mxCell>
<mxCell id="a" value="节点 A" style="fontSize=14;" vertex="1" parent="panel"><mxGeometry x="80" y="140" width="180" height="80" as="geometry"/></mxCell>
<mxCell id="b" value="节点 B" style="fontSize=14;" vertex="1" parent="panel"><mxGeometry x="600" y="140" width="180" height="80" as="geometry"/></mxCell>
<mxCell id="e1" value="边标签" style="fontSize=12;" edge="1" parent="panel" source="a" target="b"><mxGeometry relative="1" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert "drawio_font_size_excessive" not in _warning_codes(result)


def test_visual_role_applies_semantic_profile_without_overriding_explicit_style() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="core" value="核心处理" style="visualRole=primary;fillColor=#ffffff;strokeWidth=2;" vertex="1" parent="1"><mxGeometry x="400" y="250" width="220" height="100" as="geometry"/></mxCell>
<mxCell id="choice" value="是否满足条件" style="visualRole=decision;shape=hexagon;" vertex="1" parent="1"><mxGeometry x="760" y="250" width="180" height="100" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert result["status"] == "success"
    normalized_xml = result["output"]["drawio_xml"]
    core_xml = normalized_xml.split('id="core"', 1)[1].split("</mxCell>", 1)[0]
    choice_xml = normalized_xml.split('id="choice"', 1)[1].split("</mxCell>", 1)[0]
    assert "visualRole=primary;" in core_xml
    assert "fontStyle=1;" in core_xml
    assert "fillColor=#ffffff;" in core_xml
    assert "fillColor=#e9e9e9;" not in core_xml
    assert "strokeWidth=2;" in core_xml
    assert "visualRole=decision;" in choice_xml
    assert "shape=hexagon;" in choice_xml
    assert "shape=rhombus;" not in choice_xml


def test_unknown_visual_role_warns_but_falls_back_to_inferred_style() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="node" value="处理" style="visualRole=hero;" vertex="1" parent="1"><mxGeometry x="300" y="200" width="180" height="80" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert result["status"] == "success"
    assert "drawio_visual_role_unknown" in _warning_codes(result)
    assert "fillColor=#ffffff;" in result["output"]["drawio_xml"]


def test_visual_scene_tolerates_parent_cycles_until_hard_validation_reports_them() -> None:
    xml = """<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" value="A" vertex="1" parent="b"><mxGeometry x="100" y="100" width="180" height="80" as="geometry"/></mxCell>
<mxCell id="b" value="B" vertex="1" parent="a"><mxGeometry x="300" y="100" width="180" height="80" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert result["status"] == "failed"
    assert any(item["code"] == "drawio_parent_cycle" for item in result["output"]["errors"])


def test_same_visual_role_with_divergent_styles_is_a_non_blocking_warning() -> None:
    nodes = "\n".join(
        [
            '<mxCell id="a" value="A" style="visualRole=normal;" vertex="1" parent="1"><mxGeometry x="100" y="200" width="180" height="80" as="geometry"/></mxCell>',
            '<mxCell id="b" value="B" style="visualRole=normal;fillColor=#eeeeee;" vertex="1" parent="1"><mxGeometry x="400" y="200" width="180" height="80" as="geometry"/></mxCell>',
            '<mxCell id="c" value="C" style="visualRole=normal;" vertex="1" parent="1"><mxGeometry x="700" y="200" width="180" height="80" as="geometry"/></mxCell>',
        ]
    )
    xml = f"""<mxfile><diagram><mxGraphModel page="1" pageWidth="1500" pageHeight="900"><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>{nodes}
</root></mxGraphModel></diagram></mxfile>"""

    result = validate_drawio_xml(xml)

    assert result["status"] == "success"
    assert "drawio_visual_role_inconsistent" in _warning_codes(result)
