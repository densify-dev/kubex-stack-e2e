"""Ephemeral storage metrics: node usage and reservation, container requests, limits and usage.

A fixture Service named like the chart's real ephemeral storage exporter serves known node capacity
and availability, and per-container rootfs, log and volume usage for the one pod of a StatefulSet.
The pod has two containers, each with its own volume, plus one volume both mount. Request and limit
files come from Kubernetes; usage comes from the fixture and must count the shared volume once per pod.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.archive import assert_metric_grid, expected_timestamps
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.oracle import quantity
from harness.spec import (
    CLUSTER_NAME_PREFIX,
    ClusterSpec,
    Collection,
    FixtureService,
    ForwarderSpec,
    Metric,
    Node,
    PrometheusQuery,
    PrometheusSpec,
    ScenarioSpec,
    StackSpec,
)

HERE = Path(__file__).parent
NAME = "ephemeral-storage"
NAMESPACE = "ephemeral-storage-workload"
GROUP_LABEL = "topology.kubex.ai/node-group"
_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]
CLUSTER_SPEC = ClusterSpec(
    control_planes=[Node(labels=_SYSTEM_NODE)],
    workers=[
        # W1 reserves 1Gi, so its allocatable is below its capacity and differs from W2's
        Node(labels={GROUP_LABEL: "eph-workers"}, kubelet={"system-reserved": "ephemeral-storage=1Gi"}),
        Node(labels={GROUP_LABEL: "eph-workers"}),
    ],
)
NODES = CLUSTER_SPEC.node_names(CLUSTER_NAME_PREFIX + NAME)
CP, W1, W2 = NODES  # W1 is the node workload.yaml selects

# --- fixture inputs ----------------------------------------------------------

GIB, MIB = 2**30, 2**20
NODE_STORAGE = {CP: (100 * GIB, 70 * GIB), W1: (200 * GIB, 150 * GIB), W2: (400 * GIB, 380 * GIB)}  # (capacity, available)
POD = {"pod_name": "eph-0", "pod_namespace": NAMESPACE, "node_name": W1}
CONTAINERS = ["c1", "c2"]
ROOTFS = {"c1": 10 * MIB, "c2": 20 * MIB}
LOGS = {"c1": 1 * MIB, "c2": 2 * MIB}
VOLUMES = [
    ("c1", "u1", 30 * MIB),
    ("c2", "u2", 40 * MIB),
    ("c1", "shared", 50 * MIB),
    ("c2", "shared", 50 * MIB),  # same volume, same usage, seen from both containers
]
OWN = {"c1": (10 + 1 + 30) * MIB, "c2": (20 + 2 + 40) * MIB}  # rootfs + logs + unshared volume
SHARED = 50 * MIB


def _ephemeral_series():
    def gauge(name, value, **labels):
        return Metric(f"ephemeral_storage_{name}", "gauge", float(value), labels=labels)

    series = []
    for node, (capacity, available) in NODE_STORAGE.items():
        series += [gauge("node_capacity", capacity, node_name=node), gauge("node_available", available, node_name=node)]
    for c in CONTAINERS:
        series += [
            gauge("container_rootfs_used_bytes", ROOTFS[c], **POD, container=c),
            gauge("container_logs_used_bytes", LOGS[c], **POD, container=c),
        ]
    series += [gauge("container_volume_usage", usage, **POD, container=c, volume_name=v) for c, v, usage in VOLUMES]
    return series


NODE_REQUEST, NODE_LIMIT, NODE_RESERVATION = (f"node/ephemeral_storage_{f}.csv" for f in ("request", "limit", "reservation_percent"))
NODE_USAGE, NODE_UTILIZATION = "node/ephemeral_storage_usage_bytes.csv", "node/ephemeral_storage_usage_utilization.csv"
CONTAINER_REQUEST, CONTAINER_LIMIT = (f"container/ephemeral_storage_{f}.csv" for f in ("request", "limit"))
CONTAINER_USAGE = [  # path, column
    ("container/ephemeral_storage_usage_bytes_avg_workload.csv", "EphemeralStorageUsageBytesAvg"),
    ("container/ephemeral_storage_usage_bytes_max_workload.csv", "EphemeralStorageUsageBytesMax"),
]
EPHEMERAL_FILES = [NODE_REQUEST, NODE_LIMIT, NODE_RESERVATION, NODE_USAGE, NODE_UTILIZATION, CONTAINER_REQUEST, CONTAINER_LIMIT]
EPHEMERAL_FILES += [path for path, _ in CONTAINER_USAGE]

SCENARIO = ScenarioSpec(
    name=NAME,
    cluster=CLUSTER_SPEC,
    stack=StackSpec(
        helm_overrides={
            "stack": {"densify": {"createSecret": False}, "prometheus": {"deploy": True}},
            "gpu-process-exporter": {"enabled": False},
            "beyla": {"enabled": False},
            "node-labeler": {"enabled": False},
            "k8s-ephemeral-storage-metrics": {"enabled": False},
            "prometheus": {
                "prometheus-node-exporter": {"enabled": False},
                "server": {
                    "nodeSelector": _SYSTEM_NODE,
                    "tolerations": _CONTROL_PLANE_TOLERATION,
                    "persistentVolume": {"enabled": False},
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "512Mi"},
                        "limits": {"cpu": "500m", "memory": "1Gi"},
                    },
                },
                "kube-state-metrics": {
                    "nodeSelector": _SYSTEM_NODE,
                    "tolerations": _CONTROL_PLANE_TOLERATION,
                },
            },
            "container-optimization-data-forwarder": {
                "nodeSelector": _SYSTEM_NODE,
                "tolerations": _CONTROL_PLANE_TOLERATION,
                "config": {"collection": {"node_group_list": "label_topology_kubex_ai_node_group"}},
            },
        },
    ),
    # The chart's real exporter is disabled; this Service has its name, so Prometheus keeps its target.
    fixtures=[FixtureService(name="k8s-ephemeral-storage-metrics", metrics=_ephemeral_series())],
    prometheus=PrometheusSpec(
        queries=[
            PrometheusQuery("exporter-nodes", "count by (node_name) (ephemeral_storage_node_available)", min_results=3),
            PrometheusQuery("exporter-volumes", 'ephemeral_storage_container_volume_usage{pod_name="eph-0"}', min_results=4),
            PrometheusQuery("eph-running", f'kube_pod_status_phase{{namespace="{NAMESPACE}",pod="eph-0",phase="Running"}} == 1', min_results=1),
            PrometheusQuery("eph-owner", f'kube_pod_owner{{namespace="{NAMESPACE}",owner_kind="StatefulSet"}}', min_results=1),
            PrometheusQuery("eph-requests", f'kube_pod_container_resource_requests{{namespace="{NAMESPACE}",resource="ephemeral_storage"}}', min_results=2),
            PrometheusQuery("eph-limits", f'kube_pod_container_resource_limits{{namespace="{NAMESPACE}",resource="ephemeral_storage"}}', min_results=2),
            PrometheusQuery("allocatable-storage", 'kube_node_status_allocatable{resource="ephemeral_storage"}', min_results=3),
        ],
    ),
    forwarder=ForwarderSpec(
        job_name="ephemeral-storage-forwarder",
        window_seconds=180,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    resources=["workload.yaml"],
    # No lifecycle Wait: `kubectl wait pod/eph-0` fails with NotFound if run before the StatefulSet
    # controller creates the pod. The polling readiness queries above wait for Running instead.
    files={
        # Same files as node-pressure produces: the node group files exist only so this map is deterministic
        **dict.fromkeys(
            (
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
            ),
            "populated",
        ),
        **dict.fromkeys(EPHEMERAL_FILES, "populated"),
    },
)

pytestmark = pytest.mark.pr

# Everything is exact: bytes and ratios of exact integers, written with %f (5e-7).
TOLERANCE = 1e-6


@pytest.fixture(scope="session")
def run(stack_run):
    with stack_run(SCENARIO, HERE) as active:
        yield active


def _node(name):
    return {"kubernetes.io/hostname": name}


def _status(oracle, name, field):
    node = next(n for n in oracle.primary.nodes if n["metadata"]["name"] == name)
    return float(quantity(node["status"][field]["ephemeral-storage"], f"node {name} {field} ephemeral-storage"))


def _spec(oracle, container, kind):
    """The request or limit ("requests"/"limits") of a container of eph-0, in bytes."""

    pod = next(p for p in oracle.primary.pods if p["metadata"]["name"] == "eph-0")
    spec = next(c for c in pod["spec"]["containers"] if c["name"] == container)
    return float(quantity(spec["resources"][kind]["ephemeral-storage"], f"eph-0 {container} {kind}"))


def _grid(archive):
    return expected_timestamps(archive.csv("cluster/config.csv").one(Name=NAME)["AuditTime"], SCENARIO.forwarder.collection)


def test_reserved_worker_has_less_allocatable_storage(oracle):
    """W1 gives up exactly 1Gi more between capacity and allocatable than W2, which reserves nothing."""

    def lost(name):
        return _status(oracle, name, "capacity") - _status(oracle, name, "allocatable")

    assert lost(W1) - lost(W2) == 2**30


def test_ephemeral_exporter_is_detected(archive):
    """Without it the collector logs this and writes no usage file (node.go:215)."""

    assert "Ephemeral storage exporter metrics not present" not in archive.raw("log.txt").decode()


@pytest.mark.parametrize("path", EPHEMERAL_FILES)
def test_ephemeral_series_follow_the_collector_grid(archive, oracle, path):
    """Only W1 has a request and limit, so only W1 has a row in those node files; usage covers every node."""

    if path.startswith("container/"):
        keys = ["ClusterName", "Namespace", "EntityName", "EntityType", "ContainerName"]
        identities = [(NAME, NAMESPACE, "eph", "StatefulSet", c) for c in CONTAINERS]
    else:
        calculation = {NODE_LIMIT: oracle.limits}.get(path, oracle.requests)
        nodes = NODES if path in (NODE_USAGE, NODE_UTILIZATION) else [n for n in NODES if calculation("ephemeral-storage", _node(n)) > 0]
        keys, identities = ["ClusterName", "NodeName"], [(NAME, n) for n in nodes]
    assert_metric_grid(archive.csv(path), keys, identities, _grid(archive))


@pytest.mark.parametrize("path,column", CONTAINER_USAGE, ids=[column for _, column in CONTAINER_USAGE])
def test_container_usage_counts_each_volume_once(archive, path, column):
    """usage = rootfs + logs + own volumes + shared volumes, each shared volume assigned to one container
    (container.go:757-768). Each container keeps the same offset from its own usage in every row, and the
    offsets are {0, SHARED}: counting the shared volume twice or not at all gives something else."""

    offsets = {c: set() for c in CONTAINERS}
    for row in archive.csv(path):
        offsets[row["ContainerName"]].add(float(row[column]) - OWN[row["ContainerName"]])
    assert all(len(o) == 1 for o in offsets.values()), f"usage is not steady: {offsets}"
    assert sorted(o.pop() for o in offsets.values()) == [0, SHARED]


def test_container_usage_average_equals_maximum(archive):
    """One replica with constant usage, so the average and maximum of every sample are the same value,
    which also keeps the shared volume on the same container in both files."""

    (avg_path, avg_column), (max_path, max_column) = CONTAINER_USAGE

    def usage(path, column):
        return {(r["ContainerName"], r["MetricTime"]): float(r[column]) for r in archive.csv(path)}

    assert usage(avg_path, avg_column) == usage(max_path, max_column)


@pytest.mark.parametrize(
    "path,column,kind",
    [(CONTAINER_REQUEST, "EphemeralStorageRequest", "requests"), (CONTAINER_LIMIT, "EphemeralStorageLimit", "limits")],
    ids=["request", "limit"],
)
def test_container_request_and_limit_series_in_bytes(archive, oracle, path, column, kind):
    for row in archive.csv(path):
        assert float(row[column]) == pytest.approx(_spec(oracle, row["ContainerName"], kind), abs=TOLERANCE), (
            f"{path} {row['ContainerName']} at {row['MetricTime']}"
        )


# The attributes hold the last sample in whole MiB (container.go:532-546), unlike the series above.
@pytest.mark.parametrize("container", CONTAINERS)
@pytest.mark.parametrize("column,kind", [("EphemeralStorageRequest", "requests"), ("EphemeralStorageLimit", "limits")])
def test_container_attributes_in_mib(archive, oracle, container, column, kind):
    row = archive.csv("container/attributes.csv").one(Namespace=NAMESPACE, EntityName="eph", ContainerName=container)
    assert int(row[column]) == int(_spec(oracle, container, kind) / MIB)


@pytest.mark.parametrize(
    "path,column,calculation",
    [
        (NODE_REQUEST, "EphemeralStorageRequest", "requests"),
        (NODE_LIMIT, "EphemeralStorageLimit", "limits"),
        (NODE_RESERVATION, "EphemeralStorageReservationPercent", "reservation_percent"),
    ],
    ids=["request", "limit", "reservation"],
)
def test_node_request_limit_and_reservation(archive, oracle, path, column, calculation):
    """Bytes for requests and limits, percent of allocatable for reservation (node.go:150,162,179-202)."""

    rows = list(archive.csv(path))
    assert rows
    for row in rows:
        expected = getattr(oracle, calculation)("ephemeral-storage", _node(row["NodeName"]))
        assert float(row[column]) == pytest.approx(expected, abs=TOLERANCE), f"{path} {row['NodeName']} at {row['MetricTime']}"


@pytest.mark.parametrize(
    "path,column,percent",
    [(NODE_USAGE, "EphemeralStorageUsageBytes", False), (NODE_UTILIZATION, "EphemeralStorageUsageUtilization", True)],
    ids=["usage", "utilization"],
)
def test_node_usage_matches_the_fixture(archive, oracle, path, column, percent):
    """Capacity minus available, and that over Kubernetes allocatable, not the exporter's capacity
    (node.go:207-213). Allocatable is below capacity here, so utilization can exceed 100."""

    rows = list(archive.csv(path))
    assert rows
    for row in rows:
        capacity, available = NODE_STORAGE[row["NodeName"]]
        expected = 100 * (capacity - available) / _status(oracle, row["NodeName"], "allocatable") if percent else capacity - available
        assert float(row[column]) == pytest.approx(expected, abs=TOLERANCE), f"{path} {row['NodeName']} at {row['MetricTime']}"
