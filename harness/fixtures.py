"""Deploy metric fixture servers.

Fixture series are typed ``Metric`` objects. The harness normalizes them to JSON
and mounts that JSON next to the fixture server script, so the pod never parses
YAML and needs nothing but the standard library.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from harness.command import apply_yaml, create_configmap, ensure_namespace, kubectl, write_json
from harness.spec import FixtureService, ScenarioSpec

# Multi-architecture index digest for python:3.11-slim, pinned so fixture pods
# are reproducible across runs and runner architectures.
FIXTURE_IMAGE = "python@sha256:1042b61448fef4ba92d16a8c7eb4996d027568ce64792a7877fd88511e0af7c6"
SERVER_CONFIGMAP = "stack-e2e-metric-fixture-server"


def _deployment(service: FixtureService, namespace: str) -> str:
    port = service.container_port
    return yaml.safe_dump(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": f"{service.name}-fixture", "namespace": namespace},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app.kubex.ai/fixture": service.name}},
                "template": {
                    "metadata": {"labels": {"app.kubex.ai/fixture": service.name}},
                    "spec": {
                        "containers": [
                            {
                                "name": "metrics",
                                "image": FIXTURE_IMAGE,
                                "imagePullPolicy": "IfNotPresent",
                                "command": [
                                    "python",
                                    "/fixture/metric_fixture_server.py",
                                    "--fixture",
                                    "/fixture/fixture.yaml",
                                    "--port",
                                    str(port),
                                ],
                                "ports": [{"name": "metrics", "containerPort": port}],
                                "volumeMounts": [
                                    {
                                        "name": "fixture",
                                        "mountPath": "/fixture/fixture.yaml",
                                        "subPath": "fixture.yaml",
                                        "readOnly": True,
                                    },
                                    {
                                        "name": "server",
                                        "mountPath": "/fixture/metric_fixture_server.py",
                                        "subPath": "metric_fixture_server.py",
                                        "readOnly": True,
                                    },
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "fixture", "configMap": {"name": f"{service.name}-metrics"}},
                            {"name": "server", "configMap": {"name": SERVER_CONFIGMAP}},
                        ],
                    },
                },
            },
        },
        sort_keys=False,
    )


def _service(service: FixtureService, namespace: str) -> str:
    metadata: dict[str, object] = {"name": service.name, "namespace": namespace}
    if service.labels:
        metadata["labels"] = dict(service.labels)
    if service.annotations:
        metadata["annotations"] = dict(service.annotations)
    return yaml.safe_dump(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata,
            "spec": {
                # Selectorful, so Kubernetes produces Endpoints and Prometheus's
                # kubernetes-service-endpoints pool can discover the target.
                "selector": {"app.kubex.ai/fixture": service.name},
                "ports": [
                    {"name": "metrics", "port": service.port, "targetPort": service.container_port}
                ],
            },
        },
        sort_keys=False,
    )


def deploy(scenario: ScenarioSpec, repo_root: Path, artifacts: Path) -> None:
    script = repo_root / "harness" / "metric_fixture_server.py"
    stack_namespace = scenario.stack.namespace
    ensure_namespace(stack_namespace, repo_root)
    create_configmap(SERVER_CONFIGMAP, stack_namespace, "metric_fixture_server.py", script, repo_root)

    for service in scenario.fixtures:
        namespace = service.namespace or stack_namespace
        payload = {"metrics": [metric.normalized() for metric in service.metrics]}
        fixture_json = artifacts / "fixture-inputs" / f"{service.name}.json"
        write_json(fixture_json, payload)

        ensure_namespace(namespace, repo_root)
        if namespace != stack_namespace:
            create_configmap(SERVER_CONFIGMAP, namespace, "metric_fixture_server.py", script, repo_root)
        create_configmap(f"{service.name}-metrics", namespace, "fixture.yaml", fixture_json, repo_root)

        apply_yaml(_deployment(service, namespace), repo_root)
        apply_yaml(_service(service, namespace), repo_root)
        kubectl(
            ["rollout", "status", f"deployment/{service.name}-fixture", "-n", namespace, "--timeout=10m"],
            repo_root,
        )
        kubectl(
            [
                "wait",
                "--for=condition=Ready",
                "pod",
                "-l",
                f"app.kubex.ai/fixture={service.name}",
                "-n",
                namespace,
                "--timeout=5m",
            ],
            repo_root,
        )
