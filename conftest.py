"""Shared pytest fixtures wiring the harness.

A scenario module declares a session-scoped ``run`` fixture built from
``stack_run``; ``archive`` and ``oracle`` here derive from whatever ``run`` that
module defines. Cluster lifecycle lives entirely in the harness — scenario
modules never provision anything at import time.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from harness.archive import Archive
from harness.oracle import Oracle
from harness.run import Run, execute
from harness.spec import ScenarioSpec

REPO_ROOT = Path(__file__).parent.resolve()


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("kind-functional")
    group.addoption("--stack-chart-version", default=None, help="override the stack chart version")
    group.addoption("--keep-cluster", action="store_true", help="leave the Kind cluster running")
    group.addoption(
        "--artifacts-root",
        default="artifacts/kind-functional",
        help="directory for per-scenario artifacts",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "pr: fast tier, gates pull requests")
    config.addinivalue_line("markers", "release: full tier")


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def stack_run(pytestconfig: pytest.Config, repo_root: Path):
    """Factory yielding a provisioned :class:`Run` for a scenario spec."""

    artifacts_root = (repo_root / str(pytestconfig.getoption("artifacts_root"))).resolve()
    chart_version = pytestconfig.getoption("stack_chart_version")
    keep_cluster = bool(pytestconfig.getoption("keep_cluster"))

    @contextmanager
    def _run(scenario: ScenarioSpec, scenario_dir: Path) -> Iterator[Run]:
        with execute(
            scenario,
            scenario_dir,
            repo_root,
            artifacts_root,
            chart_version,
            keep_cluster,
        ) as active:
            yield active

    return _run


@pytest.fixture(scope="session")
def archive(run: Run) -> Archive:
    return run.archive


@pytest.fixture(scope="session")
def oracle(run: Run) -> Oracle:
    return run.oracle
