"""Shared pytest fixtures wiring the harness.

A scenario module declares ``SCENARIO``; the module-scoped ``run`` fixture here provisions (or replays)
it, and ``archive`` and ``oracle`` derive from that run. Cluster lifecycle lives entirely in the
harness: scenario modules never provision anything at import time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from harness.archive import Archive
from harness.oracle import Oracle
from harness.run import Run, execute, replay

REPO_ROOT = Path(__file__).parent.resolve()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("kind-functional")
    group.addoption("--stack-chart-version", default=None, help="override the stack chart version")
    group.addoption(
        "--collector-image",
        default=None,
        help="override the collector image (empty = scenario spec, else the chart default)",
    )
    group.addoption(
        "--replay",
        default=None,
        help="artifact directory of a previous run (needs snapshots.json, replay.json, extracted-csv/); no cluster is touched",
    )
    group.addoption("--keep-cluster", action="store_true", help="leave the Kind cluster running")
    group.addoption(
        "--artifacts-root",
        default="artifacts/kind-functional",
        help="directory for per-scenario artifacts",
    )


def _headline(message: str) -> str:
    """The first two lines of the failure: the custom message and its comparison or first detail."""

    lines = [line.strip() for line in message.removeprefix("AssertionError: ").splitlines() if line.strip()]
    return " — ".join(lines[:2])[:300]


def _failure(report: pytest.TestReport, rootpath: Path) -> tuple[str, str, int, str]:
    crash = getattr(report.longrepr, "reprcrash", None)
    raw = crash.message if crash else str(report.longrepr)
    message = _headline(raw)
    # An assertion in the test body is a collector-vs-truth finding; anything else is the harness or setup.
    if report.when != "call" or not raw.startswith(("AssertionError", "assert ")):
        message = f"[{report.when} error] {message}"
    location, line, _ = report.location
    # The failing line (harness helpers hide their frames), else the test definition.
    path, line = (crash.path, crash.lineno) if crash else (str(rootpath / location), (line or 0) + 1)
    path = os.path.relpath(path, rootpath)
    test = report.nodeid.split("::", 1)[-1] + ("" if report.when == "call" else f" ({report.when})")
    return test, path, line, message


def pytest_assertrepr_compare(op: str, left: object, right: object) -> list[str] | None:
    """Tests are written ``assert actual == expected``: name the sides and show only what differs."""

    if op != "==":
        return None
    if isinstance(left, dict) and isinstance(right, dict):
        absent = "<absent>"
        keys = sorted(left.keys() | right.keys(), key=repr)
        diff = [f"{k!r}: actual {left.get(k, absent)!r}, expected {right.get(k, absent)!r}" for k in keys if left.get(k, absent) != right.get(k, absent)]
        return [f"{len(diff)} of {len(keys)} keys differ", *diff[:10], *([f"... and {len(diff) - 10} more"] if len(diff) > 10 else [])]
    if isinstance(left, (set, frozenset)) and isinstance(right, (set, frozenset)):
        missing, unexpected = sorted(right - left, key=repr), sorted(left - right, key=repr)
        return [f"sets differ: missing {len(missing)}, unexpected {len(unexpected)}", *(f"missing {v!r}" for v in missing[:10]), *(f"unexpected {v!r}" for v in unexpected[:10])]
    text = f"actual {left!r}, expected {right!r}"
    return [text] if len(text) <= 300 else None


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """CI sets FAILURES_JSON; the run-level report job reads it and owns every annotation."""

    if not (failures_path := os.environ.get("FAILURES_JSON")):
        return
    reports = [r for kind in ("failed", "error") for r in terminalreporter.stats.get(kind, [])]
    failures = [_failure(r, terminalreporter.config.rootpath) for r in reports]
    if failures:
        path = Path(failures_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(failures), encoding="utf-8")


@pytest.fixture(scope="module")
def run(request: pytest.FixtureRequest, pytestconfig: pytest.Config) -> Iterator[Run]:
    """The provisioned (or replayed) run of the module's ``SCENARIO``. One cluster per scenario module."""

    scenario = request.module.SCENARIO
    if replay_dir := pytestconfig.getoption("replay"):
        yield replay(scenario, Path(replay_dir).resolve())
        return
    with execute(
        scenario,
        Path(request.module.__file__).parent,
        REPO_ROOT,
        (REPO_ROOT / str(pytestconfig.getoption("artifacts_root"))).resolve(),
        pytestconfig.getoption("stack_chart_version"),
        pytestconfig.getoption("collector_image") or None,
        bool(pytestconfig.getoption("keep_cluster")),
    ) as active:
        yield active


@pytest.fixture(scope="module")
def archive(run: Run) -> Archive:
    return run.archive


@pytest.fixture(scope="module")
def oracle(run: Run) -> Oracle:
    return run.oracle
