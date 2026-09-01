"""Subprocess, kubectl, and file helpers shared by the rest of the harness.

Thin wrappers over subprocess and kubectl. No scenario knowledge lives here.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any


class HarnessError(RuntimeError):
    """Raised for infrastructure failures during a run."""


def json_value(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json_value(value)
    return str(value)


def run(
    args: list[str],
    *,
    cwd: Path,
    input_text: str | None = None,
    timeout: float | None = None,
    merge_stderr: bool = True,
) -> str:
    """Run a command, returning stdout. Set merge_stderr=False when stdout is parsed."""

    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"failed to execute {args[0]}: {exc}") from exc
    if completed.returncode != 0:
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part)
        raise HarnessError(f"command failed ({completed.returncode}): {args[0]}\n{output.strip()[-4000:]}")
    return completed.stdout


def kubectl(args: list[str], repo_root: Path) -> str:
    return run(["kubectl", *args], cwd=repo_root)


def kubectl_json(args: list[str], repo_root: Path) -> Any:
    raw = kubectl(args, repo_root)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"kubectl returned invalid JSON for {' '.join(args)}: {exc}") from exc


def apply_yaml(document: str, repo_root: Path) -> None:
    run(["kubectl", "apply", "-f", "-"], cwd=repo_root, input_text=document)


def ensure_namespace(name: str, repo_root: Path) -> None:
    rendered = run(
        ["kubectl", "create", "namespace", name, "--dry-run=client", "-o", "yaml"], cwd=repo_root
    )
    apply_yaml(rendered, repo_root)


def create_configmap(name: str, namespace: str, key: str, source: Path, repo_root: Path) -> None:
    rendered = run(
        [
            "kubectl",
            "create",
            "configmap",
            name,
            "-n",
            namespace,
            f"--from-file={key}={source}",
            "--dry-run=client",
            "-o",
            "yaml",
        ],
        cwd=repo_root,
    )
    apply_yaml(rendered, repo_root)


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: object) -> None:
    write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def port_forward(
    repo_root: Path,
    namespace: str,
    service: str,
    local_port: int,
    remote_port: int,
    startup_timeout: float = 60,
) -> subprocess.Popen[str]:
    deadline = time.monotonic() + startup_timeout
    last_error = "service is not ready"
    while time.monotonic() < deadline:
        process = subprocess.Popen(
            ["kubectl", "port-forward", "-n", namespace, f"svc/{service}", f"{local_port}:{remote_port}"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(1)
        if process.poll() is None:
            return process
        last_error = process.stderr.read().strip() if process.stderr else "port-forward exited"
        time.sleep(2)
    raise HarnessError(f"port-forward for {service} failed: {last_error[-1000:]}")


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    except OSError:
        pass
