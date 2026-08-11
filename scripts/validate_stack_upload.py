#!/usr/bin/env python3
"""Validate a fake Kubex stack upload capture."""

from __future__ import annotations

import argparse
import base64
import csv
import http.client
import io
import json
import re
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


DEFAULT_REQUIRED_FILES = [
    "cluster/config.csv",
    "cluster/attributes.csv",
    "node/config.csv",
    "node/attributes.csv",
    "container/config.csv",
    "container/attributes.csv",
]


def _load_state(source: str, timeout: int) -> dict[str, object]:
    if source.startswith("http://") or source.startswith("https://"):
        deadline = time.monotonic() + timeout
        last_error: str | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(source, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except (
                urllib.error.URLError,
                http.client.RemoteDisconnected,
                ConnectionError,
                TimeoutError,
                json.JSONDecodeError,
            ) as exc:
                last_error = str(exc)
                time.sleep(2)
        raise SystemExit(f"timed out waiting for state endpoint: {last_error or 'unknown error'}")
    return json.loads(Path(source).read_text(encoding="utf-8"))


def _decode_body(entry: dict[str, object]) -> bytes:
    body = entry.get("body_b64")
    if not isinstance(body, str):
        return b""
    return base64.b64decode(body.encode("ascii"))


def _extract_members(body: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            return archive.namelist()
    except zipfile.BadZipFile:
        pass

    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as archive:
            return [member.name for member in archive.getmembers() if member.name]
    except tarfile.TarError:
        pass

    return []


def _write_csv_files(state: dict[str, object], output_dir: Path) -> int:
    uploads = state.get("uploads", [])
    if not isinstance(uploads, list):
        return 0

    written = 0
    for request in uploads:
        if not isinstance(request, dict):
            continue
        body = _decode_body(request)
        if not body:
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                for name in archive.namelist():
                    relative = Path(name)
                    if not name.endswith(".csv") or relative.is_absolute() or ".." in relative.parts:
                        continue
                    destination = output_dir / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(name))
                    written += 1
        except zipfile.BadZipFile:
            continue
    return written


def _csv_data_counts(state: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    uploads = state.get("uploads", [])
    if not isinstance(uploads, list):
        return counts
    for request in uploads:
        if not isinstance(request, dict):
            continue
        body = _decode_body(request)
        if not body:
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                for name in archive.namelist():
                    if not name.endswith(".csv"):
                        continue
                    text = archive.read(name).decode("utf-8", errors="replace")
                    counts[name.replace("\\", "/")] = sum(1 for row in csv.reader(io.StringIO(text)) if any(cell.strip() for cell in row)) - 1
        except zipfile.BadZipFile:
            continue
    return counts


def _observed_paths(state: dict[str, object]) -> list[str]:
    requests = state.get("uploads", [])
    if not isinstance(requests, list):
        return []

    observed: list[str] = []
    for request in requests:
        if not isinstance(request, dict):
            continue
        members = request.get("archive_members")
        if isinstance(members, list) and members:
            observed.extend(str(member) for member in members)
            continue
        body = _decode_body(request)
        if not body:
            continue
        archive_members = _extract_members(body)
        if archive_members:
            observed.extend(archive_members)
            continue
        text = body.decode("utf-8", errors="ignore")
        observed.extend(re.findall(r"[A-Za-z0-9_./-]+\.csv", text))
    return observed


def _missing_required(observed: list[str], required: list[str]) -> list[str]:
    missing: list[str] = []
    for target in required:
        if not any(path.replace("\\", "/").endswith(target) for path in observed):
            missing.append(target)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, help="Path or URL to the captured server state")
    parser.add_argument("--output-state")
    parser.add_argument("--output-dir")
    parser.add_argument("--require-data", action="store_true")
    parser.add_argument("--required-files", nargs="*", default=DEFAULT_REQUIRED_FILES)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    state = _load_state(args.state, args.timeout)
    if args.output_state:
        Path(args.output_state).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    written_files = _write_csv_files(state, Path(args.output_dir)) if args.output_dir else 0
    uploads = state.get("uploads", [])
    if not isinstance(uploads, list) or not uploads:
        raise SystemExit("no uploads were captured by the fake stack server")

    observed = _observed_paths(state)
    missing = _missing_required(observed, list(args.required_files))
    if missing:
        raise SystemExit(f"missing expected CSV files: {', '.join(missing)}")
    if args.require_data:
        counts = _csv_data_counts(state)
        empty = [
            target
            for target in args.required_files
            if not any(path.endswith(target) and count > 0 for path, count in counts.items())
        ]
        if empty:
            raise SystemExit(f"expected CSV files have no data rows: {', '.join(empty)}")

    print(f"captured {len(uploads)} upload request(s)")
    print(f"observed {len(set(observed))} archive member(s) or CSV path(s)")
    if args.output_dir:
        print(f"wrote {written_files} captured CSV file(s) to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
