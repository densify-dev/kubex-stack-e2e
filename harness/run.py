"""Orchestrate one scenario run and hand the result to the tests.

The shared pytest fixture calls :func:`execute`. Everything from cluster creation
through archive capture happens here; assertions happen in the scenario module.
"""

from __future__ import annotations

import json
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from harness import artifacts as artifacts_mod
from harness import cluster, fixtures, prometheus, receiver, stack
from harness.archive import Archive, load_directory
from harness.archive import load as load_archive
from harness.command import HarnessError, write, write_json
from harness.oracle import Oracle, Snapshot, take_snapshot
from harness.spec import ScenarioSpec

# Snapshot cadence during the collection window. Fixed, so drift detection has
# the same resolution whether the window is 3 minutes or 60.
SNAPSHOT_INTERVAL_SECONDS = 60.0

# Recorded in versions.json and summary.md; None until the run learns the value.
VERSION_FIELDS = (
    "chart_version",
    "collector_image_requested",
    "collector_image_digest",
    "collector_image_id",
    "collector_version",
)


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
    job_failures: int = 0
    # Filled by test_archive_matches_inventory; reported in summary.md and the CI annotation.
    coverage_warnings: list[str] = field(default_factory=list)


def _write_replay_inputs(artifacts: Path, run: Run) -> None:
    """Everything replay needs besides extracted-csv/. Written before the tests so a red run keeps it."""

    write(artifacts / "snapshots.json", json.dumps([asdict(s) for s in run.snapshots]))
    write_json(
        artifacts / "replay.json",
        {
            "uploads": run.uploads,
            "job_name": run.job_name,
            "job_failures": run.job_failures,
        },
    )


def replay(scenario: ScenarioSpec, directory: Path, artifacts: Path) -> Run:
    """Rebuild a Run from a previous run's artifact directory without touching a cluster.

    Read-only on ``directory``. Nothing is written (no summary, no versions.json);
    ``artifacts`` is a scratch directory for tests that want to write files.
    """

    try:
        snapshots_raw = json.loads((directory / "snapshots.json").read_text())
        meta = json.loads((directory / "replay.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"replay directory {directory}: cannot read snapshots.json/replay.json ({exc})") from exc
    snapshots = [Snapshot(**item) for item in snapshots_raw]
    return Run(
        scenario=scenario,
        archive=load_directory(directory / "extracted-csv"),
        oracle=Oracle(snapshots=snapshots),
        artifacts=artifacts,
        uploads=meta["uploads"],
        job_name=meta["job_name"],
        snapshots=snapshots,
        job_failures=meta["job_failures"],
    )


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
    collector_image: str | None = None,
    keep_cluster: bool = False,
) -> Iterator[Run]:
    """Provision, collect, capture, yield, then always diagnose and tear down."""

    artifacts = artifacts_mod.prepare(artifacts_root, scenario.name)
    cluster_name = scenario.cluster_name
    created = False
    failed = False
    job_name: str | None = None
    versions: dict[str, Any] = dict.fromkeys(VERSION_FIELDS)
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
        cronjob = stack.install(
            scenario, repo_root, artifacts, receiver.cluster_ip(repo_root), versions, chart_version, collector_image
        )
        stack.apply_resources(resources, repo_root)
        stack.await_lifecycle(scenario, repo_root)

        prometheus.await_ready(scenario.prometheus, scenario.stack.namespace, repo_root, artifacts)

        snapshots = _await_window(scenario, repo_root)
        job_name = stack.trigger_forwarder(scenario, repo_root, cronjob)
        versions["collector_image_id"] = stack.job_image_id(job_name, scenario.stack.namespace, repo_root)
        job_failures = stack.job_failures(job_name, scenario.stack.namespace, repo_root)
        state = receiver.capture_state(repo_root, artifacts)
        snapshots.append(take_snapshot("after upload", repo_root))

        archive = load_archive(
            receiver.archive_bytes(state), scenario.prefix, artifacts / "extracted-csv"
        )
        uploads = receiver.archive_upload_count(state)
        log = archive.raw("log.txt").decode("utf-8", "replace") if "log.txt" in archive else ""
        found = re.search(r"collection version (\S+)", log.split("\n", 1)[0])
        versions["collector_version"] = found.group(1) if found else "unknown"
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
            job_failures=job_failures,
        )
        _write_replay_inputs(artifacts, run)
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
            versions,
            captured_uploads=uploads,
            archive_paths=len(archive.paths),
            drift_snapshots=len(snapshots),
            job_failures=job_failures,
            assertions="see pytest report",
            coverage_warnings=run.coverage_warnings,
        )
    except Exception as exc:
        failed = True
        artifacts_mod.summary(artifacts, scenario.name, "setup failed", versions, error=str(exc))
        raise
    finally:
        write_json(artifacts / "versions.json", versions)
        artifacts_mod.collect(repo_root, artifacts, scenario.stack.namespace, job_name)
        if created and not keep_cluster:
            _cleanup_owned_cluster(cluster_name, repo_root, artifacts, failed)
