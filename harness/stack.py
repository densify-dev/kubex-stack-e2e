"""Render and apply the Kubex Automation Stack chart.

``helm template`` rather than ``helm install`` so the rendered manifests can be
rewritten — host aliases pointing the collector at the fake receiver — before
they reach the cluster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.command import HarnessError, apply_yaml, json_value, kubectl, run, scalar, write
from harness.spec import ForwarderSpec, ScenarioSpec, StackSpec

UPLOAD_HOST = "fake.kubex.ai"
UPLOAD_PORT = 8080


def _helm_key_segment(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(".", "\\.")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace(",", "\\,")
    )


def flatten_values(value: dict[str, Any], prefix: str = "") -> list[tuple[str, object]]:
    flattened: list[tuple[str, object]] = []
    for key, child in value.items():
        name = f"{prefix}.{_helm_key_segment(key)}" if prefix else _helm_key_segment(key)
        if isinstance(child, dict):
            flattened.extend(flatten_values(child, name))
        else:
            flattened.append((name, child))
    return flattened


def template_args(
    scenario_name: str,
    stack: StackSpec,
    forwarder: ForwarderSpec,
    chart_version: str | None = None,
) -> tuple[str, str, list[str]]:
    version = chart_version or stack.version
    args = ["helm", "template", stack.release_name, stack.chart, "--namespace", stack.namespace]
    if version:
        args.extend(["--version", version])
    chart_alias = (
        stack.chart.split("/", 1)[0]
        if "/" in stack.chart and not stack.chart.startswith(("oci://", "http://", "https://"))
        else "kubex"
    )
    collection = forwarder.collection
    defaults: dict[str, object] = {
        "stack.densify.createSecret": False,
        "gpu-process-exporter.enabled": False,
        "node-labeler.enabled": False,
        "beyla.enabled": False,
        "container-optimization-data-forwarder.config.forwarder.densify.url.scheme": "http",
        "container-optimization-data-forwarder.config.forwarder.densify.url.host": UPLOAD_HOST,
        "container-optimization-data-forwarder.config.forwarder.densify.url.port": UPLOAD_PORT,
        "container-optimization-data-forwarder.config.forwarder.densify.url.UserSecretName": None,
        "container-optimization-data-forwarder.config.forwarder.densify.url.username": "stack-validation",
        "container-optimization-data-forwarder.config.forwarder.densify.url.password": "stack-validation",
        "container-optimization-data-forwarder.config.forwarder.densify.endpoint": "/upload",
        "container-optimization-data-forwarder.config.collection.interval": collection.interval,
        "container-optimization-data-forwarder.config.collection.interval_size": collection.interval_size,
        "container-optimization-data-forwarder.config.collection.history": collection.history,
        "container-optimization-data-forwarder.config.collection.sample_rate": collection.sample_rate,
        "container-optimization-data-forwarder.config.clusters[0].name": scenario_name,
        "container-optimization-data-forwarder.job.enable": False,
        "container-optimization-data-forwarder.cronJob.schedule": "0 0 1 1 *",
    }
    if stack.collector_digest:
        digest = (
            stack.collector_digest
            if stack.collector_digest.startswith("sha256:")
            else f"sha256:{stack.collector_digest}"
        )
        defaults["container-optimization-data-forwarder.image"] = (
            f"densify/container-optimization-data-forwarder@{digest}"
        )
    elif stack.collector_version:
        defaults["container-optimization-data-forwarder.images.dataCollection.tag"] = stack.collector_version

    overrides = dict(stack.helm_overrides)
    supplied = {key for key, _ in flatten_values(overrides)}
    values = [(key, value) for key, value in defaults.items() if key not in supplied]
    values.extend(flatten_values(overrides))
    for key, value in values:
        if isinstance(value, list):
            args.extend(["--set-json", f"{key}={json_value(value)}"])
        else:
            args.extend(["--set-string" if isinstance(value, str) else "--set", f"{key}={scalar(value)}"])
    return stack.repository, chart_alias, args


def install(
    scenario: ScenarioSpec,
    repo_root: Path,
    artifacts: Path,
    receiver_cluster_ip: str,
    chart_version: str | None = None,
) -> Path:
    """Render the chart, inject host aliases, apply it, and keep the manifest."""

    from scripts.inject_host_aliases import inject

    repository, chart_alias, args = template_args(
        scenario.name, scenario.stack, scenario.forwarder, chart_version
    )
    run(["helm", "repo", "add", chart_alias, repository, "--force-update"], cwd=repo_root)
    run(["helm", "repo", "update"], cwd=repo_root)
    rendered = run(args, cwd=repo_root, timeout=900)
    manifest = artifacts / "kubex-stack-rendered.yaml"
    write(manifest, inject(rendered, UPLOAD_HOST, receiver_cluster_ip))
    kubectl(["apply", "-f", str(manifest)], repo_root)
    return manifest


def resource_paths(scenario: ScenarioSpec, scenario_dir: Path) -> list[Path]:
    """Resolve scenario manifests and fail before provisioning on path mistakes."""

    root = scenario_dir.resolve()
    paths: list[Path] = []
    for resource in scenario.resources:
        relative = Path(resource)
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root):
            raise HarnessError(f"scenario resource path is outside its directory: {resource!r}")
        if not path.is_file():
            raise HarnessError(f"scenario resource file does not exist: {resource!r}")
        paths.append(path)
    return paths


def apply_resources(paths: list[Path], repo_root: Path) -> None:
    for path in paths:
        kubectl(["apply", "-f", str(path)], repo_root)


def await_lifecycle(scenario: ScenarioSpec, repo_root: Path) -> None:
    for step in scenario.lifecycle:
        args = ["wait", f"--for=condition={step.condition}", f"{step.kind}/{step.name}"]
        if step.namespace:
            args.extend(["-n", step.namespace])
        args.append(f"--timeout={int(step.timeout)}s")
        kubectl(args, repo_root)


def trigger_forwarder(scenario: ScenarioSpec, repo_root: Path) -> str:
    """Create a Job from the stack CronJob and wait for it to succeed."""

    import json
    import time

    forwarder = scenario.forwarder
    namespace = scenario.stack.namespace
    release = scenario.stack.release_name
    cronjob = forwarder.cronjob_name or f"{release}-kubex-stack"
    job = forwarder.job_name or f"{release}-{scenario.name}-forwarder"
    if len(job) > 63:
        job = job[:63].rstrip("-")
    kubectl(["create", "job", job, f"--from=cronjob/{cronjob}", "-n", namespace], repo_root)
    deadline = time.monotonic() + forwarder.wait_timeout
    while time.monotonic() < deadline:
        try:
            status = json.loads(kubectl(["get", "job", job, "-n", namespace, "-o", "json"], repo_root))
            state = status.get("status", {})
            if state.get("succeeded", 0) > 0:
                return job
            if state.get("failed", 0) >= 5:
                raise HarnessError(f"forwarder Job {job} failed")
        except (json.JSONDecodeError, HarnessError) as exc:
            if isinstance(exc, HarnessError) and "command failed" not in str(exc):
                raise
        time.sleep(forwarder.poll_interval)
    raise HarnessError(f"timed out waiting for forwarder Job {job}")
