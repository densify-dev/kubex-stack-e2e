"""Reservation pressure across nodes, node groups and the cluster.

Two workers in one node group reserve CPU and memory for the system through the kubelet, so their
allocatable is below their capacity. A third worker, in another group and tainted, reserves
nothing. Each worker runs one pod with its own requests and limits. The collector's node, node
group and cluster files must match the sums and percentages Kubernetes itself reports.

Pressure here means reservation (allocatable below capacity); kubelet node conditions are not
collected and not used.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.archive import (
    assert_distinct,
    assert_metric_grid,
    assert_unique_keys,
    expected_timestamps,
    label_map,
    prometheus_labels,
)
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.oracle import quantity
from harness.spec import (
    ClusterSpec,
    Collection,
    ForwarderSpec,
    Node,
    PrometheusQuery,
    PrometheusSpec,
    ScenarioSpec,
    StackSpec,
    Taint,
    Wait,
)

HERE = Path(__file__).parent
GROUP_LABEL = "topology.kubex.ai/node-group"
# The collector reads HwModel from this label (internal/node/node.go:297); Kind sets none of its own.
INSTANCE_TYPE = "node.kubernetes.io/instance-type"
WORKLOAD_NAMESPACE = "node-pressure-workload"
DEDICATED = Taint(key="stack-e2e.kubex.ai/dedicated", value="pressure", effect="NoSchedule")
RESERVED = {"system-reserved": "cpu=1,memory=1Gi"}

# Units and rounding: the collector writes time-series values with %f (six decimal places), so
# the worst case error on a raw value is 5e-7. Node and cluster requests and limits are in mcores
# and MiB; node group requests are in cores and MiB. Percentages are compared to two decimals.
VALUE_TOLERANCE = 1e-6
PERCENT_TOLERANCE = 0.01
MIB = 2**20

_SYSTEM_NODE = {"stack-e2e-system": "system"}
_POOL_A = {GROUP_LABEL: "pool-a", INSTANCE_TYPE: "e2e.pool-a"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]

SCENARIO = ScenarioSpec(
    name="node-pressure",
    cluster=ClusterSpec(
        control_planes=[Node(labels={**_SYSTEM_NODE, INSTANCE_TYPE: "e2e.system"})],
        workers=[
            Node(labels=_POOL_A, kubelet=RESERVED),
            Node(labels=_POOL_A, kubelet=RESERVED),
            Node(labels={GROUP_LABEL: "pool-b", INSTANCE_TYPE: "e2e.pool-b"}, taints=[DEDICATED]),
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
                "pool-labels",
                'kube_node_labels{label_topology_kubex_ai_node_group=~"pool-.*"}',
                min_results=3,
            ),
            PrometheusQuery(
                "dedicated-taint",
                'kube_node_spec_taint{key="stack-e2e.kubex.ai/dedicated"}',
                min_results=1,
            ),
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
        job_name="node-pressure-forwarder",
        window_seconds=180,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    resources=["workload.yaml"],
    lifecycle=[
        Wait(kind="pod", name=name, namespace=WORKLOAD_NAMESPACE, condition="Ready", timeout=300)
        for name in ("on-worker", "on-worker2", "on-worker3")
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

CLUSTER = SCENARIO.name
NODES = SCENARIO.cluster.node_names(SCENARIO.cluster_name)
WORKERS = NODES[1:]
GROUPS = {"pool-a": WORKERS[:2], "pool-b": WORKERS[2:]}
ALL_NODES = {"kubernetes.io/os": "linux"}

pytestmark = pytest.mark.pr


@pytest.fixture(scope="session")
def run(stack_run):
    with stack_run(SCENARIO, HERE) as active:
        yield active


def _node(name):
    return {"kubernetes.io/hostname": name}


def _group(name):
    return {GROUP_LABEL: name}


def _spec_of(oracle, name):
    return next(n for n in oracle.primary.nodes if n["metadata"]["name"] == name)


def _status(oracle, name, field, resource):
    return float(quantity(_spec_of(oracle, name)["status"][field][resource], f"node {name} {field} {resource}"))


def _audit_time(archive):
    return archive.csv("cluster/config.csv").one(Name=CLUSTER)["AuditTime"]


def _grid(archive):
    return expected_timestamps(_audit_time(archive), SCENARIO.forwarder.collection)


# --- premise: the reservation is real, and distinct per node ------------------


def test_reserved_workers_have_less_allocatable_than_capacity(oracle):
    """pool-a gives up exactly 1 CPU and 1Gi more than the unreserved pool-b worker (which only loses
    the kubelet's default memory eviction threshold)."""

    def lost(name, resource):
        return _status(oracle, name, "capacity", resource) - _status(oracle, name, "allocatable", resource)

    for name in GROUPS["pool-a"]:
        assert lost(name, "cpu") == pytest.approx(1)
        assert lost(name, "memory") - lost(GROUPS["pool-b"][0], "memory") == pytest.approx(2**30)
    assert lost(GROUPS["pool-b"][0], "cpu") == 0


def test_workers_have_distinct_requests_and_limits(oracle):
    """The three pods differ, so a collector that mixed nodes up would not go unnoticed."""

    for kind in ("requests", "limits"):
        for resource in ("cpu", "memory"):
            sums = {getattr(oracle, kind)(resource, _node(name)) for name in WORKERS}
            assert len(sums) == len(WORKERS), f"{kind} of {resource} are not distinct across workers: {sums}"


# --- node/ -------------------------------------------------------------------

NODE_SERIES = [
    # path, column, calculation, resource, factor from the oracle's cores/bytes
    ("node/cpu_request.csv", "CpuRequest", "requests", "cpu", 1000),
    ("node/memory_request.csv", "MemoryRequest", "requests", "memory", 1 / MIB),
    ("node/cpu_limit.csv", "CpuLimit", "limits", "cpu", 1000),
    ("node/memory_limit.csv", "MemoryLimit", "limits", "memory", 1 / MIB),
    ("node/cpu_reservation_percent.csv", "CpuReservationPercent", "reservation_percent", "cpu", 1),
    ("node/memory_reservation_percent.csv", "MemoryReservationPercent", "reservation_percent", "memory", 1),
    ("node/pod_count.csv", "PodCount", "pod_count", None, 1),
]


def _expected(oracle, calculation, resource, name):
    selector = _node(name)
    if calculation == "pod_count":
        return oracle.pod_count(selector)
    return getattr(oracle, calculation)(resource, selector)


def _reporting_nodes(oracle, calculation, resource):
    """Nodes the collector writes a row for: those with a pod (pod_count), a request (reservation
    percent) or a limit (limits). Kubernetes exposes no series for an absent request or limit."""

    probe = {"reservation_percent": "requests"}.get(calculation, calculation)
    return [name for name in NODES if _expected(oracle, probe, resource, name) > 0]


@pytest.mark.parametrize("path", ["node/config.csv", "node/attributes.csv"])
def test_every_node_is_described_once(archive, path):
    table = archive.csv(path)
    assert_distinct(table, "ClusterName", [CLUSTER])
    assert_distinct(table, "NodeName", NODES)
    assert len(table) == len(NODES), f"{path}: entity table should hold one row per node"
    assert_unique_keys(table, ["ClusterName", "NodeName"])


@pytest.mark.parametrize("path,column,calculation,resource,factor", NODE_SERIES, ids=[row[1] for row in NODE_SERIES])
def test_node_series_follow_the_collector_grid(archive, oracle, path, column, calculation, resource, factor):
    identities = [(CLUSTER, name) for name in _reporting_nodes(oracle, calculation, resource)]
    assert_metric_grid(archive.csv(path), ["ClusterName", "NodeName"], identities, _grid(archive))


@pytest.mark.parametrize("path,column,calculation,resource,factor", NODE_SERIES, ids=[row[1] for row in NODE_SERIES])
def test_node_series_match_the_cluster(archive, oracle, path, column, calculation, resource, factor):
    tolerance = PERCENT_TOLERANCE if calculation == "reservation_percent" else VALUE_TOLERANCE
    for row in archive.csv(path):
        expected = _expected(oracle, calculation, resource, row["NodeName"]) * factor
        assert float(row[column]) == pytest.approx(expected, abs=tolerance), (
            f"{path} {row['NodeName']} at {row['MetricTime']}"
        )


def _config(archive, name):
    return archive.csv("node/config.csv").one(NodeName=name)


@pytest.mark.parametrize("name", NODES)
def test_node_config_hardware(archive, oracle, name):
    """Memory is in MiB here (internal/node/output.go:41-44); physical CPUs repeat the CPU count and
    cores per CPU and threads per core are fixed at 1 (output.go:45)."""

    row = _config(archive, name)
    cpus = int(_status(oracle, name, "capacity", "cpu"))
    assert int(row["HwTotalCpus"]) == cpus
    assert int(row["HwTotalPhysicalCpus"]) == cpus
    assert (row["HwCoresPerCpu"], row["HwThreadsPerCore"]) == ("1", "1")
    assert int(row["HwTotalMemory"]) == int(_status(oracle, name, "capacity", "memory")) // MIB


@pytest.mark.parametrize("name", NODES)
def test_node_config_os_name(archive, oracle, name):
    expected = _spec_of(oracle, name)["status"]["nodeInfo"]["operatingSystem"]
    assert _config(archive, name)["OsName"] == expected


@pytest.mark.parametrize("name", NODES)
def test_node_config_model_and_network(archive, oracle, name):
    """HwModel is the instance-type label; with no node exporter the network speed is blank."""

    row = _config(archive, name)
    expected = _spec_of(oracle, name)["metadata"]["labels"][INSTANCE_TYPE]
    assert (row["HwModel"], row["HwMaxNetworkIoBps"]) == (expected, "")


def _attributes(archive, name):
    return archive.csv("node/attributes.csv").one(NodeName=name)


@pytest.mark.parametrize("name", NODES)
def test_node_attributes_identify_the_node(archive, oracle, name):
    row, node = _attributes(archive, name), _spec_of(oracle, name)
    assert row["VirtualTechnology"] == "Nodes"
    assert row["VirtualDomain"] == CLUSTER
    assert row["OsArchitecture"] == node["status"]["nodeInfo"]["architecture"]
    assert row["ProviderId"] == node["spec"]["providerID"]
    assert row["K8sVersion"] == node["status"]["nodeInfo"]["kubeletVersion"]


ATTRIBUTES = [
    # column, calculation, resource, factor from the oracle's cores/bytes
    ("CpuRequest", "requests", "cpu", 1000),
    ("MemoryRequest", "requests", "memory", 1 / MIB),
    ("CpuLimit", "limits", "cpu", 1000),
    ("MemoryLimit", "limits", "memory", 1 / MIB),
]


# The collector reads the last sample and truncates it with Go's int() (internal/node/collection.go:29-31).
@pytest.mark.parametrize("column,calculation,resource,factor", ATTRIBUTES, ids=[row[0] for row in ATTRIBUTES])
@pytest.mark.parametrize("name", NODES)
def test_node_attribute_requests_and_limits(archive, oracle, name, column, calculation, resource, factor):
    expected = int(getattr(oracle, calculation)(resource, _node(name)) * factor)
    assert int(_attributes(archive, name)[column]) == expected


@pytest.mark.parametrize("name", NODES)
def test_node_attribute_capacity_and_allocatable(archive, oracle, name):
    """Counts and CPU in whole units, memory and ephemeral storage in bytes."""

    row = _attributes(archive, name)
    for field, prefix in (("capacity", "Capacity"), ("allocatable", "Allocatable")):
        for resource, column in (
            ("pods", "Pods"),
            ("cpu", "Cpu"),
            ("memory", "Memory"),
            ("ephemeral-storage", "EphemeralStorage"),
        ):
            expected = int(_status(oracle, name, field, resource))
            assert int(row[prefix + column]) == expected, f"{prefix}{column} of {name}"


@pytest.mark.parametrize("name", NODES)
def test_node_attribute_taints(archive, oracle, name):
    taints = _spec_of(oracle, name)["spec"].get("taints", [])
    expected = sorted(
        f"{t['key']}={t['value']}:{t['effect']}" if t.get("value") else f"{t['key']}:{t['effect']}" for t in taints
    )
    actual = sorted(filter(None, _attributes(archive, name)["NodeTaints"].split("|")))
    assert actual == expected


def _shared_labels(oracle, names):
    """Labels, as Prometheus names them, that every one of the nodes carries with the same value."""

    labels = [prometheus_labels(_spec_of(oracle, name)["metadata"]["labels"]).items() for name in names]
    return dict(set.intersection(*map(set, labels)))


@pytest.mark.parametrize("name", NODES)
def test_node_attribute_labels(archive, oracle, name):
    """Every Kubernetes label of the node. NodeLabels also carries the series' own labels (job, instance and
    kube-state-metrics' pod labels), which are not node labels and are not compared."""

    collected = label_map(_attributes(archive, name)["NodeLabels"])
    node_labels = {key: value for key, value in collected.items() if key.startswith("label_")}
    assert node_labels == _shared_labels(oracle, [name])


# --- node_group/ -------------------------------------------------------------

GROUP_TIME_SERIES = [
    "node_group/current_size.csv",
    "node_group/cpu_requests.csv",
    "node_group/memory_requests.csv",
    "node_group/cpu_reservation_percent.csv",
    "node_group/memory_reservation_percent.csv",
]
GROUP_KEYS = ["ClusterName", "NodeGroupName"]
GROUP_IDENTITIES = [(CLUSTER, group) for group in GROUPS]


def _group_row(archive, path, group):
    return archive.csv(path).one(ClusterName=CLUSTER, NodeGroupName=group)


@pytest.mark.parametrize("path", ["node_group/config.csv", "node_group/attributes.csv"])
def test_node_groups_are_exactly_the_two_pools(archive, path):
    table = archive.csv(path)
    assert_distinct(table, "ClusterName", [CLUSTER])
    assert_distinct(table, "NodeGroupName", list(GROUPS))
    assert len(table) == len(GROUPS), f"{path}: entity table should hold one row per node group"
    assert_unique_keys(table, GROUP_KEYS)


@pytest.mark.parametrize("path", GROUP_TIME_SERIES)
def test_node_group_series_follow_the_collector_grid(archive, path):
    assert_metric_grid(archive.csv(path), GROUP_KEYS, GROUP_IDENTITIES, _grid(archive))


@pytest.mark.parametrize("group", GROUPS)
def test_node_group_membership(archive, group):
    row = _group_row(archive, "node_group/attributes.csv", group)
    assert row["VirtualTechnology"] == "NodeGroup"
    assert row["VirtualDomain"] == CLUSTER
    assert sorted(row["CurrentNodes"].split("|")) == sorted(GROUPS[group])
    assert float(row["CurrentSize"]) == pytest.approx(len(GROUPS[group]))


def test_current_size_holds_for_every_sample(archive):
    for row in archive.csv("node_group/current_size.csv"):
        expected = len(GROUPS[row["NodeGroupName"]])
        assert float(row["CurrentSize"]) == pytest.approx(expected), f"{row['NodeGroupName']} at {row['MetricTime']}"


# Every sample is checked, not just one: the collector averages over the window.
@pytest.mark.parametrize(
    "path,column,resource,divisor",
    [
        ("node_group/cpu_requests.csv", "CpuRequests", "cpu", 1),
        ("node_group/memory_requests.csv", "MemoryRequests", "memory", MIB),
    ],
)
def test_group_requests_match_the_cluster(archive, oracle, path, column, resource, divisor):
    for row in archive.csv(path):
        expected = oracle.requests(resource, _group(row["NodeGroupName"])) / divisor
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
def test_group_reservation_percent_matches_the_cluster(archive, oracle, path, column, resource):
    for row in archive.csv(path):
        expected = oracle.reservation_percent(resource, _group(row["NodeGroupName"]))
        assert float(row[column]) == pytest.approx(expected, abs=PERCENT_TOLERANCE), (
            f"{path} {row['NodeGroupName']} at {row['MetricTime']}"
        )


# node_group/attributes.csv holds whole numbers, CPU in mcores and memory in MiB, truncated with
# Go's int() (internal/nodegroup/nodegroup.go:58).
@pytest.mark.parametrize("column,calculation,resource,factor", ATTRIBUTES, ids=[row[0] for row in ATTRIBUTES])
@pytest.mark.parametrize("group", GROUPS)
def test_group_attribute_requests_and_limits(archive, oracle, group, column, calculation, resource, factor):
    expected = int(getattr(oracle, calculation)(resource, _group(group)) * factor)
    assert int(_group_row(archive, "node_group/attributes.csv", group)[column]) == expected


@pytest.mark.parametrize("group", GROUPS)
def test_group_config_hardware(archive, oracle, group):
    """Unlike node/config.csv (MiB), HwTotalMemory here is the raw sum of kube_node_status_capacity
    bytes over the group (internal/nodegroup/nodegroup.go:71-78, :364-366, :113)."""

    row = _group_row(archive, "node_group/config.csv", group)
    cpus = int(oracle.capacity("cpu", _group(group)))
    assert int(row["HwTotalCpus"]) == cpus
    assert int(row["HwTotalPhysicalCpus"]) == cpus
    assert (row["HwCoresPerCpu"], row["HwThreadsPerCore"]) == ("1", "1")
    assert int(row["HwTotalMemory"]) == int(oracle.capacity("memory", _group(group)))


@pytest.mark.parametrize("group", GROUPS)
def test_group_config_os_and_model(archive, oracle, group):
    shared = _shared_labels(oracle, GROUPS[group])
    row = _group_row(archive, "node_group/config.csv", group)
    assert (row["OsName"], row["HwModel"]) == (
        shared["label_kubernetes_io_os"],
        shared["label_node_kubernetes_io_instance_type"],
    )


@pytest.mark.parametrize("group", GROUPS)
def test_group_attribute_labels(archive, oracle, group):
    """NodeLabels merges the members' labels, keeping the first value seen (internal/common/common.go:39),
    so only labels every member shares have a defined value."""

    collected = label_map(_group_row(archive, "node_group/attributes.csv", group)["NodeLabels"])
    missing = {k: v for k, v in _shared_labels(oracle, GROUPS[group]).items() if collected.get(k) != v}
    assert not missing, f"shared labels missing or wrong: {missing}"


# --- cluster/ ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["cluster/config.csv", "cluster/attributes.csv"])
def test_cluster_is_described_once(archive, path):
    assert [row["Name"] for row in archive.csv(path)] == [CLUSTER]



def test_cluster_attributes_match_the_cluster(archive, oracle):
    """Whole mcores and MiB, truncated (internal/cluster/cluster.go:63-69)."""

    row = archive.csv("cluster/attributes.csv").one(Name=CLUSTER)
    assert row["VirtualTechnology"] == "Clusters"
    assert row["VirtualDomain"] == CLUSTER
    assert int(row["CpuRequest"]) == int(oracle.requests("cpu", ALL_NODES) * 1000)
    assert int(row["MemoryRequest"]) == int(oracle.requests("memory", ALL_NODES) / MIB)
    assert int(row["CpuLimit"]) == int(oracle.limits("cpu", ALL_NODES) * 1000)
    assert int(row["MemoryLimit"]) == int(oracle.limits("memory", ALL_NODES) / MIB)
    versions = {_spec_of(oracle, name)["status"]["nodeInfo"]["kubeletVersion"] for name in NODES}
    assert versions == {row["K8sVersion"]}


@pytest.mark.parametrize(
    "path",
    [
        "cluster/cpu_requests.csv",
        "cluster/memory_requests.csv",
        "cluster/cpu_reservation_percent.csv",
        "cluster/memory_reservation_percent.csv",
    ],
)
def test_cluster_series_follow_the_collector_grid(archive, path):
    assert_metric_grid(archive.csv(path), ["Name"], [(CLUSTER,)], _grid(archive))


@pytest.mark.parametrize(
    "path,column,resource,divisor",
    [
        ("cluster/cpu_requests.csv", "CpuRequests", "cpu", 1),
        ("cluster/memory_requests.csv", "MemoryRequests", "memory", MIB),
    ],
)
def test_cluster_requests_match_the_cluster(archive, oracle, path, column, resource, divisor):
    expected = oracle.requests(resource, ALL_NODES) / divisor
    for row in archive.csv(path):
        assert float(row[column]) == pytest.approx(expected, abs=VALUE_TOLERANCE), f"{path} at {row['MetricTime']}"


@pytest.mark.parametrize(
    "path,column,resource",
    [
        ("cluster/cpu_reservation_percent.csv", "CpuReservationPercent", "cpu"),
        ("cluster/memory_reservation_percent.csv", "MemoryReservationPercent", "memory"),
    ],
)
def test_cluster_reservation_percent_matches_the_cluster(archive, oracle, path, column, resource):
    expected = oracle.reservation_percent(resource, ALL_NODES)
    for row in archive.csv(path):
        assert float(row[column]) == pytest.approx(expected, abs=PERCENT_TOLERANCE), f"{path} at {row['MetricTime']}"
