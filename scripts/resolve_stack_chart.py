#!/usr/bin/env python3
"""Resolve and record the exact automation-stack chart used by a run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def select_latest(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        raise ValueError("Helm returned no automation-stack chart versions")
    return entries[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search", type=Path, required=True)
    parser.add_argument("--chart", default="kubex/kubex-automation-stack")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    entries = json.loads(args.search.read_text(encoding="utf-8"))
    selected = select_latest(entries)
    if selected.get("name") != args.chart:
        raise SystemExit(f"unexpected chart selected: {selected.get('name')!r}")
    result = {
        "chart": args.chart,
        "version": selected.get("version"),
        "app_version": selected.get("app_version"),
        "description": selected.get("description"),
        "repository": "https://densify-dev.github.io/helm-charts",
    }
    if not result["version"]:
        raise SystemExit("selected chart has no version")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(result["version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
