"""Checks every scenario runs. A scenario opts in with ``from harness.universal import *``.

Each function is a pytest test, so importing them makes pytest collect them in that scenario's
module, against that scenario's ``run`` and ``archive`` fixtures.
"""

from __future__ import annotations

from harness.archive import assert_log_clean
from harness.coverage import INVENTORY, check_archive

__all__ = [
    "test_single_upload",
    "test_job_never_failed",
    "test_log_is_clean",
    "test_archive_matches_inventory",
    "test_expected_files",
]


def test_single_upload(run):
    assert run.uploads == 1, f"expected exactly 1 archive upload, the receiver captured {run.uploads}"


def test_job_never_failed(run):
    assert run.job_failures == 0, f"forwarder Job {run.job_name} had {run.job_failures} failed attempt(s)"


def test_log_is_clean(archive):
    assert_log_clean(archive)


def test_archive_matches_inventory(run):
    errors, warnings = check_archive(run.archive)
    run.coverage_warnings.extend(warnings)
    assert not errors, "archive differs from the coverage inventory:\n" + "\n".join(errors)


def test_expected_files(run):
    expected = run.scenario.files
    assert expected, f"scenario {run.scenario.name} declares no expected files"
    present = {p for p in run.archive.paths if p in INVENTORY}
    missing, unexpected = sorted(expected - present), sorted(present - expected)
    assert not missing and not unexpected, f"archive files differ: missing {missing}, unexpected {unexpected}"
    empty = sorted(path for path in expected if not len(run.archive.csv(path)))
    assert not empty, f"files without rows: {empty}"
