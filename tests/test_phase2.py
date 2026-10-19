"""Unit tests for universal checks, the coverage inventory, and replay."""

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
    ArchiveError,
    assert_log_clean,
    assert_metric_grid,
    expected_timestamps,
    load_directory,
    parse_csv,
)
from harness.command import HarnessError
from harness.coverage import CLASSIFICATION, INVENTORY, check_archive, workflow_warning
from harness.oracle import Oracle, Snapshot, take_snapshot
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
    def test_live_nodegroup_labels_grid(self) -> None:
        got = expected_timestamps(T2, Collection(interval="minutes", interval_size=1, history=1, sample_rate=1))
        self.assertEqual(got, [T1, T2])

    def test_step_and_interval_units(self) -> None:
        hourly = expected_timestamps(T2, Collection(interval="hours", interval_size=1, sample_rate=5))
        self.assertEqual(len(hourly), 13)
        self.assertEqual((hourly[0], hourly[-1]), ("2026-10-16T20:16:00Z", T2))
        # step longer than the interval: only the start point is on the grid
        self.assertEqual(expected_timestamps(T2, Collection(interval="minutes", sample_rate=5)), [T1])

    def test_history_beyond_one_is_rejected_for_workload_files(self) -> None:
        c = Collection(interval="minutes", interval_size=2, history=2, sample_rate=1)
        self.assertEqual(len(expected_timestamps(T2, c)), 3)
        with self.assertRaisesRegex(ValueError, "history > 1 is not modelled"):
            expected_timestamps(T2, c, workload=True)

    def test_seconds_interval_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not support interval 'seconds'"):
            expected_timestamps(T2, Collection(interval="seconds"))


class MetricGridTest(unittest.TestCase):
    ROWS = [f"c,g1,{T1},1", f"c,g1,{T2},1", f"c,g2,{T1},2", f"c,g2,{T2},2"]
    IDS = [("c", "g1"), ("c", "g2")]

    def test_exact_grid_passes(self) -> None:
        assert_metric_grid(_table(*self.ROWS), KEYS, self.IDS, [T1, T2])

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
                "[INFO] 2026/10/16 21:16:35 node.go:215: entity=node Ephemeral storage exporter metrics not present for any cluster",
            )
        )

    def test_live_failures_fail(self) -> None:
        for message in (
            "bad_data: 1:5: parse error: unexpected )",
            "client_error: client error: 404",
            "server_error: server error: 500",
            'Get "http://prom/api/v1/query_range": dial tcp 10.0.0.1:80: connect: connection refused',
            "execution: query timed out",
        ):
            with self.assertRaisesRegex(AssertionError, "query failure"):
                assert_log_clean(self._log(self.PREFIX + "message=" + message))

    def test_a_query_that_mentions_a_failure_word_is_not_a_failure(self) -> None:
        assert_log_clean(self._log(self.PREFIX.replace("query=up", 'query=up{a="timeout:"} ') + "message=no data returned"))

    def test_allowed_substring_passes(self) -> None:
        line = self.PREFIX + "message=bad_data: known"
        assert_log_clean(self._log(line), allowed=("bad_data: known",))

    def test_missing_log_fails(self) -> None:
        with self.assertRaisesRegex(AssertionError, "no file 'log.txt'"):
            assert_log_clean(_archive({}))

    def test_live_log_if_present(self) -> None:
        live = ROOT / "artifacts/kind-functional/nodegroup-labels/extracted-csv"
        if not live.is_dir():
            self.skipTest("no live artifacts (git-ignored)")
        assert_log_clean(load_directory(live))


class CoverageInventoryTest(unittest.TestCase):
    def test_every_classification_is_an_allowed_form(self) -> None:
        for path, columns in INVENTORY.items():
            for column, value in columns.items():
                self.assertRegex(value, CLASSIFICATION, f"{path}:{column}")

    def test_every_test_reference_resolves_to_a_test_function(self) -> None:
        refs = {v for cols in INVENTORY.values() for v in cols.values() if v.startswith("test:")}
        self.assertTrue(refs)
        for ref in refs:
            file, _, function = ref[len("test:"):].partition("::")
            source = (ROOT / file).read_text()
            self.assertRegex(source, rf"(?m)^def {re.escape(function)}\(", ref)

    def test_pending_sme_numbers_are_the_known_ones(self) -> None:
        found = {v for cols in INVENTORY.values() for v in cols.values() if v.startswith("pending")}
        self.assertEqual(found, {f"pending SME #{n}" for n in ("3", "4", "6a", "6b", "9")})

    def test_check_archive_cases(self) -> None:
        header = ",".join(INVENTORY["node_group/current_size.csv"])
        ok = f"{header}\nc,g,{T1},2\n"
        self.assertEqual(check_archive(_archive({"node_group/current_size.csv": ok, "log.txt": "x", "a.json": "{}"})), ([], []))

        errors, warnings = check_archive(_archive({"node_group/current_size.csv": "ClusterName,NodeGroupName,MetricTime\nc,g,t\n"}))
        self.assertEqual((len(errors), warnings), (1, []))
        renamed = header.replace("CurrentSize", "Size")
        errors, warnings = check_archive(_archive({"node_group/current_size.csv": f"{renamed}\nc,g,t,1\n"}))
        self.assertEqual(len(errors), 1)
        reordered = "NodeGroupName,ClusterName,MetricTime,CurrentSize"
        errors, _ = check_archive(_archive({"node_group/current_size.csv": f"{reordered}\ng,c,t,1\n"}))
        self.assertEqual(len(errors), 1)

        errors, warnings = check_archive(_archive({"node_group/current_size.csv": f"{header},Extra\nc,g,t,1,2\n"}))
        self.assertEqual((errors, len(warnings)), ([], 1))
        self.assertIn("Extra", warnings[0])
        errors, warnings = check_archive(_archive({"new/thing.csv": "A\n1\n"}))
        self.assertEqual((errors, len(warnings)), ([], 1))
        self.assertIn("new/thing.csv", warnings[0])

    def test_live_archive_has_no_errors_if_present(self) -> None:
        live = ROOT / "artifacts/kind-functional/nodegroup-labels/extracted-csv"
        if not live.is_dir():
            self.skipTest("no live artifacts (git-ignored)")
        self.assertEqual(check_archive(load_directory(live)), ([], []))

    def test_workflow_warning_escapes_per_github_rules(self) -> None:
        self.assertEqual(
            workflow_warning(["a 100%", "b\nc\r"]),
            "::warning title=Coverage review needed::a 100%25; b%0Ac%0D",
        )


class UniversalTest(unittest.TestCase):
    def test_single_upload_and_job_failures(self) -> None:
        universal.test_single_upload(SimpleNamespace(uploads=1))
        with self.assertRaisesRegex(AssertionError, "captured 2"):
            universal.test_single_upload(SimpleNamespace(uploads=2))
        universal.test_job_never_failed(SimpleNamespace(job_failures=0, job_name="j"))
        with self.assertRaisesRegex(AssertionError, "1 failed attempt"):
            universal.test_job_never_failed(SimpleNamespace(job_failures=1, job_name="j"))

    def test_inventory_test_records_warnings_and_fails_on_errors(self) -> None:
        run = SimpleNamespace(archive=_archive({"new/thing.csv": "A\n1\n"}), coverage_warnings=[])
        universal.test_archive_matches_inventory(run)
        self.assertEqual(len(run.coverage_warnings), 1)
        bad = SimpleNamespace(archive=_archive({"node_group/current_size.csv": "ClusterName\nc\n"}), coverage_warnings=[])
        with self.assertRaisesRegex(AssertionError, "differs from the coverage inventory"):
            universal.test_archive_matches_inventory(bad)


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
    snaps = [{"label": s, "items": {"nodes": NODES, "pods": PODS, "horizontalpodautoscalers": [{"metadata": {"name": "h"}}]}} for s in ("start", "end")]
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
        expected_timestamps(T2, Collection(interval="minutes", interval_size=1, history=1, sample_rate=1)),
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
        self.assertEqual(run.snapshots[0].other["horizontalpodautoscalers"], [{"metadata": {"name": "h"}}])
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

    def test_deleted_identity_fails_the_grid(self) -> None:
        text = f"ClusterName,NodeGroupName,MetricTime,CpuRequests\nc,g1,{T1},0.2\nc,g1,{T2},0.2\n"
        run, _ = self._run(cpu_requests=text)
        with self.assertRaisesRegex(AssertionError, "missing .*g2"):
            _grid_test(run)

    def test_deleted_timestamp_fails_the_grid(self) -> None:
        text = f"ClusterName,NodeGroupName,MetricTime,CpuRequests\nc,g1,{T1},0.2\nc,g1,{T2},0.2\nc,g2,{T1},0.2\n"
        run, _ = self._run(cpu_requests=text)
        with self.assertRaisesRegex(AssertionError, "missing .*21:16"):
            _grid_test(run)

    def test_replay_never_writes_into_the_artifact_directory(self) -> None:
        run, directory = self._run()
        before = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*"))
        _grid_test(run)
        run.oracle.requests("cpu", {"g": "x"})
        self.assertEqual(before, sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")))

    def test_artifacts_without_replay_inputs_say_so(self) -> None:
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch, True)
        (scratch / "extracted-csv").mkdir()
        with self.assertRaisesRegex(HarnessError, "regenerated with a live run"):
            replay(SCENARIO, scratch, scratch)

    def test_load_directory_rejects_missing_dir(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "does not exist"):
            load_directory(Path("/nonexistent-replay-dir"))


class JobFailuresAndReplayInputsTest(unittest.TestCase):
    def test_job_failures_reads_status_failed(self) -> None:
        for raw, expected in (("", 0), ("2", 2)):
            with patch.object(stack_mod, "kubectl", return_value=raw) as kubectl:
                self.assertEqual(stack_mod.job_failures("j", "ns", Path(".")), expected)
            self.assertIn("jsonpath={.status.failed}", kubectl.call_args.args[0])

    def test_replay_inputs_round_trip_into_a_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snaps = [Snapshot.from_items("s", {"nodes": NODES, "pods": PODS, "resourcequotas": []})]
            live = Run(SCENARIO, Archive({}), Oracle(snaps), root, uploads=1, job_name="j", job_failures=3, snapshots=snaps)
            _write_replay_inputs(root, snaps, live, {"chart_version": "1.1.24"})
            (root / "extracted-csv").mkdir()
            again = replay(SCENARIO, root, root)
        self.assertEqual((again.uploads, again.job_name, again.job_failures), (1, "j", 3))
        self.assertEqual(again.snapshots, snaps)


class SnapshotAndSummaryTest(unittest.TestCase):
    def test_take_snapshot_takes_a_resource_list_and_keys_items_by_name(self) -> None:
        calls: list[list[str]] = []

        def fake(args: list[str], _: Path) -> dict:
            calls.append(args)
            return {"items": [{"metadata": {"name": args[-3]}}]}

        with patch("harness.oracle.kubectl_json", fake):
            default = take_snapshot("s", Path("."))
            wider = take_snapshot("s", Path("."), ("nodes", "pods", "horizontalpodautoscalers"))
        self.assertEqual(calls[:2], [["get", "nodes", "-o", "json"], ["get", "-A", "pods", "-o", "json"]])
        self.assertEqual(sorted(default.items), ["nodes", "pods"])
        self.assertEqual(sorted(wider.items), ["horizontalpodautoscalers", "nodes", "pods"])
        self.assertEqual(Snapshot.from_items("s", wider.items), wider)

    def test_summary_has_a_coverage_warnings_section(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts_mod.summary(root, "demo", "collected", coverage_warnings=[])
            self.assertIn("## Coverage warnings\n\nnone", (root / "summary.md").read_text())
            artifacts_mod.summary(root, "demo", "collected", coverage_warnings=["a.csv: new column"])
            self.assertIn("- a.csv: new column", (root / "summary.md").read_text())
            artifacts_mod.summary(root, "demo", "setup failed", error="x")
            self.assertNotIn("Coverage warnings", (root / "summary.md").read_text())


if __name__ == "__main__":
    unittest.main()
