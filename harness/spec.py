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

from harness.versions import COLLECTOR_IMAGE, KIND_NODE_IMAGE, STACK_CHART_VERSION

CLUSTER_NAME_PREFIX = "kubex-e2e-"
INTERVAL_SECONDS = {"seconds": 1, "minutes": 60, "hours": 3600}

_LABEL_NAME = re.compile(r"^[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$")
_LABEL_PREFIX = re.compile(r"^[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?$")
_LABEL_VALUE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?$|^$")
_KUBERNETES_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


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

    def __post_init__(self) -> None:
        _check_labels(self.labels, "node.labels")


@dataclass(frozen=True)
class ClusterSpec:
    node_image: str = KIND_NODE_IMAGE
    control_planes: Sequence[Node] = (Node(),)
    workers: Sequence[Node] = ()
    kubelet: Mapping[str, str] = field(default_factory=dict)

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

    def derived_window_seconds(self) -> float:
        return float(self.interval_size) * INTERVAL_SECONDS[self.interval] + 10


@dataclass(frozen=True)
class StackSpec:
    chart: str = "kubex/kubex-automation-stack"
    repository: str = "https://densify-dev.github.io/helm-charts"
    version: str | None = STACK_CHART_VERSION
    release_name: str = "kubex"
    namespace: str = "kubex"
    collector_image: str | None = COLLECTOR_IMAGE
    helm_overrides: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Metric:
    """One fixture series. Replaces a ``metrics:`` entry in fixture.yaml."""

    name: str
    type: Literal["gauge", "counter"]
    value: float = 0.0
    help: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    rate: float | None = None

    def __post_init__(self) -> None:
        if self.type not in {"gauge", "counter"}:
            raise SpecError(f"Metric {self.name!r}: invalid type {self.type!r}")
        if self.type == "gauge" and self.rate is not None:
            raise SpecError(f"Metric {self.name!r}: gauges cannot define a rate")
        if self.type == "counter" and self.rate is None:
            raise SpecError(f"Metric {self.name!r}: counters require a rate")
        self._validate([self])

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
            "help": self.help if self.help is not None else self.name,
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
    target_port: int | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    annotations: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_kubernetes_name(self.name, "FixtureService.name")
        if self.namespace is not None:
            _check_kubernetes_name(self.namespace, f"FixtureService {self.name!r}.namespace")
        if not self.metrics:
            raise SpecError(f"FixtureService {self.name!r}: requires at least one metric")
        for field_name in ("port", "target_port"):
            value = getattr(self, field_name)
            if value is not None and not 1 <= value <= 65535:
                raise SpecError(f"FixtureService {self.name!r}: {field_name} must be between 1 and 65535")
        Metric._validate(self.metrics)

    @property
    def container_port(self) -> int:
        return self.target_port if self.target_port is not None else self.port


@dataclass(frozen=True)
class PrometheusQuery:
    name: str
    query: str
    min_results: int = 1


@dataclass(frozen=True)
class PrometheusTarget:
    name: str
    service: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    state: str = "up"
    required: bool = True


@dataclass(frozen=True)
class PrometheusSpec:
    service: str = "kubex-prometheus-server"
    port: int = 80
    readiness_timeout: float = 600
    targets: Sequence[PrometheusTarget] = ()
    queries: Sequence[PrometheusQuery] = ()


@dataclass(frozen=True)
class ForwarderSpec:
    cronjob_name: str | None = None
    job_name: str | None = None
    window_seconds: float | None = None
    wait_timeout: float = 900
    poll_interval: float = 5
    collection: Collection = Collection()

    def window(self) -> float:
        if self.window_seconds is not None:
            return float(self.window_seconds)
        return self.collection.derived_window_seconds()


@dataclass(frozen=True)
class Wait:
    kind: str
    name: str
    namespace: str | None = None
    condition: str = "Available"
    timeout: int = 300


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
    archive_prefix: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", self.name):
            raise SpecError(f"ScenarioSpec: invalid name {self.name!r}")

    @property
    def cluster_name(self) -> str:
        return f"{CLUSTER_NAME_PREFIX}{self.name}"[:63].rstrip("-")

    @property
    def prefix(self) -> str:
        return self.archive_prefix if self.archive_prefix is not None else f"data/{self.name}"
