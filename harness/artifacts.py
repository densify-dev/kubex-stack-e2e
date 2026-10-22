"""Diagnostics and summary output, written on success and on failure."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping, Sequence

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


_VERSION_LABELS = {
    "chart_version": "Chart",
    "collector_version": "Collector",
    "collector_image_requested": "Collector image",
    "collector_image_digest": "Collector digest",
}


def _table(header: tuple[str, str], rows: list[tuple[str, object]]) -> list[str]:
    return [f"| {header[0]} | {header[1]} |", "| --- | --- |", *(f"| {k} | {v} |" for k, v in rows), ""]


def summary(
    artifacts: Path,
    scenario_name: str,
    status: str,
    versions: Mapping[str, object],
    coverage_warnings: Sequence[str] | None = None,
    **details: object,
) -> None:
    lines = [f"# Scenario {scenario_name}", "", f"**Stack run:** {status}", ""]
    lines += _table(
        ("Version", "Value"),
        [(label, f"`{versions[key]}`" if versions.get(key) else "unknown") for key, label in _VERSION_LABELS.items()],
    )
    if details:
        lines += _table(("Run", "Value"), [(k.replace("_", " ").capitalize(), v) for k, v in details.items()])
    if coverage_warnings is not None:
        lines += ["## Coverage warnings", "", *([f"- {w}" for w in coverage_warnings] or ["none"]), ""]
    write(artifacts / "summary.md", "\n".join(lines))


def _command_escape(value: str, *, property_value: bool = False) -> str:
    value = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return value.replace(":", "%3A").replace(",", "%2C") if property_value else value


# GitHub keeps at most 10 error annotations per step and drops the rest silently.
ANNOTATION_LIMIT = 10

Failure = tuple[str, str, int, str]  # test, file, line, message
Group = tuple[str, list[tuple[str, str, int]]]  # message, its (test, file, line) entries


def group_failures(failures: Sequence[Failure]) -> list[Group]:
    """Failures with the same message share one group, so a broken fixture is one entry, not forty."""

    grouped: dict[str, list[tuple[str, str, int]]] = {}
    for test, file, line, message in failures:
        grouped.setdefault(message, []).append((test, file, line))
    return list(grouped.items())


def _listed(tests: Sequence[tuple[str, str, int]], shown: int) -> list[str]:
    more = [f"and {len(tests) - shown} more"] if len(tests) > shown else []
    return [test for test, _, _ in tests[:shown]] + more


def failure_table(groups: Sequence[Group], shown: int = 5) -> list[str]:
    """Markdown table lines, one row per group, pointing at the group's first test."""

    def cell(value: str) -> str:
        return value.replace("|", "\\|")

    rows = []
    for message, tests in groups:
        _, file, line = tests[0]
        names = "<br>".join(cell(name) for name in _listed(tests, shown))
        rows.append(f"| {names} | {cell(message)} | `{file}:{line}` |")
    return ["| Test | Failure | Where |", "| --- | --- | --- |", *rows]


def failure_annotation(group: Group, shown: int = 5, prefix: str = "") -> str:
    """One ``::error`` annotation on the group's first test."""

    message, tests = group
    _, file, line = tests[0]
    title = prefix + ", ".join(_listed(tests, shown))
    return (
        f"::error file={_command_escape(file, property_value=True)},line={line},"
        f"title={_command_escape(title, property_value=True)}::{_command_escape(message)}"
    )


def failure_digest(failures: Sequence[Failure], shown: int = 5) -> str:
    """Markdown failure table for a scenario's ``failures.md``."""

    table = failure_table(group_failures(failures), shown)
    return "\n".join(["## Failed tests", "", f"{len(failures)} failed.", "", *table]) + "\n\n"
