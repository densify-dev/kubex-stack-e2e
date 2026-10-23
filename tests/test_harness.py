"""Unit tests for the Kind functional harness.

Everything here tests harness behaviour or the collector contract. The tests that
covered the old scenario-schema validator and the CSV assertion DSL are gone with
those modules; the structural CSV and unique-key coverage they also carried was
rewritten against ``harness.archive``.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

import conftest

from harness import artifacts as artifacts_mod
from harness import run as run_mod
from harness.archive import (
    ArchiveError,
    assert_unique_keys,
    load,
    parse_csv,
    read_archive_files,
)
from harness.cluster import render_config
from harness.command import HarnessError, run
from harness.fixtures import _deployment
from harness.metric_fixture_server import (
    FixtureError,
    FixtureServer,
    render_metrics,
    validate_fixture_document,
)
from harness.oracle import Oracle, OracleDrift, Snapshot
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
from harness import stack as stack_mod
from harness.stack import find_forwarder, resource_paths, template_args

REPO_ROOT = Path(__file__).resolve().parents[1]


class FixtureServerTest(unittest.TestCase):
    def test_metrics_escaping_and_elapsed_counter(self) -> None:
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

    def test_deployment_pins_the_fixture_to_its_node(self) -> None:
        metrics = [Metric(name="fixture_value", type="gauge")]
        pinned = yaml.safe_load(_deployment(FixtureService(name="metrics", metrics=metrics, node="kind-worker2"), "fixtures"))
        self.assertEqual(
            pinned["spec"]["template"]["spec"]["nodeSelector"], {"kubernetes.io/hostname": "kind-worker2"}
        )
        free = yaml.safe_load(_deployment(FixtureService(name="metrics", metrics=metrics), "fixtures"))
        self.assertNotIn("nodeSelector", free["spec"]["template"]["spec"])

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
                    "harness.receiver.http_json",
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

    def test_failure_table_groups_by_message_and_escapes(self) -> None:
        failures = [
            ("test_os[a]", "s/t.py", 10, "assert '' == 'linux'"),
            ("test_os[b]", "s/t.py", 10, "assert '' == 'linux'"),
            ("test_os[c]", "s/t.py", 10, "assert '' == 'linux'"),
            ("test_cpu", "s/t.py", 20, "assert 1 == 2 | 100%"),
        ]
        table = "\n".join(artifacts_mod.failure_table(artifacts_mod.group_failures(failures), shown=2))
        self.assertIn("| test_os[a]<br>test_os[b]<br>and 1 more | assert '' == 'linux' | `s/t.py:10` |", table)
        self.assertIn("| test_cpu | assert 1 == 2 \\| 100% | `s/t.py:20` |", table)

    def test_failure_annotation_escapes_and_points_at_the_first_test(self) -> None:
        group = ("assert 1 == 2 | 100%", [("test_a", "s/t.py", 10), ("test_b", "s/t.py", 11), ("test_c", "s/t.py", 12)])
        self.assertEqual(
            artifacts_mod.failure_annotation(group, shown=2, prefix="scn: "),
            "::error file=s/t.py,line=10,title=scn%3A test_a%2C test_b%2C and 1 more::assert 1 == 2 | 100%25",
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
                    "harness.prometheus.http_json",
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


class HelmValuesTest(unittest.TestCase):
    def test_chart_and_collector_default_to_latest(self) -> None:
        args = template_args("demo", StackSpec(), ForwarderSpec())
        self.assertNotIn("--version", args)
        self.assertFalse(any(a.startswith("container-optimization-data-forwarder.image") for a in args))

    def test_preloaded_collector_image_is_never_pulled(self) -> None:
        args = template_args(
            "demo", StackSpec(), ForwarderSpec(), "1.1.24", collector_image="kubex-e2e/collector:abc"
        )
        self.assertIn("container-optimization-data-forwarder.image=kubex-e2e/collector:abc", args)
        self.assertIn("container-optimization-data-forwarder.images.dataCollection.pullPolicy=Never", args)

    def test_chart_version_is_passed_only_when_given(self) -> None:
        self.assertNotIn("--version", template_args("demo", StackSpec(), ForwarderSpec(), chart_version=""))
        args = template_args("demo", StackSpec(), ForwarderSpec(), chart_version="2.0.0")
        self.assertEqual(args[args.index("--version") + 1], "2.0.0")

    def test_local_chart_path_is_used_without_published_version(self) -> None:
        args = template_args("demo", StackSpec(), ForwarderSpec(), chart_path="helm-charts/charts/kubex-automation-stack")
        self.assertEqual(args[3], "helm-charts/charts/kubex-automation-stack")
        self.assertNotIn("--version", args)

    def test_unused_subcharts_are_disabled_by_default_and_overridable(self) -> None:
        keys = ["kubex-connector", "kubex-ai-cdi", "kubex-automation-engine", "k8s-ephemeral-storage-metrics"]
        args = template_args("demo", StackSpec(), ForwarderSpec())
        for key in keys:
            self.assertIn(f"{key}.enabled=false", args)
        args = template_args(
            "demo", StackSpec(helm_overrides={"kubex-connector": {"enabled": True}}), ForwarderSpec()
        )
        self.assertIn("kubex-connector.enabled=true", args)
        self.assertNotIn("kubex-connector.enabled=false", args)
        self.assertIn("kubex-ai-cdi.enabled=false", args)

    def test_overrides_escape_literal_segments_and_encode_lists_as_json(self) -> None:
        stack = StackSpec(
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
        args = template_args("demo", stack, ForwarderSpec())
        joined = " ".join(args)
        self.assertIn(r"prometheus.server.nodeSelector.topology\.kubex\.ai/node-group=mvp-workers", joined)
        self.assertIn(r"literal\[a\]\,b\\c", joined)
        # Lists go through --set-json so commas inside them are not split.
        self.assertIn("--set-json", args)
        self.assertIn(
            'prometheus.server.tolerations=[{"effect":"NoSchedule","key":"a,b","operator":"Exists"}]', args
        )

    def test_string_values_escape_commas_and_backslashes(self) -> None:
        stack = StackSpec(helm_overrides={"a": {"roles": "control-plane,infra", "path": "x\\y"}})
        args = template_args("demo", stack, ForwarderSpec())
        self.assertIn(r"a.roles=control-plane\,infra", args)
        self.assertIn(r"a.path=x\\y", args)

    def test_collection_settings_reach_the_chart(self) -> None:
        args = template_args(
            "demo", StackSpec(), ForwarderSpec(collection=Collection(interval="hours", interval_size=2))
        )
        joined = " ".join(args)
        self.assertIn("container-optimization-data-forwarder.config.collection.interval=hours", joined)
        self.assertIn("container-optimization-data-forwarder.config.collection.interval_size=2", joined)


def _cronjob(name: str, image: str, policy: str = "IfNotPresent") -> str:
    return f"""apiVersion: batch/v1
kind: CronJob
metadata:
  name: {name}
spec:
  schedule: "0 0 1 1 *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: forwarder
              image: "{image}"
              imagePullPolicy: {policy}
"""


FORWARDER_IMAGE = "densify/container-optimization-data-forwarder:4"
OTHER_CRONJOB = _cronjob("other", "busybox:latest")


class ForwarderDiscoveryTest(unittest.TestCase):
    def test_finds_the_cronjob_in_both_chart_shapes(self) -> None:
        for name in ("kubex-kubex-stack", "kubex-stack"):  # chart 1.1.7, 1.1.23+
            manifest = OTHER_CRONJOB + "---\n" + _cronjob(name, FORWARDER_IMAGE)
            self.assertEqual(find_forwarder(manifest), (name, FORWARDER_IMAGE))

    def test_zero_or_two_matches_are_errors(self) -> None:
        with self.assertRaisesRegex(HarnessError, r"found \[\]"):
            find_forwarder(OTHER_CRONJOB)
        two = _cronjob("a", FORWARDER_IMAGE) + "---\n" + _cronjob("b", FORWARDER_IMAGE)
        with self.assertRaisesRegex(HarnessError, "'a'.*'b'"):
            find_forwarder(two)


class StackInstallTest(unittest.TestCase):
    SEARCH = json.dumps(
        [
            {"name": "kubex/kubex-automation-stack", "version": v}
            for v in ("1.1.9", "1.1.23", "1.1.24", "1.2.0-beta1", "1.1.10")
        ]
    )

    def _install(self, scenario: ScenarioSpec, **kwargs: object) -> tuple[list[list[str]], dict, str]:
        calls: list[list[str]] = []

        def fake_run(args: list[str], **_: object) -> str:
            calls.append(args)
            if args[:2] == ["helm", "search"]:
                return self.SEARCH
            if args[:2] == ["helm", "template"]:
                # Last --set wins, as in helm.
                image, policy = FORWARDER_IMAGE, "IfNotPresent"
                for arg in args:
                    if arg.startswith("container-optimization-data-forwarder.image="):
                        image = arg.split("=", 1)[1]
                    if arg.startswith("container-optimization-data-forwarder.images.dataCollection.pullPolicy="):
                        policy = arg.split("=", 1)[1]
                return _cronjob("kubex-stack", image, policy)
            if args[:3] == ["docker", "image", "inspect"]:
                digest = "densify/container-optimization-data-forwarder@sha256:" + "ab" * 32
                return json.dumps([{"RepoDigests": [digest], "Os": "linux", "Architecture": "arm64"}])
            return ""

        versions: dict = {}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            stack_mod, "run", fake_run
        ), patch.object(stack_mod, "kubectl") as kubectl:
            root = Path(directory)
            cronjob = stack_mod.install(scenario, root, root, "10.0.0.1", versions, **kwargs)
        self.kubectl_calls = [call.args[0] for call in kubectl.call_args_list]
        return calls, versions, cronjob

    def _scenario(self, **stack: object) -> ScenarioSpec:
        return ScenarioSpec(
            name="demo", cluster=ClusterSpec(workers=[Node()]), stack=StackSpec(**stack)
        )

    def test_latest_stable_chart_is_resolved_and_always_passed(self) -> None:
        calls, versions, cronjob = self._install(self._scenario())
        template = [c for c in calls if c[:2] == ["helm", "template"]]
        self.assertEqual(len(template), 2)  # default render (finds CronJob and image), then the real one
        for args in template:
            self.assertEqual(args[args.index("--version") + 1], "1.1.24")
        self.assertEqual(versions["chart_version"], "1.1.24")
        self.assertEqual(cronjob, "kubex-stack")

    def test_the_cronjob_is_suspended_after_it_is_applied(self) -> None:
        self._install(self._scenario())
        self.assertEqual(self.kubectl_calls[0][:2], ["apply", "-f"])
        self.assertEqual(
            self.kubectl_calls[1], ["patch", "cronjob", "kubex-stack", "-n", "kubex", "-p", '{"spec":{"suspend":true}}']
        )

    def test_helm_overrides_cannot_replace_the_preloaded_collector(self) -> None:
        overrides = {
            "container-optimization-data-forwarder": {
                "image": "old/img:1",
                "images": {"dataCollection": {"pullPolicy": "Always"}},
            }
        }
        with self.assertRaisesRegex(HarnessError, "helm_overrides must not set"):
            self._install(self._scenario(helm_overrides=overrides))

    def test_render_that_ignores_the_preloaded_image_fails(self) -> None:
        with patch.object(stack_mod, "template_args", return_value=["helm", "template", "x"]):
            with self.assertRaisesRegex(HarnessError, "does not run kubex-e2e/collector"):
                self._install(self._scenario())

    def test_explicit_chart_version_wins_over_search(self) -> None:
        calls, versions, _ = self._install(self._scenario(), chart_version="1.1.10")
        self.assertFalse(any(c[:2] == ["helm", "search"] for c in calls))
        self.assertEqual(versions["chart_version"], "1.1.10")

    def test_collector_image_override_beats_rendered_default(self) -> None:
        _, versions, _ = self._install(self._scenario(), collector_image="over/img:2")
        self.assertEqual(versions["collector_image_requested"], "over/img:2")
        _, versions, _ = self._install(self._scenario())
        self.assertEqual(versions["collector_image_requested"], FORWARDER_IMAGE)

    def test_collector_is_preloaded_and_rendered_with_the_local_tag_and_never(self) -> None:
        calls, versions, _ = self._install(self._scenario())
        local = "kubex-e2e/collector:" + "ab" * 6
        self.assertIn(["docker", "pull", FORWARDER_IMAGE], calls)
        self.assertIn(["docker", "tag", FORWARDER_IMAGE, local], calls)
        save = next(c for c in calls if c[:2] == ["docker", "save"])
        self.assertEqual(save[2:4], ["--platform", "linux/arm64"])
        self.assertEqual(save[-1], local)
        load = next(c for c in calls if c[:3] == ["kind", "load", "image-archive"])
        self.assertEqual(load[3:], [save[5], "--name", "kubex-e2e-demo"])
        self.assertEqual(
            versions["collector_image_digest"],
            "densify/container-optimization-data-forwarder@sha256:" + "ab" * 32,
        )
        final = [c for c in calls if c[:2] == ["helm", "template"]][-1]
        self.assertIn(f"container-optimization-data-forwarder.image={local}", final)
        self.assertIn("container-optimization-data-forwarder.images.dataCollection.pullPolicy=Never", final)

    def test_versions_json_and_summary_carry_every_version_field_even_on_failure(self) -> None:
        def partial_install(scenario: object, repo: object, artifacts: object, ip: str, versions: dict, *_: object) -> str:
            versions["chart_version"] = "1.1.24"
            raise HarnessError("preload failed")

        scenario = ScenarioSpec(name="demo", cluster=ClusterSpec(workers=[Node()]))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(run_mod.cluster, "assert_absent"),
                patch.object(run_mod.cluster, "create"),
                patch.object(run_mod.cluster, "configure_nodes"),
                patch.object(run_mod.cluster, "delete"),
                patch.object(run_mod.receiver, "deploy"),
                patch.object(run_mod.receiver, "cluster_ip", return_value="1.2.3.4"),
                patch.object(run_mod.fixtures, "deploy"),
                patch.object(run_mod.stack, "install", partial_install),
                patch.object(run_mod.artifacts_mod, "collect"),
            ):
                with self.assertRaisesRegex(HarnessError, "preload failed"):
                    with run_mod.execute(scenario, root, root, root / "artifacts"):
                        pass
            versions = json.loads((root / "artifacts" / "demo" / "versions.json").read_text())
            summary = (root / "artifacts" / "demo" / "summary.md").read_text()
        self.assertEqual(set(versions), set(run_mod.VERSION_FIELDS))
        self.assertEqual(versions["chart_version"], "1.1.24")
        self.assertIn("| Chart | `1.1.24` |", summary)
        self.assertIn("| Collector | unknown |", summary)


class TriggerForwarderTest(unittest.TestCase):
    @staticmethod
    def _job(*condition_types: str) -> str:
        return json.dumps({"status": {"conditions": [{"type": t, "status": "True"} for t in condition_types]}})

    def _trigger(self, *polls: str) -> str:
        scenario = ScenarioSpec(name="demo", cluster=ClusterSpec(workers=[Node()]))
        with patch.object(stack_mod, "kubectl", side_effect=["created", *polls]), patch.object(stack_mod.time, "sleep"):
            return stack_mod.trigger_forwarder(scenario, Path("."), "kubex-stack")

    def test_waits_for_the_job_to_complete(self) -> None:
        # a pod failure inside the chart's backoff limit and a transient kubectl error do not end the wait
        self.assertEqual(self._trigger(self._job(), "not json", self._job("Complete")), "demo-forwarder")

    def test_a_failed_job_raises(self) -> None:
        with self.assertRaisesRegex(HarnessError, "demo-forwarder failed"):
            self._trigger(self._job(), self._job("Failed"))


class SpecValidationTest(unittest.TestCase):
    """Setup objects are validated at construction, before any cluster exists."""

    def test_cluster_requires_a_worker(self) -> None:
        with self.assertRaisesRegex(SpecError, "at least one worker"):
            ClusterSpec()

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
            FixtureService(name="f", metrics=[Metric(name="bad-name", type="gauge")])

    def test_fixture_ports_are_validated_before_provisioning(self) -> None:
        with self.assertRaisesRegex(SpecError, "port must be between"):
            FixtureService(name="f", metrics=[Metric(name="g", type="gauge")], port=70000)

    def test_invalid_node_label_is_rejected(self) -> None:
        with self.assertRaisesRegex(SpecError, "invalid label value"):
            Node(labels={"a": "not a valid value"})
        with self.assertRaisesRegex(SpecError, "invalid label name"):
            Node(labels={"a/b/c": "value"})

    def test_invalid_extended_resource_is_rejected(self) -> None:
        with self.assertRaisesRegex(SpecError, "invalid extended resource name"):
            Node(extended_resources={"gpu": "1"})
        for value in ("1.5", "-1"):
            with self.assertRaisesRegex(SpecError, "must be a whole number"):
                Node(extended_resources={"nvidia.com/gpu": value})
        Node(extended_resources={"nvidia.com/gpu": "0"})

    def test_fixture_service_requires_metrics(self) -> None:
        with self.assertRaisesRegex(SpecError, "at least one metric"):
            FixtureService(name="f", metrics=[])

    def test_fixture_service_names_are_validated_before_provisioning(self) -> None:
        metric = Metric(name="g", type="gauge")
        with self.assertRaisesRegex(SpecError, "invalid Kubernetes name"):
            FixtureService(name="Bad Name", metrics=[metric])
        with self.assertRaisesRegex(SpecError, "invalid Kubernetes name"):
            FixtureService(name="fixture", namespace="Bad Namespace", metrics=[metric])

    def test_cluster_name_and_node_names_are_deterministic(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(control_planes=[Node()], workers=[Node(), Node()]),
        )
        self.assertEqual(scenario.cluster_name, "kubex-e2e-demo")
        self.assertEqual(
            scenario.cluster.node_names(scenario.cluster_name),
            ["kubex-e2e-demo-control-plane", "kubex-e2e-demo-worker", "kubex-e2e-demo-worker2"],
        )
        self.assertEqual(scenario.prefix, "data/demo")

    def test_kubelet_configuration_renders_one_top_level_patch(self) -> None:
        configuration = {"crashLoopBackOff": {"maxContainerRestartPeriod": "1s"}}
        rendered = yaml.safe_load(render_config(ClusterSpec(workers=[Node()], kubelet_configuration=configuration)))
        self.assertEqual(
            [yaml.safe_load(patch) for patch in rendered["kubeadmConfigPatches"]],
            [{"kind": "KubeletConfiguration", **configuration}],
        )
        self.assertNotIn("kubeadmConfigPatches", yaml.safe_load(render_config(ClusterSpec(workers=[Node()]))))

    def test_resource_paths_fail_before_cluster_work(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(workers=[Node()]),
            resources=["missing.yaml"],
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(HarnessError, "does not exist"):
                resource_paths(scenario, Path(directory))


class ClusterLifecycleTest(unittest.TestCase):
    def test_existing_cluster_is_never_deleted(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(workers=[Node()]),
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

    def test_extended_resources_are_patched_and_awaited_before_the_stack(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(
                workers=[Node(labels={"g": "x"}, extended_resources={"nvidia.com/gpu": "4"}), Node()],
            ),
        )
        calls: list[list[str]] = []

        def record(args: list[str], *_: object, **__: object) -> str:
            calls.append(args)
            return ""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(run_mod.cluster, "assert_absent"),
                patch.object(run_mod.cluster, "create"),
                patch.object(run_mod.cluster, "delete"),
                patch("harness.cluster.kubectl", record),
                patch.object(run_mod.receiver, "deploy", side_effect=HarnessError("stop after nodes")),
                patch.object(run_mod.artifacts_mod, "collect"),
            ):
                with self.assertRaisesRegex(HarnessError, "stop after nodes"):
                    with run_mod.execute(scenario, root, root, root / "artifacts"):
                        pass

        self.assertEqual(
            calls,
            [
                ["label", "node", "kubex-e2e-demo-worker", "g=x", "--overwrite"],
                [
                    "patch", "node", "kubex-e2e-demo-worker", "--subresource=status", "--type=merge",
                    "-p", '{"status": {"capacity": {"nvidia.com/gpu": "4"}}}',
                ],
                [
                    "wait", "node/kubex-e2e-demo-worker",
                    "--for=jsonpath={.status.allocatable.nvidia\\.com/gpu}=4", "--timeout=120s",
                ],
            ],
        )

    def test_partial_creation_is_cleaned_up(self) -> None:
        scenario = ScenarioSpec(
            name="demo",
            cluster=ClusterSpec(workers=[Node()]),
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

    def test_the_prefix_is_stripped_from_members(self) -> None:
        payload = self._zip({"data/demo/node/a.csv": b"h\n", "data/demo/": b""})
        self.assertEqual(sorted(read_archive_files(payload, "data/demo")), ["node/a.csv"])

    def test_rejects_traversal(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "traversal"):
            read_archive_files(self._zip({"../escape.csv": b"h\n"}))

    def test_rejects_absolute_paths(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "absolute path"):
            read_archive_files(self._zip({"/etc/passwd": b"x"}))

    def test_rejects_members_outside_the_prefix(self) -> None:
        with self.assertRaisesRegex(ArchiveError, "outside configured archive prefix"):
            read_archive_files(self._zip({"other/a.csv": b"h\n"}), "data/demo")

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
        self.assertEqual([row["b"] for row in table], ["2", "4"])

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
            "status": {"allocatable": {"cpu": "2", "memory": "1Gi"}, "capacity": {"cpu": "4", "memory": "2Gi"}},
        },
        {
            "metadata": {"name": "worker-b", "labels": {"group": "workers"}},
            "status": {"allocatable": {"cpu": "2", "memory": "1Gi"}, "capacity": {"cpu": "4", "memory": "2Gi"}},
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
                    {
                        "name": "app",
                        "resources": {
                            "requests": {"cpu": "200m", "memory": "64Mi"},
                            "limits": {"cpu": "500m", "memory": "128Mi"},
                        },
                    },
                    {
                        "name": "finished",
                        "resources": {"requests": {"cpu": "5", "memory": "5Gi"}, "limits": {"cpu": "6"}},
                    },
                ],
                "initContainers": [
                    {
                        "name": "sidecar",
                        "restartPolicy": "Always",
                        "resources": {
                            "requests": {"cpu": "50m", "memory": "16Mi"},
                            "limits": {"cpu": "100m", "memory": "32Mi"},
                        },
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
                "containers": [{
                        "name": "kindnet",
                        "resources": {"requests": {"cpu": "100m", "memory": "50Mi"}, "limits": {"cpu": "200m"}},
                    }],
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

    def test_limits_use_the_same_filtering_as_requests(self) -> None:
        oracle = self._oracle()
        self.assertAlmostEqual(oracle.limits("cpu", {"group": "workers"}), 0.8)
        self.assertEqual(oracle.limits("memory", {"group": "workers"}), 160 * 1024**2)

    def test_a_resource_a_node_does_not_advertise_counts_as_zero(self) -> None:
        nodes = copy.deepcopy(self.NODES)
        nodes[0]["status"]["capacity"]["nvidia.com/gpu"] = "4"
        nodes[0]["status"]["allocatable"]["nvidia.com/gpu"] = "4"
        oracle = Oracle([Snapshot("only", nodes, self.PODS)])
        self.assertEqual(oracle.capacity("nvidia.com/gpu", {"group": "workers"}), 4.0)
        self.assertEqual(oracle.requests("nvidia.com/gpu", {"group": "system"}), 0.0)
        with self.assertRaisesRegex(HarnessError, "no allocatable"):
            oracle.reservation_percent("nvidia.com/gpu", {"group": "system"})
        with self.assertRaisesRegex(HarnessError, "invalid Kubernetes quantity"):
            oracle.capacity("cpu", {"group": "system"})

    def test_capacity_and_pod_count(self) -> None:
        oracle = self._oracle()
        self.assertEqual(oracle.capacity("cpu", {"group": "workers"}), 8.0)
        self.assertEqual(oracle.capacity("memory", {"group": "workers"}), 4 * 1024**3)
        self.assertEqual(oracle.pod_count({"group": "workers"}), 2)

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

    def test_limit_and_pod_count_drift_are_detected(self) -> None:
        kindnet = self.PODS[1]
        raised = {
            **kindnet,
            "spec": {**kindnet["spec"], "containers": [{"name": "kindnet", "resources": {"limits": {"cpu": "300m"}}}]},
        }
        start = Snapshot("window start", self.NODES, self.PODS)
        oracle = self._oracle(start, Snapshot("window end", self.NODES, [self.PODS[0], raised]))
        with self.assertRaisesRegex(OracleDrift, "limits changed from 0.8 at window start"):
            oracle.limits("cpu", {"group": "workers"})
        oracle = self._oracle(start, Snapshot("window end", self.NODES, self.PODS[:1]))
        with self.assertRaisesRegex(OracleDrift, "pod_count.*changed from 2"):
            oracle.pod_count({"group": "workers"})

    def test_quantity_suffixes(self) -> None:
        from harness.oracle import quantity

        self.assertEqual(quantity("1Ki", "t"), 1024)
        self.assertEqual(quantity("500m", "t"), Decimal("0.5"))
        self.assertEqual(quantity("2Gi", "t"), 2 * 1024**3)
        with self.assertRaisesRegex(HarnessError, "invalid Kubernetes quantity"):
            quantity("12Zi", "t")


class FailureOutputTest(unittest.TestCase):
    def test_assertrepr_names_actual_and_expected(self) -> None:
        compare = conftest.pytest_assertrepr_compare
        self.assertEqual(compare("==", {"a": "1", "b": "2"}, {"a": "1", "b": "3"}), ["1 of 2 keys differ", "'b': actual '2', expected '3'"])
        self.assertEqual(
            compare("==", {"x": 1}, {"y": 1})[1:],
            ["'x': actual 1, expected '<absent>'", "'y': actual '<absent>', expected 1"],
        )
        self.assertEqual(
            compare("==", {1, 2}, {2, 3}), ["sets differ: missing 1, unexpected 1", "missing 3", "unexpected 1"]
        )
        self.assertEqual(compare("==", 2.0, pytest.approx(0.5, abs=1e-6)), ["actual 2.0, expected 0.5 ± 1.0e-06"])
        self.assertIsNone(compare("==", "x" * 200, "y" * 200))
        self.assertIsNone(compare("!=", 1, 2))

    def test_failure_headline_and_prefix(self) -> None:
        def failure(message: str, when: str = "call"):
            crash = SimpleNamespace(message=message, path=str(conftest.REPO_ROOT / "s/t.py"), lineno=42)
            report = SimpleNamespace(
                longrepr=SimpleNamespace(reprcrash=crash), when=when, location=("s/t.py", 9, "t"), nodeid="s/t.py::test_x[a]"
            )
            return conftest._failure(report, conftest.REPO_ROOT)

        self.assertEqual(
            failure("AssertionError: a.csv at T\nassert actual 1, expected 2\nextra"),
            ("test_x[a]", "s/t.py", 42, "a.csv at T — assert actual 1, expected 2"),
        )
        self.assertEqual(failure("assert actual 1, expected 2")[3], "assert actual 1, expected 2")
        self.assertEqual(failure("KeyError: 'k'")[3], "[call error] KeyError: 'k'")
        self.assertEqual(failure("RuntimeError: no cluster", "setup")[::3], ("test_x[a] (setup)", "[setup error] RuntimeError: no cluster"))


if __name__ == "__main__":
    unittest.main()
