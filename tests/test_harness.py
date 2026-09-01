"""Unit tests for the Kind functional harness.

Everything here tests harness behaviour or the collector contract. The tests that
covered the old scenario-schema validator and the CSV assertion DSL are gone with
those modules; the structural CSV and unique-key coverage they also carried was
rewritten against ``harness.archive``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness import artifacts as artifacts_mod
from harness import run as run_mod
from harness.archive import (
    ArchiveError,
    assert_unique_keys,
    load,
    parse_csv,
    read_archive_files,
)
from harness.command import HarnessError, run
from harness.metric_fixture_server import (
    FixtureError,
    FixtureServer,
    render_metrics,
    validate_fixture_document,
)
from harness.oracle import Oracle, OracleDrift, Snapshot
from harness.prometheus import validate_targets
from harness.receiver import archive_bytes, archive_upload_count, capture_state, sanitize
from harness.spec import (
    ClusterSpec,
    Collection,
    FixtureService,
    ForwarderSpec,
    Metric,
    Node,
    ScenarioSpec,
    SpecError,
    StackSpec,
)
from harness.stack import resource_paths, template_args

REPO_ROOT = Path(__file__).resolve().parents[1]


class FixtureServerTest(unittest.TestCase):
    def test_health_metrics_escaping_and_elapsed_counter(self) -> None:
        metrics = validate_fixture_document(
            {
                "metrics": [
                    {
                        "name": "fixture_gauge",
                        "help": 'help \\ "line\nnext',
                        "type": "gauge",
                        "labels": {"quote": 'a"b'},
                        "value": 4,
                    },
                    {"name": "fixture_counter", "type": "counter", "labels": {"id": "x"}, "value": 2, "rate": 3},
                ]
            }
        )
        first = render_metrics(metrics, 1)
        later = render_metrics(metrics, 3)
        self.assertIn('# HELP fixture_gauge help \\\\ "line\\nnext', first)
        self.assertIn('fixture_gauge{quote="a\\"b"} 4', first)
        first_counter = float(re.search(r"fixture_counter\{id=\"x\"\} ([0-9.]+)", first).group(1))
        later_counter = float(re.search(r"fixture_counter\{id=\"x\"\} ([0-9.]+)", later).group(1))
        self.assertLess(first_counter, later_counter)

        server = FixtureServer(("127.0.0.1", 0), metrics)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(base + "/healthz") as response:
                self.assertEqual(response.status, 200)
            with urllib.request.urlopen(base + "/metrics") as response:
                self.assertIn(b"# TYPE fixture_counter counter", response.read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_invalid_fixture_is_rejected(self) -> None:
        with self.assertRaisesRegex(FixtureError, "unknown field"):
            validate_fixture_document({"metrics": [{"name": "x", "type": "gauge", "value": 1, "shell": "bad"}]})

    def test_normalized_fixture_is_valid_input(self) -> None:
        metrics = validate_fixture_document({"metrics": [{"name": "gauge", "type": "gauge", "value": 1}]})
        self.assertEqual(validate_fixture_document({"metrics": metrics}), metrics)

    def test_typed_metric_normalizes_into_valid_fixture_input(self) -> None:
        """The harness generates the fixture, so its output must satisfy the server."""

        payload = {
            "metrics": [
                Metric(name="g", type="gauge", labels={"a": "b"}, value=1).normalized(),
                Metric(name="c", type="counter", value=0, rate=2.5).normalized(),
            ]
        }
        self.assertEqual(validate_fixture_document(payload), payload["metrics"])

    def test_fixture_server_runs_standalone_in_the_pod(self) -> None:
        """The pod mounts this file alone on a stdlib-only image.

        Any import outside the standard library would fail there, eight minutes
        into a run, as a target that never comes up.
        """

        with tempfile.TemporaryDirectory() as directory:
            pod = Path(directory)
            (pod / "metric_fixture_server.py").write_text(
                (REPO_ROOT / "harness" / "metric_fixture_server.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (pod / "fixture.yaml").write_text(
                json.dumps({"metrics": [{"name": "pod_gauge", "type": "gauge", "value": 7}]}), encoding="utf-8"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json, pathlib, metric_fixture_server as server;"
                    "print(json.dumps(server.load_fixture(pathlib.Path('fixture.yaml'))))",
                ],
                cwd=pod,
                capture_output=True,
                text=True,
                # Keep the repository off sys.path so `harness` really is absent.
                env={**os.environ, "PYTHONPATH": ""},
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)[0]["name"], "pod_gauge")

    def test_pod_fixture_must_be_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.yaml"
            path.write_text("metrics:\n  - name: g\n", encoding="utf-8")
            from harness.metric_fixture_server import load_fixture

            with self.assertRaisesRegex(FixtureError, "must be JSON"):
                load_fixture(path)


class CommandAndReceiverTest(unittest.TestCase):
    def test_run_keeps_stderr_out_of_parsed_stdout(self) -> None:
        output = run(
            [sys.executable, "-c", "import sys; sys.stderr.write('noise'); print('{}')"],
            cwd=REPO_ROOT,
            merge_stderr=False,
        )
        self.assertEqual(json.loads(output), {})

    def test_run_reports_failure_with_output(self) -> None:
        with self.assertRaisesRegex(HarnessError, "command failed"):
            run([sys.executable, "-c", "import sys; sys.exit(3)"], cwd=REPO_ROOT)

    def test_upload_state_sanitizer_removes_credentials(self) -> None:
        sanitized = sanitize(
            {"headers": {"Authorization": "secret", "X-Test": "ok"}, "apiToken": "secret", "body_size": 3}
        )
        self.assertEqual(sanitized, {"headers": {"X-Test": "ok"}, "body_size": 3})

    def test_capture_state_reconnects_after_http_failure(self) -> None:
        first, second = MagicMock(), MagicMock()
        first.poll.return_value = None
        second.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("harness.receiver.port_forward", side_effect=[first, second]) as port_forward,
                patch(
                    "harness.receiver._http_json",
                    side_effect=[HarnessError("connection refused"), {"uploads": []}],
                ) as http_json,
                patch("harness.receiver.time.sleep"),
            ):
                state = capture_state(root, root / "artifacts", timeout=5)

        self.assertEqual(state, {"uploads": []})
        self.assertEqual(port_forward.call_count, 2)
        self.assertEqual(http_json.call_count, 2)
        first.terminate.assert_called_once_with()
        second.terminate.assert_called_once_with()

    def test_archive_count_excludes_authorization_requests(self) -> None:
        payload = b"archive"
        state = {
            "uploads": [
                {"path": "/authorize", "body_b64": base64.b64encode(b"credentials").decode()},
                {
                    "path": "/upload/container-data",
                    "body_b64": base64.b64encode(payload).decode(),
                    "archive_members": ["data/demo/a.csv"],
                },
            ]
        }

        self.assertEqual(archive_upload_count(state), 1)
        self.assertEqual(archive_bytes(state), payload)

    def test_diagnostics_capture_each_pods_logs(self) -> None:
        pods = {
            "items": [
                {"metadata": {"namespace": "kubex", "name": "fixture-abc"}},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("harness.artifacts.kubectl", return_value="pod output\n"),
                patch("harness.artifacts.kubectl_json", return_value=pods),
            ):
                artifacts_mod.collect(root, root / "artifacts", "kubex", None)

            self.assertEqual(
                (root / "artifacts" / "diagnostics" / "pod-logs" / "kubex__fixture-abc.log").read_text(),
                "pod output\n",
            )

    def test_prometheus_reconnects_and_stops_forward(self) -> None:
        from harness.prometheus import await_ready
        from harness.spec import PrometheusSpec

        first, second = MagicMock(), MagicMock()
        first.poll.side_effect = [None, 1, 1]
        second.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("harness.prometheus.port_forward", side_effect=[first, second]) as port_forward,
                patch(
                    "harness.prometheus._http_json",
                    side_effect=[HarnessError("forwarder closed"), {"data": {"activeTargets": []}}],
                ),
                patch("harness.prometheus.urllib.request.urlopen") as urlopen,
                patch("harness.prometheus.time.sleep"),
            ):
                urlopen.return_value.__enter__.return_value = MagicMock(status=200)
                self.assertIsNone(
                    await_ready(PrometheusSpec(readiness_timeout=5), "kubex", root, root / "artifacts")
                )

        self.assertEqual(port_forward.call_count, 2)
        second.terminate.assert_called_once_with()

    def test_prometheus_target_predicates(self) -> None:
        from harness.spec import PrometheusTarget

        targets = {
            "data": {
                "activeTargets": [
                    {"health": "up", "labels": {"service": "fixture", "environment": "test"}, "discoveredLabels": {}}
                ]
            }
        }
        validate_targets(
            targets, [PrometheusTarget(name="fixture", service="fixture", labels={"environment": "test"})]
        )
        with self.assertRaisesRegex(HarnessError, "not found"):
            validate_targets(targets, [PrometheusTarget(name="absent", service="absent")])
        with self.assertRaisesRegex(HarnessError, "is not down"):
            validate_targets(targets, [PrometheusTarget(name="fixture", service="fixture", state="down")])


class HelmValuesTest(unittest.TestCase):
    def test_overrides_escape_literal_segments_and_encode_lists_as_json(self) -> None:
        stack = StackSpec(
            version="1.0.0",
            helm_overrides={
                "prometheus": {
                    "server": {
                        "nodeSelector": {
                            "topology.kubex.ai/node-group": "mvp-workers",
                            "literal[a],b\\c": "value",
                        },
                        "tolerations": [{"key": "a,b", "operator": "Exists", "effect": "NoSchedule"}],
                        "nested": ["value,with,comma", [1, 2]],
                    }
                }
            },
        )
        _, _, args = template_args("demo", stack, ForwarderSpec())
        joined = " ".join(args)
        self.assertIn(r"prometheus.server.nodeSelector.topology\.kubex\.ai/node-group=mvp-workers", joined)
        self.assertIn(r"literal\[a\]\,b\\c", joined)
        # Lists go through --set-json so commas inside them are not split.
        self.assertIn("--set-json", args)
        self.assertIn(
            'prometheus.server.tolerations=[{"effect":"NoSchedule","key":"a,b","operator":"Exists"}]', args
        )

    def test_collection_settings_reach_the_chart(self) -> None:
        _, _, args = template_args(
            "demo", StackSpec(), ForwarderSpec(collection=Collection(interval="hours", interval_size=2))
        )
        joined = " ".join(args)
        self.assertIn("container-optimization-data-forwarder.config.collection.interval=hours", joined)
        self.assertIn("container-optimization-data-forwarder.config.collection.interval_size=2", joined)


class SpecValidationTest(unittest.TestCase):
    """Setup objects are validated at construction, before any cluster exists."""

    def test_cluster_requires_a_worker(self) -> None:
        with self.assertRaisesRegex(SpecError, "at least one worker"):
            ClusterSpec(node_image="kindest/node:v1.30.0")

    def test_invalid_collection_interval_is_rejected(self) -> None:
        with self.assertRaisesRegex(SpecError, "invalid interval"):
            Collection(interval="fortnights")  # type: ignore[arg-type]

    def test_gauges_cannot_declare_a_rate(self) -> None:
        with self.assertRaisesRegex(SpecError, "gauges cannot define a rate"):
            Metric(name="g", type="gauge", rate=1)

    def test_counters_require_a_rate(self) -> None:
        with self.assertRaisesRegex(SpecError, "counters require a rate"):
            Metric(name="c", type="counter")

    def test_metric_names_are_validated_before_provisioning(self) -> None:
        with self.assertRaisesRegex(SpecError, "invalid Prometheus metric name"):
            Metric(name="bad-name", type="gauge")

    def test_fixture_ports_are_validated_before_provisioning(self) -> None:
        with self.assertRaisesRegex(SpecError, "port must be between"):
            FixtureService(name="f", metrics=[Metric(name="g", type="gauge")], port=70000)

    def test_invalid_node_label_is_rejected(self) -> None:
        with self.assertRaisesRegex(SpecError, "invalid label value"):
            Node(labels={"a": "not a valid value"})
        with self.assertRaisesRegex(SpecError, "invalid label name"):
            Node(labels={"a/b/c": "value"})

    def test_fixture_service_requires_metrics(self) -> None:
        with self.assertRaisesRegex(SpecError, "at least one metric"):
            FixtureService(name="f", metrics=[])

    def test_fixture_service_names_are_validated_before_provisioning(self) -> None:
        metric = Metric(name="g", type="gauge")
        with self.assertRaisesRegex(SpecError, "invalid Kubernetes name"):
            FixtureService(name="Bad Name", metrics=[metric])
        with self.assertRaisesRegex(SpecError, "invalid Kubernetes name"):
            FixtureService(name="fixture", namespace="Bad Namespace", metrics=[metric])

    def test_window_prefers_explicit_seconds_over_the_derived_value(self) -> None:
        self.assertEqual(ForwarderSpec(collection=Collection(interval="hours")).window(), 3610)
        self.assertEqual(ForwarderSpec(window_seconds=180, collection=Collection(interval="hours")).window(), 180)

    def test_cluster_name_and_node_names_are_deterministic(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(node_image="i", control_planes=[Node()], workers=[Node(), Node()]),
        )
        self.assertEqual(scenario.cluster_name, "kubex-e2e-demo")
        self.assertEqual(
            scenario.cluster.node_names(scenario.cluster_name),
            ["kubex-e2e-demo-control-plane", "kubex-e2e-demo-worker", "kubex-e2e-demo-worker2"],
        )
        self.assertEqual(scenario.prefix, "data/demo")

    def test_resource_paths_fail_before_cluster_work(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(node_image="i", workers=[Node()]),
            resources=["missing.yaml"],
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(HarnessError, "does not exist"):
                resource_paths(scenario, Path(directory))


class ClusterLifecycleTest(unittest.TestCase):
    def test_existing_cluster_is_never_deleted(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(node_image="i", workers=[Node()]),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(
                    run_mod.cluster,
                    "assert_absent",
                    side_effect=HarnessError("Kind cluster already exists: kubex-e2e-demo"),
                ),
                patch.object(run_mod.cluster, "create") as create,
                patch.object(run_mod.cluster, "delete") as delete,
                patch.object(run_mod.artifacts_mod, "collect"),
            ):
                with self.assertRaisesRegex(HarnessError, "already exists"):
                    with run_mod.execute(scenario, root, root, root / "artifacts"):
                        pass

        create.assert_not_called()
        delete.assert_not_called()

    def test_partial_creation_is_cleaned_up(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(node_image="i", workers=[Node()]),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(run_mod.cluster, "assert_absent"),
                patch.object(run_mod.cluster, "create", side_effect=HarnessError("create failed")),
                patch.object(run_mod.cluster, "delete") as delete,
                patch.object(run_mod.artifacts_mod, "collect"),
            ):
                with self.assertRaisesRegex(HarnessError, "create failed"):
                    with run_mod.execute(scenario, root, root, root / "artifacts"):
                        pass

        delete.assert_called_once_with("kubex-e2e-demo", root)

    def test_cleanup_failure_fails_an_otherwise_successful_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(run_mod.cluster, "delete", side_effect=HarnessError("delete failed")):
                with self.assertRaisesRegex(HarnessError, "delete failed"):
                    run_mod._cleanup_owned_cluster("demo", root, root / "artifacts", False)

            self.assertIn(
                "delete failed",
                (root / "artifacts" / "diagnostics" / "cleanup.txt").read_text(),
            )

    def test_cleanup_failure_does_not_mask_run_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(run_mod.cluster, "delete", side_effect=HarnessError("delete failed")):
                run_mod._cleanup_owned_cluster("demo", root, root / "artifacts", True)


class ArchiveSafetyTest(unittest.TestCase):
    """The archive comes from the system under test, so it stays untrusted."""

    @staticmethod
    def _zip(members: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return buffer.getvalue()

    def test_zip_and_tar_prefix_are_normalized_safely(self) -> None:
        payload = self._zip({"data/demo/node/a.csv": b"h\n", "data/demo/": b""})
        self.assertEqual(sorted(read_archive_files(payload, "data/demo")), ["node/a.csv"])

        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("./data/demo/node/b.csv")
            body = b"h\n"
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
        self.assertEqual(sorted(read_archive_files(buffer.getvalue(), "data/demo")), ["node/b.csv"])

    def test_rejects_traversal(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "traversal"):
            read_archive_files(self._zip({"../escape.csv": b"h\n"}))

    def test_rejects_absolute_paths(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "absolute path"):
            read_archive_files(self._zip({"/etc/passwd": b"x"}))

    def test_rejects_members_outside_the_prefix(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "outside configured archive prefix"):
            read_archive_files(self._zip({"other/a.csv": b"h\n"}), "data/demo")

    def test_rejects_tar_special_files(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            info = tarfile.TarInfo("link.csv")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        with self.assertRaisesRegex(ArchiveError, "links and special files"):
            read_archive_files(buffer.getvalue())

    def test_extraction_stays_inside_the_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "out"
            archive = load(self._zip({"node/a.csv": b"h\n"}), "", destination)
            self.assertEqual(archive.paths, ["node/a.csv"])
            self.assertTrue((destination / "node" / "a.csv").is_file())


class CsvStructureTest(unittest.TestCase):
    """Structural checks run for every CSV, independent of any scenario."""

    def test_parses_rows_and_skips_blank_lines(self) -> None:
        table = parse_csv("t.csv", b"a,b\n1,2\n\n3,4\n")
        self.assertEqual(table.headers, ["a", "b"])
        self.assertEqual(len(table), 2)
        self.assertEqual(table.column("b"), ["2", "4"])

    def test_rejects_non_utf8(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "not UTF-8"):
            parse_csv("t.csv", b"a,b\n\xff\xfe,2\n")

    def test_rejects_row_width_mismatch(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "expected 2 columns, actual 3"):
            parse_csv("t.csv", b"a,b\n1,2,3\n")

    def test_archive_load_validates_every_csv_eagerly(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("unasserted.csv", "a,b\n1,2,3\n")
        with self.assertRaisesRegex(ArchiveError, "expected 2 columns, actual 3"):
            load(buffer.getvalue())

    def test_rejects_duplicate_headers(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "duplicate header"):
            parse_csv("t.csv", b"a,a\n1,2\n")

    def test_rejects_a_completely_empty_file(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "missing header"):
            parse_csv("t.csv", b"")

    def test_rejects_an_empty_header_record(self) -> None:
        """A bare newline parses to [[]], which is truthy.

        The old emptiness guard passed it through with headers=[], and a file
        with leading blank lines then treated its real header row as data.
        """

        with self.assertRaisesRegex(ArchiveError, "header record is empty"):
            parse_csv("t.csv", b"\n")
        with self.assertRaisesRegex(ArchiveError, "header record is empty"):
            parse_csv("t.csv", b"\n\na,b\n1,2\n")

    def test_unique_keys_detects_duplicates(self) -> None:
        table = parse_csv("t.csv", b"k,v\nx,1\nx,2\n")
        with self.assertRaisesRegex(AssertionError, "duplicate key"):
            assert_unique_keys(table, ["k"])

    def test_unique_keys_reports_a_missing_column(self) -> None:
        table = parse_csv("t.csv", b"k,v\nx,1\n")
        with self.assertRaisesRegex(AssertionError, "unique key column\\(s\\) missing"):
            assert_unique_keys(table, ["absent"])

    def test_one_requires_exactly_one_match(self) -> None:
        table = parse_csv("t.csv", b"k,v\nx,1\nx,2\n")
        with self.assertRaisesRegex(AssertionError, "expected exactly 1 row"):
            table.one(k="x")


class OracleTest(unittest.TestCase):
    NODES = [
        {
            "metadata": {"name": "worker-a", "labels": {"group": "workers", "enabled": "true"}},
            "status": {"allocatable": {"cpu": "2", "memory": "1Gi"}},
        },
        {
            "metadata": {"name": "worker-b", "labels": {"group": "workers"}},
            "status": {"allocatable": {"cpu": "2", "memory": "1Gi"}},
        },
        {
            "metadata": {"name": "control-plane", "labels": {"group": "system"}},
            "status": {"allocatable": {"cpu": "8", "memory": "8Gi"}},
        },
    ]
    PODS = [
        {
            "metadata": {"name": "workload"},
            "spec": {
                "nodeName": "worker-a",
                "containers": [
                    {"name": "app", "resources": {"requests": {"cpu": "200m", "memory": "64Mi"}}},
                    {"name": "finished", "resources": {"requests": {"cpu": "5", "memory": "5Gi"}}},
                ],
                "initContainers": [
                    {
                        "name": "sidecar",
                        "restartPolicy": "Always",
                        "resources": {"requests": {"cpu": "50m", "memory": "16Mi"}},
                    },
                    {"name": "init", "resources": {"requests": {"cpu": "5", "memory": "5Gi"}}},
                ],
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [{"name": "finished", "state": {"terminated": {"exitCode": 0}}}],
            },
        },
        {
            "metadata": {"name": "kindnet"},
            "spec": {
                "nodeName": "worker-b",
                "containers": [{"name": "kindnet", "resources": {"requests": {"cpu": "100m", "memory": "50Mi"}}}],
            },
            "status": {"phase": "Running"},
        },
        {
            "metadata": {"name": "pending"},
            "spec": {
                "nodeName": "worker-b",
                "containers": [{"name": "app", "resources": {"requests": {"cpu": "9", "memory": "9Gi"}}}],
            },
            "status": {"phase": "Pending"},
        },
    ]

    def _oracle(self, *snapshots: Snapshot) -> Oracle:
        return Oracle(snapshots=list(snapshots) or [Snapshot("only", self.NODES, self.PODS)])

    def test_counts_running_pods_native_sidecars_and_skips_terminated(self) -> None:
        oracle = self._oracle()
        self.assertAlmostEqual(oracle.requests("cpu", {"group": "workers"}), 0.35)
        self.assertEqual(oracle.requests("memory", {"group": "workers"}), 130 * 1024**2)
        self.assertAlmostEqual(oracle.reservation_percent("cpu", {"group": "workers"}), 8.75)
        self.assertEqual(oracle.evidence[0]["nodes"], ["worker-a", "worker-b"])
        self.assertEqual(oracle.evidence[0]["allocatable"], 4.0)
        self.assertEqual(oracle.evidence[0]["unit"], "cores")

    def test_boolean_label_selectors_compare_as_text(self) -> None:
        self.assertAlmostEqual(self._oracle().requests("cpu", {"enabled": True}), 0.25)

    def test_selector_matching_no_nodes_is_an_error(self) -> None:
        with self.assertRaisesRegex(HarnessError, "matched no nodes"):
            self._oracle().requests("cpu", {"group": "missing"})

    def test_drift_between_snapshots_is_detected(self) -> None:
        moved = [
            {
                "metadata": {"name": "extra"},
                "spec": {
                    "nodeName": "worker-a",
                    "containers": [{"name": "app", "resources": {"requests": {"cpu": "1"}}}],
                },
                "status": {"phase": "Running"},
            },
            *self.PODS,
        ]
        oracle = self._oracle(
            Snapshot("window start", self.NODES, self.PODS), Snapshot("window end", self.NODES, moved)
        )
        with self.assertRaisesRegex(OracleDrift, "requests changed from 0.35 at window start"):
            oracle.requests("cpu", {"group": "workers"})

    def test_drift_can_be_allowed_for_a_scenario_that_mutates_on_purpose(self) -> None:
        moved = [
            {
                "metadata": {"name": "extra"},
                "spec": {
                    "nodeName": "worker-a",
                    "containers": [{"name": "app", "resources": {"requests": {"cpu": "1"}}}],
                },
                "status": {"phase": "Running"},
            },
            *self.PODS,
        ]
        oracle = self._oracle(
            Snapshot("window start", self.NODES, self.PODS), Snapshot("window end", self.NODES, moved)
        )
        self.assertAlmostEqual(oracle.requests("cpu", {"group": "workers"}, allow_drift=True), 0.35)

    def test_quantity_suffixes(self) -> None:
        from harness.oracle import quantity

        self.assertEqual(quantity("1Ki", "t"), 1024)
        self.assertEqual(quantity("500m", "t"), Decimal("0.5"))
        self.assertEqual(quantity("2Gi", "t"), 2 * 1024**3)
        with self.assertRaisesRegex(HarnessError, "invalid Kubernetes quantity"):
            quantity("12Zi", "t")


if __name__ == "__main__":
    unittest.main()
