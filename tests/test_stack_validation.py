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
from scripts.summarize_stack_validation import summarize
from scripts.validate_stack_upload import _load_state, main as validate_main


class StackValidationHelpersTest(unittest.TestCase):
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
