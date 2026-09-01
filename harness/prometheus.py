"""Prometheus readiness gating and evidence capture.

Readiness is query-based, not pod-based: the stack is only ready when the series
a scenario depends on actually return results.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from harness.command import HarnessError, port_forward, stop_process, write_json
from harness.spec import PrometheusSpec, PrometheusTarget

PROMETHEUS_PORT = 19090
BASE = f"http://127.0.0.1:{PROMETHEUS_PORT}"


def _http_json(url: str, timeout: float = 10) -> Any:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HarnessError(f"HTTP request failed for {url}: {exc}") from exc


def validate_targets(payload: object, expected: list[PrometheusTarget]) -> None:
    if not expected:
        return
    if not isinstance(payload, dict):
        raise HarnessError("Prometheus targets response is not an object")
    targets = payload.get("data", {}).get("activeTargets", [])
    if not isinstance(targets, list):
        raise HarnessError("Prometheus targets response has no activeTargets list")
    for declaration in expected:
        matched = []
        for target in targets:
            labels = target.get("labels", {})
            discovered = target.get("discoveredLabels", {})
            if declaration.service is not None:
                identity_matches = declaration.service in {
                    labels.get("service"),
                    discovered.get("__meta_kubernetes_service_name"),
                }
            else:
                identity_matches = declaration.name == target.get("scrapePool")
            combined = {**discovered, **labels}
            labels_match = all(str(combined.get(k)) == str(v) for k, v in declaration.labels.items())
            if identity_matches and labels_match:
                matched.append(target)
        if not matched:
            if declaration.required:
                raise HarnessError(f"Prometheus target {declaration.name!r} not found")
            continue
        if declaration.state != "any" and not any(t.get("health") == declaration.state for t in matched):
            raise HarnessError(f"Prometheus target {declaration.name!r} is not {declaration.state}")


def await_ready(spec: PrometheusSpec, namespace: str, repo_root: Path, artifacts: Path) -> None:
    """Block until targets are up and every declared query returns enough results."""

    deadline = time.monotonic() + spec.readiness_timeout
    last_error = "not ready"
    process = None
    try:
        while time.monotonic() < deadline:
            if process is None or process.poll() is not None:
                stop_process(process)
                process = None
                try:
                    process = port_forward(
                        repo_root,
                        namespace,
                        spec.service,
                        PROMETHEUS_PORT,
                        spec.port,
                        min(60, max(1, deadline - time.monotonic())),
                    )
                except (OSError, HarnessError) as exc:
                    last_error = str(exc)
                    time.sleep(min(5, max(0, deadline - time.monotonic())))
                    continue
            try:
                with urllib.request.urlopen(BASE + "/-/ready", timeout=10) as response:
                    if response.status >= 400:
                        raise OSError(f"status {response.status}")
                targets = _http_json(BASE + "/api/v1/targets")
                queries: dict[str, object] = {}
                missing: list[str] = []
                for query in spec.queries:
                    result = _http_json(
                        BASE + "/api/v1/query?" + urllib.parse.urlencode({"query": query.query})
                    )
                    queries[query.name] = result
                    data = result.get("data", {}) if isinstance(result, dict) else {}
                    if len(data.get("result", [])) < query.min_results:
                        missing.append(query.name)
                write_json(artifacts / "prometheus-targets.json", targets)
                write_json(artifacts / "prometheus-queries.json", queries)
                if missing:
                    last_error = "queries without enough results: " + ", ".join(missing)
                else:
                    validate_targets(targets, list(spec.targets))
                    return
            except HarnessError as exc:
                last_error = str(exc)
                stop_process(process)
                process = None
            except (OSError, urllib.error.URLError, json.JSONDecodeError, KeyError, TypeError) as exc:
                last_error = str(exc)
                stop_process(process)
                process = None
            time.sleep(min(5, max(0, deadline - time.monotonic())))
        raise HarnessError(f"timed out waiting for Prometheus: {last_error}")
    finally:
        stop_process(process)
