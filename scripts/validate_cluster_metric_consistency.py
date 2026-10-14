#!/usr/bin/env python3
"""Compare Kubernetes object inventories with Prometheus identity metrics."""

from __future__ import annotations

import argparse
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
import re


def kubectl_json(resource: str) -> dict:
    return json.loads(subprocess.check_output(["kubectl", "get", resource, "-A", "-o", "json"], text=True))


def query(base_url: str, expression: str) -> list[dict]:
    url = f"{base_url.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=15) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise ValueError(payload.get("error", "Prometheus query failed"))
    return payload.get("data", {}).get("result", [])


def prometheus_label_name(key: str) -> str:
    return "label_" + re.sub(r"[^a-zA-Z0-9_]", "_", key)


def validate(base_url: str) -> dict:
    node_items = kubectl_json("nodes").get("items", [])
    nodes = {item["metadata"]["name"] for item in node_items}
    pods = {
        (item["metadata"].get("namespace", ""), item["metadata"]["name"])
        for item in kubectl_json("pods").get("items", [])
    }
    node_series = query(base_url, "kube_node_info")
    node_label_series = query(base_url, "kube_node_labels")
    pod_series = query(base_url, "kube_pod_info")
    prometheus_nodes = {item.get("metric", {}).get("node") for item in node_series} - {None, ""}
    prometheus_pods = {
        (item.get("metric", {}).get("namespace", ""), item.get("metric", {}).get("pod", ""))
        for item in pod_series
        if item.get("metric", {}).get("pod")
    }
    prometheus_labels = {
        item.get("metric", {}).get("node"): item.get("metric", {})
        for item in node_label_series
        if item.get("metric", {}).get("node")
    }
    label_mismatches = {}
    for item in node_items:
        node = item["metadata"]["name"]
        expected = item["metadata"].get("labels", {})
        actual = prometheus_labels.get(node, {})
        missing = {
            key: value for key, value in expected.items()
            if actual.get(prometheus_label_name(key)) != value
        }
        if missing or node not in prometheus_labels:
            label_mismatches[node] = {
                "kubernetes": expected,
                "prometheus": {
                    key.removeprefix("label_"): value
                    for key, value in actual.items()
                    if key.startswith("label_")
                },
                "missing_or_mismatched": missing,
            }
    checks = {
        "nodes": {
            "kubernetes_count": len(nodes),
            "prometheus_count": len(prometheus_nodes),
            "kubernetes_only": sorted(nodes - prometheus_nodes),
            "prometheus_only": sorted(prometheus_nodes - nodes),
        },
        "pods": {
            "kubernetes_count": len(pods),
            "prometheus_count": len(prometheus_pods),
            "kubernetes_only": sorted([list(item) for item in pods - prometheus_pods]),
            "prometheus_only": sorted([list(item) for item in prometheus_pods - pods]),
        },
        "node_labels": {
            "kubernetes_count": len(nodes),
            "prometheus_count": len(prometheus_labels),
            "mismatches": label_mismatches,
        },
    }
    for check in checks.values():
        check["valid"] = (
            not check.get("kubernetes_only")
            and not check.get("prometheus_only")
            and not check.get("mismatches")
        )
    return {"valid": all(check["valid"] for check in checks.values()), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.prometheus_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not result["valid"]:
        raise SystemExit("Kubernetes and Prometheus identity metrics disagree")
    print("Kubernetes and Prometheus identity metrics agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
