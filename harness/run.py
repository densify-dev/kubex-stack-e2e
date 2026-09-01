"""Orchestrate one scenario run and hand the result to the tests.

The shared pytest fixture calls :func:`execute`. Everything from cluster creation
through archive capture happens here; assertions happen in the scenario module.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from harness import artifacts as artifacts_mod
from harness import cluster, fixtures, prometheus, receiver, stack
from harness.archive import Archive
from harness.archive import load as load_archive
from harness.command import write, write_json
from harness.oracle import Oracle, Snapshot, take_snapshot
from harness.spec import ScenarioSpec

# Snapshot cadence during the collection window. Fixed, so drift detection has
# the same resolution whether the window is 3 minutes or 60.
SNAPSHOT_INTERVAL_SECONDS = 60.0


@dataclass
class Run:
    """Everything a scenario's tests need. Yielded by the shared fixture."""

    scenario: ScenarioSpec
    archive: Archive
    oracle: Oracle
    artifacts: Path
    uploads: int
    job_name: str | None = None
    snapshots: list[Snapshot] = field(default_factory=list)


def _await_window(scenario: ScenarioSpec, repo_root: Path) -> list[Snapshot]:
    """Sleep out the collection window, snapshotting on a fixed cadence.

    Sampling on a cadence rather than only at the endpoints keeps drift detection
    at constant resolution as the window grows. It still only *detects sampled*
    drift — a change that appears and reverts between two snapshots is invisible.
    """

    total = scenario.forwarder.window()
    snapshots = [take_snapshot("collection window start", repo_root)]
    remaining = total
    elapsed = 0.0
    while remaining > 0:
        step = min(SNAPSHOT_INTERVAL_SECONDS, remaining)
        time.sleep(step)
        remaining -= step
        elapsed += step
        label = "collection window end" if remaining <= 0 else f"collection window +{int(elapsed)}s"
        snapshots.append(take_snapshot(label, repo_root))
    return snapshots


def _cleanup_owned_cluster(
    cluster_name: str,
    repo_root: Path,
    artifacts: Path,
    run_failed: bool,
) -> None:
    try:
        cluster.delete(cluster_name, repo_root)
    except Exception as cleanup_error:  # noqa: BLE001
        write(
            artifacts / "diagnostics" / "cleanup.txt",
            f"kind cluster cleanup failed: {cleanup_error}\n",
        )
        if not run_failed:
            raise


@contextmanager
def execute(
    scenario: ScenarioSpec,
    scenario_dir: Path,
    repo_root: Path,
    artifacts_root: Path,
    chart_version: str | None = None,
    keep_cluster: bool = False,
) -> Iterator[Run]:
    """Provision, collect, capture, yield, then always diagnose and tear down."""

    artifacts = artifacts_mod.prepare(artifacts_root, scenario.name)
    cluster_name = scenario.cluster_name
    created = False
    failed = False
    job_name: str | None = None
    try:
        resources = stack.resource_paths(scenario, scenario_dir)
        config_path = artifacts / "kind-config.yaml"
        write(config_path, cluster.render_config(scenario.cluster))
        # Refuse someone else's deterministic cluster before arming cleanup.
        # Once absence is established, cleanup owns any partial Kind creation.
        cluster.assert_absent(cluster_name, repo_root)
        created = True
        cluster.create(scenario.cluster, cluster_name, config_path, repo_root)
        cluster.configure_nodes(scenario.cluster, cluster_name, repo_root)

        receiver.deploy(repo_root)
        fixtures.deploy(scenario, repo_root, artifacts)
        stack.install(scenario, repo_root, artifacts, receiver.cluster_ip(repo_root), chart_version)
        stack.apply_resources(resources, repo_root)
        stack.await_lifecycle(scenario, repo_root)

        prometheus.await_ready(scenario.prometheus, scenario.stack.namespace, repo_root, artifacts)

        snapshots = _await_window(scenario, repo_root)
        job_name = stack.trigger_forwarder(scenario, repo_root)
        state = receiver.capture_state(repo_root, artifacts)
        snapshots.append(take_snapshot("after upload", repo_root))

        archive = load_archive(
            receiver.archive_bytes(state), scenario.prefix, artifacts / "extracted-csv"
        )
        uploads = receiver.archive_upload_count(state)
        oracle = Oracle(snapshots=snapshots)
        write_json(artifacts / "archive-paths.json", archive.paths)

        run = Run(
            scenario=scenario,
            archive=archive,
            oracle=oracle,
            artifacts=artifacts,
            uploads=uploads,
            job_name=job_name,
            snapshots=snapshots,
        )
        yield run

        # Runs after the tests, whether they passed or failed — a generator
        # fixture is always resumed for teardown. The oracle's evidence is only
        # complete once the tests have queried it.
        write_json(artifacts / "kubernetes-oracles.json", oracle.evidence)
        # "collected" describes the run, not the assertions: pytest owns pass and
        # fail, and saying "success" here would contradict a red test run.
        artifacts_mod.summary(
            artifacts,
            scenario.name,
            "collected",
            captured_uploads=uploads,
            archive_paths=len(archive.paths),
            drift_snapshots=len(snapshots),
            assertions="see pytest report",
        )
    except Exception as exc:
        failed = True
        artifacts_mod.summary(artifacts, scenario.name, "setup failed", error=str(exc))
        raise
    finally:
        artifacts_mod.collect(repo_root, artifacts, scenario.stack.namespace, job_name)
        if created and not keep_cluster:
            _cleanup_owned_cluster(cluster_name, repo_root, artifacts, failed)
