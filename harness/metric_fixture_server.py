#!/usr/bin/env python3
"""Small Prometheus exposition server for deterministic metric fixtures.

This file is mounted into the fixture pod on its own, alongside a JSON fixture
the harness generated from typed ``Metric`` objects. It must therefore import
nothing outside the standard library and must never need PyYAML.

The harness has already validated the fixture through the dataclasses, but the
checks here stay: a malformed fixture should fail loudly at pod start rather
than silently serve nothing eight minutes into a run.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class FixtureError(ValueError):
    """Raised for malformed metric fixture input."""


def _load_document(path: Path) -> object:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FixtureError(f"{path}: cannot read fixture: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FixtureError(f"{path}: fixture must be JSON: {exc}") from exc


def _mapping(value: object, where: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FixtureError(f"{where}: expected mapping")
    if any(not isinstance(key, str) for key in value):
        raise FixtureError(f"{where}: field names must be strings")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise FixtureError(f"{where}: unknown field(s): {', '.join(unknown)}")
    return value


def _string(value: object, where: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        suffix = " non-empty" if nonempty else ""
        raise FixtureError(f"{where}: expected{suffix} string")
    return value


def _number(value: object, where: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixtureError(f"{where}: expected number")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise FixtureError(f"{where}: expected finite{' non-negative' if nonnegative else ''} number")
    return result


def validate_fixture_document(document: object, source: str = "fixture") -> list[dict[str, Any]]:
    """Validate a fixture document and return normalized metric definitions."""

    root = _mapping(document, source, {"metrics"})
    metrics = root.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise FixtureError(f"{source}.metrics: expected non-empty list")

    normalized: list[dict[str, Any]] = []
    definitions: dict[str, tuple[str, str]] = {}
    samples: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    for index, item in enumerate(metrics):
        where = f"{source}.metrics[{index}]"
        metric = _mapping(item, where, {"name", "help", "type", "labels", "value", "rate"})
        name = _string(metric.get("name"), f"{where}.name")
        if not METRIC_NAME.fullmatch(name):
            raise FixtureError(f"{where}.name: invalid Prometheus metric name {name!r}")
        help_text = _string(metric.get("help", name), f"{where}.help", nonempty=False)
        metric_type = _string(metric.get("type"), f"{where}.type")
        if metric_type not in {"gauge", "counter"}:
            raise FixtureError(f"{where}.type: expected 'gauge' or 'counter'")
        if name in definitions and definitions[name] != (metric_type, help_text):
            raise FixtureError(f"{where}: metric {name!r} changes type or help text")
        definitions[name] = (metric_type, help_text)

        labels = metric.get("labels", {})
        if not isinstance(labels, dict) or any(not isinstance(key, str) for key in labels):
            raise FixtureError(f"{where}.labels: expected string-keyed mapping")
        normalized_labels: dict[str, str] = {}
        for label, label_value in labels.items():
            if not LABEL_NAME.fullmatch(label) or label == "__name__":
                raise FixtureError(f"{where}.labels: invalid label name {label!r}")
            if not isinstance(label_value, (str, int, float, bool)):
                raise FixtureError(f"{where}.labels.{label}: expected scalar")
            normalized_labels[label] = str(label_value).lower() if isinstance(label_value, bool) else str(label_value)
        identity = (name, tuple(sorted(normalized_labels.items())))
        if identity in samples:
            raise FixtureError(f"{where}: duplicate metric sample {name!r} with same labels")
        samples.add(identity)

        if "value" not in metric:
            raise FixtureError(f"{where}: requires a value")
        initial = _number(metric.get("value"), f"{where}.value")
        rate = _number(metric.get("rate", 0), f"{where}.rate", nonnegative=True)
        if metric_type == "gauge" and "rate" in metric:
            raise FixtureError(f"{where}.rate: gauges cannot define rate")
        if metric_type == "counter" and "rate" not in metric:
            raise FixtureError(f"{where}.rate: counters require a rate")
        normalized_metric: dict[str, Any] = {
            "name": name,
            "help": help_text,
            "type": metric_type,
            "labels": normalized_labels,
            "value": initial,
        }
        if metric_type == "counter":
            normalized_metric["rate"] = rate
        normalized.append(normalized_metric)
    return normalized


def load_fixture(path: Path) -> list[dict[str, Any]]:
    return validate_fixture_document(_load_document(path), str(path))


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_metrics(metrics: list[dict[str, Any]], elapsed: float) -> str:
    """Render valid Prometheus text for normalized metrics."""

    lines: list[str] = []
    seen_samples: set[tuple[str, tuple[tuple[str, str], ...]]] = set()
    emitted_definitions: set[str] = set()
    for metric in metrics:
        name = metric["name"]
        labels = metric["labels"]
        label_items = tuple(sorted(labels.items()))
        identity = (name, label_items)
        if identity in seen_samples:
            raise FixtureError(f"duplicate metric sample {name}{labels}")
        seen_samples.add(identity)
        if name not in emitted_definitions:
            lines.append(f"# HELP {name} {_escape_help(metric['help'])}")
            lines.append(f"# TYPE {name} {metric['type']}")
            emitted_definitions.add(name)
        label_text = ""
        if labels:
            label_text = "{" + ",".join(f'{key}="{_escape_label(value)}"' for key, value in label_items) + "}"
        value = metric["value"] + (metric["rate"] * max(elapsed, 0.0) if metric["type"] == "counter" else 0)
        lines.append(f"{name}{label_text} {value:.12g}")
    return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    server_version = "kubex-metric-fixture/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    @property
    def fixture_server(self) -> "FixtureServer":
        return self.server.fixture_server  # type: ignore[attr-defined]

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        elif path == "/metrics":
            with self.fixture_server.lock:
                elapsed = time.monotonic() - self.fixture_server.started_at
                body = render_metrics(self.fixture_server.metrics, elapsed).encode("utf-8")
            self._send(HTTPStatus.OK, body, "text/plain; version=0.0.4; charset=utf-8")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")


class FixtureServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], metrics: list[dict[str, Any]]) -> None:
        super().__init__(address, _Handler)
        self.metrics = metrics
        # Phase 3 will arm this after Prometheus reports the target ready, so a
        # scheduled step lands inside the collection window rather than at pod
        # start. Until then counters ramp from process start, as before.
        self.started_at = time.monotonic()
        self.lock = Lock()
        self.fixture_server = self


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()
    try:
        metrics = load_fixture(args.fixture)
    except FixtureError as exc:
        parser.error(str(exc))
    server = FixtureServer((args.host, args.port), metrics)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
