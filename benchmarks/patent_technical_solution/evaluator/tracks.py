from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


BENCHMARK_DIR = Path(__file__).resolve().parents[1]
DEFAULT_TRACK_ID = "general_solution"
TRACK_SCHEMA_VERSION = "patent-technical-solution-track-v1"
STANDALONE_SCHEMA_VERSION = "patent-solution-benchmark-v1"

JudgeProfile = Literal["general", "representation_semantics"]
RepresentationPolicy = Literal["recommended", "optional"]

_TRACK_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_CASE_ID_PATTERN = re.compile(r"^[0-9]{3}$")
_REPRESENTATION_POLICIES = frozenset({"recommended", "optional"})
_SOURCE_CASE_FILES = ("request.md", "snapshot.json", "reference_solution.md", "rubric.md")
_BUILTIN_TRACKS: dict[str, tuple[JudgeProfile, tuple[str, ...]]] = {
    "general_solution": (
        "general",
        tuple(f"{value:03d}" for value in range(1, 11)),
    ),
}
_BUILTIN_BENCHMARKS: dict[str, tuple[JudgeProfile, tuple[str, ...]]] = {
    "patent_representation_semantics": (
        "representation_semantics",
        ("001", "004", "006", "008", "009", "010", "011", "012", "013", "014"),
    ),
}


class TrackConfigError(ValueError):
    """Raised when a benchmark track manifest or its referenced files are invalid."""


@dataclass(frozen=True, slots=True)
class SubjectPolicy:
    web_search_enabled: bool | None
    expose_snapshot_provenance: bool
    preserve_snapshot_git: bool


@dataclass(frozen=True, slots=True)
class TrackCasePolicy:
    case_id: str
    figure_policy: RepresentationPolicy | None = None
    formula_policy: RepresentationPolicy | None = None


@dataclass(frozen=True, slots=True)
class DefaultJudge:
    model: str
    provider: str
    reasoning_effort: str


@dataclass(frozen=True, slots=True)
class TrackConfig:
    track_id: str
    judge_profile: JudgeProfile
    cases: tuple[TrackCasePolicy, ...]
    subject_policy: SubjectPolicy
    benchmark_dir: Path
    track_dir: Path
    manifest_path: Path
    source_cases_dir: Path
    runner_path: Path
    runner_addendum_path: Path | None
    base_judge_path: Path
    default_judge: DefaultJudge | None
    standalone: bool = False

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(case.case_id for case in self.cases)

    @property
    def track_judge_path(self) -> Path | None:
        if self.judge_profile == "representation_semantics":
            filename = "representation_judge.md" if self.standalone else "judge.md"
            return self.track_dir / filename
        return None


@dataclass(frozen=True, slots=True)
class ResolvedTrackCase:
    track_id: str
    judge_profile: JudgeProfile
    case_id: str
    figure_policy: RepresentationPolicy | None
    formula_policy: RepresentationPolicy | None
    source_case_dir: Path
    request_path: Path
    snapshot_path: Path
    reference_solution_path: Path
    base_rubric_path: Path
    track_metadata_path: Path | None
    track_rubric_path: Path | None


def load_track(
    track_id: str | None = None,
    *,
    benchmark_dir: Path | None = None,
) -> TrackConfig:
    """Load and fully validate one benchmark track.

    Validation covers the manifest schema, built-in case membership, source case
    contracts, and representation-specific Judge/rubric overlays. Omitting
    ``track_id`` always resolves to ``general_solution``.
    """

    root = (benchmark_dir or BENCHMARK_DIR).resolve()
    standalone_manifest = root / "benchmark.json"
    if standalone_manifest.is_file():
        return _load_standalone_benchmark(
            root=root,
            manifest_path=standalone_manifest,
            requested_id=track_id,
        )

    selected_id = DEFAULT_TRACK_ID if track_id is None else track_id
    if not isinstance(selected_id, str) or not _TRACK_ID_PATTERN.fullmatch(selected_id):
        raise TrackConfigError(f"invalid track id: {selected_id!r}")

    track_dir = root / "tracks" / selected_id
    manifest_path = track_dir / "track.json"
    standalone_replacement = root.parent / "patent_representation_semantics" / "benchmark.json"
    if (
        selected_id == "representation_semantics"
        and not manifest_path.is_file()
        and standalone_replacement.is_file()
    ):
        raise TrackConfigError(
            "representation_semantics is now a standalone benchmark; use "
            "benchmarks/patent_representation_semantics/bench.py"
        )
    raw = _read_json_object(manifest_path)
    _require_exact_keys(
        raw,
        required={"schema_version", "track_id", "judge_profile", "cases"},
        optional={"subject_policy"},
        context=str(manifest_path),
    )

    if raw["schema_version"] != TRACK_SCHEMA_VERSION:
        raise TrackConfigError(
            f"{manifest_path}: schema_version must be {TRACK_SCHEMA_VERSION!r}"
        )
    if raw["track_id"] != selected_id:
        raise TrackConfigError(
            f"{manifest_path}: track_id {raw['track_id']!r} does not match directory {selected_id!r}"
        )

    judge_profile = raw["judge_profile"]
    if judge_profile not in {"general", "representation_semantics"}:
        raise TrackConfigError(f"{manifest_path}: unsupported judge_profile {judge_profile!r}")

    cases = _parse_cases(raw["cases"], judge_profile=judge_profile, manifest_path=manifest_path)
    subject_policy = _parse_subject_policy(
        raw.get("subject_policy"),
        judge_profile=judge_profile,
        manifest_path=manifest_path,
    )
    track = TrackConfig(
        track_id=selected_id,
        judge_profile=judge_profile,
        cases=cases,
        subject_policy=subject_policy,
        benchmark_dir=root,
        track_dir=track_dir,
        manifest_path=manifest_path,
        source_cases_dir=root / "cases",
        runner_path=root / "runner.md",
        runner_addendum_path=(track_dir / "runner.md" if (track_dir / "runner.md").is_file() else None),
        base_judge_path=root / "judge.md",
        default_judge=None,
    )
    _validate_builtin_contract(track)
    _require_file(track.runner_path, "shared Agent runner rules")
    _require_file(track.base_judge_path, "shared Judge rules")
    if track.track_judge_path is not None:
        _require_file(track.track_judge_path, "track Judge rules")
    for case in track.cases:
        resolve_track_case(track, case.case_id)
    return track


def resolve_track_case(track: TrackConfig, case_id: str | int) -> ResolvedTrackCase:
    """Resolve and validate one case that belongs to ``track``."""

    normalized = _normalize_case_id(case_id)
    policy = next((item for item in track.cases if item.case_id == normalized), None)
    if policy is None:
        raise TrackConfigError(f"case {normalized} is not part of track {track.track_id}")

    source_case_dir = track.source_cases_dir / normalized
    if not source_case_dir.is_dir():
        raise TrackConfigError(f"source case directory does not exist: {source_case_dir}")
    source_paths = {name: source_case_dir / name for name in _SOURCE_CASE_FILES}
    for name, path in source_paths.items():
        _require_file(path, f"source case {normalized} {name}")

    track_rubric_path: Path | None = None
    track_metadata_path: Path | None = None
    if track.judge_profile == "representation_semantics":
        track_case_dir = source_case_dir if track.standalone else track.track_dir / "cases" / normalized
        track_metadata_path = track_case_dir / "metadata.json"
        _require_file(track_metadata_path, f"track metadata for case {normalized}")
        metadata = _read_json_object(track_metadata_path)
        _require_exact_keys(
            metadata,
            required={"figure_policy", "formula_policy"},
            optional=set(),
            context=str(track_metadata_path),
        )
        if (
            metadata["figure_policy"] != policy.figure_policy
            or metadata["formula_policy"] != policy.formula_policy
        ):
            raise TrackConfigError(
                f"{track_metadata_path}: policies must match {track.manifest_path}"
            )
        rubric_name = "representation_rubric.md" if track.standalone else "rubric.md"
        track_rubric_path = track_case_dir / rubric_name
        _require_file(track_rubric_path, f"track rubric for case {normalized}")

    return ResolvedTrackCase(
        track_id=track.track_id,
        judge_profile=track.judge_profile,
        case_id=normalized,
        figure_policy=policy.figure_policy,
        formula_policy=policy.formula_policy,
        source_case_dir=source_case_dir,
        request_path=source_paths["request.md"],
        snapshot_path=source_paths["snapshot.json"],
        reference_solution_path=source_paths["reference_solution.md"],
        base_rubric_path=source_paths["rubric.md"],
        track_metadata_path=track_metadata_path,
        track_rubric_path=track_rubric_path,
    )


def _load_standalone_benchmark(
    *,
    root: Path,
    manifest_path: Path,
    requested_id: str | None,
) -> TrackConfig:
    raw = _read_json_object(manifest_path)
    _require_exact_keys(
        raw,
        required={
            "schema_version",
            "benchmark_id",
            "judge_profile",
            "subject_policy",
            "default_judge",
            "cases",
        },
        optional=set(),
        context=str(manifest_path),
    )
    if raw["schema_version"] != STANDALONE_SCHEMA_VERSION:
        raise TrackConfigError(
            f"{manifest_path}: schema_version must be {STANDALONE_SCHEMA_VERSION!r}"
        )
    benchmark_id = raw["benchmark_id"]
    if not isinstance(benchmark_id, str) or not _TRACK_ID_PATTERN.fullmatch(benchmark_id):
        raise TrackConfigError(f"{manifest_path}: invalid benchmark_id {benchmark_id!r}")
    if requested_id is not None and requested_id != benchmark_id:
        raise TrackConfigError(
            f"{manifest_path}: benchmark_id is {benchmark_id!r}, not {requested_id!r}"
        )
    judge_profile = raw["judge_profile"]
    if judge_profile not in {"general", "representation_semantics"}:
        raise TrackConfigError(f"{manifest_path}: unsupported judge_profile {judge_profile!r}")

    cases = _parse_cases(raw["cases"], judge_profile=judge_profile, manifest_path=manifest_path)
    track = TrackConfig(
        track_id=benchmark_id,
        judge_profile=judge_profile,
        cases=cases,
        subject_policy=_parse_subject_policy(
            raw["subject_policy"],
            judge_profile=judge_profile,
            manifest_path=manifest_path,
        ),
        benchmark_dir=root,
        track_dir=root,
        manifest_path=manifest_path,
        source_cases_dir=root / "cases",
        runner_path=root / "runner.md",
        runner_addendum_path=(
            root / "representation_runner.md"
            if judge_profile == "representation_semantics"
            else None
        ),
        base_judge_path=root / "judge.md",
        default_judge=_parse_default_judge(raw["default_judge"], manifest_path=manifest_path),
        standalone=True,
    )
    _validate_builtin_contract(track)
    _require_file(track.runner_path, "Agent runner rules")
    _require_file(track.base_judge_path, "Judge rules")
    if track.runner_addendum_path is not None:
        _require_file(track.runner_addendum_path, "representation Agent runner rules")
    if track.track_judge_path is not None:
        _require_file(track.track_judge_path, "representation Judge rules")
    for case in track.cases:
        resolve_track_case(track, case.case_id)
    return track


def _parse_default_judge(value: Any, *, manifest_path: Path) -> DefaultJudge:
    if not isinstance(value, dict):
        raise TrackConfigError(f"{manifest_path}: default_judge must be an object")
    _require_exact_keys(
        value,
        required={"model", "provider", "reasoning_effort"},
        optional=set(),
        context=f"{manifest_path}: default_judge",
    )
    for key in ("model", "provider", "reasoning_effort"):
        item = value[key]
        if not isinstance(item, str) or not item.strip():
            raise TrackConfigError(f"{manifest_path}: default_judge.{key} must be non-empty")
    return DefaultJudge(
        model=value["model"].strip(),
        provider=value["provider"].strip(),
        reasoning_effort=value["reasoning_effort"].strip().lower(),
    )


def _parse_cases(
    raw_cases: Any,
    *,
    judge_profile: JudgeProfile,
    manifest_path: Path,
) -> tuple[TrackCasePolicy, ...]:
    if not isinstance(raw_cases, list) or not raw_cases:
        raise TrackConfigError(f"{manifest_path}: cases must be a non-empty array")

    parsed: list[TrackCasePolicy] = []
    seen: set[str] = set()
    representation = judge_profile == "representation_semantics"
    expected_keys = (
        {"case_id", "figure_policy", "formula_policy"}
        if representation
        else {"case_id"}
    )
    for index, raw_case in enumerate(raw_cases):
        context = f"{manifest_path}: cases[{index}]"
        if not isinstance(raw_case, dict):
            raise TrackConfigError(f"{context} must be an object")
        _require_exact_keys(raw_case, required=expected_keys, optional=set(), context=context)
        case_id = raw_case["case_id"]
        if not isinstance(case_id, str) or not _CASE_ID_PATTERN.fullmatch(case_id):
            raise TrackConfigError(f"{context}.case_id must be exactly three digits")
        if case_id in seen:
            raise TrackConfigError(f"{manifest_path}: duplicate case id {case_id}")
        seen.add(case_id)

        figure_policy = None
        formula_policy = None
        if representation:
            figure_policy = _parse_representation_policy(
                raw_case["figure_policy"], context=f"{context}.figure_policy"
            )
            formula_policy = _parse_representation_policy(
                raw_case["formula_policy"], context=f"{context}.formula_policy"
            )
        parsed.append(
            TrackCasePolicy(
                case_id=case_id,
                figure_policy=figure_policy,
                formula_policy=formula_policy,
            )
        )
    return tuple(parsed)


def _parse_representation_policy(value: Any, *, context: str) -> RepresentationPolicy:
    if not isinstance(value, str) or value not in _REPRESENTATION_POLICIES:
        allowed = ", ".join(sorted(_REPRESENTATION_POLICIES))
        raise TrackConfigError(f"{context} must be one of: {allowed}")
    return value  # type: ignore[return-value]


def _parse_subject_policy(
    raw_policy: Any,
    *,
    judge_profile: JudgeProfile,
    manifest_path: Path,
) -> SubjectPolicy:
    if judge_profile == "general":
        if raw_policy is not None:
            raise TrackConfigError(
                f"{manifest_path}: general track must inherit the existing subject policy"
            )
        return SubjectPolicy(
            web_search_enabled=None,
            expose_snapshot_provenance=True,
            preserve_snapshot_git=True,
        )

    if not isinstance(raw_policy, dict):
        raise TrackConfigError(f"{manifest_path}: representation track requires subject_policy")
    _require_exact_keys(
        raw_policy,
        required={
            "web_search_enabled",
            "expose_snapshot_provenance",
            "preserve_snapshot_git",
        },
        optional=set(),
        context=f"{manifest_path}: subject_policy",
    )
    for key, value in raw_policy.items():
        if not isinstance(value, bool):
            raise TrackConfigError(f"{manifest_path}: subject_policy.{key} must be boolean")
    if raw_policy["web_search_enabled"]:
        raise TrackConfigError(f"{manifest_path}: representation track must disable web search")
    if raw_policy["expose_snapshot_provenance"]:
        raise TrackConfigError(f"{manifest_path}: representation track must hide snapshot provenance")
    if raw_policy["preserve_snapshot_git"]:
        raise TrackConfigError(f"{manifest_path}: representation track must strip snapshot Git metadata")
    return SubjectPolicy(
        web_search_enabled=False,
        expose_snapshot_provenance=False,
        preserve_snapshot_git=False,
    )


def _validate_builtin_contract(track: TrackConfig) -> None:
    expected = (
        _BUILTIN_BENCHMARKS.get(track.track_id)
        if track.standalone
        else _BUILTIN_TRACKS.get(track.track_id)
    )
    if expected is None:
        return
    expected_profile, expected_case_ids = expected
    if track.judge_profile != expected_profile:
        raise TrackConfigError(
            f"{track.manifest_path}: built-in track {track.track_id} must use "
            f"judge_profile {expected_profile!r}"
        )
    if track.case_ids != expected_case_ids:
        raise TrackConfigError(
            f"{track.manifest_path}: built-in track {track.track_id} must contain cases "
            f"{list(expected_case_ids)!r} in that order"
        )


def _normalize_case_id(value: str | int) -> str:
    if isinstance(value, bool):
        raise TrackConfigError(f"invalid case id: {value!r}")
    raw = str(value).strip()
    if not raw.isdigit() or len(raw) > 3:
        raise TrackConfigError(f"invalid case id: {value!r}")
    normalized = raw.zfill(3)
    if not _CASE_ID_PATTERN.fullmatch(normalized):
        raise TrackConfigError(f"invalid case id: {value!r}")
    return normalized


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise TrackConfigError(f"track manifest does not exist: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrackConfigError(f"cannot read track manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TrackConfigError(f"{path}: manifest must be a JSON object")
    return value


def _require_exact_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    optional: set[str],
    context: str,
) -> None:
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise TrackConfigError(f"{context}: missing fields {sorted(missing)!r}")
    if extra:
        raise TrackConfigError(f"{context}: unexpected fields {sorted(extra)!r}")


def _require_file(path: Path, description: str) -> None:
    if not path.is_file():
        raise TrackConfigError(f"missing {description}: {path}")
