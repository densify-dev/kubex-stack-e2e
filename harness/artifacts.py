"""Diagnostics and summary output, written on success and on failure."""

from __future__ import annotations

import shutil
from pathlib import Path

from harness.command import kubectl, kubectl_json, write


def prepare(artifacts_root: Path, scenario_name: str) -> Path:
    """Create a clean per-scenario artifact directory under the root."""

    artifacts_root = artifacts_root.resolve()
    artifacts_root.mkdir(parents=True, exist_ok=True)
    target = (artifacts_root / scenario_name).resolve()
    if target == artifacts_root or target.parent != artifacts_root:
        raise ValueError(f"unsafe scenario artifact path: {target}")
    if (artifacts_root / scenario_name).is_symlink():
        raise ValueError(f"scenario artifact path is a symlink: {artifacts_root / scenario_name}")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    return target


def collect(repo_root: Path, artifacts: Path, namespace: str, job: str | None) -> None:
    """Best-effort cluster diagnostics. Never raises — it runs on the failure path."""

    directory = artifacts / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    commands = {
        "pods.txt": ["get", "pods", "-A", "-o", "wide"],
        "events.txt": ["get", "events", "-A", "--sort-by=.lastTimestamp"],
        "nodes.txt": ["get", "nodes", "-o", "wide"],
        "endpointslices.yaml": ["get", "endpointslice", "-A", "-o", "yaml"],
    }
    if job:
        commands["job.txt"] = ["describe", "job", job, "-n", namespace]
        commands["job.log"] = ["logs", "-n", namespace, f"job/{job}", "--all-containers=true"]
    for filename, args in commands.items():
        try:
            write(directory / filename, kubectl(args, repo_root))
        except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the real error
            write(directory / filename, f"diagnostic command failed: {exc}\n")

    logs = directory / "pod-logs"
    try:
        payload = kubectl_json(["get", "pods", "-A", "-o", "json"], repo_root)
        items = payload.get("items", []) if isinstance(payload, dict) else []
        for pod in items if isinstance(items, list) else []:
            metadata = pod.get("metadata", {}) if isinstance(pod, dict) else {}
            namespace = str(metadata.get("namespace", "default"))
            name = str(metadata.get("name", ""))
            if not name:
                continue
            try:
                output = kubectl(
                    [
                        "logs",
                        "-n",
                        namespace,
                        name,
                        "--all-containers=true",
                        "--prefix=true",
                        "--tail=500",
                    ],
                    repo_root,
                )
            except Exception as exc:  # noqa: BLE001 - one pending pod must not stop diagnostics
                output = f"pod log command failed: {exc}\n"
            write(logs / f"{namespace}__{name}.log", output)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the real error
        write(logs / "collection-error.txt", f"pod log discovery failed: {exc}\n")


def summary(artifacts: Path, scenario_name: str, status: str, **details: object) -> None:
    lines = [f"# Scenario {scenario_name}", "", f"**Status:** {status}"]
    lines.extend(f"**{key.replace('_', ' ').capitalize()}:** {value}" for key, value in details.items())
    lines.append("")
    write(artifacts / "summary.md", "\n".join(lines))
