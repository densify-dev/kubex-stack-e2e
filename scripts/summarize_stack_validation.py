#!/usr/bin/env python3
"""Create a Markdown summary for a stack validation run."""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path


REQUIRED_FILES = [
    "cluster/config.csv",
    "cluster/attributes.csv",
    "node/config.csv",
    "node/attributes.csv",
    "container/config.csv",
    "container/attributes.csv",
]


def _find_csv(csv_dir: Path, relative: str) -> Path | None:
    direct = csv_dir / relative
    if direct.exists():
        return direct
    matches = [path for path in csv_dir.rglob(Path(relative).name) if str(path).replace("\\", "/").endswith(relative)]
    return matches[0] if matches else None


def collection_results(
    state_path: Path,
    csv_dir: Path,
    status: str,
    chart_metadata_path: Path | None = None,
    beyla_detection_path: Path | None = None,
) -> dict[str, object]:
    uploads = 0
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            requests = state.get("uploads", [])
            uploads = len(requests) if isinstance(requests, list) else 0
        except (json.JSONDecodeError, OSError):
            pass

    csv_files: dict[str, object] = {}
    for relative in REQUIRED_FILES:
        path = _find_csv(csv_dir, relative)
        if path is None:
            csv_files[relative] = {"exists": False, "columns": 0, "column_names": [], "data_rows": 0, "has_data": False}
            continue
        with path.open(newline="", encoding="utf-8", errors="replace") as handle:
            rows = list(csv.reader(handle))
        header = rows[0] if rows else []
        data_rows = sum(1 for row in rows[1:] if any(cell.strip() for cell in row))
        csv_files[relative] = {
            "exists": True,
            "columns": len(header),
            "column_names": header,
            "data_rows": data_rows,
            "has_data": bool(header) and data_rows > 0,
        }

    runtimes = {runtime: 0 for runtime in sorted({"go", "java", "nodejs", "python", "dotnet"})}
    healthy_targets = 0
    if beyla_detection_path is not None and beyla_detection_path.exists():
        try:
            detection = json.loads(beyla_detection_path.read_text(encoding="utf-8"))
            detected = detection.get("detected", {})
            if isinstance(detected, dict):
                for runtime in runtimes:
                    entries = detected.get(runtime, [])
                    runtimes[runtime] = len(entries) if isinstance(entries, list) else 0
            healthy_targets = int(detection.get("healthy_beyla_targets", 0))
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    chart_version = "unknown"
    if chart_metadata_path is not None and chart_metadata_path.exists():
        try:
            chart_version = str(json.loads(chart_metadata_path.read_text(encoding="utf-8")).get("version", "unknown"))
        except (OSError, json.JSONDecodeError):
            pass

    return {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "commit": os.getenv("GITHUB_SHA"),
        "status": status,
        "chart_version": chart_version,
        "uploads": uploads,
        "csv_files": csv_files,
        "beyla_runtimes": runtimes,
        "beyla_total_series": sum(runtimes.values()),
        "healthy_beyla_targets": healthy_targets,
    }


def summarize(
    state_path: Path,
    csv_dir: Path,
    status: str,
    chart_metadata_path: Path | None = None,
    beyla_detection_path: Path | None = None,
) -> str:
    uploads = 0
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            requests = state.get("uploads", [])
            uploads = len(requests) if isinstance(requests, list) else 0
        except (json.JSONDecodeError, OSError):
            pass

    lines = [
        "## Stack Validation",
        "",
        f"**Status:** {status}",
        f"**Captured uploads:** {uploads}",
        "",
        "| CSV | Data rows |",
        "| --- | ---: |",
    ]
    for relative in REQUIRED_FILES:
        path = _find_csv(csv_dir, relative)
        rows: int | str = "missing"
        if path is not None:
            with path.open(newline="", encoding="utf-8", errors="replace") as handle:
                rows = max(sum(1 for row in csv.reader(handle) if any(cell.strip() for cell in row)) - 1, 0)
        lines.append(f"| `{relative}` | {rows} |")
    if chart_metadata_path is not None and chart_metadata_path.exists():
        try:
            metadata = json.loads(chart_metadata_path.read_text(encoding="utf-8"))
            lines.extend(["", f"**Automation-stack chart:** `{metadata.get('version', 'unknown')}`"])
        except (OSError, json.JSONDecodeError):
            lines.extend(["", "**Automation-stack chart:** metadata unavailable"])
    if beyla_detection_path is not None and beyla_detection_path.exists():
        try:
            detection = json.loads(beyla_detection_path.read_text(encoding="utf-8"))
            detected = ", ".join(sorted(name for name, entries in detection.get("detected", {}).items() if entries))
            lines.extend(["", f"**Beyla runtimes detected:** {detected or 'none'}"])
        except (OSError, json.JSONDecodeError):
            lines.extend(["", "**Beyla runtimes detected:** unavailable"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--csv-dir", type=Path, required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--chart-metadata", type=Path)
    parser.add_argument("--beyla-detection", type=Path)
    parser.add_argument("--collection-results", type=Path)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        summarize(args.state, args.csv_dir, args.status, args.chart_metadata, args.beyla_detection),
        encoding="utf-8",
    )
    if args.collection_results:
        args.collection_results.parent.mkdir(parents=True, exist_ok=True)
        args.collection_results.write_text(
            json.dumps(collection_results(args.state, args.csv_dir, args.status, args.chart_metadata, args.beyla_detection), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
