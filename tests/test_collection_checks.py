"""Unit tests for the collection checks: metric grid, log scan, coverage inventory, universal tests, replay."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness import artifacts as artifacts_mod
from harness import stack as stack_mod
from harness import universal
from harness.archive import (
    Archive,
    assert_log_clean,
    assert_metric_grid,
    expected_timestamps,
    parse_csv,
)
from harness.command import HarnessError
from harness.coverage import INVENTORY, check_archive, workflow_warning
from harness.oracle import Oracle, Snapshot
from harness.run import Run, _write_replay_inputs, replay
from harness.spec import ClusterSpec, Collection, Node, ScenarioSpec

ROOT = Path(__file__).resolve().parent.parent
T1, T2 = "2026-10-16T21:15:00Z", "2026-10-16T21:16:00Z"
KEYS = ["ClusterName", "NodeGroupName"]


def _table(*rows: str, header: str = "ClusterName,NodeGroupName,MetricTime,CpuRequests") -> object:
    return parse_csv("t.csv", ("\n".join([header, *rows]) + "\n").encode())


def _archive(files: dict[str, str]) -> Archive:
    return Archive({k: v.encode() for k, v in files.items()})


class ExpectedTimestampsTest(unittest.TestCase):
    def test_grid_follows_interval_and_step(self) -> None:
        self.assertEqual(expected_timestamps(T2, Collection(interval="minutes", interval_size=1, sample_rate=1)), [T1, T2])
        hourly = expected_timestamps(T2, Collection(interval="hours", interval_size=1, sample_rate=5))
        self.assertEqual((len(hourly), hourly[0], hourly[-1]), (13, "2026-10-16T20:16:00Z", T2))
        # step longer than the interval: only the start point is on the grid
        self.assertEqual(expected_timestamps(T2, Collection(interval="minutes", sample_rate=5)), [T1])

    def test_seconds_interval_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not support interval 'seconds'"):
            expected_timestamps(T2, Collection(interval="seconds"))


class MetricGridTest(unittest.TestCase):
    ROWS = [f"c,g1,{T1},1", f"c,g1,{T2},1", f"c,g2,{T1},2", f"c,g2,{T2},2"]
    IDS = [("c", "g1"), ("c", "g2")]

    def test_missing_and_unexpected_identities_are_named(self) -> None:
        with self.assertRaisesRegex(AssertionError, r"missing \[\('c', 'g3'\)\], unexpected \[\('c', 'g2'\)\]"):
            assert_metric_grid(_table(*self.ROWS), KEYS, [("c", "g1"), ("c", "g3")], [T1, T2])

    def test_duplicate_sample_fails(self) -> None:
        with self.assertRaisesRegex(AssertionError, "duplicate MetricTime"):
            assert_metric_grid(_table(*self.ROWS, f"c,g1,{T1},9"), KEYS, self.IDS, [T1, T2])

    def test_missing_and_extra_timestamps_are_named_per_identity(self) -> None:
        with self.assertRaisesRegex(AssertionError, r"'g2'.*missing \['%s'\]" % T2):
            assert_metric_grid(_table(*self.ROWS[:3]), KEYS, self.IDS, [T1, T2])
        with self.assertRaisesRegex(AssertionError, r"unexpected \['2026-10-16T21:17:00Z'\]"):
            assert_metric_grid(_table(*self.ROWS, "c,g1,2026-10-16T21:17:00Z,1"), KEYS, self.IDS, [T1, T2])


class LogCleanTest(unittest.TestCase):
    PREFIX = "[WARN] 2026/10/16 21:16:35 prometheus.go:364: cluster=x metric= query=up "

    def _log(self, *lines: str) -> Archive:
        return _archive({"log.txt": "\n".join(lines) + "\n"})

    def test_expected_skips_pass(self) -> None:
        assert_log_clean(
            self._log(
                self.PREFIX + "message=no data returned, model.Matrix is empty",
                "[ERROR] 2026/10/16 21:16:35 node.go:235: entity=node message=prometheus node exporter metrics not present for any cluster",
            )
        )

    def test_query_failures_fail(self) -> None:
        for message in (
            "bad_data: 1:5: parse error: unexpected )",
            "server_error: server error: 500",
            'Get "http://prom/api/v1/query_range": dial tcp 10.0.0.1:80: connect: connection refused',
        ):
            with self.assertRaisesRegex(AssertionError, "query failure"):
                assert_log_clean(self._log(self.PREFIX + "message=" + message))

    def test_a_query_that_mentions_a_failure_word_is_not_a_failure(self) -> None:
        assert_log_clean(self._log(self.PREFIX.replace("query=up", 'query=up{a="timeout:"} ') + "message=no data returned"))


class CoverageInventoryTest(unittest.TestCase):
    def test_every_classification_is_valid_and_test_references_resolve(self) -> None:
        refs = set()
        for path, columns in INVENTORY.items():
            for column, value in columns.items():
                self.assertTrue(
                    value in ("key", "unclassified") or value.startswith(("test:", "excluded:")), f"{path}:{column}"
                )
                if value.startswith("test:"):
                    refs.add(value)
        self.assertTrue(refs)
        for ref in refs:
            file, _, function = ref[len("test:"):].partition("::")
            self.assertRegex((ROOT / file).read_text(), rf"(?m)^def {re.escape(function)}\(", ref)

    def test_check_archive_cases(self) -> None:
        path = "node_group/current_size.csv"
        header = ",".join(INVENTORY[path])
        ok = f"{header}\nc,g,{T1},2\n"
        self.assertEqual(check_archive(_archive({path: ok, "log.txt": "x", "a.json": "{}"})), ([], []))

        for bad in (
            "ClusterName,NodeGroupName,MetricTime\nc,g,t\n",  # lost column
            f"{header.replace('CurrentSize', 'Size')}\nc,g,t,1\n",  # renamed
            "NodeGroupName,ClusterName,MetricTime,CurrentSize\ng,c,t,1\n",  # reordered
        ):
            errors, warnings = check_archive(_archive({path: bad}))
            self.assertEqual((len(errors), warnings), (1, []), bad)

        errors, warnings = check_archive(_archive({path: f"{header},Extra\nc,g,t,1,2\n"}))
        self.assertEqual((errors, len(warnings)), ([], 1))
        self.assertIn("Extra", warnings[0])
        errors, warnings = check_archive(_archive({"new/thing.csv": "A\n1\n"}))
        self.assertEqual((errors, len(warnings)), ([], 1))
        self.assertIn("new/thing.csv", warnings[0])

    def test_workflow_warning_escapes_per_github_rules(self) -> None:
        self.assertEqual(
            workflow_warning(["a 100%", "b\nc\r"]),
            "::warning title=Coverage review needed::a 100%25; b%0Ac%0D",
        )


class UniversalTest(unittest.TestCase):
    def test_inventory_test_records_warnings_and_fails_on_errors(self) -> None:
        run = SimpleNamespace(archive=_archive({"new/thing.csv": "A\n1\n"}), coverage_warnings=[])
        universal.test_archive_matches_inventory(run)
        self.assertEqual(len(run.coverage_warnings), 1)
        bad = SimpleNamespace(archive=_archive({"node_group/current_size.csv": "ClusterName\nc\n"}), coverage_warnings=[])
        with self.assertRaisesRegex(AssertionError, "differs from the coverage inventory"):
            universal.test_archive_matches_inventory(bad)

    def test_expected_files_checks_presence_and_populated_state(self) -> None:
        header = ",".join(INVENTORY["node_group/current_size.csv"])
        archive = _archive({"node_group/current_size.csv": f"{header}\nc,g,t,1\n", "node_group/config.csv": "AuditTime\n"})

        def check(files: dict[str, str]) -> None:
            universal.test_expected_files(SimpleNamespace(scenario=SimpleNamespace(name="s", files=files), archive=archive))

        check({"node_group/current_size.csv": "populated", "node_group/config.csv": "header-only"})
        with self.assertRaisesRegex(AssertionError, "unexpected.*config.csv"):
            check({"node_group/current_size.csv": "populated"})
        with self.assertRaisesRegex(AssertionError, "missing.*node/config.csv"):
            check({"node_group/current_size.csv": "populated", "node_group/config.csv": "header-only", "node/config.csv": "populated"})
        with self.assertRaisesRegex(AssertionError, "file states differ"):
            check({"node_group/current_size.csv": "header-only", "node_group/config.csv": "header-only"})
        with self.assertRaisesRegex(AssertionError, "declares no expected files"):
            check({})


NODES = [
    {"metadata": {"name": "w1", "labels": {"g": "x"}}, "status": {"allocatable": {"cpu": "2", "memory": "1Gi"}}},
    {"metadata": {"name": "w2", "labels": {"g": "x"}}, "status": {"allocatable": {"cpu": "2", "memory": "1Gi"}}},
]
PODS = [
    {
        "metadata": {"name": "p"},
        "spec": {"nodeName": "w1", "containers": [{"name": "c", "resources": {"requests": {"cpu": "200m"}}}]},
        "status": {"phase": "Running"},
    }
]
SCENARIO = ScenarioSpec(name="demo", cluster=ClusterSpec(node_image="i", workers=[Node()]))


def _artifact_dir(root: Path, **overrides: str) -> Path:
    """A small synthetic run directory: two groups, two timestamps, CPU requests 0.2 each."""

    directory = root / "demo"
    csv_dir = directory / "extracted-csv" / "node_group"
    csv_dir.mkdir(parents=True)
    rows = [f"c,{g},{t},0.200000" for g in ("g1", "g2") for t in (T1, T2)]
    text = "ClusterName,NodeGroupName,MetricTime,CpuRequests\n" + "\n".join(rows) + "\n"
    (csv_dir / "cpu_requests.csv").write_text(overrides.get("cpu_requests", text))
    (directory / "extracted-csv" / "log.txt").write_text("[INFO] collection version v4.7.5-beta1\n")
    snaps = [{"label": s, "nodes": NODES, "pods": PODS} for s in ("start", "end")]
    (directory / "snapshots.json").write_text(json.dumps(snaps))
    (directory / "replay.json").write_text(
        json.dumps({"uploads": 1, "job_name": "j", "job_failures": 0, "versions": {}})
    )
    return directory


def _value_test(run: Run) -> None:
    """Stand-in for a scenario value test: every sample equals the oracle's request sum / 2 groups."""

    expected = run.oracle.requests("cpu", {"g": "x"})
    for row in run.archive.csv("node_group/cpu_requests.csv"):
        assert float(row["CpuRequests"]) == pytest.approx(expected, abs=1e-6), row


def _grid_test(run: Run) -> None:
    assert_metric_grid(
        run.archive.csv("node_group/cpu_requests.csv"),
        KEYS,
        [("c", "g1"), ("c", "g2")],
        expected_timestamps(T2, Collection(interval="minutes", interval_size=1, sample_rate=1)),
    )


class ReplayTest(unittest.TestCase):
    def _run(self, **overrides: str) -> tuple[Run, Path]:
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        directory = _artifact_dir(scratch, **overrides)
        return replay(SCENARIO, directory, scratch / "scratch"), directory

    def test_run_is_rebuilt_without_a_cluster_and_tests_pass(self) -> None:
        run, _ = self._run()
        self.assertEqual((run.uploads, run.job_name, run.job_failures), (1, "j", 0))
        self.assertEqual([s.label for s in run.snapshots], ["start", "end"])
        self.assertEqual(run.snapshots[0].nodes, NODES)
        _grid_test(run)
        _value_test(run)

    def test_mutated_value_fails_a_value_test(self) -> None:
        text = (
            "ClusterName,NodeGroupName,MetricTime,CpuRequests\n"
            f"c,g1,{T1},0.200000\nc,g1,{T2},0.900000\nc,g2,{T1},0.200000\nc,g2,{T2},0.200000\n"
        )
        run, _ = self._run(cpu_requests=text)
        with self.assertRaises(AssertionError):
            _value_test(run)
        _grid_test(run)  # the grid is still intact

    def test_replay_never_writes_into_the_artifact_directory(self) -> None:
        run, directory = self._run()
        before = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*"))
        _grid_test(run)
        run.oracle.requests("cpu", {"g": "x"})
        self.assertEqual(before, sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")))

    def test_directory_without_replay_inputs_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(HarnessError, "cannot read snapshots.json"):
                replay(SCENARIO, Path(directory), Path(directory))

    def test_replay_inputs_round_trip_into_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snaps = [Snapshot("s", NODES, PODS)]
            live = Run(SCENARIO, Archive({}), Oracle(snaps), root, uploads=1, job_name="j", job_failures=3, snapshots=snaps)
            _write_replay_inputs(root, live)
            (root / "extracted-csv").mkdir()
            again = replay(SCENARIO, root, root)
        self.assertEqual((again.uploads, again.job_name, again.job_failures), (1, "j", 3))
        self.assertEqual(again.snapshots, snaps)


class StackAndSummaryTest(unittest.TestCase):
    def test_job_failures_reads_status_failed(self) -> None:
        for raw, expected in (("", 0), ("2", 2)):
            with patch.object(stack_mod, "kubectl", return_value=raw):
                self.assertEqual(stack_mod.job_failures("j", "ns", Path(".")), expected)

    def test_summary_has_a_coverage_warnings_section_only_when_given(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts_mod.summary(root, "demo", "collected", {}, coverage_warnings=[])
            self.assertIn("## Coverage warnings\n\nnone", (root / "summary.md").read_text())
            artifacts_mod.summary(root, "demo", "collected", {}, coverage_warnings=["a.csv: new column"])
            self.assertIn("- a.csv: new column", (root / "summary.md").read_text())
            artifacts_mod.summary(root, "demo", "setup failed", {}, error="x")
            self.assertNotIn("Coverage warnings", (root / "summary.md").read_text())


if __name__ == "__main__":
    unittest.main()
