"""Cluster-truth expectations computed independently from live Kubernetes state.

Valid only for spec-derived step functions — requests, limits, node capacity and
allocatable, the reservation percentages derived from them, and pod counts. Kubernetes has no opinion about usage,
throttling, or working set, so those metrics have no oracle here and belong to
fixture-truth or observed invariants instead.

Every query checks drift across all captured snapshots. That **detects sampled
drift**; it cannot prove no transient change occurred between two snapshots.
Pass ``allow_drift=True`` on a scenario that mutates the cluster on purpose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from harness.command import HarnessError, kubectl_json

_QUANTITY_FACTORS = {
    "": Decimal(1),
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal("1e3"),
    "K": Decimal("1e3"),
    "M": Decimal("1e6"),
    "G": Decimal("1e9"),
    "T": Decimal("1e12"),
    "P": Decimal("1e15"),
    "E": Decimal("1e18"),
    "Ki": Decimal(2) ** 10,
    "Mi": Decimal(2) ** 20,
    "Gi": Decimal(2) ** 30,
    "Ti": Decimal(2) ** 40,
    "Pi": Decimal(2) ** 50,
    "Ei": Decimal(2) ** 60,
}

_QUANTITY = re.compile(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)")


class OracleDrift(AssertionError):
    """Raised when the cluster moved during the collection window."""


def quantity(value: object, where: str) -> Decimal:
    match = _QUANTITY.fullmatch(str(value))
    if match is None or match.group(2) not in _QUANTITY_FACTORS:
        raise HarnessError(f"{where}: invalid Kubernetes quantity {value!r}")
    try:
        return Decimal(match.group(1)) * _QUANTITY_FACTORS[match.group(2)]
    except InvalidOperation as exc:
        raise HarnessError(f"{where}: invalid Kubernetes quantity {value!r}") from exc


DEFAULT_RESOURCES = ("nodes", "pods")
_CLUSTER_SCOPED = {"nodes"}


@dataclass(frozen=True)
class Snapshot:
    """Raw kubectl items as captured. ``other`` holds later object kinds keyed by resource name."""

    label: str
    nodes: list[dict[str, Any]]
    pods: list[dict[str, Any]]
    other: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def items(self) -> dict[str, list[dict[str, Any]]]:
        return {"nodes": self.nodes, "pods": self.pods, **self.other}

    @classmethod
    def from_items(cls, label: str, items: Mapping[str, list[dict[str, Any]]]) -> "Snapshot":
        extra = {k: v for k, v in items.items() if k not in ("nodes", "pods")}
        return cls(label, items["nodes"], items["pods"], extra)


def take_snapshot(label: str, repo_root: Path, resources: Sequence[str] = DEFAULT_RESOURCES) -> Snapshot:
    payloads: dict[str, list[dict[str, Any]]] = {}
    for resource in resources:
        args = ["get", resource, "-o", "json"]
        if resource not in _CLUSTER_SCOPED:
            args.insert(1, "-A")
        payload = kubectl_json(args, repo_root)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise HarnessError(f"kubectl returned invalid {resource} item list")
        payloads[resource] = items
    return Snapshot.from_items(label, payloads)


def _container_amount(container: dict[str, Any], kind: str, resource: str, where: str) -> Decimal:
    resources = container.get("resources", {})
    amounts = resources.get(kind, {}) if isinstance(resources, dict) else {}
    value = amounts.get(resource) if isinstance(amounts, dict) else None
    return Decimal(0) if value in {None, ""} else quantity(value, where)


def _select_nodes(snapshot: Snapshot, selector: Mapping[str, str]) -> tuple[list[dict[str, Any]], set[str]]:
    wanted = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in selector.items()}
    nodes = [
        node
        for node in snapshot.nodes
        if all(str(node.get("metadata", {}).get("labels", {}).get(k)) == v for k, v in wanted.items())
    ]
    names = {str(node.get("metadata", {}).get("name", "")) for node in nodes} - {""}
    if not names:
        raise HarnessError(f"Kubernetes oracle node selector {dict(selector)!r} matched no nodes")
    return nodes, names


def _live_containers(snapshot: Snapshot, names: set[str]) -> Iterator[tuple[tuple[str, str], dict[str, Any], str]]:
    """((namespace, name), container, description) for every live container of a Running pod on ``names``."""

    for pod in snapshot.pods:
        spec = pod.get("spec", {})
        status = pod.get("status", {})
        if spec.get("nodeName") not in names or status.get("phase") != "Running":
            continue
        pod_name = pod.get("metadata", {}).get("name", "<unknown>")
        pod_id = (pod.get("metadata", {}).get("namespace", ""), pod_name)
        terminated = {
            item.get("name")
            for item in status.get("containerStatuses", [])
            if isinstance(item, dict) and isinstance(item.get("state"), dict) and "terminated" in item["state"]
        }
        for container in spec.get("containers", []):
            if isinstance(container, dict) and container.get("name") not in terminated:
                yield pod_id, container, f"pod {pod_name} container {container.get('name', '<unknown>')}"
        terminated_init = {
            item.get("name")
            for item in status.get("initContainerStatuses", [])
            if isinstance(item, dict) and isinstance(item.get("state"), dict) and "terminated" in item["state"]
        }
        for container in spec.get("initContainers", []):
            # Native sidecars keep running, so they count; ordinary init
            # containers have already finished and do not.
            if (
                isinstance(container, dict)
                and container.get("restartPolicy") == "Always"
                and container.get("name") not in terminated_init
            ):
                yield pod_id, container, f"pod {pod_name} init container {container.get('name', '<unknown>')}"


def _node_total(nodes: list[dict[str, Any]], field_name: str, resource: str) -> float:
    return float(
        sum(
            (
                quantity(
                    node.get("status", {}).get(field_name, {}).get(resource),
                    f"node {node.get('metadata', {}).get('name', '<unknown>')} {field_name} {resource}",
                )
                for node in nodes
            ),
            Decimal(0),
        )
    )


def _compute(snapshot: Snapshot, selector: Mapping[str, str], resource: str, kind: str) -> dict[str, Any]:
    """``kind`` is ``requests`` or ``limits`` (summed over live containers) or ``capacity`` (node status)."""

    nodes, names = _select_nodes(snapshot, selector)
    if kind == "capacity":
        total = _node_total(nodes, "capacity", resource)
    else:
        total = float(
            sum(
                (_container_amount(c, kind, resource, where) for _, c, where in _live_containers(snapshot, names)),
                Decimal(0),
            )
        )
    return {
        "nodes": sorted(names),
        kind: total,
        "allocatable": _node_total(nodes, "allocatable", resource),
        "resource": resource,
        "unit": "cores" if resource == "cpu" else "bytes",
    }


def _compute_pod_count(snapshot: Snapshot, selector: Mapping[str, str]) -> dict[str, Any]:
    _, names = _select_nodes(snapshot, selector)
    return {"nodes": sorted(names), "pod_count": len({pod for pod, _, _ in _live_containers(snapshot, names)})}


@dataclass
class Oracle:
    """Cluster-truth queries over one primary snapshot, with drift detection."""

    snapshots: list[Snapshot]
    evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary(self) -> Snapshot:
        return self.snapshots[0]

    def _query(
        self,
        description: str,
        compute: Callable[[Snapshot], dict[str, Any]],
        fields: Sequence[str],
        allow_drift: bool,
    ) -> dict[str, Any]:
        computed = [(s.label, compute(s)) for s in self.snapshots]
        baseline_label, baseline = computed[0]
        if not allow_drift:
            for label, other in computed[1:]:
                for field_name in fields:
                    if baseline[field_name] != other[field_name]:
                        raise OracleDrift(
                            f"cluster oracle for {description} drifted: "
                            f"{field_name} changed from {baseline[field_name]!r} at {baseline_label} "
                            f"to {other[field_name]!r} at {label}"
                        )
        return baseline

    def _amount(
        self, selector: Mapping[str, str], resource: str, calculation: str, allow_drift: bool
    ) -> float:
        kind = calculation if calculation in ("requests", "limits", "capacity") else "requests"
        baseline = self._query(
            f"{calculation}({resource}, {dict(selector)!r})",
            lambda s: _compute(s, selector, resource, kind),
            ("nodes", kind, "allocatable"),
            allow_drift,
        )
        if calculation == "reservation_percent":
            if baseline["allocatable"] <= 0:
                raise HarnessError(f"no allocatable {resource} for nodes {baseline['nodes']!r}")
            expected = 100.0 * baseline["requests"] / baseline["allocatable"]
        else:
            expected = baseline[kind]
        self._record(calculation, selector, expected, allow_drift, baseline)
        return expected

    def _record(
        self, calculation: str, selector: Mapping[str, str], expected: float, allow_drift: bool, baseline: dict[str, Any]
    ) -> None:
        self.evidence.append(
            {
                "calculation": calculation,
                "node_selector": dict(selector),
                "expected": expected,
                "allow_drift": allow_drift,
                **baseline,
            }
        )

    def requests(self, resource: str, selector: Mapping[str, str], *, allow_drift: bool = False) -> float:
        """Total requested ``resource`` across pods on nodes matching ``selector``."""

        return self._amount(selector, resource, "requests", allow_drift)

    def limits(self, resource: str, selector: Mapping[str, str], *, allow_drift: bool = False) -> float:
        """Total ``resource`` limit across pods on nodes matching ``selector`` (same filtering as requests)."""

        return self._amount(selector, resource, "limits", allow_drift)

    def capacity(self, resource: str, selector: Mapping[str, str], *, allow_drift: bool = False) -> float:
        """Total node ``status.capacity`` of ``resource`` across nodes matching ``selector``."""

        return self._amount(selector, resource, "capacity", allow_drift)

    def reservation_percent(
        self, resource: str, selector: Mapping[str, str], *, allow_drift: bool = False
    ) -> float:
        """Aggregate requests as a percentage of aggregate allocatable."""

        return self._amount(selector, resource, "reservation_percent", allow_drift)

    def pod_count(self, selector: Mapping[str, str], *, allow_drift: bool = False) -> int:
        """Running pods on nodes matching ``selector`` with at least one live container or native sidecar."""

        baseline = self._query(
            f"pod_count({dict(selector)!r})",
            lambda s: _compute_pod_count(s, selector),
            ("nodes", "pod_count"),
            allow_drift,
        )
        self._record("pod_count", selector, baseline["pod_count"], allow_drift, baseline)
        return baseline["pod_count"]
