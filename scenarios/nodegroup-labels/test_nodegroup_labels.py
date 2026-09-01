"""Label-derived node-group membership and resource metrics.

Two ordinary Kind workers share one configured node-group label and one label the
collector is not configured to group on. The collector must produce exactly one
node group, containing both workers, with requests and reservation percentages
matching what Kubernetes itself reports.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.archive import assert_distinct, assert_headers, assert_paths, assert_unique_keys
from harness.spec import (
    ClusterSpec,
    Collection,
    FixtureService,
    ForwarderSpec,
    Metric,
    Node,
    PrometheusQuery,
    PrometheusSpec,
    PrometheusTarget,
    ScenarioSpec,
    StackSpec,
    Wait,
)

HERE = Path(__file__).parent
NODE_GROUP = {"topology.kubex.ai/node-group": "mvp-workers"}
WORKLOAD_NAMESPACE = "nodegroup-labels-workload"

# Rounding: the collector writes six decimal places, so the worst case error on a
# raw value is 5e-7. Memory is in bytes, where sub-byte precision is meaningless.
CPU_TOLERANCE = 1e-6
MEMORY_TOLERANCE = 1.0
PERCENT_TOLERANCE = 0.01

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
    fixtures=[
        FixtureService(
            name="kubex-dcgm-fixture",
            port=9100,
            target_port=9100,
            metrics=[
                Metric(
                    name="DCGM_FI_DEV_GPU_UTIL",
                    type="gauge",
                    help="Deterministic zero GPU utilization fixture.",
                    labels={"gpu": "0", "UUID": "MVP-DCGM-0"},
                    value=0,
                )
            ],
        )
    ],
    prometheus=PrometheusSpec(
        targets=[PrometheusTarget(name="kubex-dcgm-fixture", service="kubex-dcgm-fixture")],
        queries=[
            PrometheusQuery("fixture-up", 'up{service="kubex-dcgm-fixture"}'),
            PrometheusQuery(
                "retained-fixture-gauge",
                'DCGM_FI_DEV_GPU_UTIL{service="kubex-dcgm-fixture",gpu="0"}',
            ),
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
        ],
    ),
    forwarder=ForwarderSpec(
        cronjob_name="kubex-kubex-stack",
        job_name="nodegroup-labels-forwarder",
        window_seconds=180,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    resources=["workload.yaml"],
    lifecycle=[
        Wait(kind="deployment", name="mvp-workload", namespace=WORKLOAD_NAMESPACE, timeout=300)
    ],
)

TIME_SERIES = [
    "node_group/current_size.csv",
    "node_group/cpu_requests.csv",
    "node_group/memory_requests.csv",
    "node_group/cpu_reservation_percent.csv",
    "node_group/memory_reservation_percent.csv",
]
ENTITY_TABLES = ["node_group/config.csv", "node_group/attributes.csv"]

pytestmark = pytest.mark.pr


@pytest.fixture(scope="session")
def run(stack_run):
    with stack_run(SCENARIO, HERE) as active:
        yield active


# --- archive shape -----------------------------------------------------------


def test_required_files_are_present(archive):
    assert_paths(archive, required=ENTITY_TABLES + TIME_SERIES)


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
def test_time_series_have_samples_with_unique_timestamps(archive, path):
    """Sample count varies with how the window lines up with interval boundaries."""

    table = archive.csv(path)
    assert len(table) >= 1, f"{path}: expected at least one sample"
    assert_unique_keys(table, ["ClusterName", "NodeGroupName", "MetricTime"])


def test_config_headers(archive):
    assert_headers(
        archive.csv("node_group/config.csv"),
        [
            "AuditTime",
            "ClusterName",
            "NodeGroupName",
            "HwTotalCpus",
            "HwTotalPhysicalCpus",
            "HwCoresPerCpu",
            "HwThreadsPerCore",
            "HwTotalMemory",
            "HwModel",
            "OsName",
        ],
    )


def test_attributes_headers(archive):
    assert_headers(
        archive.csv("node_group/attributes.csv"),
        [
            "ClusterName",
            "NodeGroupName",
            "VirtualTechnology",
            "VirtualDomain",
            "CpuLimit",
            "CpuRequest",
            "MemoryLimit",
            "MemoryRequest",
            "CurrentSize",
            "CurrentNodes",
            "NodeLabels",
        ],
    )


@pytest.mark.parametrize(
    "path,metric_column",
    [
        ("node_group/current_size.csv", "CurrentSize"),
        ("node_group/cpu_requests.csv", "CpuRequests"),
        ("node_group/memory_requests.csv", "MemoryRequests"),
        ("node_group/cpu_reservation_percent.csv", "CpuReservationPercent"),
        ("node_group/memory_reservation_percent.csv", "MemoryReservationPercent"),
    ],
)
def test_time_series_headers(archive, path, metric_column):
    assert_headers(archive.csv(path), ["ClusterName", "NodeGroupName", "MetricTime", metric_column])


# --- membership --------------------------------------------------------------


def test_both_workers_are_in_the_group(archive):
    row = archive.csv("node_group/attributes.csv").one(
        ClusterName="nodegroup-labels", NodeGroupName="mvp-workers"
    )
    expected = sorted(SCENARIO.cluster.node_names(SCENARIO.cluster_name)[1:])
    assert sorted(row["CurrentNodes"].split("|")) == expected
    assert float(row["CurrentSize"]) == pytest.approx(2)


def test_attributes_describe_a_node_group(archive):
    row = archive.csv("node_group/attributes.csv").one(
        ClusterName="nodegroup-labels", NodeGroupName="mvp-workers"
    )
    assert row["VirtualTechnology"] == "NodeGroup"
    assert row["VirtualDomain"] == "nodegroup-labels"


def test_current_size_holds_for_every_sample(archive):
    for row in archive.csv("node_group/current_size.csv"):
        assert float(row["CurrentSize"]) == pytest.approx(2), f"at {row['MetricTime']}"


# --- values against the cluster ----------------------------------------------
# Every sample is checked, not just one: the collector averages over the window,
# so a value that only holds at one instant is not the property under test.


@pytest.mark.parametrize(
    "path,column,resource,tolerance",
    [
        ("node_group/cpu_requests.csv", "CpuRequests", "cpu", CPU_TOLERANCE),
        ("node_group/memory_requests.csv", "MemoryRequests", "memory", MEMORY_TOLERANCE),
    ],
)
def test_requests_match_the_cluster(archive, oracle, path, column, resource, tolerance):
    expected = oracle.requests(resource, NODE_GROUP)
    for row in archive.csv(path):
        assert float(row[column]) == pytest.approx(expected, abs=tolerance), (
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
