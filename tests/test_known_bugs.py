"""The ``--known-bugs`` hook in the root ``conftest.py``, run in an isolated pytest session."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytest_plugins = ["pytester"]

TESTS = '''
import pytest

@pytest.fixture
def broken():
    raise RuntimeError("fixture boom")

def test_listed_mismatch(): assert 1 == 2, "other message"
def test_listed_match(): assert 798 == 800, "CpuRequest"
def test_listed_setup_error(broken): pass
def test_listed_other_exception(): raise ValueError("CpuRequest")
def test_listed_passes(): pass
def test_unlisted(): assert 1 == 2, "CpuRequest"
'''
LISTED = {
    "test_listed_mismatch": ("B-1", "CpuRequest"),
    "test_listed_match": ("B-2", "CpuRequest"),
    "test_listed_setup_error": ("B-3", "fixture boom"),
    "test_listed_other_exception": ("B-4", "CpuRequest"),
    "test_listed_passes": ("B-5", "anything"),
}


@pytest.fixture
def session(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch):
    pytester.makeconftest((Path(__file__).parent.parent / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_bugs=TESTS)
    listed = [{"test": f"test_bugs.py::{t}", "bugs": [bug], "match": match} for t, (bug, match) in LISTED.items()]
    pytester.makefile(".yaml", **{"known-bugs": yaml.safe_dump(listed)})
    monkeypatch.setenv("FAILURES_JSON", str(pytester.path / "out" / "failures.json"))
    return pytester


def test_known_bug_is_an_xfail_and_everything_else_stays_red(session: pytest.Pytester) -> None:
    result = session.runpytest("--known-bugs", "known-bugs.yaml", "-rxX", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=0, failed=3, errors=1, xfailed=1, xpassed=1)
    result.stdout.fnmatch_lines(["XFAIL test_bugs.py::test_listed_match - B-2", "XPASS test_bugs.py::test_listed_passes - B-5"])
    found = json.loads((session.path / "out" / "known-bugs.json").read_text())
    assert [(k[0], k[3]) for k in found["known"]] == [("test_listed_match", ["B-2"])]
    assert found["known"][0][4] == "CpuRequest — assert actual 798, expected 800"
    assert found["xpassed"] == [["test_bugs.py::test_listed_passes", ["B-5"]]]
    # The unknown failures are still written for the roll-up.
    assert len(json.loads((session.path / "out" / "failures.json").read_text())) == 4


def test_without_the_option_nothing_changes(session: pytest.Pytester) -> None:
    result = session.runpytest("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1, failed=4, errors=1)
    assert not (session.path / "out" / "known-bugs.json").exists()


def test_every_entry_names_a_collected_test() -> None:
    """A renamed test or parameter would leave an entry that gates nothing."""

    root = Path(__file__).resolve().parents[1]
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "scenarios", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    listed = {entry["test"] for entry in yaml.safe_load((root / "known-bugs.yaml").read_text(encoding="utf-8"))}
    assert sorted(listed - set(collected)) == []
