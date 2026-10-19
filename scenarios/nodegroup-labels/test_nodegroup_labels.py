"""Label-derived node-group membership and resource metrics.

Two ordinary Kind workers share one configured node-group label and one label the
collector is not configured to group on. The collector must produce exactly one
node group, containing both workers, with requests, limits, capacity and reservation
percentages matching what Kubernetes itself reports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.archive import (
    assert_distinct,
    assert_metric_grid,
    assert_unique_keys,
    expected_timestamps,
)
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.spec import (
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

HERE = Path(__file__).parent
NODE_GROUP = {"topology.kubex.ai/node-group": "mvp-workers"}
WORKLOAD_NAMESPACE = "nodegroup-labels-workload"

# Units and rounding: the collector writes time-series values with %f (six decimal
# places), so the worst case error on a raw value is 5e-7. CPU is in cores. Memory is
# in MiB (as in the node and container files), so bytes from Kubernetes are divided by
# 2**20 before comparing. Percentages are compared to two decimals.
VALUE_TOLERANCE = 1e-6
PERCENT_TOLERANCE = 0.01
MIB = 2**20

_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]

SCENARIO = ScenarioSpec(
    name="nodegroup-labels",
    cluster=ClusterSpec(
        control_planes=[Node(labels=_SYSTEM_NODE)],
        workers=[
            Node(labels={**NODE_GROUP, "ignored.kubex.ai/group": "ignored-value"}),
            Node(labels={**NODE_GROUP, "ignored.kubex.ai/group": "ignored-value"}),
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
                "config": {"collection": {"node_group_list": "label_topology_kubex_ai_node_group"}},
            },
        },
    ),
    prometheus=PrometheusSpec(
        queries=[
            PrometheusQuery(
                "mvp-worker-labels",
                'kube_node_labels{label_topology_kubex_ai_node_group="mvp-workers"}',
                min_results=2,
            ),
            PrometheusQuery(
                "node-allocatable-resources",
                'kube_node_status_allocatable{resource=~"cpu|memory"}',
                min_results=2,
            ),
            PrometheusQuery(
                "workload-cpu-requests",
                f'kube_pod_container_resource_requests{{namespace="{WORKLOAD_NAMESPACE}",resource="cpu"}}',
                min_results=2,
            ),
            PrometheusQuery(
                "workload-memory-requests",
                f'kube_pod_container_resource_requests{{namespace="{WORKLOAD_NAMESPACE}",resource="memory"}}',
                min_results=2,
            ),
            PrometheusQuery(
                "workload-cpu-limits",
                f'kube_pod_container_resource_limits{{namespace="{WORKLOAD_NAMESPACE}",resource="cpu"}}',
                min_results=2,
            ),
            PrometheusQuery(
                "workload-memory-limits",
                f'kube_pod_container_resource_limits{{namespace="{WORKLOAD_NAMESPACE}",resource="memory"}}',
                min_results=2,
            ),
        ],
    ),
    forwarder=ForwarderSpec(
        job_name="nodegroup-labels-forwarder",
        window_seconds=180,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    resources=["workload.yaml"],
    lifecycle=[
        Wait(kind="deployment", name="mvp-workload", namespace=WORKLOAD_NAMESPACE, timeout=300)
    ],
    files={
        "cluster/attributes.csv": "populated",
        "cluster/config.csv": "populated",
        "cluster/cpu_requests.csv": "populated",
        "cluster/cpu_reservation_percent.csv": "populated",
        "cluster/memory_requests.csv": "populated",
        "cluster/memory_reservation_percent.csv": "populated",
        "container/attributes.csv": "populated",
        "container/avg_cpu_mcores_workload.csv": "populated",
        "container/avg_cpu_throttling_percent_workload.csv": "populated",
        "container/avg_mem_workload.csv": "populated",
        "container/avg_rss_workload.csv": "populated",
        "container/avg_ws_workload.csv": "populated",
        "container/config.csv": "populated",
        "container/cpu_limit.csv": "populated",
        "container/cpu_request.csv": "populated",
        "container/current_size.csv": "populated",
        "container/max_cpu_mcores_workload.csv": "populated",
        "container/max_cpu_throttling_percent_workload.csv": "populated",
        "container/max_mem_workload.csv": "populated",
        "container/max_restarts.csv": "populated",
        "container/max_rss_workload.csv": "populated",
        "container/max_ws_workload.csv": "populated",
        "container/memory_limit.csv": "populated",
        "container/memory_request.csv": "populated",
        "container/sum_cpu_throttling_seconds_workload.csv": "populated",
        "node/attributes.csv": "populated",
        "node/config.csv": "populated",
        "node/cpu_limit.csv": "populated",
        "node/cpu_request.csv": "populated",
        "node/cpu_reservation_percent.csv": "populated",
        "node/memory_limit.csv": "populated",
        "node/memory_request.csv": "populated",
        "node/memory_reservation_percent.csv": "populated",
        "node/pod_count.csv": "populated",
        "node_group/attributes.csv": "populated",
        "node_group/config.csv": "populated",
        "node_group/cpu_requests.csv": "populated",
        "node_group/cpu_reservation_percent.csv": "populated",
        "node_group/current_size.csv": "populated",
        "node_group/memory_requests.csv": "populated",
        "node_group/memory_reservation_percent.csv": "populated",
    },
)

TIME_SERIES = [
    "node_group/current_size.csv",
    "node_group/cpu_requests.csv",
    "node_group/memory_requests.csv",
    "node_group/cpu_reservation_percent.csv",
    "node_group/memory_reservation_percent.csv",
]
ENTITY_TABLES = ["node_group/config.csv", "node_group/attributes.csv"]
KEYS = ["ClusterName", "NodeGroupName"]
IDENTITIES = [("nodegroup-labels", "mvp-workers")]
ROW = {"ClusterName": "nodegroup-labels", "NodeGroupName": "mvp-workers"}

pytestmark = pytest.mark.pr


@pytest.fixture(scope="session")
def run(stack_run):
    with stack_run(SCENARIO, HERE) as active:
        yield active


# --- archive shape -----------------------------------------------------------



@pytest.mark.parametrize("path", ENTITY_TABLES + TIME_SERIES)
def test_cluster_and_group_identity_is_the_only_one(archive, path):
    """Exactly one cluster and one node group. The ignored label must not group."""

    table = archive.csv(path)
    assert_distinct(table, "ClusterName", ["nodegroup-labels"])
    assert_distinct(table, "NodeGroupName", ["mvp-workers"])


@pytest.mark.parametrize("path", ENTITY_TABLES)
def test_entity_tables_hold_exactly_one_row(archive, path):
    table = archive.csv(path)
    assert len(table) == 1, f"{path}: entity table should hold one row per node group"
    assert_unique_keys(table, ["ClusterName", "NodeGroupName"])


@pytest.mark.parametrize("path", TIME_SERIES)
def test_time_series_follow_the_collector_grid(archive, path):
    """One identity, with exactly the MetricTime grid the collector derives from AuditTime."""

    audit_time = archive.csv("node_group/config.csv").one(**ROW)["AuditTime"]
    assert_metric_grid(
        archive.csv(path),
        KEYS,
        IDENTITIES,
        expected_timestamps(audit_time, SCENARIO.forwarder.collection),
    )


# --- membership --------------------------------------------------------------


def test_both_workers_are_in_the_group(archive):
    row = archive.csv("node_group/attributes.csv").one(**ROW)
    expected = sorted(SCENARIO.cluster.node_names(SCENARIO.cluster_name)[1:])
    assert sorted(row["CurrentNodes"].split("|")) == expected
    assert float(row["CurrentSize"]) == pytest.approx(2)


def test_attributes_describe_a_node_group(archive):
    row = archive.csv("node_group/attributes.csv").one(**ROW)
    assert row["VirtualTechnology"] == "NodeGroup"
    assert row["VirtualDomain"] == "nodegroup-labels"


def test_current_size_holds_for_every_sample(archive):
    for row in archive.csv("node_group/current_size.csv"):
        assert float(row["CurrentSize"]) == pytest.approx(2), f"at {row['MetricTime']}"


# --- values against the cluster ----------------------------------------------
# Every sample is checked, not just one: the collector averages over the window,
# so a value that only holds at one instant is not the property under test.


@pytest.mark.parametrize(
    "path,column,resource,divisor",
    [
        ("node_group/cpu_requests.csv", "CpuRequests", "cpu", 1),
        ("node_group/memory_requests.csv", "MemoryRequests", "memory", MIB),
    ],
)
def test_requests_match_the_cluster(archive, oracle, path, column, resource, divisor):
    expected = oracle.requests(resource, NODE_GROUP) / divisor
    for row in archive.csv(path):
        assert float(row[column]) == pytest.approx(expected, abs=VALUE_TOLERANCE), (
            f"{path} at {row['MetricTime']}"
        )


@pytest.mark.parametrize(
    "path,column,resource",
    [
        ("node_group/cpu_reservation_percent.csv", "CpuReservationPercent", "cpu"),
        ("node_group/memory_reservation_percent.csv", "MemoryReservationPercent", "memory"),
    ],
)
def test_reservation_percent_matches_the_cluster(archive, oracle, path, column, resource):
    expected = oracle.reservation_percent(resource, NODE_GROUP)
    for row in archive.csv(path):
        assert float(row[column]) == pytest.approx(expected, abs=PERCENT_TOLERANCE), (
            f"{path} at {row['MetricTime']}"
        )


# --- node_group/attributes.csv: whole numbers, CPU in mcores and memory in MiB ----
# The collector reads the last sample of each query and truncates it with Go's int(),
# so the expectation truncates too (internal/nodegroup/nodegroup.go:58).


def _attribute(archive, column):
    return int(archive.csv("node_group/attributes.csv").one(**ROW)[column])


def test_attribute_cpu_request(archive, oracle):
    assert _attribute(archive, "CpuRequest") == int(oracle.requests("cpu", NODE_GROUP) * 1000)


def test_attribute_memory_request(archive, oracle):
    assert _attribute(archive, "MemoryRequest") == int(oracle.requests("memory", NODE_GROUP) / MIB)


def test_attribute_cpu_limit(archive, oracle):
    assert _attribute(archive, "CpuLimit") == int(oracle.limits("cpu", NODE_GROUP) * 1000)


def test_attribute_memory_limit(archive, oracle):
    assert _attribute(archive, "MemoryLimit") == int(oracle.limits("memory", NODE_GROUP) / MIB)


# --- node_group/config.csv ---------------------------------------------------
# HwTotalMemory is not asserted: its unit (bytes here, MiB in node/config.csv) is
# pending SME #9.


def test_config_total_cpus(archive, oracle):
    row = archive.csv("node_group/config.csv").one(**ROW)
    assert int(row["HwTotalCpus"]) == int(oracle.capacity("cpu", NODE_GROUP))


def test_config_os_name(archive, oracle):
    systems = {
        node["status"]["nodeInfo"]["operatingSystem"]
        for node in oracle.primary.nodes
        if NODE_GROUP.items() <= node["metadata"]["labels"].items()
    }
    assert len(systems) == 1, f"node group members report several operating systems: {sorted(systems)!r}"
    assert archive.csv("node_group/config.csv").one(**ROW)["OsName"] == systems.pop()
