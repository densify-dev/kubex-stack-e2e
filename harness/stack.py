"""Render and apply the Kubex Automation Stack chart.

``helm template`` rather than ``helm install`` so the rendered manifests can be
rewritten — host aliases pointing the collector at the fake receiver — before
they reach the cluster.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from harness.command import HarnessError, json_value, kubectl, run, scalar, write
from harness.receiver import UPLOAD_PORT
from harness.spec import ForwarderSpec, ScenarioSpec, StackSpec

UPLOAD_HOST = "fake.kubex.ai"
CHART = "kubex/kubex-automation-stack"
REPOSITORY = "https://densify-dev.github.io/helm-charts"
RELEASE_NAME = "kubex"
_FORWARDER = "container-optimization-data-forwarder"
_STABLE_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _helm_key_segment(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(".", "\\.")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace(",", "\\,")
    )


def _helm_string(value: str) -> str:
    """Escape a --set-string value: helm splits unescaped commas into further key=value pairs."""

    return value.replace("\\", "\\\\").replace(",", "\\,")


def flatten_values(value: dict[str, Any], prefix: str = "") -> list[tuple[str, object]]:
    flattened: list[tuple[str, object]] = []
    for key, child in value.items():
        name = f"{prefix}.{_helm_key_segment(key)}" if prefix else _helm_key_segment(key)
        if isinstance(child, dict):
            flattened.extend(flatten_values(child, name))
        else:
            flattened.append((name, child))
    return flattened


def resolve_chart_version(chart: str, repo_root: Path) -> str:
    """Highest stable semver published for ``chart``. Prereleases are ignored."""

    found = json.loads(
        run(["helm", "search", "repo", chart, "--versions", "-o", "json"], cwd=repo_root, merge_stderr=False)
    )
    stable = [e["version"] for e in found if e.get("name") == chart and _STABLE_SEMVER.match(e["version"])]
    if not stable:
        raise HarnessError(f"no stable version of {chart} found in the helm repository")
    return max(stable, key=lambda v: tuple(int(part) for part in v.split(".")))


def find_forwarder(manifest: str) -> tuple[str, str]:
    """(CronJob name, image) of the single CronJob running the data forwarder."""

    found = []
    for doc in yaml.safe_load_all(manifest):
        if not isinstance(doc, dict) or doc.get("kind") != "CronJob":
            continue
        pod = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        for container in pod.get("containers", []):
            if _FORWARDER in container.get("image", ""):
                found.append((doc["metadata"]["name"], container["image"]))
    if len(found) != 1:
        raise HarnessError(f"expected exactly one forwarder CronJob in the rendered chart, found {found}")
    return found[0]


def _assert_runs_image(manifest: str, cronjob: str, image: str) -> None:
    for doc in yaml.safe_load_all(manifest):
        if isinstance(doc, dict) and doc.get("kind") == "CronJob" and doc["metadata"]["name"] == cronjob:
            containers = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
            if any(c.get("image") == image and c.get("imagePullPolicy") == "Never" for c in containers):
                return
            raise HarnessError(f"CronJob {cronjob} does not run {image} with pull policy Never: {containers}")
    raise HarnessError(f"CronJob {cronjob} missing from the final render")


def preload_collector(image: str, cluster_name: str, repo_root: Path, versions: dict[str, Any]) -> str:
    """Pull ``image``, load it into Kind under a local tag, return the tag.

    The repo digest goes into ``versions`` as soon as it is known. ``kind load``
    does not handle digest references, hence the local tag. A single-platform
    archive is loaded because ``kind load docker-image`` fails on multi-platform
    images with Docker's containerd image store. The Job runs with pull policy
    Never so a missing preload fails instead of pulling.
    """

    run(["docker", "pull", image], cwd=repo_root, timeout=900)
    info = json.loads(run(["docker", "image", "inspect", image], cwd=repo_root, merge_stderr=False))[0]
    if not info["RepoDigests"]:
        raise HarnessError(f"docker image {image} has no repo digest")
    digest = versions["collector_image_digest"] = info["RepoDigests"][0]
    local = f"kubex-e2e/collector:{digest.split('sha256:')[-1][:12]}"
    run(["docker", "tag", image, local], cwd=repo_root)
    with tempfile.TemporaryDirectory() as directory:
        archive = str(Path(directory) / "collector.tar")
        platform = f"{info['Os']}/{info['Architecture']}"
        run(["docker", "save", "--platform", platform, "-o", archive, local], cwd=repo_root, timeout=600)
        run(["kind", "load", "image-archive", archive, "--name", cluster_name], cwd=repo_root, timeout=900)
    return local


def template_args(
    scenario_name: str,
    stack: StackSpec,
    forwarder: ForwarderSpec,
    chart_version: str | None = None,
    collector_image: str | None = None,
    chart_path: str | None = None,
) -> list[str]:
    """``helm template`` args. ``collector_image`` is the preloaded local tag, never pulled."""

    args = ["helm", "template", RELEASE_NAME, chart_path or CHART, "--namespace", stack.namespace]
    if chart_version:
        args.extend(["--version", chart_version])
    collection = forwarder.collection
    defaults: dict[str, object] = {
        "stack.densify.createSecret": False,
        "gpu-process-exporter.enabled": False,
        "node-labeler.enabled": False,
        "beyla.enabled": False,
        "kubex-connector.enabled": False,
        "kubex-ai-cdi.enabled": False,
        "kubex-automation-engine.enabled": False,
        "k8s-ephemeral-storage-metrics.enabled": False,
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
    overrides = dict(stack.helm_overrides)
    supplied = {key for key, _ in flatten_values(overrides)}
    # The collector under test is chosen only via --collector-image.
    reserved = {f"{_FORWARDER}.image", f"{_FORWARDER}.images.dataCollection.pullPolicy"}
    if supplied & reserved:
        raise HarnessError(f"helm_overrides must not set {sorted(supplied & reserved)}; use --collector-image")
    if collector_image:
        defaults[f"{_FORWARDER}.image"] = collector_image
        defaults[f"{_FORWARDER}.images.dataCollection.pullPolicy"] = "Never"
    values = [(key, value) for key, value in defaults.items() if key not in supplied]
    values.extend(flatten_values(overrides))
    for key, value in values:
        if isinstance(value, list):
            args.extend(["--set-json", f"{key}={json_value(value)}"])
        elif isinstance(value, str):
            args.extend(["--set-string", f"{key}={_helm_string(value)}"])
        else:
            args.extend(["--set", f"{key}={scalar(value)}"])
    return args


def install(
    scenario: ScenarioSpec,
    repo_root: Path,
    artifacts: Path,
    receiver_cluster_ip: str,
    versions: dict[str, Any],
    chart_version: str | None = None,
    collector_image: str | None = None,
    chart_path: str | None = None,
) -> str:
    """Resolve versions, preload the collector, render, inject host aliases, apply.

    Returns the forwarder CronJob name. ``versions`` is filled as each value
    becomes known, so the caller can record it even if a later step fails.
    """

    from scripts.inject_host_aliases import inject

    stack = scenario.stack
    if chart_path:
        chart_metadata = yaml.safe_load(Path(chart_path, "Chart.yaml").read_text(encoding="utf-8"))
        version = str(chart_metadata["version"])
    else:
        run(["helm", "repo", "add", CHART.split("/")[0], REPOSITORY, "--force-update"], cwd=repo_root)
        run(["helm", "repo", "update"], cwd=repo_root)
        version = chart_version or resolve_chart_version(CHART, repo_root)
    versions["chart_version"] = version

    def render(image: str | None = None) -> str:
        args = template_args(scenario.name, stack, scenario.forwarder, version if not chart_path else None, image, chart_path)
        return run(args, cwd=repo_root, timeout=900)

    # The default render finds the CronJob (its image still names the forwarder)
    # and the chart's default collector image. The final render swaps in the local tag.
    cronjob, default_image = find_forwarder(render())
    requested = collector_image or default_image
    versions["collector_image_requested"] = requested
    local = preload_collector(requested, scenario.cluster_name, repo_root, versions)

    rendered = render(local)
    _assert_runs_image(rendered, cronjob, local)
    manifest = artifacts / "kubex-stack-rendered.yaml"
    write(manifest, inject(rendered, UPLOAD_HOST, receiver_cluster_ip))
    kubectl(["apply", "-f", str(manifest)], repo_root)
    # Only the Job the harness creates may upload, even if the run straddles the CronJob's schedule.
    kubectl(["patch", "cronjob", cronjob, "-n", stack.namespace, "-p", '{"spec":{"suspend":true}}'], repo_root)
    return cronjob


def job_failures(job: str, namespace: str, repo_root: Path) -> int:
    """Failed pod attempts of the Job; empty status.failed means none."""

    raw = kubectl(["get", "job", job, "-n", namespace, "-o", "jsonpath={.status.failed}"], repo_root)
    return int(raw.strip() or 0)


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


def trigger_forwarder(scenario: ScenarioSpec, repo_root: Path, cronjob: str) -> str:
    """Create a Job from the forwarder CronJob and wait for it to complete."""

    namespace = scenario.stack.namespace
    job = f"{scenario.name}-forwarder"
    kubectl(["create", "job", job, f"--from=cronjob/{cronjob}", "-n", namespace], repo_root)
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        try:
            status = json.loads(kubectl(["get", "job", job, "-n", namespace, "-o", "json"], repo_root))
        except (HarnessError, json.JSONDecodeError):
            status = {}  # a transient API error: poll again
        done = {c["type"] for c in status.get("status", {}).get("conditions", []) if c["status"] == "True"}
        if "Complete" in done:
            return job
        if "Failed" in done:
            raise HarnessError(f"forwarder Job {job} failed")
        time.sleep(5)
    raise HarnessError(f"timed out waiting for forwarder Job {job}")
