#!/usr/bin/env python3
"""Validate Beyla runtime survey records in Prometheus."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


EXPECTED = {"go", "java", "nodejs", "python", "dotnet"}
RUNTIME_LABELS = {"go": "go", "java": "java", "nodejs": "nodejs", "python": "python", "dotnet": "dotnet"}


def query(base_url: str, expression: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.load(response)
    return payload.get("data", {}).get("result", [])


def validate(
    base_url: str,
    namespace: str = "stack-validation-runtime",
    beyla_namespace: str = "kubex",
    expected_fixtures: list[dict[str, str]] | None = None,
) -> dict:
    fixtures = expected_fixtures or [{"runtime": runtime, "deployment": f"beyla-runtime-{runtime}"} for runtime in sorted(EXPECTED)]
    expected_pairs = {(item["runtime"], item["deployment"]) for item in fixtures}
    targets = query(base_url, f'up{{namespace="{beyla_namespace}",service=~".*beyla.*"}}')
    healthy_targets = [item for item in targets if str(item.get("value", [None, "0"])[1]) == "1"]
    if not healthy_targets:
        raise ValueError("Prometheus has no healthy kubex-beyla target")
    results = query(base_url, f'survey_info{{k8s_namespace_name="{namespace}"}}')
    if not results:
        raise ValueError("Prometheus returned no survey_info series")
    found: dict[str, list[dict]] = {item["runtime"]: [] for item in fixtures}
    unexpected: list[dict] = []
    inactive: list[dict] = []
    for result in results:
        labels = result.get("metric", {})
        runtime_value = labels.get("telemetry_sdk_language", "").lower()
        deployment = labels.get("k8s_deployment_name", "")
        if (runtime_value, deployment) not in expected_pairs:
            unexpected.append(labels)
            continue
        if str(result.get("value", [None, "0"])[1]) != "1":
            inactive.append(labels)
        found[runtime_value].append(labels)
    missing = sorted(runtime for runtime, entries in found.items() if not entries)
    duplicates = {runtime: len(entries) - 1 for runtime, entries in found.items() if len(entries) > 1}
    return {"metric": "survey_info", "namespace": namespace, "expected": fixtures, "detected": found, "missing": missing, "duplicates": duplicates, "unexpected": unexpected, "inactive": inactive, "series_count": len(results), "survey_series": [result.get("metric", {}) for result in results], "healthy_beyla_targets": len(healthy_targets)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--namespace", default="stack-validation-runtime")
    parser.add_argument("--beyla-namespace", default="kubex")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-fixtures", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    expected_fixtures = None
    if args.expected_fixtures:
        expected_fixtures = json.loads(args.expected_fixtures.read_text(encoding="utf-8"))["fixtures"]
    deadline = time.monotonic() + args.timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            result = validate(args.prometheus_url, args.namespace, args.beyla_namespace, expected_fixtures)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if not result["missing"] and not result["duplicates"] and not result["unexpected"] and not result["inactive"]:
                print("all Beyla runtime fixtures detected")
                return 0
            last_error = f"missing={result['missing']} duplicates={result['duplicates']} unexpected={len(result['unexpected'])} inactive={len(result['inactive'])}"
        except Exception as exc:
            last_error = str(exc)
        print(f"waiting for Beyla runtime detection: {last_error}", flush=True)
        time.sleep(10)
    raise SystemExit(f"timed out waiting for Beyla runtime detection: {last_error}")


if __name__ == "__main__":
    raise SystemExit(main())
