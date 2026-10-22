"""Role-derived node-group membership and resource metrics.

The collector is configured with the roles control-plane, infra and worker, in that order, and with
no node-group label. A node joins the first configured role it has (internal/nodegroup/role.go:111-125),
and nodes whose role is not configured are grouped by that role (at most one such role is allowed,
role.go:98-100). So:

- control-plane node: group control-plane
- worker 1 (roles infra and worker): group infra; worker is already taken, so no worker group
- worker 2 (role gpu, not configured): group gpu
- worker 3 (no role): in no group at all

Requests, limits, capacity and reservation percentages of every group must match what Kubernetes
itself reports for that group's members, including for the groups after the first.
"""

from __future__ import annotations


import pytest

from harness.archive import (
    assert_distinct,
    assert_metric_grid,
    expected_timestamps,
    label_map,
    prometheus_labels,
)
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.spec import (
    BASE_FILES,
    ClusterSpec,
    Collection,
    ForwarderSpec,
    Node,
    PrometheusQuery,
    PrometheusSpec,
    ScenarioSpec,
    StackSpec,
    Wait,
)

EXPECT_GROUP = "stack-e2e.kubex.ai/expect-group"
# The collector reads HwModel from this label (internal/node/node.go:297); Kind sets none of its own.
INSTANCE_TYPE = "node.kubernetes.io/instance-type"
WORKLOAD_NAMESPACE = "nodegroup-roles-workload"

# Units and rounding: the collector writes time-series values with %f (six decimal places), so
# the worst case error on a raw value is 5e-7. CPU is in cores. Memory is in MiB, so bytes from
# Kubernetes are divided by 2**20 before comparing. Percentages are compared to two decimals.
VALUE_TOLERANCE = 1e-6
PERCENT_TOLERANCE = 0.01
MIB = 2**20

_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]

SCENARIO = ScenarioSpec(
    name="nodegroup-roles",
    cluster=ClusterSpec(
        # The expect-group label is for the oracle only; the collector does not group on it.
        control_planes=[
            Node(labels={**_SYSTEM_NODE, EXPECT_GROUP: "control-plane", INSTANCE_TYPE: "e2e.control-plane"})
        ],
        workers=[
            Node(labels={EXPECT_GROUP: "infra", INSTANCE_TYPE: "e2e.infra"}, roles=["infra", "worker"]),
            Node(labels={EXPECT_GROUP: "gpu", INSTANCE_TYPE: "e2e.gpu"}, roles=["gpu"]),
            Node(labels={INSTANCE_TYPE: "e2e.none"}),
        ],
    ),
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
                # No node_group_list: the chart default matches no label on Kind, so roles decide.
                "config": {"collection": {"role_list": "control-plane,infra,worker"}},
            },
        },
    ),
    prometheus=PrometheusSpec(
        queries=[
            PrometheusQuery("node-roles", 'kube_node_role{role=~"control-plane|infra|worker|gpu"}', min_results=4),
            PrometheusQuery(
                "node-allocatable-resources",
                'kube_node_status_allocatable{resource=~"cpu|memory"}',
                min_results=8,
            ),
            PrometheusQuery(
                "workload-cpu-requests",
                f'kube_pod_container_resource_requests{{namespace="{WORKLOAD_NAMESPACE}",resource="cpu"}}',
                min_results=3,
            ),
            PrometheusQuery(
                "workload-memory-requests",
                f'kube_pod_container_resource_requests{{namespace="{WORKLOAD_NAMESPACE}",resource="memory"}}',
                min_results=3,
            ),
            PrometheusQuery(
                "workload-cpu-limits",
                f'kube_pod_container_resource_limits{{namespace="{WORKLOAD_NAMESPACE}",resource="cpu"}}',
                min_results=3,
            ),
            PrometheusQuery(
                "workload-memory-limits",
                f'kube_pod_container_resource_limits{{namespace="{WORKLOAD_NAMESPACE}",resource="memory"}}',
                min_results=3,
            ),
        ],
    ),
    forwarder=ForwarderSpec(
        window_seconds=180,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    resources=["workload.yaml"],
    lifecycle=[
        Wait(kind="pod", name=name, namespace=WORKLOAD_NAMESPACE, condition="Ready", timeout=300)
        for name in ("on-worker", "on-worker2", "on-worker3")
    ],
    files=BASE_FILES,
)

CLUSTER = SCENARIO.name
_NODES = SCENARIO.cluster.node_names(SCENARIO.cluster_name)
MEMBERS = {"control-plane": [_NODES[0]], "infra": [_NODES[1]], "gpu": [_NODES[2]]}

TIME_SERIES = [
    "node_group/current_size.csv",
    "node_group/cpu_requests.csv",
    "node_group/memory_requests.csv",
    "node_group/cpu_reservation_percent.csv",
    "node_group/memory_reservation_percent.csv",
]
ENTITY_TABLES = ["node_group/config.csv", "node_group/attributes.csv"]
KEYS = ["ClusterName", "NodeGroupName"]
IDENTITIES = [(CLUSTER, group) for group in MEMBERS]


def _selector(group):
    return {EXPECT_GROUP: group}


def _row(archive, path, group):
    return archive.csv(path).one(ClusterName=CLUSTER, NodeGroupName=group)


# --- archive shape -----------------------------------------------------------


@pytest.mark.parametrize("path", ENTITY_TABLES)
def test_entity_tables_hold_exactly_the_three_groups(archive, path):
    """control-plane, infra and gpu: no worker (claimed by infra), no __default__, no label_* name."""

    table = archive.csv(path)
    assert_distinct(table, "ClusterName", [CLUSTER])
    assert_distinct(table, "NodeGroupName", list(MEMBERS))
    assert len(table) == len(MEMBERS), f"{path}: entity table should hold one row per node group"


@pytest.mark.parametrize("path", TIME_SERIES)
def test_time_series_follow_the_collector_grid(archive, path):
    """Exactly the three groups, each with exactly the MetricTime grid the collector derives from AuditTime."""

    audit_time = archive.csv("node_group/config.csv").one(ClusterName=CLUSTER, NodeGroupName="infra")["AuditTime"]
    assert_metric_grid(
        archive.csv(path),
        KEYS,
        IDENTITIES,
        expected_timestamps(audit_time, SCENARIO.forwarder.collection),
    )


# --- membership --------------------------------------------------------------


@pytest.mark.parametrize("group", MEMBERS)
def test_group_membership(archive, group):
    row = _row(archive, "node_group/attributes.csv", group)
    assert row["VirtualTechnology"] == "NodeGroup"
    assert row["VirtualDomain"] == CLUSTER
    assert sorted(row["CurrentNodes"].split("|")) == sorted(MEMBERS[group])
    assert float(row["CurrentSize"]) == pytest.approx(len(MEMBERS[group]))


def test_current_size_holds_for_every_sample(archive):
    for row in archive.csv("node_group/current_size.csv"):
        expected = len(MEMBERS[row["NodeGroupName"]])
        assert float(row["CurrentSize"]) == pytest.approx(expected), f"{row['NodeGroupName']} at {row['MetricTime']}"


# --- premise: every group has its own sums --------------------------------------


def test_groups_have_distinct_requests_and_limits(oracle):
    """A group that reported a node count or another group's sums could not match by accident."""

    for kind in ("requests", "limits"):
        for resource in ("cpu", "memory"):
            sums = {getattr(oracle, kind)(resource, _selector(group)) for group in MEMBERS}
            assert len(sums) == len(MEMBERS), f"{kind} of {resource} are not distinct across groups: {sums}"


# --- values against the cluster ----------------------------------------------
# Every sample is checked, not just one: the collector averages over the window.


@pytest.mark.parametrize(
    "path,column,resource,divisor",
    [
        ("node_group/cpu_requests.csv", "CpuRequests", "cpu", 1),
        ("node_group/memory_requests.csv", "MemoryRequests", "memory", MIB),
    ],
)
def test_requests_match_the_cluster(archive, oracle, path, column, resource, divisor):
    for row in archive.csv(path):
        expected = oracle.requests(resource, _selector(row["NodeGroupName"])) / divisor
        assert float(row[column]) == pytest.approx(expected, abs=VALUE_TOLERANCE), (
            f"{path} {row['NodeGroupName']} at {row['MetricTime']}"
        )


@pytest.mark.parametrize(
    "path,column,resource",
    [
        ("node_group/cpu_reservation_percent.csv", "CpuReservationPercent", "cpu"),
        ("node_group/memory_reservation_percent.csv", "MemoryReservationPercent", "memory"),
    ],
)
def test_reservation_percent_matches_the_cluster(archive, oracle, path, column, resource):
    for row in archive.csv(path):
        expected = oracle.reservation_percent(resource, _selector(row["NodeGroupName"]))
        assert float(row[column]) == pytest.approx(expected, abs=PERCENT_TOLERANCE), (
            f"{path} {row['NodeGroupName']} at {row['MetricTime']}"
        )


# --- node_group/attributes.csv: whole numbers, CPU in mcores and memory in MiB ----
# The collector reads the last sample of each query and truncates it with Go's int()
# (internal/nodegroup/nodegroup.go:58).


ATTRIBUTES = [
    # column, calculation, resource, factor from the oracle's cores/bytes
    ("CpuRequest", "requests", "cpu", 1000),
    ("MemoryRequest", "requests", "memory", 1 / MIB),
    ("CpuLimit", "limits", "cpu", 1000),
    ("MemoryLimit", "limits", "memory", 1 / MIB),
]


@pytest.mark.parametrize("column,calculation,resource,factor", ATTRIBUTES, ids=[row[0] for row in ATTRIBUTES])
@pytest.mark.parametrize("group", MEMBERS)
def test_attribute_requests_and_limits(archive, oracle, group, column, calculation, resource, factor):
    expected = int(getattr(oracle, calculation)(resource, _selector(group)) * factor)
    assert int(_row(archive, "node_group/attributes.csv", group)[column]) == expected


# --- node_group/config.csv ---------------------------------------------------


@pytest.mark.parametrize("group", MEMBERS)
def test_config_hardware(archive, oracle, group):
    """Unlike requests and limits (/1024/1024) and node/config.csv (MiB), HwTotalMemory is the raw sum of
    kube_node_status_capacity bytes over the group (internal/nodegroup/nodegroup.go:71-78, :364-366, :113)."""

    row, selector = _row(archive, "node_group/config.csv", group), _selector(group)
    cpus = int(oracle.capacity("cpu", selector))
    assert int(row["HwTotalCpus"]) == cpus
    assert int(row["HwTotalPhysicalCpus"]) == cpus
    assert (row["HwCoresPerCpu"], row["HwThreadsPerCore"]) == ("1", "1")
    assert int(row["HwTotalMemory"]) == int(oracle.capacity("memory", selector))


def _shared_labels(oracle, group):
    """Labels, as Prometheus names them, that every member of the group carries with the same value."""

    labels = [
        prometheus_labels(node["metadata"]["labels"]).items()
        for node in oracle.primary.nodes
        if node["metadata"]["name"] in MEMBERS[group]
    ]
    return dict(set.intersection(*map(set, labels)))


@pytest.mark.parametrize("group", MEMBERS)
def test_config_os_and_model(archive, oracle, group):
    shared = _shared_labels(oracle, group)
    row = _row(archive, "node_group/config.csv", group)
    assert (row["OsName"], row["HwModel"]) == (
        shared["label_kubernetes_io_os"],
        shared["label_node_kubernetes_io_instance_type"],
    )


@pytest.mark.parametrize("group", MEMBERS)
def test_attribute_labels(archive, oracle, group):
    """NodeLabels merges the members' labels, keeping the first value seen (internal/common/common.go:39),
    so only labels every member shares have a defined value."""

    collected = label_map(_row(archive, "node_group/attributes.csv", group)["NodeLabels"])
    missing = {k: v for k, v in _shared_labels(oracle, group).items() if collected.get(k) != v}
    assert not missing, f"shared labels missing or wrong: {missing}"
