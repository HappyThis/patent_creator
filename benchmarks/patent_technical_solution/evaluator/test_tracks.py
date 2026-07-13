from __future__ import annotations

import json
from pathlib import Path

import pytest

from .tracks import (
    DEFAULT_TRACK_ID,
    TRACK_SCHEMA_VERSION,
    TrackConfigError,
    load_track,
    resolve_track_case,
)


GENERAL_CASE_IDS = tuple(f"{value:03d}" for value in range(1, 11))
REPRESENTATION_CASE_IDS = ("001", "004", "006", "008", "009", "010", "011", "012", "013", "014")
REPRESENTATION_POLICIES = {
    "001": ("optional", "optional"),
    "004": ("optional", "optional"),
    "006": ("optional", "optional"),
    "008": ("recommended", "optional"),
    "009": ("recommended", "optional"),
    "010": ("recommended", "optional"),
    "011": ("optional", "recommended"),
    "012": ("optional", "recommended"),
    "013": ("recommended", "recommended"),
    "014": ("recommended", "recommended"),
}


def test_checked_in_manifests_keep_the_canonical_case_sets_and_policies() -> None:
    benchmark_dir = Path(__file__).resolve().parents[1]
    general = json.loads(
        (benchmark_dir / "tracks" / "general_solution" / "track.json").read_text(encoding="utf-8")
    )
    representation = json.loads(
        (benchmark_dir / "tracks" / "representation_semantics" / "track.json").read_text(
            encoding="utf-8"
        )
    )

    assert tuple(item["case_id"] for item in general["cases"]) == GENERAL_CASE_IDS
    assert tuple(item["case_id"] for item in representation["cases"]) == REPRESENTATION_CASE_IDS
    assert {
        item["case_id"]: (item["figure_policy"], item["formula_policy"])
        for item in representation["cases"]
    } == REPRESENTATION_POLICIES
    for case_id, expected in REPRESENTATION_POLICIES.items():
        metadata = json.loads(
            (
                benchmark_dir
                / "tracks"
                / "representation_semantics"
                / "cases"
                / case_id
                / "metadata.json"
            ).read_text(encoding="utf-8")
        )
        assert (metadata["figure_policy"], metadata["formula_policy"]) == expected


def test_checked_in_representation_rules_distinguish_coverage_from_optional_errors() -> None:
    benchmark_dir = Path(__file__).resolve().parents[1]
    rules = (
        benchmark_dir / "tracks" / "representation_semantics" / "judge.md"
    ).read_text(encoding="utf-8")

    assert "对 `recommended` 通道" in rules
    assert "没有覆盖逐 Case rubric 指定的核心关系，可以判为“部分正确”" in rules
    assert "对 `optional` 通道，只有实际写出或画出的内容存在具体错误" in rules
    assert "偏装饰性本身不得扣分" in rules


def test_default_track_is_fixed_general_solution(tmp_path: Path) -> None:
    benchmark_dir = make_benchmark(tmp_path, general_manifest())

    track = load_track(benchmark_dir=benchmark_dir)

    assert DEFAULT_TRACK_ID == "general_solution"
    assert track.track_id == "general_solution"
    assert track.judge_profile == "general"
    assert track.case_ids == GENERAL_CASE_IDS
    assert track.subject_policy.web_search_enabled is None
    resolved = resolve_track_case(track, 1)
    assert resolved.case_id == "001"
    assert resolved.figure_policy is None
    assert resolved.formula_policy is None
    assert resolved.track_rubric_path is None


def test_representation_track_resolves_policies_and_overlays(tmp_path: Path) -> None:
    benchmark_dir = make_benchmark(tmp_path, representation_manifest())

    track = load_track("representation_semantics", benchmark_dir=benchmark_dir)

    assert track.case_ids == REPRESENTATION_CASE_IDS
    assert track.subject_policy.web_search_enabled is False
    assert track.subject_policy.expose_snapshot_provenance is False
    assert track.subject_policy.preserve_snapshot_git is False
    assert track.track_judge_path == benchmark_dir / "tracks" / "representation_semantics" / "judge.md"
    for case_id, expected in REPRESENTATION_POLICIES.items():
        resolved = resolve_track_case(track, case_id)
        assert (resolved.figure_policy, resolved.formula_policy) == expected
        assert resolved.track_rubric_path == (
            benchmark_dir / "tracks" / "representation_semantics" / "cases" / case_id / "rubric.md"
        )
        assert resolved.track_metadata_path == (
            benchmark_dir
            / "tracks"
            / "representation_semantics"
            / "cases"
            / case_id
            / "metadata.json"
        )


def test_resolve_track_case_rejects_case_outside_track(tmp_path: Path) -> None:
    benchmark_dir = make_benchmark(tmp_path, representation_manifest())
    track = load_track("representation_semantics", benchmark_dir=benchmark_dir)

    with pytest.raises(TrackConfigError, match="case 003 is not part"):
        resolve_track_case(track, "003")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["cases"].append(dict(value["cases"][0])), "duplicate case id"),
        (lambda value: value["cases"][0].update(figure_policy="forbidden"), "must be one of"),
        (lambda value: value["cases"][0].update(extra=True), "unexpected fields"),
        (lambda value: value.update(track_id="other"), "does not match directory"),
    ],
)
def test_representation_manifest_rejects_invalid_contract(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    manifest = representation_manifest()
    mutate(manifest)
    benchmark_dir = make_benchmark(tmp_path, manifest, directory_track_id="representation_semantics")

    with pytest.raises(TrackConfigError, match=message):
        load_track("representation_semantics", benchmark_dir=benchmark_dir)


def test_builtin_track_rejects_case_membership_drift(tmp_path: Path) -> None:
    manifest = general_manifest()
    manifest["cases"] = manifest["cases"][:-1]
    benchmark_dir = make_benchmark(tmp_path, manifest)

    with pytest.raises(TrackConfigError, match="must contain cases"):
        load_track(benchmark_dir=benchmark_dir)


def test_representation_track_requires_every_overlay_rubric(tmp_path: Path) -> None:
    benchmark_dir = make_benchmark(tmp_path, representation_manifest())
    missing = benchmark_dir / "tracks" / "representation_semantics" / "cases" / "013" / "rubric.md"
    missing.unlink()

    with pytest.raises(TrackConfigError, match="track rubric for case 013"):
        load_track("representation_semantics", benchmark_dir=benchmark_dir)


def test_representation_metadata_must_match_manifest(tmp_path: Path) -> None:
    benchmark_dir = make_benchmark(tmp_path, representation_manifest())
    metadata_path = (
        benchmark_dir
        / "tracks"
        / "representation_semantics"
        / "cases"
        / "010"
        / "metadata.json"
    )
    write_json(metadata_path, {"figure_policy": "optional", "formula_policy": "optional"})

    with pytest.raises(TrackConfigError, match="policies must match"):
        load_track("representation_semantics", benchmark_dir=benchmark_dir)


def test_representation_track_requires_leakage_safe_subject_policy(tmp_path: Path) -> None:
    manifest = representation_manifest()
    manifest["subject_policy"]["web_search_enabled"] = True
    benchmark_dir = make_benchmark(tmp_path, manifest)

    with pytest.raises(TrackConfigError, match="must disable web search"):
        load_track("representation_semantics", benchmark_dir=benchmark_dir)


def make_benchmark(
    tmp_path: Path,
    manifest: dict,
    *,
    directory_track_id: str | None = None,
) -> Path:
    benchmark_dir = tmp_path / "benchmark"
    for filename in ("runner.md", "judge.md"):
        write_text(benchmark_dir / filename)

    track_id = directory_track_id or str(manifest["track_id"])
    track_dir = benchmark_dir / "tracks" / track_id
    write_json(track_dir / "track.json", manifest)
    if manifest.get("judge_profile") == "representation_semantics":
        write_text(track_dir / "judge.md")

    raw_cases = manifest.get("cases")
    if isinstance(raw_cases, list):
        case_ids = {
            str(raw_case.get("case_id"))
            for raw_case in raw_cases
            if isinstance(raw_case, dict) and isinstance(raw_case.get("case_id"), str)
        }
        for case_id in case_ids:
            source_dir = benchmark_dir / "cases" / case_id
            for filename in ("request.md", "snapshot.json", "reference_solution.md", "rubric.md"):
                write_text(source_dir / filename)
            if manifest.get("judge_profile") == "representation_semantics":
                track_case_dir = track_dir / "cases" / case_id
                write_text(track_case_dir / "rubric.md")
                expected = REPRESENTATION_POLICIES[case_id]
                write_json(
                    track_case_dir / "metadata.json",
                    {"figure_policy": expected[0], "formula_policy": expected[1]},
                )
    return benchmark_dir


def general_manifest() -> dict:
    return {
        "schema_version": TRACK_SCHEMA_VERSION,
        "track_id": "general_solution",
        "judge_profile": "general",
        "cases": [{"case_id": case_id} for case_id in GENERAL_CASE_IDS],
    }


def representation_manifest() -> dict:
    return {
        "schema_version": TRACK_SCHEMA_VERSION,
        "track_id": "representation_semantics",
        "judge_profile": "representation_semantics",
        "subject_policy": {
            "web_search_enabled": False,
            "expose_snapshot_provenance": False,
            "preserve_snapshot_git": False,
        },
        "cases": [
            {
                "case_id": case_id,
                "figure_policy": REPRESENTATION_POLICIES[case_id][0],
                "formula_policy": REPRESENTATION_POLICIES[case_id][1],
            }
            for case_id in REPRESENTATION_CASE_IDS
        ],
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test\n", encoding="utf-8")
