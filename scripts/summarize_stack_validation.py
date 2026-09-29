#!/usr/bin/env python3
"""Create a Markdown summary for a stack validation run."""

from __future__ import annotations

import argparse
import csv
import json
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
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        summarize(args.state, args.csv_dir, args.status, args.chart_metadata, args.beyla_detection),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
