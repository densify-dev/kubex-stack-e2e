"""A node exporter scraped as kube-prometheus-stack does it, with no `node` label.

`instance` is the exporter pod's IP and port, so the collector maps each series to a node through the pod's
IP (internal/node/node.go:552-568). Two workers in one label-derived node group each run a pinned exporter
fixture; node, group and cluster usage files must hold the per-node fixture values. Node-group membership and
resource totals are covered by node-pressure and nodegroup-roles.
"""

from __future__ import annotations

import statistics

import pytest
import yaml

from harness.archive import assert_metric_grid, expected_timestamps
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.spec import (
    BASE_FILES,
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

NODE_GROUP = {"topology.kubex.ai/node-group": "mvp-workers"}

MIB = 2**20

_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]

CLUSTER_SPEC = ClusterSpec(
    control_planes=[Node(labels=_SYSTEM_NODE)],
    workers=[Node(labels=NODE_GROUP), Node(labels=NODE_GROUP)],
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
        ],
    ),
    forwarder=ForwarderSpec(
        window_seconds=180,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    files=BASE_FILES | frozenset(EXPORTER_FILES),
)

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


@pytest.mark.parametrize("path", ["node/memory_ws.csv", "node/memory_ws_utilization.csv"])
def test_working_set_series_follow_the_collector_grid(archive, path):
    """The real cAdvisor root cgroup of every node, so only identities and the grid are checked (node.go:621,650)."""

    audit_time = archive.csv("node_group/config.csv").one(**ROW)["AuditTime"]
    identities = [("nodegroup-labels", n) for n in (CP, W1, W2)]
    assert_metric_grid(archive.csv(path), NODE_KEYS, identities, expected_timestamps(audit_time, SCENARIO.forwarder.collection))
