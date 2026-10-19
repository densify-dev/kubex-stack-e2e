"""Shared pytest fixtures wiring the harness.

A scenario module declares a session-scoped ``run`` fixture built from
``stack_run``; ``archive`` and ``oracle`` here derive from whatever ``run`` that
module defines. Cluster lifecycle lives entirely in the harness — scenario
modules never provision anything at import time.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from harness.archive import Archive
from harness.artifacts import failure_digest
from harness.coverage import workflow_warning
from harness.oracle import Oracle
from harness.run import Run, execute, replay
from harness.spec import ScenarioSpec

REPO_ROOT = Path(__file__).parent.resolve()

# Collected from every Run at session end; printed as one CI annotation by pytest_terminal_summary.
COVERAGE_WARNINGS: list[str] = []


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


def _failure(report: pytest.TestReport, rootpath: Path) -> tuple[str, str, int, str]:
    crash = getattr(report.longrepr, "reprcrash", None)
    # A custom assert message comes first and pytest's comparison follows it, so keep both on one line.
    message = " ".join((crash.message if crash else str(report.longrepr)).split())[:300]
    location, line, _ = report.location
    # Relative to the repository, not pytest's rootdir, which path-like option values can move.
    path = os.path.relpath(rootpath / location, REPO_ROOT)
    test = report.nodeid.split("::", 1)[-1] + ("" if report.when == "call" else f" ({report.when})")
    return test, path, (line or 0) + 1, message


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    if COVERAGE_WARNINGS:
        terminalreporter.write_line(workflow_warning(COVERAGE_WARNINGS))
    reports = [r for kind in ("failed", "error") for r in terminalreporter.stats.get(kind, [])]
    failures = [_failure(r, terminalreporter.config.rootpath) for r in reports]
    if failures:
        table, annotations = failure_digest(failures)
        for annotation in annotations:
            terminalreporter.write_line(annotation)
        if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary_path, "a", encoding="utf-8") as summary_file:
                summary_file.write(table)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "pr: fast tier, gates pull requests")
    config.addinivalue_line("markers", "release: full tier")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def stack_run(pytestconfig: pytest.Config, repo_root: Path, tmp_path_factory: pytest.TempPathFactory):
    """Factory yielding a provisioned :class:`Run` for a scenario spec."""

    artifacts_root = (repo_root / str(pytestconfig.getoption("artifacts_root"))).resolve()
    chart_version = pytestconfig.getoption("stack_chart_version")
    collector_image = pytestconfig.getoption("collector_image") or None
    keep_cluster = bool(pytestconfig.getoption("keep_cluster"))
    replay_dir = pytestconfig.getoption("replay")

    @contextmanager
    def _run(scenario: ScenarioSpec, scenario_dir: Path) -> Iterator[Run]:
        if replay_dir:
            active = replay(scenario, Path(replay_dir).resolve(), tmp_path_factory.mktemp("replay"))
            yield active
        else:
            with execute(
                scenario, scenario_dir, repo_root, artifacts_root, chart_version, collector_image, keep_cluster
            ) as active:
                yield active
        COVERAGE_WARNINGS.extend(active.coverage_warnings)

    return _run


@pytest.fixture(scope="session")
def archive(run: Run) -> Archive:
    return run.archive


@pytest.fixture(scope="session")
def oracle(run: Run) -> Oracle:
    return run.oracle
