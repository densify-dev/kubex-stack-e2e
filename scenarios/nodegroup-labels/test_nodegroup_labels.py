"""Label-derived node-group membership and resource metrics.

Two ordinary Kind workers share one configured node-group label and one label the
collector is not configured to group on. The collector must produce exactly one
node group, containing both workers, with requests, limits, capacity and reservation
percentages matching what Kubernetes itself reports.

The node exporter is scraped as kube-prometheus-stack does it (no `node` label, `instance` = pod IP and port),
so the collector maps each series to a node through the exporter pod's IP (internal/node/node.go:552-568).
"""

from __future__ import annotations

import statistics
from pathlib import Path

import pytest
import yaml

from harness.archive import (
    assert_distinct,
    assert_metric_grid,
    assert_unique_keys,
    expected_timestamps,
)
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
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

CLUSTER_SPEC = ClusterSpec(
    control_planes=[Node(labels=_SYSTEM_NODE)],
    workers=[
        Node(labels={**NODE_GROUP, "ignored.kubex.ai/group": "ignored-value"}),
        Node(labels={**NODE_GROUP, "ignored.kubex.ai/group": "ignored-value"}),
    ],
)
CP, W1, W2 = CLUSTER_SPEC.node_names(CLUSTER_NAME_PREFIX + "nodegroup-labels")

# --- fixture inputs ----------------------------------------------------------

GIB = 2**30
START = 1000.0  # every counter starts positive
# (user, system) CPU-seconds per second for each of a node's two CPUs; idle = 1 - user - system
EXPORTER_CPU = {W1: [(0.40, 0.10), (0.20, 0.10)], W2: [(0.50, 0.20), (0.30, 0.20)]}
EXPORTER_MEMORY = {  # node_memory_<field>_bytes
    W1: {"MemTotal": 16 * GIB, "MemFree": 5 * GIB, "Cached": 3 * GIB, "Buffers": 512 * MIB, "SReclaimable": 256 * MIB},
    W2: {"MemTotal": 32 * GIB, "MemFree": 12 * GIB, "Cached": 6 * GIB, "Buffers": 1 * GIB, "SReclaimable": 512 * MIB},
}
EXPORTERS = {W1: "pod-ip-node-exporter-1", W2: "pod-ip-node-exporter-2"}  # fixture names; the pods carry "node-exporter"


def _exporter_series(node):  # no node label: the series are attributed by the serving pod's IP
    counters = [
        Metric("node_cpu_seconds_total", "counter", START, labels={"cpu": str(cpu), "mode": mode}, rate=rate)
        for cpu, (user, system) in enumerate(EXPORTER_CPU[node])
        for mode, rate in (("user", user), ("system", system), ("idle", 1 - user - system))
    ]
    return counters + [Metric(f"node_memory_{f}_bytes", "gauge", float(v)) for f, v in EXPORTER_MEMORY[node].items()]


# A pod target with no node relabel, so series carry only instance=<pod IP>:9100. This replaces the chart's
# app-metrics generator, which emits nothing without selectors.
POD_IP_SCRAPE = yaml.safe_dump(
    [
        {
            "job_name": "pod-ip-node-exporter",
            "kubernetes_sd_configs": [{"role": "pod", "namespaces": {"names": ["kubex"]}}],
            "relabel_configs": [
                {
                    "source_labels": ["__meta_kubernetes_pod_label_app_kubex_ai_fixture"],
                    "regex": "pod-ip-node-exporter-.*",
                    "action": "keep",
                }
            ],
        }
    ],
    sort_keys=False,
)

EXPORTER_FILES = (
    "node/cpu_utilization.csv", "node/memory_raw_bytes.csv", "node/memory_utilization.csv",
    "node/memory_actual_workload.csv", "node/memory_actual_utilization.csv", "node/memory_ws.csv",
    "node/memory_ws_utilization.csv", "node_group/cpu_utilization.csv", "node_group/memory_raw_bytes.csv",
    "node_group/memory_actual_workload.csv", "cluster/cpu_utilization.csv", "cluster/memory_raw_bytes.csv",
    "cluster/memory_actual_workload.csv",
)  # fmt: skip

SCENARIO = ScenarioSpec(
    name="nodegroup-labels",
    cluster=CLUSTER_SPEC,
    stack=StackSpec(
        helm_overrides={
            "stack": {"densify": {"createSecret": False}, "prometheus": {"deploy": True}},
            "gpu-process-exporter": {"enabled": False},
            "beyla": {"enabled": False},
            "node-labeler": {"enabled": False},
            "k8s-ephemeral-storage-metrics": {"enabled": False},
            "prometheus": {
                "extraScrapeConfigs": POD_IP_SCRAPE,
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
    fixtures=[FixtureService(name=EXPORTERS[n], metrics=_exporter_series(n), node=n) for n in (W1, W2)],
    prometheus=PrometheusSpec(
        queries=[
            PrometheusQuery(
                "exporter-without-node",
                'count by (instance) (node_memory_MemTotal_bytes{job="pod-ip-node-exporter",node=""})',
                min_results=2,
            ),
            PrometheusQuery("exporter-pods", 'kube_pod_info{pod=~".*node-exporter.*",pod_ip!=""}', min_results=2),
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
        **dict.fromkeys(EXPORTER_FILES, "populated"),
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

# --- exporter expectations (FX) ------------------------------------------------
# RATE: irate of a constant-rate counter differs from the rate by scrape-time jitter. EXACT: gauges and ratios of
# exact integers, written with %f.
RATE = {"rel": 1e-2}
EXACT = {"rel": 0, "abs": 1e-6}
WORKERS = [W1, W2]
_ACTUAL = ("MemFree", "Cached", "Buffers", "SReclaimable")  # SReclaimable is added once (node/node.go:633-644)


def _memory(n, *subtract):
    m = EXPORTER_MEMORY[n]
    return m["MemTotal"] - sum(m[f] for f in subtract)


# stem, column, value per node, how node_group/ and cluster/ combine the workers (None: node/ only), tolerance
FAMILIES = [
    # CPU utilization is averaged over nodes on purpose (nodegroup/nodegroup.go:387)
    ("cpu_utilization", "CpuUtilization", lambda n: 100 * sum(u + s for u, s in EXPORTER_CPU[n]) / len(EXPORTER_CPU[n]), statistics.mean, RATE),
    ("memory_raw_bytes", "MemoryBytes", lambda n: _memory(n, "MemFree"), sum, EXACT),
    ("memory_utilization", "MemoryUtilization", lambda n: 100 * _memory(n, "MemFree") / EXPORTER_MEMORY[n]["MemTotal"], None, EXACT),
    ("memory_actual_workload", "MemoryActualWorkload", lambda n: _memory(n, *_ACTUAL), sum, EXACT),
    ("memory_actual_utilization", "MemoryActualUtilization", lambda n: 100 * _memory(n, *_ACTUAL) / EXPORTER_MEMORY[n]["MemTotal"], None, EXACT),
]  # fmt: skip
NODE_KEYS, GROUP_KEYS, CLUSTER_KEYS = ["ClusterName", "NodeName"], KEYS, ["Name"]


def _cases():
    """(path, keys, column, expected value per identity, tolerance) for every fixture-valued file."""

    for stem, column, per_node, combine, tolerance in FAMILIES:
        values = {n: per_node(n) for n in WORKERS}
        yield f"node/{stem}.csv", NODE_KEYS, column, {("nodegroup-labels", n): v for n, v in values.items()}, tolerance
        if combine:
            group = {IDENTITIES[0]: combine(values.values())}
            yield f"node_group/{stem}.csv", GROUP_KEYS, column, group, tolerance
            yield f"cluster/{stem}.csv", CLUSTER_KEYS, column, {("nodegroup-labels",): group[IDENTITIES[0]]}, tolerance


CASES = list(_cases())
# the hand values the fixture inputs must give: CPU in percent, memory in bytes
HAND = {
    "node/cpu_utilization.csv": [40, 60], "node/memory_raw_bytes.csv": [11 * GIB, 20 * GIB],
    "node/memory_utilization.csv": [68.75, 62.5], "node/memory_actual_workload.csv": [7.25 * GIB, 12.5 * GIB],
    "node/memory_actual_utilization.csv": [45.3125, 39.0625], "node_group/cpu_utilization.csv": [50],
    "node_group/memory_raw_bytes.csv": [31 * GIB], "node_group/memory_actual_workload.csv": [19.75 * GIB],
    "cluster/cpu_utilization.csv": [50], "cluster/memory_raw_bytes.csv": [31 * GIB],
    "cluster/memory_actual_workload.csv": [19.75 * GIB],
}  # fmt: skip
assert {path: list(expected.values()) for path, _, _, expected, _ in CASES} == {p: pytest.approx(v, abs=1e-6) for p, v in HAND.items()}

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
    assert_unique_keys(table, KEYS)


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


def test_config_total_cpus(archive, oracle):
    row = archive.csv("node_group/config.csv").one(**ROW)
    assert int(row["HwTotalCpus"]) == int(oracle.capacity("cpu", NODE_GROUP))


def test_config_total_memory_in_bytes(archive, oracle):
    """Unlike requests and limits (/1024/1024) and node/config.csv (MiB), this is the raw sum of
    kube_node_status_capacity bytes over the group (internal/nodegroup/nodegroup.go:71-78, :364-366, :113)."""

    row = archive.csv("node_group/config.csv").one(**ROW)
    assert int(row["HwTotalMemory"]) == int(oracle.capacity("memory", NODE_GROUP))


def test_config_os_name(archive, oracle):
    systems = {
        node["status"]["nodeInfo"]["operatingSystem"]
        for node in oracle.primary.nodes
        if NODE_GROUP.items() <= node["metadata"]["labels"].items()
    }
    assert len(systems) == 1, f"node group members report several operating systems: {sorted(systems)!r}"
    assert archive.csv("node_group/config.csv").one(**ROW)["OsName"] == systems.pop()


# --- node exporter scraped by pod IP -------------------------------------------


def test_pod_ip_exporters_run_on_their_nodes(run):
    """The collector maps a series to a node through the node of the pod that served it."""

    for snapshot in run.snapshots:
        for node, fixture in EXPORTERS.items():
            pods = [
                p
                for p in snapshot.pods
                if p["metadata"]["namespace"] == "kubex"
                and p["metadata"].get("labels", {}).get("app.kubex.ai/fixture") == fixture
                and p["status"].get("phase") == "Running"
            ]
            assert [(p["spec"]["nodeName"], bool(p["status"].get("podIP"))) for p in pods] == [(node, True)], f"{fixture} at {snapshot.label}"


@pytest.mark.parametrize("path,keys,column,expected,tolerance", CASES, ids=[case[0].removesuffix(".csv") for case in CASES])
def test_pod_ip_exporter_series(archive, path, keys, column, expected, tolerance):
    table = archive.csv(path)
    audit_time = archive.csv("node_group/config.csv").one(**ROW)["AuditTime"]
    assert_metric_grid(table, keys, expected, expected_timestamps(audit_time, SCENARIO.forwarder.collection))
    for row in table:
        assert float(row[column]) == pytest.approx(expected[tuple(row[k] for k in keys)], **tolerance), f"{path} at {row['MetricTime']}"
