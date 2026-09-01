"""Cluster-truth expectations computed independently from live Kubernetes state.

Valid only for spec-derived step functions — requests, allocatable, and the
reservation percentages derived from them. Kubernetes has no opinion about usage,
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
from typing import Any, Mapping

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


def take_snapshot(label: str, repo_root: Path) -> Snapshot:
    payloads: dict[str, list[dict[str, Any]]] = {}
    for resource in ("nodes", "pods"):
        args = ["get", resource, "-o", "json"]
        if resource == "pods":
            args.insert(1, "-A")
        payload = kubectl_json(args, repo_root)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise HarnessError(f"kubectl returned invalid {resource} item list")
        payloads[resource] = items
    return Snapshot(label=label, nodes=payloads["nodes"], pods=payloads["pods"])


def _container_request(container: dict[str, Any], resource: str, where: str) -> Decimal:
    resources = container.get("resources", {})
    requests = resources.get("requests", {}) if isinstance(resources, dict) else {}
    value = requests.get(resource) if isinstance(requests, dict) else None
    return Decimal(0) if value in {None, ""} else quantity(value, where)


def _compute(snapshot: Snapshot, selector: Mapping[str, str], resource: str) -> dict[str, Any]:
    wanted = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in selector.items()}
    nodes = [
        node
        for node in snapshot.nodes
        if all(str(node.get("metadata", {}).get("labels", {}).get(k)) == v for k, v in wanted.items())
    ]
    names = {str(node.get("metadata", {}).get("name", "")) for node in nodes} - {""}
    if not names:
        raise HarnessError(f"Kubernetes oracle node selector {dict(selector)!r} matched no nodes")

    requests = Decimal(0)
    for pod in snapshot.pods:
        spec = pod.get("spec", {})
        status = pod.get("status", {})
        if spec.get("nodeName") not in names or status.get("phase") != "Running":
            continue
        pod_name = pod.get("metadata", {}).get("name", "<unknown>")
        terminated = {
            item.get("name")
            for item in status.get("containerStatuses", [])
            if isinstance(item, dict) and isinstance(item.get("state"), dict) and "terminated" in item["state"]
        }
        for container in spec.get("containers", []):
            if isinstance(container, dict) and container.get("name") not in terminated:
                where = f"pod {pod_name} container {container.get('name', '<unknown>')}"
                requests += _container_request(container, resource, where)
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
                where = f"pod {pod_name} init container {container.get('name', '<unknown>')}"
                requests += _container_request(container, resource, where)

    allocatable = sum(
        (
            quantity(
                node.get("status", {}).get("allocatable", {}).get(resource),
                f"node {node.get('metadata', {}).get('name', '<unknown>')} allocatable {resource}",
            )
            for node in nodes
        ),
        Decimal(0),
    )
    return {
        "nodes": sorted(names),
        "requests": float(requests),
        "allocatable": float(allocatable),
        "resource": resource,
        "unit": "cores" if resource == "cpu" else "bytes",
    }


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
        selector: Mapping[str, str],
        resource: str,
        calculation: str,
        allow_drift: bool,
    ) -> float:
        computed = [(s.label, _compute(s, selector, resource)) for s in self.snapshots]
        baseline_label, baseline = computed[0]
        if not allow_drift:
            for label, other in computed[1:]:
                for field_name in ("nodes", "requests", "allocatable"):
                    if baseline[field_name] != other[field_name]:
                        raise OracleDrift(
                            f"cluster oracle for {calculation}({resource}, {dict(selector)!r}) drifted: "
                            f"{field_name} changed from {baseline[field_name]!r} at {baseline_label} "
                            f"to {other[field_name]!r} at {label}"
                        )
        if calculation == "requests":
            expected = baseline["requests"]
        else:
            if baseline["allocatable"] <= 0:
                raise HarnessError(f"no allocatable {resource} for nodes {baseline['nodes']!r}")
            expected = 100.0 * baseline["requests"] / baseline["allocatable"]
        self.evidence.append(
            {
                "calculation": calculation,
                "node_selector": dict(selector),
                "expected": expected,
                "allow_drift": allow_drift,
                **baseline,
            }
        )
        return expected

    def requests(self, resource: str, selector: Mapping[str, str], *, allow_drift: bool = False) -> float:
        """Total requested ``resource`` across pods on nodes matching ``selector``."""

        return self._query(selector, resource, "requests", allow_drift)

    def reservation_percent(
        self, resource: str, selector: Mapping[str, str], *, allow_drift: bool = False
    ) -> float:
        """Aggregate requests as a percentage of aggregate allocatable."""

        return self._query(selector, resource, "reservation_percent", allow_drift)
