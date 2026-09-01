"""Fake upload receiver: deployment, state capture, credential sanitization."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from harness.command import (
    HarnessError,
    create_configmap,
    ensure_namespace,
    kubectl,
    port_forward,
    stop_process,
    write_json,
)

UPLOAD_NAMESPACE = "kubex"
UPLOAD_SERVICE = "stack-validation-webserver"
UPLOAD_CONFIGMAP = "stack-validation-webserver-script"
UPLOAD_MANIFEST = "manifests/stack-validation-webserver.yaml"
RECEIVER_PORT = 18080
UPLOAD_PORT = 8080

_SENSITIVE = {
    "authorization",
    "proxyauthorization",
    "apitoken",
    "token",
    "password",
    "secret",
    "credential",
}


def sanitize(value: object) -> object:
    """Drop credential-bearing fields before anything reaches an artifact."""

    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, child in value.items():
            normalized = str(key).replace("-", "").replace("_", "").lower()
            if normalized in _SENSITIVE or ("authorization" in normalized and str(key).lower() != "authorization"):
                continue
            result[str(key)] = sanitize(child)
        return result
    if isinstance(value, list):
        return [sanitize(child) for child in value]
    return value


def deploy(repo_root: Path) -> None:
    manifest = repo_root / UPLOAD_MANIFEST
    if not manifest.is_file():
        raise HarnessError(f"missing upload receiver manifest: {manifest}")
    ensure_namespace(UPLOAD_NAMESPACE, repo_root)
    create_configmap(
        UPLOAD_CONFIGMAP,
        UPLOAD_NAMESPACE,
        "fake_stack_webserver.py",
        repo_root / "scripts" / "fake_stack_webserver.py",
        repo_root,
    )
    kubectl(["apply", "-f", str(manifest)], repo_root)
    kubectl(
        ["rollout", "status", f"deployment/{UPLOAD_SERVICE}", "-n", UPLOAD_NAMESPACE, "--timeout=10m"],
        repo_root,
    )


def cluster_ip(repo_root: Path) -> str:
    return kubectl(
        ["get", "svc", UPLOAD_SERVICE, "-n", UPLOAD_NAMESPACE, "-o", "jsonpath={.spec.clusterIP}"], repo_root
    ).strip()


def _http_json(url: str, timeout: float = 10) -> object:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HarnessError(f"HTTP request failed for {url}: {exc}") from exc


def capture_state(repo_root: Path, artifacts: Path, timeout: float = 300) -> dict[str, Any]:
    """Read the receiver's captured uploads, retrying a dropped port-forward."""

    deadline = time.monotonic() + timeout
    last_error = "upload receiver is not ready"
    while time.monotonic() < deadline:
        process = None
        try:
            process = port_forward(
                repo_root,
                UPLOAD_NAMESPACE,
                UPLOAD_SERVICE,
                RECEIVER_PORT,
                UPLOAD_PORT,
                min(60, max(0.1, deadline - time.monotonic())),
            )
            payload = _http_json(
                f"http://127.0.0.1:{RECEIVER_PORT}/debug/state",
                timeout=min(30, max(0.1, deadline - time.monotonic())),
            )
            if not isinstance(payload, dict):
                raise HarnessError("upload receiver returned invalid state")
            sanitized = sanitize(payload)
            if not isinstance(sanitized, dict):
                raise HarnessError("sanitized upload state is invalid")
            write_json(artifacts / "server-state.json", sanitized)
            return sanitized
        except (OSError, urllib.error.URLError, HarnessError) as exc:
            last_error = str(exc)
        finally:
            stop_process(process)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(2, remaining))
    raise HarnessError(f"timed out capturing upload receiver state: {last_error}")


def archive_bytes(state: dict[str, Any]) -> bytes:
    """The most recent uploaded archive body, preferring one with members."""

    uploads = state.get("uploads", [])
    if not isinstance(uploads, list) or not uploads:
        raise HarnessError("no uploads captured by upload receiver")
    archive_uploads = [
        entry
        for entry in uploads
        if isinstance(entry, dict) and str(entry.get("path", "")).startswith("/upload")
    ]
    with_members = [
        entry
        for entry in archive_uploads
        if isinstance(entry.get("archive_members"), list) and entry["archive_members"]
    ]
    for entry in reversed(with_members or archive_uploads):
        if isinstance(entry, dict) and isinstance(entry.get("body_b64"), str):
            try:
                return base64.b64decode(entry["body_b64"], validate=True)
            except (ValueError, TypeError) as exc:
                raise HarnessError(f"upload body is not valid base64: {exc}") from exc
    raise HarnessError("uploads contain no archive body")


def archive_upload_count(state: dict[str, Any]) -> int:
    uploads = state.get("uploads", [])
    if not isinstance(uploads, list):
        return 0
    return sum(
        1
        for entry in uploads
        if isinstance(entry, dict) and str(entry.get("path", "")).startswith("/upload")
    )
