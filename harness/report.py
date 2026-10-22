"""Run-level roll-up of the scenario artifacts: the one job summary and the annotations.

Usage: ``python3 -m harness.report <artifacts-dir> <scenario>...`` where the directory holds the
``kind-functional-<scenario>-<run-id>`` artifact directories that ``actions/download-artifact`` creates.
"""

from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from harness.artifacts import (
    ANNOTATION_LIMIT,
    Failure,
    _command_escape,
    failure_annotation,
    failure_table,
    group_failures,
)
from harness.coverage import workflow_warning

# A scenario with fewer failure groups than this starts expanded in the summary.
OPEN_BELOW = 6
NO_ARTIFACT = "no artifact"


@dataclass
class Scenario:
    name: str
    status: str = NO_ARTIFACT
    passed: int = 0
    failed: int = 0
    failures: list[Failure] = field(default_factory=list)
    versions: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def setup_errors(self) -> int:
        return sum(1 for *_, message in self.failures if message.startswith(("[setup error]", "[call error]")))


def load(directory: Path, name: str) -> Scenario:
    """Read one scenario artifact directory. A missing directory is status ``no artifact``."""

    scenario = Scenario(name)
    if not directory.is_dir():
        return scenario
    summary = (directory / "summary.md").read_text(encoding="utf-8") if (directory / "summary.md").is_file() else ""
    scenario.warnings = re.findall(r"^- (.+)$", summary.partition("## Coverage warnings")[2], re.MULTILINE)
    if (directory / "versions.json").is_file():
        versions = json.loads((directory / "versions.json").read_text(encoding="utf-8"))
        scenario.versions = {key: versions[key] for key in ("chart_version", "collector_version") if versions.get(key)}
    if (directory / "failures.json").is_file():
        scenario.failures = [tuple(f) for f in json.loads((directory / "failures.json").read_text(encoding="utf-8"))]
    scenario.status = "setup error"
    if (directory / "pytest.xml").is_file():
        suite = ET.parse(directory / "pytest.xml").getroot().find("testsuite")
        scenario.failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        scenario.passed = int(suite.get("tests", 0)) - scenario.failed - int(suite.get("skipped", 0))
        # Without a finished stack run the pytest result says nothing about the collector.
        if "**Stack run:** collected" in summary:
            scenario.status = "failed" if scenario.failed else "passed"
    return scenario


def headline(scenarios: Sequence[Scenario]) -> str:
    failed = sum(s.failed for s in scenarios)
    passed = sum(s.passed for s in scenarios)
    bad = sum(s.status != "passed" for s in scenarios)
    text = f"{failed} failed, {passed} passed. {bad} of {len(scenarios)} scenarios failed"
    if missing := sum(s.status == NO_ARTIFACT for s in scenarios):
        text += f" ({missing} without an artifact)"
    text += "."
    versions = []
    for key, label in (("chart_version", "chart"), ("collector_version", "collector")):
        found = sorted({s.versions[key] for s in scenarios if key in s.versions})
        if found:
            versions.append(f"{label} {' and '.join(found)}" + (" (differs between scenarios)" if len(found) > 1 else ""))
    return text + (f" Tested {', '.join(versions)}." if versions else "")


def _warnings(scenarios: Sequence[Scenario]) -> list[str]:
    return list(dict.fromkeys(w for s in scenarios for w in s.warnings))


def markdown(scenarios: Sequence[Scenario], doc_url: str) -> str:
    lines = ["# E2E run summary", "", f"**{headline(scenarios)}**", ""]
    lines += ["| Scenario | Status | Passed | Failed | Setup errors |", "| --- | --- | --- | --- | --- |"]
    for s in scenarios:
        counts = ("-", "-", "-") if s.status == NO_ARTIFACT else (s.passed, s.failed, s.setup_errors)
        lines.append(f"| [{s.name}]({doc_url}/{s.name}.md) | {s.status} | {counts[0]} | {counts[1]} | {counts[2]} |")
    lines.append("")
    with_failures = [s for s in scenarios if s.failures]
    if with_failures:
        lines += ["## Failed tests", ""]
    for s in with_failures:
        groups = group_failures(s.failures)
        lines += [
            f"<details{' open' if len(groups) < OPEN_BELOW else ''}>",
            f"<summary><b>{s.name}</b>: {len(s.failures)} failed, {len(groups)} distinct</summary>",
            "",
            *failure_table(groups),
            "",
            "</details>",
            "",
        ]
    if warnings := _warnings(scenarios):
        lines += ["## Coverage warnings", "", *(f"- {w}" for w in warnings), ""]
    return "\n".join(lines)


def annotations(scenarios: Sequence[Scenario]) -> list[str]:
    """The headline, failure groups taken round-robin across scenarios, a count of the rest, and one coverage warning.

    Round-robin, largest group first, so every failing scenario shows up before any shows a second group.
    """

    ranked = []
    for s in scenarios:
        by_size = sorted(group_failures(s.failures), key=lambda group: -len(group[1]))
        ranked += [(rank, -len(group[1]), group, s.name) for rank, group in enumerate(by_size)]
    groups = [(group, name) for *_, group, name in sorted(ranked, key=lambda item: item[:2])]
    room = ANNOTATION_LIMIT - 1
    shown = groups if len(groups) <= room else groups[: room - 1]
    level = "error" if any(s.status != "passed" for s in scenarios) else "notice"
    result = [f"::{level} title=E2E run summary::{_command_escape(headline(scenarios))}"]
    result += [failure_annotation(group, prefix=f"{name}: ") for group, name in shown]
    if hidden := sum(len(group[1]) for group, _ in groups[len(shown) :]):
        result.append(f"::error title={hidden} more failures::see the run summary")
    if warnings := _warnings(scenarios):
        result.append(workflow_warning(warnings))
    return result


def main(argv: Sequence[str]) -> int:
    root, names = Path(argv[0]), argv[1:]
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    scenarios = [load(root / f"kind-functional-{name}-{run_id}", name) for name in names]
    repo = f"{os.environ.get('GITHUB_SERVER_URL', '')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
    doc_url = f"{repo}/blob/{os.environ.get('GITHUB_SHA', '')}/docs/scenarios"
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a", encoding="utf-8") as summary:
        summary.write(markdown(scenarios, doc_url))
    # The run page lists a job's annotations newest first, so print them last to first.
    print("\n".join(reversed(annotations(scenarios))))
    # The roll-up succeeded. A failed scenario already shows as its own red job.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
