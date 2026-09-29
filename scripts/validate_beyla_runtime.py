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
RUNTIME_ALIASES = {"go": {"go"}, "java": {"java", "jvm"}, "nodejs": {"node", "nodejs", "javascript"}, "python": {"python", "cpython"}, "dotnet": {"dotnet", ".net", "clr"}}


def query(base_url: str, expression: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.load(response)
    return payload.get("data", {}).get("result", [])


def validate(
    base_url: str,
    namespace: str = "stack-validation-runtime",
    beyla_namespace: str = "kubex",
) -> dict:
    targets = query(base_url, f'up{{namespace="{beyla_namespace}",service=~".*beyla.*"}}')
    healthy_targets = [item for item in targets if str(item.get("value", [None, "0"])[1]) == "1"]
    if not healthy_targets:
        raise ValueError("Prometheus has no healthy kubex-beyla target")
    results = query(base_url, "survey_info")
    if not results:
        raise ValueError("Prometheus returned no survey_info series")
    found: dict[str, list[dict]] = {runtime: [] for runtime in EXPECTED}
    for result in results:
        labels = result.get("metric", {})
        runtime_value = next((labels.get(key, "").lower() for key in ("runtime", "process_runtime", "language") if labels.get(key)), "")
        if not runtime_value:
            continue
        if labels.get("namespace") != namespace and labels.get("k8s_namespace") != namespace:
            continue
        for runtime, aliases in RUNTIME_ALIASES.items():
            if runtime_value in aliases:
                found[runtime].append(labels)
    missing = sorted(runtime for runtime, entries in found.items() if not entries)
    return {"metric": "survey_info", "namespace": namespace, "expected": sorted(EXPECTED), "detected": {key: value for key, value in found.items()}, "missing": missing, "series_count": len(results), "healthy_beyla_targets": len(healthy_targets)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--namespace", default="stack-validation-runtime")
    parser.add_argument("--beyla-namespace", default="kubex")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    deadline = time.monotonic() + args.timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            result = validate(args.prometheus_url, args.namespace, args.beyla_namespace)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            if not result["missing"]:
                print("all Beyla runtime fixtures detected")
                return 0
            last_error = f"missing runtimes: {', '.join(result['missing'])}"
        except Exception as exc:
            last_error = str(exc)
        print(f"waiting for Beyla runtime detection: {last_error}", flush=True)
        time.sleep(10)
    raise SystemExit(f"timed out waiting for Beyla runtime detection: {last_error}")


if __name__ == "__main__":
    raise SystemExit(main())
