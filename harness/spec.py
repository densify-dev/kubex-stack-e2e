"""Typed scenario setup objects.

These replace the YAML contract. Validation happens in ``__post_init__`` at
construction time, which runs at scenario-module import — before any pytest
fixture provisions a cluster. That preserves the old "validate before starting
Kind" property without a separate validation pass.

Scenario modules are trusted internal test code, so these checks target ordinary
mistakes (an empty node group, a bad interval name), not hostile input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence


CLUSTER_NAME_PREFIX = "kubex-e2e-"
INTERVAL_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600}

_LABEL_NAME = re.compile(r"^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
_LABEL_PREFIX = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$|^$")
_KUBERNETES_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_WHOLE_NUMBER = re.compile(r"^[0-9]+$")


class SpecError(ValueError):
    """Raised when a scenario's setup objects are malformed."""


def _check_labels(labels: Mapping[str, str], where: str) -> None:
    for key, value in labels.items():
        parts = str(key).split("/")
        if len(parts) > 2 or not _LABEL_NAME.fullmatch(parts[-1]):
            raise SpecError(f"{where}: invalid label name {key!r}")
        if len(parts) == 2 and not _LABEL_PREFIX.fullmatch(parts[0]):
            raise SpecError(f"{where}: invalid label prefix {parts[0]!r}")
        if not _LABEL_VALUE.fullmatch(str(value)):
            raise SpecError(f"{where}: invalid label value {value!r} for {key!r}")


def _check_extended_resources(resources: Mapping[str, str], where: str) -> None:
    for name, value in resources.items():
        prefix, slash, base = str(name).partition("/")
        if not slash or not _LABEL_PREFIX.fullmatch(prefix) or not _LABEL_NAME.fullmatch(base):
            raise SpecError(f"{where}: invalid extended resource name {name!r}")
        if not _WHOLE_NUMBER.fullmatch(str(value)):
            raise SpecError(f"{where}: extended resource {name!r} must be a whole number, got {value!r}")


def _check_kubernetes_name(value: str, where: str) -> None:
    if not _KUBERNETES_NAME.fullmatch(value):
        raise SpecError(f"{where}: invalid Kubernetes name {value!r}")


@dataclass(frozen=True)
class Taint:
    key: str
    effect: str
    value: str = ""

    def __post_init__(self) -> None:
        if self.effect not in {"NoSchedule", "PreferNoSchedule", "NoExecute"}:
            raise SpecError(f"taint {self.key!r}: invalid effect {self.effect!r}")

    def assignment(self) -> str:
        return f"{self.key}={self.value}:{self.effect}" if self.value else f"{self.key}:{self.effect}"


@dataclass(frozen=True)
class Node:
    labels: Mapping[str, str] = field(default_factory=dict)
    roles: Sequence[str] = ()
    taints: Sequence[Taint] = ()
    kubelet: Mapping[str, str] = field(default_factory=dict)
    # Extended resources patched into status.capacity, e.g. {"nvidia.com/gpu": "4"}; the kubelet mirrors them into allocatable.
    extended_resources: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_labels(self.labels, "node.labels")
        _check_extended_resources(self.extended_resources, "node.extended_resources")


@dataclass(frozen=True)
class ClusterSpec:
    control_planes: Sequence[Node] = (Node(),)
    workers: Sequence[Node] = ()
    # KubeletConfiguration fields applied to every node, e.g. {"crashLoopBackOff": {"maxContainerRestartPeriod": "1s"}}
    kubelet_configuration: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workers:
            raise SpecError("ClusterSpec: requires at least one worker")
        if not self.control_planes:
            raise SpecError("ClusterSpec: requires at least one control plane")

    def nodes(self) -> list[tuple[str, Node]]:
        """Nodes paired with their role, control planes first, in kind order."""

        return [("control-plane", n) for n in self.control_planes] + [("worker", n) for n in self.workers]

    def node_names(self, cluster_name: str) -> list[str]:
        """Node names kind will assign, matching ``nodes()`` order."""

        names: list[str] = []
        seen = {"control-plane": 0, "worker": 0}
        for role, _ in self.nodes():
            seen[role] += 1
            index = seen[role]
            suffix = role if index == 1 else f"{role}{index}"
            names.append(f"{cluster_name}-{suffix}")
        return names


@dataclass(frozen=True)
class Collection:
    interval: Literal["seconds", "minutes", "hours"] = "minutes"
    interval_size: int = 1
    history: int = 1
    sample_rate: int = 1

    def __post_init__(self) -> None:
        if self.interval not in INTERVAL_SECONDS:
            raise SpecError(f"Collection: invalid interval {self.interval!r}")
        for name in ("interval_size", "history", "sample_rate"):
            if getattr(self, name) < 1:
                raise SpecError(f"Collection.{name}: must be at least 1")


@dataclass(frozen=True)
class StackSpec:
    namespace: str = "kubex"
    helm_overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Metric:
    """One fixture series. Replaces a ``metrics:`` entry in fixture.yaml."""

    name: str
    type: Literal["gauge", "counter"]
    value: float = 0.0
    labels: Mapping[str, str] = field(default_factory=dict)
    rate: float | None = None

    def __post_init__(self) -> None:
        if self.type not in {"gauge", "counter"}:
            raise SpecError(f"Metric {self.name!r}: invalid type {self.type!r}")
        if self.type == "gauge" and self.rate is not None:
            raise SpecError(f"Metric {self.name!r}: gauges cannot define a rate")
        if self.type == "counter" and self.rate is None:
            raise SpecError(f"Metric {self.name!r}: counters require a rate")

    @staticmethod
    def _validate(metrics: Sequence["Metric"]) -> None:
        # Reuse the pod server's validation so typed input and served input cannot
        # drift into two subtly different fixture contracts.
        from harness.metric_fixture_server import FixtureError, validate_fixture_document

        try:
            validate_fixture_document({"metrics": [metric.normalized() for metric in metrics]})
        except (FixtureError, TypeError, ValueError) as exc:
            raise SpecError(f"invalid metric fixture: {exc}") from exc

    def normalized(self) -> dict[str, Any]:
        """The shape ``metric_fixture_server`` expects."""

        payload: dict[str, Any] = {
            "name": self.name,
            "help": self.name,
            "type": self.type,
            "labels": {str(k): str(v) for k, v in self.labels.items()},
            "value": float(self.value),
        }
        if self.type == "counter":
            payload["rate"] = float(self.rate or 0.0)
        return payload


@dataclass(frozen=True)
class FixtureService:
    name: str
    metrics: Sequence[Metric]
    namespace: str | None = None
    port: int = 9100
    labels: Mapping[str, str] = field(default_factory=dict)
    # Node name the fixture pod is pinned to (kubernetes.io/hostname), for a fixture that stands in for one node's exporter.
    node: str | None = None

    def __post_init__(self) -> None:
        _check_kubernetes_name(self.name, "FixtureService.name")
        if self.namespace is not None:
            _check_kubernetes_name(self.namespace, f"FixtureService {self.name!r}.namespace")
        if not self.metrics:
            raise SpecError(f"FixtureService {self.name!r}: requires at least one metric")
        if not 1 <= self.port <= 65535:
            raise SpecError(f"FixtureService {self.name!r}: port must be between 1 and 65535")
        Metric._validate(self.metrics)


@dataclass(frozen=True)
class PrometheusQuery:
    name: str
    query: str
    min_results: int = 1


@dataclass(frozen=True)
class PrometheusSpec:
    service: str = "kubex-prometheus-server"
    port: int = 80
    readiness_timeout: float = 600
    queries: Sequence[PrometheusQuery] = ()


@dataclass(frozen=True)
class ForwarderSpec:
    window_seconds: float = 180
    collection: Collection = Collection()


@dataclass(frozen=True)
class Wait:
    kind: str
    name: str
    namespace: str | None = None
    condition: str = "Available"
    timeout: int = 300


# The inventory CSVs every scenario's archive holds (real cAdvisor feeds the container files from system pods); a scenario's ``files`` adds what its workload produces.
BASE_FILES = frozenset(
    {
        "cluster/attributes.csv",
        "cluster/config.csv",
        "cluster/cpu_requests.csv",
        "cluster/cpu_reservation_percent.csv",
        "cluster/memory_requests.csv",
        "cluster/memory_reservation_percent.csv",
        "container/attributes.csv",
        "container/avg_cpu_mcores_workload.csv",
        "container/avg_cpu_throttling_percent_workload.csv",
        "container/avg_mem_workload.csv",
        "container/avg_rss_workload.csv",
        "container/avg_ws_workload.csv",
        "container/config.csv",
        "container/cpu_limit.csv",
        "container/cpu_request.csv",
        "container/current_size.csv",
        "container/max_cpu_mcores_workload.csv",
        "container/max_cpu_throttling_percent_workload.csv",
        "container/max_mem_workload.csv",
        "container/max_restarts.csv",
        "container/max_rss_workload.csv",
        "container/max_ws_workload.csv",
        "container/memory_limit.csv",
        "container/memory_request.csv",
        "container/sum_cpu_throttling_seconds_workload.csv",
        "node/attributes.csv",
        "node/config.csv",
        "node/cpu_limit.csv",
        "node/cpu_request.csv",
        "node/cpu_reservation_percent.csv",
        "node/memory_limit.csv",
        "node/memory_request.csv",
        "node/memory_reservation_percent.csv",
        "node/pod_count.csv",
        "node_group/attributes.csv",
        "node_group/config.csv",
        "node_group/cpu_requests.csv",
        "node_group/cpu_reservation_percent.csv",
        "node_group/current_size.csv",
        "node_group/memory_requests.csv",
        "node_group/memory_reservation_percent.csv",
    }
)


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    cluster: ClusterSpec
    stack: StackSpec = StackSpec()
    forwarder: ForwarderSpec = ForwarderSpec()
    prometheus: PrometheusSpec = PrometheusSpec()
    fixtures: Sequence[FixtureService] = ()
    resources: Sequence[str] = ()
    lifecycle: Sequence[Wait] = ()
    # kubectl resource names captured in every snapshot besides nodes and pods
    snapshot_resources: Sequence[str] = ()
    # Every inventory CSV the archive must hold, non-empty; any other inventory CSV fails.
    files: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", self.name):
            raise SpecError(f"ScenarioSpec: invalid name {self.name!r}")

    @property
    def cluster_name(self) -> str:
        return f"{CLUSTER_NAME_PREFIX}{self.name}"[:63].rstrip("-")

    @property
    def prefix(self) -> str:
        return f"data/{self.name}"
