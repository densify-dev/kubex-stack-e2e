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
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator, Mapping, Sequence


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


# --- assertion helpers -------------------------------------------------------
# Small functions, deliberately. They must not grow into a declarative language.


def assert_headers(table: Csv, expected: Sequence[str]) -> None:
    assert table.headers == list(expected), (
        f"{table.path}: expected headers {list(expected)!r}, actual {table.headers!r}"
    )


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


def assert_paths(archive: Archive, required: Sequence[str] = (), forbidden: Sequence[str] = ()) -> None:
    missing = [p for p in required if p not in archive]
    assert not missing, f"archive is missing required path(s): {missing!r}"
    present = [p for p in forbidden if p in archive]
    assert not present, f"archive contains forbidden path(s): {present!r}"
