"""Cluster-truth expectations computed independently from live Kubernetes state.

Valid only for spec-derived step functions — requests, limits, node capacity and
allocatable, the reservation percentages derived from them, and pod counts. Kubernetes
has no opinion about usage, throttling, or working set, so those metrics have no oracle
here and belong to fixture-truth or observed invariants instead.

Every query checks drift across all captured snapshots. That **detects sampled
drift**; it cannot prove no transient change occurred between two snapshots.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

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


@dataclass(frozen=True)
class Snapshot:
    label: str
    nodes: list[dict[str, Any]]
    pods: list[dict[str, Any]]
    # Extra kubectl resources the scenario asked for, keyed by resource name.
    objects: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


def take_snapshot(label: str, repo_root: Path, resources: Sequence[str] = ()) -> Snapshot:
    payloads: dict[str, list[dict[str, Any]]] = {}
    for resource in ("nodes", "pods", *resources):
        args = ["get", resource, "-o", "json"] if resource == "nodes" else ["get", resource, "-A", "-o", "json"]
        payload = kubectl_json(args, repo_root)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise HarnessError(f"kubectl returned invalid {resource} item list")
        payloads[resource] = items
    return Snapshot(
        label=label,
        nodes=payloads["nodes"],
        pods=payloads["pods"],
        objects={resource: payloads[resource] for resource in resources},
    )


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
    total = Decimal(0)
    for node in nodes:
        amounts = node.get("status", {}).get(field_name, {})
        # a node that does not advertise an extended resource has none of it
        if "/" in resource and resource not in amounts:
            continue
        total += quantity(
            amounts.get(resource), f"node {node.get('metadata', {}).get('name', '<unknown>')} {field_name} {resource}"
        )
    return float(total)


def _compute(snapshot: Snapshot, selector: Mapping[str, str], resource: str | None, kind: str) -> dict[str, Any]:
    """``kind``: ``requests``/``limits`` (live containers), ``capacity`` (node status) or ``pod_count``."""

    nodes, names = _select_nodes(snapshot, selector)
    if kind == "pod_count":
        return {"nodes": sorted(names), kind: len({pod for pod, _, _ in _live_containers(snapshot, names)})}
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
    }


@dataclass
class Oracle:
    """Cluster-truth queries over one primary snapshot, with drift detection."""

    snapshots: list[Snapshot]

    @property
    def primary(self) -> Snapshot:
        return self.snapshots[0]

    def _query(self, calculation: str, selector: Mapping[str, str], resource: str | None) -> float:
        kind = "requests" if calculation == "reservation_percent" else calculation
        computed = [(s.label, _compute(s, selector, resource, kind)) for s in self.snapshots]
        baseline_label, baseline = computed[0]
        for label, other in computed[1:]:
            for name, value in baseline.items():
                if value != other[name]:
                    args = ", ".join(a for a in (resource, repr(dict(selector))) if a)
                    raise OracleDrift(
                        f"cluster oracle for {calculation}({args}) drifted: "
                        f"{name} changed from {value!r} at {baseline_label} to {other[name]!r} at {label}"
                    )
        if calculation == "reservation_percent":
            if baseline["allocatable"] <= 0:
                raise HarnessError(f"no allocatable {resource} for nodes {baseline['nodes']!r}")
            return 100.0 * baseline["requests"] / baseline["allocatable"]
        return baseline[kind]

    def requests(self, resource: str, selector: Mapping[str, str]) -> float:
        """Total requested ``resource`` across pods on nodes matching ``selector``."""

        return self._query("requests", selector, resource)

    def limits(self, resource: str, selector: Mapping[str, str]) -> float:
        """Total ``resource`` limit across pods on nodes matching ``selector`` (same filtering as requests)."""

        return self._query("limits", selector, resource)

    def capacity(self, resource: str, selector: Mapping[str, str]) -> float:
        """Total node ``status.capacity`` of ``resource`` across nodes matching ``selector``."""

        return self._query("capacity", selector, resource)

    def reservation_percent(self, resource: str, selector: Mapping[str, str]) -> float:
        """Aggregate requests as a percentage of aggregate allocatable."""

        return self._query("reservation_percent", selector, resource)

    def pod_count(self, selector: Mapping[str, str]) -> int:
        """Running pods on nodes matching ``selector`` with at least one live container or native sidecar."""

        return int(self._query("pod_count", selector, None))
