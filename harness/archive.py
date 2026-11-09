"""Safe archive extraction and strict CSV reading.

This is the untrusted-input boundary. The archive is produced by the collector
under test, so member paths are validated before anything is read, and every CSV
is structurally validated regardless of what a scenario asserts.

Row-count bounds, selectors, tolerances, and distinct-value rules are not here:
those are scenario Python. This module owns only what is true of every archive
the collector can produce.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Mapping, Sequence

from harness.spec import INTERVAL_SECONDS, Collection


class ArchiveError(ValueError):
    """Raised when archive safety or CSV structure checks fail."""


def _logical_path(member: str, prefix: str) -> str:
    name = member.replace("\\", "/")
    if "\x00" in name or re.match(r"^[A-Za-z]:($|/)", name) or name.startswith("/"):
        raise ArchiveError(f"archive member {member!r}: absolute path")
    name = name.rstrip("/")
    parts = name.split("/")
    if any(part in {"..", ""} for part in parts[:-1]) or (parts and parts[-1] == ".."):
        raise ArchiveError(f"archive member {member!r}: traversal or empty path component")
    normalized = "/".join(part for part in parts if part != ".")
    if prefix:
        if normalized == prefix:
            return ""
        marker = prefix + "/"
        if not normalized.startswith(marker):
            raise ArchiveError(f"archive member {member!r}: outside configured archive prefix {prefix!r}")
        normalized = normalized[len(marker) :]
    if not normalized or normalized.startswith("/"):
        return normalized
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ArchiveError(f"archive member {member!r}: invalid logical path")
    return normalized


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return (mode & 0o170000) == 0o120000


def read_archive_files(source: bytes, prefix: str = "") -> dict[str, bytes]:
    """Regular files of a ZIP archive as logical path -> bytes, after safe path and prefix validation."""

    files: dict[str, bytes] = {}
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            for info in archive.infolist():
                logical = _logical_path(info.filename, prefix)
                if logical in seen:
                    raise ArchiveError(f"duplicate logical archive path {logical!r}")
                seen.add(logical)
                if not logical:
                    continue
                if _is_zip_symlink(info):
                    raise ArchiveError(f"archive member {info.filename!r}: symbolic links are not allowed")
                if not info.is_dir():
                    files[logical] = archive.read(info)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError) as exc:
        raise ArchiveError(f"cannot read ZIP archive: {exc}") from exc
    return files


@dataclass(frozen=True)
class Csv:
    """One parsed CSV. Structurally valid by construction."""

    path: str
    headers: list[str]
    rows: list[dict[str, str]]

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[dict[str, str]]:
        return iter(self.rows)

    def where(self, **selector: str) -> list[dict[str, str]]:
        """Rows whose columns all equal the given values, compared as text."""

        wanted = {k: str(v) for k, v in selector.items()}
        return [r for r in self.rows if all(r.get(k, "\x00missing") == v for k, v in wanted.items())]

    def one(self, **selector: str) -> dict[str, str]:
        __tracebackhide__ = True
        matched = self.where(**selector)
        if len(matched) != 1:
            raise AssertionError(
                f"{self.path}: expected exactly 1 row matching {selector!r}, found {len(matched)}"
            )
        return matched[0]


def parse_csv(path: str, content: bytes) -> Csv:
    """Parse one CSV, enforcing structure that holds for every collector output.

    These checks are unconditional: they describe a well-formed CSV, not a
    scenario's expectations.
    """

    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArchiveError(f"file {path}: CSV is not UTF-8: {exc}") from exc
    try:
        records = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error as exc:
        raise ArchiveError(f"file {path}: invalid CSV: {exc}") from exc
    if not records:
        raise ArchiveError(f"file {path}: missing header")
    headers = records[0]
    # A file of just "\n" parses to [[]], which is truthy, so the emptiness check
    # above passes and leaves headers empty. Require a real header record.
    if not any(header.strip() for header in headers):
        raise ArchiveError(f"file {path}: header record is empty")
    duplicates = sorted({h for h in headers if headers.count(h) > 1})
    if duplicates:
        raise ArchiveError(f"file {path}: duplicate header(s): {', '.join(repr(d) for d in duplicates)}")

    rows: list[dict[str, str]] = []
    for number, record in enumerate(records[1:], start=2):
        if not record or not any(cell.strip() for cell in record):
            continue
        if len(record) != len(headers):
            raise ArchiveError(
                f"file {path}, row {number}: expected {len(headers)} columns, actual {len(record)}"
            )
        rows.append(dict(zip(headers, record)))
    return Csv(path=path, headers=headers, rows=rows)


class Archive:
    """The captured upload, parsed. Every CSV is structurally validated on construction."""

    def __init__(self, files: Mapping[str, bytes]) -> None:
        self._files = dict(files)
        # Structural CSV validity is an archive-wide contract, not something a
        # scenario opts into by mentioning a path.
        self._csv = {path: parse_csv(path, content) for path, content in self._files.items() if path.lower().endswith(".csv")}

    @property
    def paths(self) -> list[str]:
        return sorted(self._files)

    def __contains__(self, path: str) -> bool:
        return path in self._files

    def raw(self, path: str) -> bytes:
        __tracebackhide__ = True
        if path not in self._files:
            raise AssertionError(f"archive has no file {path!r} ({len(self._files)} files present)")
        return self._files[path]

    def csv(self, path: str) -> Csv:
        __tracebackhide__ = True
        self.raw(path)
        return self._csv[path]


def load(source: bytes, prefix: str = "", extracted_dir: Path | None = None) -> Archive:
    """Validate paths, optionally extract to disk, and return the Archive."""

    files = read_archive_files(source, prefix)
    archive = Archive(files)
    if extracted_dir is not None:
        destination = extracted_dir.resolve()
        destination.mkdir(parents=True, exist_ok=True)
        for relative, content in files.items():
            target = (destination / PurePosixPath(relative)).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise ArchiveError(f"logical path escaped extraction directory: {relative!r}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return archive


def load_directory(directory: Path) -> Archive:
    """Build an Archive from an already-extracted directory (replay). Read-only."""

    root = directory.resolve()
    if not root.is_dir():
        raise ArchiveError(f"extracted archive directory {directory} does not exist")
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ArchiveError(f"extracted archive member {path}: symbolic links are not allowed")
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return Archive(files)


# --- assertion helpers -------------------------------------------------------
# Small functions, deliberately. They must not grow into a declarative language.


def assert_unique_keys(table: Csv, keys: Sequence[str]) -> None:
    __tracebackhide__ = True
    missing = [k for k in keys if k not in table.headers]
    assert not missing, f"{table.path}: unique key column(s) missing: {', '.join(missing)}"
    seen: dict[tuple[str, ...], int] = {}
    for index, row in enumerate(table.rows, start=2):
        identity = tuple(row.get(k, "") for k in keys)
        assert identity not in seen, (
            f"{table.path}: duplicate key {dict(zip(keys, identity))!r} "
            f"at rows {seen[identity]} and {index}"
        )
        seen[identity] = index


def assert_distinct(table: Csv, column: str, expected: Sequence[str]) -> None:
    __tracebackhide__ = True
    actual = sorted({row.get(column, "") for row in table.rows})
    assert actual == sorted(expected), (
        f"{table.path}: column {column!r} has distinct values {actual!r}, expected {sorted(expected)!r}"
    )


def row_label(row: Mapping[str, str], keys: Sequence[str]) -> str:
    """A row's identity for failure messages: key values joined by "/", then its MetricTime."""

    return "/".join(row[k] for k in keys) + (f" at {row['MetricTime']}" if "MetricTime" in row else "")


def label_map(cell: str) -> dict[str, str]:
    """A collector label column, "<name> : <value>|..." (internal/common/output.go ConditionalPrintCSVLabelMap).

    A malformed entry raises instead of being skipped.
    """

    return dict(entry.split(" : ", 1) for entry in cell.split("|") if entry)


def prometheus_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """Kubernetes labels as kube-state-metrics exports them: "label_" plus the name with every character
    outside [a-zA-Z0-9_] replaced by "_". Empty values are dropped, as Prometheus stores no empty label."""

    return {"label_" + re.sub(r"[^a-zA-Z0-9_]", "_", key): value for key, value in labels.items() if value}



# --- universal checks --------------------------------------------------------


def expected_timestamps(audit_time: str, collection: Collection) -> list[str]:
    """The MetricTime grid the collector emits for ``audit_time`` and ``collection`` (collector v4.8.1).

    ``AuditTime`` is "now" truncated to the minute (internal/common/common.go:21-35). Each time series
    is one Prometheus range query over ``[AuditTime - Interval, AuditTime]`` with ``step = sample_rate``
    minutes (internal/common/prometheus.go:254-273), so the grid is exactly ``start + k*step`` in
    RFC3339 UTC (output.go:63-79). ``history`` only repeats the query for workload files
    (workload.go:252), which are not modelled. A step is absent when the series has no sample in
    Prometheus' 5-minute lookback at that step; scenarios guarantee warm-up.
    """

    if collection.interval not in ("minutes", "hours"):
        raise ValueError(f"collector v4.8.1 does not support interval {collection.interval!r}")
    interval = timedelta(seconds=collection.interval_size * INTERVAL_SECONDS[collection.interval])
    step = timedelta(minutes=collection.sample_rate)
    start = datetime.fromisoformat(audit_time).astimezone(timezone.utc) - interval
    return [(start + step * k).strftime("%Y-%m-%dT%H:%M:%SZ") for k in range(interval // step + 1)]


def assert_metric_grid(
    table: Csv,
    keys: Sequence[str],
    identities: Iterable[Sequence[str]],
    timestamps: Iterable[str],
) -> None:
    """Exact identity set, unique (identity, MetricTime), and the same timestamp set per identity."""

    __tracebackhide__ = True
    absent = [k for k in (*keys, "MetricTime") if k not in table.headers]
    assert not absent, f"{table.path}: grid column(s) missing: {', '.join(absent)}"
    expected = {tuple(identity) for identity in identities}
    seen: dict[tuple[str, ...], list[str]] = {}
    for row in table.rows:
        seen.setdefault(tuple(row[k] for k in keys), []).append(row["MetricTime"])
    name = "/".join
    missing, unexpected = sorted(expected - seen.keys()), sorted(seen.keys() - expected)
    if missing or unexpected:
        raise AssertionError(
            f"{table.path}: identities differ ({len(missing)} missing, {len(unexpected)} unexpected; key {'/'.join(keys)})"
            + "".join(f"\n  missing    {name(i)}" for i in missing[:5])
            + "".join(f"\n  unexpected {name(i)}" for i in unexpected[:5])
        )
    want = set(timestamps)
    problems = []
    for identity, times in sorted(seen.items()):
        repeated = sorted({t for t in times if times.count(t) > 1})
        lost, extra = sorted(want - set(times)), sorted(set(times) - want)
        if repeated or lost or extra:
            parts = [f"missing {lost}" if lost else "", f"unexpected {extra}" if extra else "", f"duplicate {repeated}" if repeated else ""]
            problems.append(f"{name(identity)}: " + ", ".join(p for p in parts if p))
    if problems:
        raise AssertionError(
            f"{table.path}: MetricTime grid ({len(want)} steps) differs for {len(problems)} of {len(seen)} identities"
            + "".join(f"\n  {p}" for p in problems[:5])
            + (f"\n  ... and {len(problems) - 5} more" if len(problems) > 5 else "")
        )


# A failed query is logged as "... query=<q> message=<err>" (collector internal/common/result.go:34,42);
# the "no data" skip is the same line with a different <err> (filter.go:268-269) and must pass. Queries of
# workload files are the exception: their failures are not logged (common/workload.go:255-261 skips a result
# whose query failed as empty), so test_expected_files is the guard for those. The
# markers are the prometheus/client_golang error types, transport errors from net/http and
# go-retryablehttp, and the collector's own FatalError messages that stop a run.
QUERY_FAILURES = (
    "bad_data:", "execution:", "timeout:", "canceled:", "bad_response:", "server_error:", "client_error:",
    "giving up after", "connection refused", "no such host", "i/o timeout",
    "context deadline exceeded", "context canceled",
    "cannot filter model.Value",
    "Failed to connect to Prometheus:", "Failed to calculate scrape intervals:", "Failed to log all metrics:",
)


def assert_log_clean(archive: Archive) -> None:
    """Fail on query-execution failures in log.txt only; skipped-because-absent lines pass."""

    bad = []
    for number, line in enumerate(archive.raw("log.txt").decode("utf-8", "replace").splitlines(), start=1):
        _, marker, message = line.rpartition(" message=")
        text = message if marker and " query=" in line else line
        if any(f in text for f in QUERY_FAILURES):
            bad.append(f"log.txt:{number}: {line[:300]}")
    assert not bad, f"collector reported {len(bad)} query failure(s):\n" + "\n".join(bad[:10])
