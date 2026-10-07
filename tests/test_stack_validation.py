import base64
import http.client
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.inject_host_aliases import inject
from scripts.build_stack_validation_workloads import build
from scripts.build_runtime_fixtures import RUNTIMES, build as build_runtime_fixtures
from scripts.resolve_stack_chart import select_latest
from scripts.summarize_stack_validation import collection_results, summarize
from scripts.validate_stack_upload import _load_state, main as validate_main
from scripts.validate_beyla_runtime import validate


class StackValidationHelpersTest(unittest.TestCase):
    def test_runtime_fixtures_cover_required_runtimes_and_real_node(self) -> None:
        rendered = build_runtime_fixtures()

        self.assertEqual(set(RUNTIMES), {"go", "java", "nodejs", "python", "dotnet"})
        for runtime in RUNTIMES:
            self.assertIn(f"name: beyla-runtime-{runtime}", rendered)
            self.assertIn(f"kubex.ai/runtime: {runtime}", rendered)
        self.assertEqual(rendered.count("stack-validation-real: \"true\""), 5)
        self.assertIn("kind: Service", rendered)
        self.assertIn("kind: ConfigMap", rendered)

    def test_latest_chart_selection_uses_helm_order(self) -> None:
        selected = select_latest([
            {"name": "kubex/kubex-automation-stack", "version": "1.2.0"},
            {"name": "kubex/kubex-automation-stack", "version": "1.1.0"},
        ])
        self.assertEqual(selected["version"], "1.2.0")

    def test_validate_beyla_runtime_requires_all_runtime_labels(self) -> None:
        responses = [
            [{"metric": {"namespace": "kubex", "service": "kubex-beyla"}, "value": [0, "1"]}],
            [
                {"metric": {"k8s_namespace_name": "stack-validation-runtime", "telemetry_sdk_language": runtime, "k8s_deployment_name": f"beyla-runtime-{runtime}"}, "value": [0, "1"]}
                for runtime in ("go", "java", "nodejs", "python", "dotnet")
            ],
        ]
        with patch("scripts.validate_beyla_runtime.query", side_effect=responses):
            result = validate("http://prometheus", "stack-validation-runtime")

        self.assertEqual(result["missing"], [])
        self.assertEqual(result["healthy_beyla_targets"], 1)
    def test_stack_workloads_are_mixed_resource_bearing_and_kwok_scheduled(self) -> None:
        rendered = build()

        self.assertEqual(rendered.count("kind: Deployment"), 25)
        self.assertEqual(rendered.count("kind: StatefulSet"), 25)
        self.assertEqual(rendered.count("kind: CronJob"), 10)
        self.assertEqual(rendered.count("kind: DaemonSet"), 1)
        self.assertEqual(rendered.count("cpu: 25m"), 61)
        self.assertEqual(rendered.count("memory: 32Mi"), 61)
        self.assertEqual(rendered.count("- kwok"), 61)
        self.assertIn('schedule: "*/5 * * * *"', rendered)
        self.assertIn("topologySpreadConstraints:", rendered)

    def test_summary_reports_uploads_rows_and_missing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            csv_dir = root / "captured"
            state.write_text(json.dumps({"uploads": [{}, {}]}), encoding="utf-8")
            path = csv_dir / "data" / "stack-validation" / "cluster" / "config.csv"
            path.parent.mkdir(parents=True)
            path.write_text("name,value\nfirst,1\nsecond,2\n", encoding="utf-8")

            result = summarize(state, csv_dir, "success")

            self.assertIn("**Status:** success", result)
            self.assertIn("**Captured uploads:** 2", result)
            self.assertIn("| `cluster/config.csv` | 2 |", result)
            self.assertIn("| `container/config.csv` | missing |", result)

    def test_collection_results_reports_csv_schema_and_beyla_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            csv_dir = root / "captured"
            path = csv_dir / "cluster" / "config.csv"
            path.parent.mkdir(parents=True)
            path.write_text("name,value\ncluster-a,1\n\n", encoding="utf-8")
            detection = root / "beyla.json"
            detection.write_text(json.dumps({"healthy_beyla_targets": 1, "detected": {"go": [{}, {}], "java": [{}]}}), encoding="utf-8")

            result = collection_results(root / "state.json", csv_dir, "success", beyla_detection_path=detection)

            self.assertEqual(result["beyla_runtimes"]["go"], 2)
            self.assertEqual(result["beyla_runtimes"]["java"], 1)
            self.assertEqual(result["beyla_runtimes"]["python"], 0)
            self.assertEqual(result["beyla_total_series"], 3)
            self.assertEqual(result["csv_files"]["cluster/config.csv"], {"exists": True, "columns": 2, "column_names": ["name", "value"], "data_rows": 1, "has_data": True})
            self.assertFalse(result["csv_files"]["node/config.csv"]["has_data"])

    def test_load_state_retries_disconnected_port_forward(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"uploads": []}'
        with (
            patch(
                "scripts.validate_stack_upload.urllib.request.urlopen",
                side_effect=[http.client.RemoteDisconnected(), response],
            ) as urlopen,
            patch("scripts.validate_stack_upload.time.sleep"),
        ):
            self.assertEqual(_load_state("http://127.0.0.1/state", 10), {"uploads": []})

        self.assertEqual(urlopen.call_count, 2)

    def test_inject_host_aliases_into_job_and_cronjob(self) -> None:
        rendered = "\n".join(
            [
                "apiVersion: batch/v1",
                "kind: Job",
                "metadata:",
                "  name: example",
                "spec:",
                "  template:",
                "    spec:",
                "      initContainers:",
                "      - name: init",
                "      containers:",
                "      - name: main",
                "      restartPolicy: Never",
                "---",
                "apiVersion: batch/v1",
                "kind: CronJob",
                "metadata:",
                "  name: example-cron",
                "spec:",
                "  jobTemplate:",
                "    spec:",
                "      template:",
                "        spec:",
                "          containers:",
                "          - name: main",
                "          restartPolicy: Never",
            ]
        )

        transformed = inject(rendered, "fake.kubex.ai", "10.0.0.10")
        self.assertIn("hostAliases:", transformed)
        self.assertIn("hostNetwork: true", transformed)
        self.assertIn("dnsPolicy: ClusterFirstWithHostNet", transformed)
        self.assertIn("- ip: 10.0.0.10", transformed)
        self.assertIn("- fake.kubex.ai", transformed)

    def test_validate_upload_accepts_zip_archive_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            output = root / "captured"

            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, mode="w") as archive:
                for path in [
                    "cluster/config.csv",
                    "cluster/attributes.csv",
                    "node/config.csv",
                    "node/attributes.csv",
                    "container/config.csv",
                    "container/attributes.csv",
                ]:
                    archive.writestr(path, "name,value\nfixture,1\n")

            state.write_text(
                json.dumps(
                    {
                        "uploads": [
                            {
                                "body_b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "sys.argv",
                [
                    "validate_stack_upload.py",
                    "--state",
                    str(state),
                    "--output-dir",
                    str(output),
                    "--require-data",
                ],
            ):
                self.assertEqual(validate_main(), 0)

            self.assertEqual(
                (output / "cluster" / "config.csv").read_text(encoding="utf-8"),
                "name,value\nfixture,1\n",
            )
            self.assertEqual(
                (output / "container" / "attributes.csv").read_text(encoding="utf-8"),
                "name,value\nfixture,1\n",
            )

    def test_validate_upload_writes_captured_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state.json"
            output = root / "captured.json"
            state.write_text(json.dumps({"uploads": []}), encoding="utf-8")

            with patch(
                "sys.argv",
                [
                    "validate_stack_upload.py",
                    "--state",
                    str(state),
                    "--output-state",
                    str(output),
                ],
            ):
                with self.assertRaisesRegex(SystemExit, "no uploads"):
                    validate_main()

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"uploads": []})


if __name__ == "__main__":
    unittest.main()
