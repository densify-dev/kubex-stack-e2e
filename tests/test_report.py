"""Unit tests for the run-level roll-up in ``harness.report``."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness import report
from harness.artifacts import ANNOTATION_LIMIT

SUMMARY = "# Scenario s\n\n**Stack run:** collected\n\n## Coverage warnings\n\n{warnings}\n"


def _junit(tests: int, failures: int = 0, errors: int = 0, skipped: int = 0) -> str:
    return f'<testsuites><testsuite tests="{tests}" failures="{failures}" errors="{errors}" skipped="{skipped}"/></testsuites>'


def _artifact(
    root: Path,
    name: str,
    junit: str | None,
    failures: list | None = None,
    warnings: tuple[str, ...] = (),
    summary: str | None = None,
    collector: str = "v4.8.0",
) -> None:
    directory = root / f"kind-functional-{name}-1"
    directory.mkdir(parents=True)
    body = SUMMARY.format(warnings="\n".join(f"- {w}" for w in warnings) or "none") if summary is None else summary
    (directory / "summary.md").write_text(body)
    (directory / "versions.json").write_text(json.dumps({"chart_version": "1.0", "collector_version": collector}))
    if junit is not None:
        (directory / "pytest.xml").write_text(junit)
    if failures is not None:
        (directory / "failures.json").write_text(json.dumps(failures))


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def load(self, *names: str) -> list[report.Scenario]:
        return [report.load(self.root / f"kind-functional-{n}-1", n) for n in names]

    def test_load_statuses_and_counts(self) -> None:
        _artifact(self.root, "ok", _junit(5, skipped=1))
        _artifact(self.root, "red", _junit(5, failures=1, errors=1), [["t1", "f.py", 3, "assert 1 == 2"], ["t2 (setup)", "f.py", 4, "[setup error] boom"]])
        _artifact(self.root, "dead", _junit(3, errors=3), summary="# Scenario dead\n\n**Stack run:** setup failed\n")
        _artifact(self.root, "nojunit", None)
        ok, red, dead, nojunit, gone = self.load("ok", "red", "dead", "nojunit", "gone")
        self.assertEqual((ok.status, ok.passed, ok.failed), ("passed", 4, 0))
        self.assertEqual((red.status, red.passed, red.failed, red.setup_errors), ("failed", 3, 2, 1))
        self.assertEqual((dead.status, dead.failed), ("setup error", 3))
        self.assertEqual(nojunit.status, "setup error")
        self.assertEqual((gone.status, gone.failed), (report.NO_ARTIFACT, 0))

    def test_headline_counts_and_version_differences(self) -> None:
        _artifact(self.root, "a", _junit(4, failures=1), [["t", "f.py", 1, "m"]])
        _artifact(self.root, "b", _junit(2), collector="v4.9.0")
        scenarios = self.load("a", "b", "gone")
        self.assertEqual(
            report.headline(scenarios),
            "1 failed, 5 passed. 2 of 3 scenarios failed (1 without an artifact)."
            " Tested chart 1.0, collector v4.8.0 and v4.9.0 (differs between scenarios).",
        )

    def test_markdown_table_links_and_details(self) -> None:
        few = [[f"t{i}", "f.py", i, f"m{i}"] for i in range(report.OPEN_BELOW - 1)]
        many = [[f"t{i}", "f.py", i, f"m{i}"] for i in range(report.OPEN_BELOW)]
        _artifact(self.root, "few", _junit(10, failures=len(few)), few, warnings=("shared",))
        _artifact(self.root, "many", _junit(10, failures=len(many)), many, warnings=("shared", "other"))
        text = report.markdown(self.load("few", "many", "gone"), "https://x/docs")
        self.assertIn("| [few](https://x/docs/few.md) | failed | 5 | 5 | 0 |", text)
        self.assertIn("| [gone](https://x/docs/gone.md) | no artifact | - | - | - |", text)
        self.assertIn("<details open>\n<summary><b>few</b>", text)
        self.assertIn("<details>\n<summary><b>many</b>", text)
        self.assertEqual(text.count("- shared"), 1)
        self.assertIn("- other", text)

    def test_annotations_cap_and_order(self) -> None:
        big = [[f"big{i}", "f.py", 1, "same"] for i in range(3)]
        singles = [[f"t{i}", "f.py", i, f"m{i}"] for i in range(12)]
        _artifact(self.root, "a", _junit(20, failures=12), singles)
        _artifact(self.root, "b", _junit(20, failures=3), big, warnings=("w1", "w2"))
        _artifact(self.root, "c", _junit(20, failures=1), [["c0", "f.py", 1, "c"]])
        result = report.annotations(self.load("a", "b", "c"))
        errors = [line for line in result if line.startswith("::error")]
        self.assertEqual(len(errors), ANNOTATION_LIMIT)
        self.assertTrue(errors[0].startswith("::error title=E2E run summary::16 failed"))
        self.assertIn("title=b%3A big0%2C big1%2C big2::same", errors[1])
        self.assertIn("title=a%3A t0::m0", errors[2])
        self.assertIn("title=c%3A c0::c", errors[3])
        self.assertEqual(errors[-1], "::error title=6 more failures::see the run summary")
        self.assertEqual(result[-1], "::warning title=Coverage review needed::w1; w2")

    def test_annotations_without_overflow_list_every_group(self) -> None:
        _artifact(self.root, "a", _junit(10, failures=9), [[f"t{i}", "f.py", i, f"m{i}"] for i in range(9)])
        result = report.annotations(self.load("a"))
        self.assertEqual(len(result), ANNOTATION_LIMIT)
        self.assertNotIn("more failures", result[-1])

    def test_a_green_run_is_a_notice_not_an_error(self) -> None:
        _artifact(self.root, "ok", _junit(2))
        self.assertTrue(report.annotations(self.load("ok"))[0].startswith("::notice title=E2E run summary::0 failed, 2 passed"))

    def test_main_succeeds_when_a_scenario_is_missing(self) -> None:
        _artifact(self.root, "ok", _junit(2))
        summary = self.root / "step-summary.md"
        # Never the real job summary: in CI the unit-test job has one too.
        with patch.dict("os.environ", {"GITHUB_RUN_ID": "1", "GITHUB_STEP_SUMMARY": str(summary)}):
            self.assertEqual(report.main([str(self.root), "ok", "gone"]), 0)
        self.assertIn("| no artifact |", summary.read_text())


if __name__ == "__main__":
    unittest.main()
