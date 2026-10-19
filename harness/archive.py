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
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Iterator, Mapping, Sequence

from harness.spec import INTERVAL_SECONDS, Collection


class ArchiveError(ValueError):
    """Raised when archive safety or CSV structure checks fail."""


def _source_bytes(source: bytes | bytearray | Path | str | BinaryIO) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, (Path, str)):
        try:
            return Path(source).read_bytes()
        except OSError as exc:
            raise ArchiveError(f"cannot read archive {source}: {exc}") from exc
    try:
        return source.read()
    except OSError as exc:
        raise ArchiveError(f"cannot read archive: {exc}") from exc


def _normal_prefix(prefix: str) -> str:
    if not isinstance(prefix, str):
        raise ArchiveError("archive prefix: expected string")
    raw = prefix.replace("\\", "/")
    if "\x00" in raw or re.match(r"^[A-Za-z]:($|/)", raw) or raw.startswith("/"):
        raise ArchiveError(f"archive prefix {prefix!r}: absolute or invalid path")
    value = raw.strip("/")
    if not value:
        return ""
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveError(f"archive prefix {prefix!r}: invalid path components")
    return "/".join(parts)


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


def _archive_files(source: bytes, prefix: str) -> dict[str, tuple[bytes, bool]]:
    """Return logical path -> (bytes, is_directory), rejecting unsafe entries."""

    normalized_prefix = _normal_prefix(prefix)
    result: dict[str, tuple[bytes, bool]] = {}
    seen_logical: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            for info in archive.infolist():
                logical = _logical_path(info.filename, normalized_prefix)
                if logical in seen_logical:
                    raise ArchiveError(f"duplicate logical archive path {logical!r}")
                seen_logical.add(logical)
                if not logical:
                    continue
                if _is_zip_symlink(info):
                    raise ArchiveError(f"archive member {info.filename!r}: symbolic links are not allowed")
                is_directory = info.is_dir() or info.filename.replace("\\", "/").endswith("/")
                result[logical] = (b"" if is_directory else archive.read(info), is_directory)
            return result
    except zipfile.BadZipFile:
        pass
    except (OSError, RuntimeError, zipfile.LargeZipFile) as exc:
        raise ArchiveError(f"cannot read ZIP archive: {exc}") from exc

    try:
        with tarfile.open(fileobj=io.BytesIO(source), mode="r:*") as archive:
            for info in archive.getmembers():
                logical = _logical_path(info.name, normalized_prefix)
                if logical in seen_logical:
                    raise ArchiveError(f"duplicate logical archive path {logical!r}")
                seen_logical.add(logical)
                if not logical:
                    continue
                is_directory = info.isdir()
                if not (is_directory or info.isreg()):
                    raise ArchiveError(f"archive member {info.name!r}: links and special files are not allowed")
                extracted = None if is_directory else archive.extractfile(info)
                if not is_directory and extracted is None:
                    raise ArchiveError(f"archive member {info.name!r}: cannot read regular file")
                content = b"" if extracted is None else extracted.read()
                result[logical] = (content, is_directory)
            return result
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ArchiveError(f"archive is neither valid ZIP nor TAR: {exc}") from exc


def read_archive_files(
    source: bytes | bytearray | Path | str | BinaryIO, archive_prefix: str = ""
) -> dict[str, bytes]:
    """Read regular archive files after safe path and prefix validation."""

    entries = _archive_files(_source_bytes(source), archive_prefix)
    return {path: content for path, (content, is_directory) in entries.items() if not is_directory}


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

    def column(self, name: str) -> list[str]:
        if name not in self.headers:
            raise AssertionError(f"{self.path}: no column {name!r}; headers are {self.headers!r}")
        return [row[name] for row in self.rows]

    def where(self, **selector: str) -> list[dict[str, str]]:
        """Rows whose columns all equal the given values, compared as text."""

        wanted = {k: str(v) for k, v in selector.items()}
        return [r for r in self.rows if all(r.get(k, "\x00missing") == v for k, v in wanted.items())]

    def one(self, **selector: str) -> dict[str, str]:
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
    """The captured upload, extracted and parsed on demand."""

    def __init__(self, files: Mapping[str, bytes], extracted_dir: Path | None = None) -> None:
        self._files = dict(files)
        # Structural CSV validity is an archive-wide contract, not something a
        # scenario opts into by mentioning a path.
        self._csv_cache = {
            path: parse_csv(path, content)
            for path, content in self._files.items()
            if path.lower().endswith(".csv")
        }
        self.extracted_dir = extracted_dir

    @property
    def paths(self) -> list[str]:
        return sorted(self._files)

    def __contains__(self, path: str) -> bool:
        return path in self._files

    def raw(self, path: str) -> bytes:
        if path not in self._files:
            raise AssertionError(f"archive has no file {path!r}; present: {self.paths}")
        return self._files[path]

    def csv(self, path: str) -> Csv:
        """Parse a CSV, with structural validation. Cached per path."""

        if path not in self._csv_cache:
            self._csv_cache[path] = parse_csv(path, self.raw(path))
        return self._csv_cache[path]


def load(
    source: bytes | bytearray | Path | str | BinaryIO,
    prefix: str = "",
    extracted_dir: Path | None = None,
) -> Archive:
    """Validate paths, optionally extract to disk, and return the Archive."""

    files = read_archive_files(source, prefix)
    archive = Archive(files, extracted_dir)
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
    return Archive(files, root)


# --- assertion helpers -------------------------------------------------------
# Small functions, deliberately. They must not grow into a declarative language.


def assert_unique_keys(table: Csv, keys: Sequence[str]) -> None:
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
    actual = sorted({row.get(column, "") for row in table.rows})
    assert actual == sorted(expected), (
        f"{table.path}: column {column!r} has distinct values {actual!r}, expected {sorted(expected)!r}"
    )



# --- universal checks --------------------------------------------------------


def expected_timestamps(audit_time: str, collection: Collection) -> list[str]:
    """The MetricTime grid the collector emits for ``audit_time`` and ``collection`` (collector v4.7.5-beta1).

    ``AuditTime`` is "now" truncated to the minute (internal/common/common.go:21-35). Each time series
    is one Prometheus range query over ``[AuditTime - Interval, AuditTime]`` with ``step = sample_rate``
    minutes (internal/common/prometheus.go:254-273), so the grid is exactly ``start + k*step`` in
    RFC3339 UTC (output.go:63-79). ``history`` only repeats the query for workload files
    (workload.go:252), which are not modelled. A step is absent when the series has no sample in
    Prometheus' 5-minute lookback at that step; scenarios guarantee warm-up, and one that mutates
    mid-window passes its own timestamps to ``assert_metric_grid``.
    """

    if collection.interval not in ("minutes", "hours"):
        raise ValueError(f"collector v4.7.5-beta1 does not support interval {collection.interval!r}")
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

    absent = [k for k in (*keys, "MetricTime") if k not in table.headers]
    assert not absent, f"{table.path}: grid column(s) missing: {', '.join(absent)}"
    expected = {tuple(identity) for identity in identities}
    seen: dict[tuple[str, ...], list[str]] = {}
    for row in table.rows:
        seen.setdefault(tuple(row[k] for k in keys), []).append(row["MetricTime"])
    missing, unexpected = sorted(expected - seen.keys()), sorted(seen.keys() - expected)
    assert not missing and not unexpected, (
        f"{table.path}: identities by {list(keys)!r} differ: missing {missing!r}, unexpected {unexpected!r}"
    )
    for identity, times in sorted(seen.items()):
        repeated = sorted({t for t in times if times.count(t) > 1})
        assert not repeated, f"{table.path} {dict(zip(keys, identity))!r}: duplicate MetricTime {repeated!r}"
    want = set(timestamps)
    for identity, times in sorted(seen.items()):
        lost, extra = sorted(want - set(times)), sorted(set(times) - want)
        assert not lost and not extra, (
            f"{table.path} {dict(zip(keys, identity))!r}: MetricTime grid differs: "
            f"missing {lost!r}, unexpected {extra!r}"
        )


# A failed query is logged as "... query=<q> message=<err>" (collector internal/common/result.go:34,42);
# the "no data" skip is the same line with a different <err> (filter.go:268-269) and must pass. The
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
