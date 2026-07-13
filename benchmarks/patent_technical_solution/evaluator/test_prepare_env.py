from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .prepare_env import prepare_exploration_environment


def test_representation_snapshot_hides_provenance_and_strips_git(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    run_git(["init"], cwd=source_repo)
    (source_repo / "source.txt").write_text("frozen\n", encoding="utf-8")
    run_git(["add", "source.txt"], cwd=source_repo)
    run_git(
        [
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=benchmark@example.invalid",
            "commit",
            "-m",
            "frozen",
        ],
        cwd=source_repo,
    )
    commit = run_git(["rev-parse", "HEAD"], cwd=source_repo).stdout.strip()

    case_dir = tmp_path / "case"
    case_dir.mkdir()
    repo_url = str(source_repo.resolve())
    (case_dir / "snapshot.json").write_text(
        json.dumps({"repo_url": repo_url, "commit": commit}),
        encoding="utf-8",
    )
    destination = tmp_path / "prepared"

    prepare_exploration_environment(
        case_dir,
        destination,
        expose_snapshot_provenance=False,
        preserve_snapshot_git=False,
    )

    environment = (destination / "ENVIRONMENT.md").read_text(encoding="utf-8")
    assert repo_url not in environment
    assert commit not in environment
    assert not (destination / "project_snapshot" / ".git").exists()
    assert (destination / "project_snapshot" / "source.txt").read_text(encoding="utf-8") == "frozen\n"


def test_general_snapshot_keeps_existing_provenance_and_git_defaults(tmp_path: Path) -> None:
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    run_git(["init"], cwd=source_repo)
    (source_repo / "source.txt").write_text("frozen\n", encoding="utf-8")
    run_git(["add", "source.txt"], cwd=source_repo)
    run_git(
        [
            "-c",
            "user.name=Benchmark",
            "-c",
            "user.email=benchmark@example.invalid",
            "commit",
            "-m",
            "frozen",
        ],
        cwd=source_repo,
    )
    commit = run_git(["rev-parse", "HEAD"], cwd=source_repo).stdout.strip()
    case_dir = tmp_path / "case"
    case_dir.mkdir()
    repo_url = str(source_repo.resolve())
    (case_dir / "snapshot.json").write_text(
        json.dumps({"repo_url": repo_url, "commit": commit}),
        encoding="utf-8",
    )
    destination = tmp_path / "prepared"

    prepare_exploration_environment(case_dir, destination)

    environment = (destination / "ENVIRONMENT.md").read_text(encoding="utf-8")
    assert repo_url in environment
    assert commit in environment
    assert (destination / "project_snapshot" / ".git").is_dir()


def run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
