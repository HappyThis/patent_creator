from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def prepare_exploration_environment(
    case_dir: Path,
    destination: Path,
    *,
    expose_snapshot_provenance: bool = True,
    preserve_snapshot_git: bool = True,
) -> Path:
    """Prepare the agent-visible exploration environment for one benchmark case."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    if (case_dir / "snapshot.json").exists():
        material_type = "software_project"
        prepare_project_checkout(
            case_dir,
            destination / "project_snapshot",
            preserve_git_metadata=preserve_snapshot_git,
        )
    else:
        raise ValueError("software_project case requires snapshot.json.")

    write_environment_readme(
        case_dir,
        destination,
        material_type,
        expose_snapshot_provenance=expose_snapshot_provenance,
    )
    return destination

def write_environment_readme(
    case_dir: Path,
    destination: Path,
    material_type: str,
    *,
    expose_snapshot_provenance: bool = True,
) -> None:
    lines = [
        "# Exploration Environment",
        "",
        "This directory is visible to the benchmark subject agent.",
        "",
        f"Material type: `{material_type}`.",
        "",
        "## Contents",
        "",
    ]
    if (destination / "project_snapshot").exists():
        lines.append("- `project_snapshot/`: frozen Git project checkout. This is the primary exploration target.")
        if expose_snapshot_provenance:
            snapshot = load_snapshot(case_dir)
            lines.extend(
                [
                    "",
                    "## Project Snapshot",
                    "",
                    f"- Repository: `{snapshot_repo_url(snapshot)}`",
                    f"- Commit: `{commit_from_snapshot(snapshot)}`",
                ]
            )
    if len(lines) == 8:
        lines.append("- No additional files were provided.")
    lines.extend(
        [
            "",
            "## Exploration Rule",
            "",
            "Inspect `project_snapshot/` with tools before drafting.",
        ]
    )
    (destination / "ENVIRONMENT.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def load_snapshot(case_dir: Path) -> dict[str, Any]:
    return json.loads((case_dir / "snapshot.json").read_text(encoding="utf-8"))


def snapshot_repo_url(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("repo_url")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("snapshot.json 缺少 repo_url。")
    return value.strip()


def commit_from_snapshot(snapshot: dict[str, Any]) -> str:
    value = snapshot.get("commit")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("snapshot.json 缺少 commit。")
    return value.strip()


def prepare_project_checkout(
    case_dir: Path,
    destination: Path,
    *,
    preserve_git_metadata: bool = True,
) -> Path:
    snapshot = load_snapshot(case_dir)
    repo_url = snapshot_repo_url(snapshot)
    commit = commit_from_snapshot(snapshot)

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and (destination / ".git").exists():
        current = _run(["git", "rev-parse", "HEAD"], cwd=destination, check=False).stdout.strip()
        if current == commit:
            if not preserve_git_metadata:
                shutil.rmtree(destination / ".git")
            return destination

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    _run(["git", "init"], cwd=destination)
    _run(["git", "remote", "add", "origin", repo_url], cwd=destination)
    fetch = _run(
        ["git", "fetch", "--depth", "1", "origin", commit],
        cwd=destination,
        check=False,
    )
    if fetch.returncode != 0:
        _run(["git", "fetch", "origin", commit], cwd=destination)
    _run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=destination)
    if not preserve_git_metadata:
        shutil.rmtree(destination / ".git")

    return destination


def _run(
    command: list[str],
    *,
    cwd: Path,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode != 0:
        command_text = " ".join(command)
        message = completed.stderr.strip() or completed.stdout.strip() or "命令执行失败。"
        raise RuntimeError(f"{command_text}: {message}")
    return completed
