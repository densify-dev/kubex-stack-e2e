"""Checks every scenario runs. A scenario opts in with ``from harness.universal import *``.

Each function is a pytest test, so importing them makes pytest collect them in that scenario's
module, against that scenario's ``run`` and ``archive`` fixtures.
"""

from __future__ import annotations

from datetime import datetime

from harness.archive import assert_log_clean
from harness.coverage import INVENTORY, check_archive
from harness.spec import INTERVAL_SECONDS

__all__ = [
    "test_single_upload",
    "test_job_never_failed",
    "test_log_is_clean",
    "test_archive_matches_inventory",
    "test_expected_files",
    "test_audit_time_is_when_the_forwarder_ran",
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


def test_audit_time_is_when_the_forwarder_ran(run):
    """AuditTime is the collector's "now" floored to the collection interval (internal/common/common.go:21-35), so it
    lies between the floored start and finish of the forwarder pod that produced the archive. The MetricTime grid is
    derived from AuditTime, so a shifted window moves both together and only the pod's own clock can expose it."""

    unit = INTERVAL_SECONDS[run.scenario.forwarder.collection.interval]

    def floored(stamp):
        return int(datetime.fromisoformat(stamp).timestamp()) // unit * unit

    pods = [
        p
        for p in run.snapshots[-1].pods
        if p["metadata"].get("labels", {}).get("job-name") == run.job_name and p["status"]["phase"] == "Succeeded"
    ]
    assert len(pods) == 1, f"expected one finished pod of Job {run.job_name}, found {len(pods)}"
    ran = pods[0]["status"]["containerStatuses"][0]["state"]["terminated"]
    audit_time = run.archive.csv("cluster/config.csv").one()["AuditTime"]
    assert floored(ran["startedAt"]) <= floored(audit_time) <= floored(ran["finishedAt"]), (
        f"AuditTime {audit_time} is outside the forwarder run {ran['startedAt']} to {ran['finishedAt']}"
    )
