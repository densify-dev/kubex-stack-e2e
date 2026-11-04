#!/usr/bin/env python3
"""Validate and record the Prometheus metrics required by stack validation."""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


REQUIRED_METRICS = [
    "kubernetes_build_info",
    "kube_node_info",
    "kube_node_status_capacity",
    "node_cpu_seconds_total",
    "kube_pod_info",
    "kube_pod_container_resource_requests",
    "container_cpu_usage_seconds_total",
    "ephemeral_storage_node_available",
]


def query(base_url: str, expression: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise ValueError(payload.get("error", "Prometheus query failed"))
    return payload.get("data", {}).get("result", [])


def validate(base_url: str, metrics: list[str] = REQUIRED_METRICS) -> dict:
    results: dict[str, object] = {}
    for metric in metrics:
        try:
            series = query(base_url, metric)
            fingerprints = [json.dumps(item.get("metric", {}), sort_keys=True) for item in series]
            duplicate_series = len(fingerprints) - len(set(fingerprints))
            results[metric] = {
                "query": metric,
                "series_count": len(series),
                "has_data": bool(series),
                "duplicate_series": duplicate_series,
                "series": series,
                "error": None,
            }
        except Exception as exc:
            results[metric] = {
                "query": metric,
                "series_count": 0,
                "has_data": False,
                "duplicate_series": 0,
                "series": [],
                "error": str(exc),
            }
    missing = [metric for metric, result in results.items() if not result["has_data"]]
    duplicates = {metric: result["duplicate_series"] for metric, result in results.items() if result["duplicate_series"]}
    return {
        "valid": not missing and not duplicates,
        "metrics": results,
        "missing": missing,
        "duplicates": duplicates,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    last_result: dict | None = None
    while time.monotonic() < deadline:
        last_result = validate(args.prometheus_url)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(last_result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if last_result["valid"]:
            print("required Prometheus metrics are available")
            time.sleep(70)
            return 0
        print(f"waiting for Prometheus metrics: missing={last_result['missing']} duplicates={last_result['duplicates']}", flush=True)
        time.sleep(10)
    raise SystemExit(f"timed out waiting for required Prometheus metrics: {last_result}")


if __name__ == "__main__":
    raise SystemExit(main())
