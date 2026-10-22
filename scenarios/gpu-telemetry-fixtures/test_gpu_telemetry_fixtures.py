"""Container GPU columns from the Kubex GPU exporter's `kubex_gpu_container_*` families.

This is a protocol and collector test, not an NVIDIA runtime test. A fixture named like the chart's exporter
serves two owners. `trainer-0/train` holds two whole GPUs with distinct per-GPU values and is labelled as a
plain scrape. The two replicas `infer-0/serve` and `infer-1/serve` each hold half a GPU under the KAI scheduler,
with different values, and are labelled the way a namespace-enforcing scrape (for example OpenShift
user-workload monitoring) rewrites an exporter's series: the workload identity sits in
`exported_namespace/pod/container`, and `namespace/pod/container` name the exporter. A DCGM fixture also runs,
with conflicting values for infer-0's GPU; the collector must take container data from
the Kubex exporter (internal/node/node.go:510-531). The DCGM fixture exports no `Hostname`, as dcgm-exporter
does with `--no-hostname`, so the node and node group DCGM files must take their node from the scrape's `node` label.

There is no `nvidia.com/gpu` request and no extended resource: on this path the collector reads container GPU
requests from the exporter, not from Kubernetes, and a fractional KAI share cannot be expressed as one anyway.
"""

from __future__ import annotations

import statistics
from collections import namedtuple

import pytest

from harness.archive import assert_metric_grid, expected_timestamps, row_label
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

NAME = "gpu-telemetry-fixtures"
NAMESPACE = "gtf-workload"
GROUP_LABEL = "topology.kubex.ai/node-group"
GROUP = "gtf-workers"
_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]
# Without the present label the sharing strategy is never read (node/gpu.go:79-80)
CLUSTER_SPEC = ClusterSpec(
    control_planes=[Node(labels=_SYSTEM_NODE)],
    workers=[
        Node(
            labels={
                GROUP_LABEL: GROUP,
                "nvidia.com/gpu.present": "true",
                "nvidia.com/gpu.sharing-strategy": "time-slicing",
            }
        )
    ],
)
CP, W1 = CLUSTER_SPEC.node_names(CLUSTER_NAME_PREFIX + NAME)  # workload.yaml pins W1 by hostname

# --- fixture inputs ----------------------------------------------------------

# Series shape of gpu-process-exporter v1.1.1 (pkg/prometheus/prometheus.go:86-103): container-level families
# carry node, namespace, pod, container, container_id and gpu_allocation_type, one series per container;
# per-device families add gpu_uuid and gpu_model, one series per container per GPU. There is no gpu index
# label, and gpu_allocation_type is always set ("K8sResource" by default).
GIB, MIB = 2**30, 2**20
START = 1000.0  # counters start positive, so rate() never sees a reset
KUBEX_MODEL = "NVIDIA A100-SXM4-40GB"
GPU_MEMORY = 40 * GIB  # kubex_gpu_container_memory_total_bytes of each GPU (40960 MiB)
EXPORTER_POD, EXPORTER_CONTAINER = "gpu-process-exporter-x7k2p", "exporter"
Share = namedtuple("Share", "entity container pod requests limits allocation exported gpus")
SHARES = [
    # gpus: index -> (SM utilization %, memory used MiB); the index only builds gpu_uuid
    Share("trainer", "train", "trainer-0", 2.0, 2.0, "K8sResource", False, {0: (40, 8192), 1: (60, 12288)}),
    Share("infer", "serve", "infer-0", 0.5, 1.0, "KaiScheduler", True, {2: (25, 6144)}),
    Share("infer", "serve", "infer-1", 0.5, 1.0, "KaiScheduler", True, {3: (75, 10240)}),
]


def _identity(share):
    if share.exported:  # namespace comes from the scrape target: the fixture's own namespace
        return {
            "exported_namespace": NAMESPACE,
            "exported_pod": share.pod,
            "exported_container": share.container,
            "pod": EXPORTER_POD,
            "container": EXPORTER_CONTAINER,
        }
    return {"namespace": NAMESPACE, "pod": share.pod, "container": share.container}


def _kubex_series():
    series = []
    for share in SHARES:
        container = {
            **_identity(share),
            "node": W1,
            "container_id": f"containerd://{share.pod}-{share.container}",
            "gpu_allocation_type": share.allocation,
        }
        series.append(Metric("kubex_gpu_container_requests", "gauge", share.requests, labels=container))
        series.append(Metric("kubex_gpu_container_limits", "gauge", share.limits, labels=container))
        for index, (util, used) in share.gpus.items():
            gpu = {**container, "gpu_uuid": f"GPU-0000000{index}-a100-4000-8000-00000000000{index}", "gpu_model": KUBEX_MODEL}
            series += [
                Metric("kubex_gpu_container_sm_utilization_percent_seconds_total", "counter", START, labels=gpu, rate=float(util)),
                Metric("kubex_gpu_container_memory_bytes", "gauge", float(used * MIB), labels=gpu),
                Metric("kubex_gpu_container_memory_total_bytes", "gauge", float(GPU_MEMORY), labels=gpu),
                Metric("kubex_gpu_container_memory_footprint_percent", "gauge", 100 * used * MIB / GPU_MEMORY, labels=gpu),
            ]
    return series


# Conflicting DCGM view of infer-0's GPU (GPU 2): every value differs from the Kubex one.
DCGM_FB_TOTAL = 40326
DCGM = {"util": 90, "used": 30000, "power": 250, "model": "NVIDIA A100-PCIE-40GB"}


def _dcgm_series():
    labels = {
        "gpu": "2",
        "UUID": "GPU-00000002-a100-4000-8000-000000000002",
        "device": "nvidia2",
        "modelName": DCGM["model"],
        # As dcgm-exporter --no-hostname exports it: no Hostname. node is what the chart's scrape relabel sets for an
        # exporter pod on W1; exposed here because one fixture pod serves it, and honor_labels keeps it.
        "node": W1,
        "namespace": NAMESPACE,
        "pod": "infer-0",
        "container": "serve",
    }
    return [
        Metric(name, "gauge", float(value), labels=labels)
        for name, value in (
            ("DCGM_FI_DEV_GPU_UTIL", DCGM["util"]),
            ("DCGM_FI_DEV_FB_USED", DCGM["used"]),
            ("DCGM_FI_DEV_FB_FREE", DCGM_FB_TOTAL - DCGM["used"]),
            ("DCGM_FI_DEV_POWER_USAGE", DCGM["power"]),
        )
    ]


def _query(name, query, n):
    return PrometheusQuery(name, query, min_results=n)


# --- expectations ------------------------------------------------------------

CLUSTER = NAME
RATE = {"rel": 1e-2}  # rate() of a constant-rate counter is off only by scrape-latency jitter (~1e-4); every defect here moves a value by >= 20 %
EXACT = {"rel": 0, "abs": 1e-6}  # gauges and their ratios, written with %f (print error 5e-7)
INSTANCES = {}  # (entity, container) -> its shares, one per replica
for _share in SHARES:
    INSTANCES.setdefault((_share.entity, _share.container), []).append(_share)
CONTAINERS = list(INSTANCES)
KEYS = ["ClusterName", "Namespace", "EntityName", "EntityType", "ContainerName"]
IDENTITIES = [(CLUSTER, NAMESPACE, e, "StatefulSet", c) for e, c in CONTAINERS]
GPU_COLUMNS = ["GpuLimit", "GpuRequest", "GpuLimitFloat", "GpuRequestFloat", "GpuModel", "GpuSharingStrategy"]


def _truth(share):
    """(value, tolerance) per series of one replica: a container's utilization is the mean over its GPUs, memory
    used the sum, and memory utilization is total used over total memory."""

    util = statistics.mean(u for u, _ in share.gpus.values())
    used = sum(m for _, m in share.gpus.values())
    return {
        "gpu_utilization": (util, RATE),
        "gpu_utilization_gpus": (util * share.requests / 100, RATE),
        "gpu_mem_utilization": (100 * used * MIB / (len(share.gpus) * GPU_MEMORY), EXACT),
        "gpu_mem_used": (used, EXACT),
    }


def _owner_truth(shares, aggregate):
    """The owner's average or maximum over its replicas, per series."""

    per_pod = [_truth(share) for share in shares]
    return {k: (aggregate(p[k][0] for p in per_pod), per_pod[0][k][1]) for k in per_pod[0]}


def _values(entity, container, aggregate):
    return {k: v for k, (v, _) in _owner_truth(INSTANCES[entity, container], aggregate).items()}


# The formulas above, checked against hand-computed values
assert _values("trainer", "train", statistics.mean) == pytest.approx(
    {"gpu_utilization": 50, "gpu_utilization_gpus": 1.0, "gpu_mem_utilization": 25.0, "gpu_mem_used": 20480}
)
assert _values("infer", "serve", statistics.mean) == pytest.approx(
    {"gpu_utilization": 50, "gpu_utilization_gpus": 0.25, "gpu_mem_utilization": 20.0, "gpu_mem_used": 8192}
)
assert _values("infer", "serve", max) == pytest.approx(
    {"gpu_utilization": 75, "gpu_utilization_gpus": 0.375, "gpu_mem_utilization": 25.0, "gpu_mem_used": 10240}
)

# The node and node group files come from the DCGM fixture; their identity and values are asserted here for the
# no-Hostname case (gpu-device-plugin asserts them with a Hostname).
GPU_FILES = (
    "container/gpu_limit.csv",
    "container/gpu_request.csv",
    *(f"container/{stem}_{agg}_workload.csv" for stem in ("gpu_utilization", "gpu_utilization_gpus", "gpu_mem_utilization", "gpu_mem_used") for agg in ("avg", "max")),
    "node/gpu_utilization_avg.csv",
    "node/gpu_mem_utilization_avg.csv",
    "node/gpu_mem_used_avg.csv",
    "node/gpu_power_usage_avg.csv",
    "node_group/gpu_utilization_avg.csv",
    "node_group/gpu_mem_utilization_avg.csv",
    "node_group/gpu_mem_used_avg.csv",
    "node_group/gpu_power_usage_avg.csv",
)
assert len(GPU_FILES) == 18

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
    # The chart's gpu-process-exporter stays disabled; the fixture takes its name, so its EndpointSlice is kept.
    fixtures=[
        FixtureService(name="gpu-process-exporter", metrics=_kubex_series()),
        FixtureService(name="dcgm-exporter", metrics=_dcgm_series()),
    ],
    resources=["workload.yaml"],
    prometheus=PrometheusSpec(
        queries=[
            # the exporter identity survived the scrape, so only the exported_ mapping can attribute the infer series
            _query("kubex-exported", f'kubex_gpu_container_requests{{exported_namespace="{NAMESPACE}",namespace="kubex",pod="{EXPORTER_POD}"}}', 2),
            _query("kubex-gpus", "kubex_gpu_container_sm_utilization_percent_seconds_total", 4),
            _query("dcgm", f'DCGM_FI_DEV_GPU_UTIL{{node="{W1}",Hostname=""}}', 1),
            _query("pods-running", f'kube_pod_status_phase{{namespace="{NAMESPACE}",phase="Running"}} == 1', 3),
            _query("owners", f'kube_pod_owner{{namespace="{NAMESPACE}",owner_kind="StatefulSet"}}', 3),
            _query("sharing-label", 'kube_node_labels{label_nvidia_com_gpu_sharing_strategy="time-slicing"}', 1),
        ],
    ),
    forwarder=ForwarderSpec(
        # rate(...[1m20s]) inside a [1m:20s] subquery at AuditTime-60s needs samples from AuditTime-180s
        window_seconds=240,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    # No lifecycle Wait: the StatefulSet pods may not exist yet when kubectl wait runs; the queries wait instead.
    files=BASE_FILES | frozenset(GPU_FILES),
)


# --- archive helpers ----------------------------------------------------------


def _grid(archive):
    return expected_timestamps(archive.csv("cluster/config.csv").one(Name=NAME)["AuditTime"], SCENARIO.forwarder.collection)


def _node(snapshot, name):
    return next(n for n in snapshot.nodes if n["metadata"]["name"] == name)


TARGETS = [*CONTAINERS, "others"]
TARGET_IDS = [f"{e}-{c}" for e, c in CONTAINERS] + ["others"]


def _rows(table, target):
    """The row of one tracked container, or every row of the others."""

    if target == "others":
        tracked = {(NAMESPACE, e, c) for e, c in CONTAINERS}
        rows = [r for r in table if (r["Namespace"], r["EntityName"], r["ContainerName"]) not in tracked]
        assert rows, f"{table.path} has no rows besides the tracked containers"
        return rows
    entity, container = target
    return [table.one(ClusterName=CLUSTER, Namespace=NAMESPACE, EntityName=entity, ContainerName=container)]


# --- container/ ---------------------------------------------------------------


@pytest.mark.parametrize("target", TARGETS, ids=TARGET_IDS)
def test_container_gpu_attributes(archive, oracle, target):
    """The fraction of a GPU is in the float column; the integer column is int(0.5)=0 (container/output.go:144-157).
    The sharing strategy is the KAI scheduler's, or else the node's."""

    expected = {c: "" for c in GPU_COLUMNS}
    if target != "others":
        share = INSTANCES[target][0]  # the replicas of an owner are requested alike
        node_strategy = _node(oracle.primary, W1)["metadata"]["labels"]["nvidia.com/gpu.sharing-strategy"]
        expected = {
            "GpuLimit": str(int(share.limits)),
            "GpuRequest": str(int(share.requests)),
            "GpuLimitFloat": f"{share.limits:f}",
            "GpuRequestFloat": f"{share.requests:f}",
            "GpuModel": KUBEX_MODEL,
            "GpuSharingStrategy": "kai-scheduler" if share.allocation == "KaiScheduler" else node_strategy,
        }
    for row in _rows(archive.csv("container/attributes.csv"), target):
        assert {c: row[c] for c in GPU_COLUMNS} == expected


@pytest.mark.parametrize("target", TARGETS, ids=TARGET_IDS)
def test_container_gpu_memory_total(archive, target):
    """Kubex is the cluster's container exporter, so the memory total is the sum of the container's GPUs there,
    averaged over the owner's replicas with integer division (container/output.go:61-64)."""

    totals = [] if target == "others" else [len(share.gpus) * GPU_MEMORY // MIB for share in INSTANCES[target]]
    expected = "" if target == "others" else str(sum(totals) // len(totals))
    for row in _rows(archive.csv("container/config.csv"), target):
        assert row["GpuMemoryTotal"] == expected


@pytest.mark.parametrize("path", ["container/gpu_request.csv", "container/gpu_limit.csv"])
def test_container_gpu_request_and_limit_series(archive, path):
    column, field = ("GpuRequest", "requests") if path.endswith("request.csv") else ("GpuLimit", "limits")
    table = archive.csv(path)
    assert_metric_grid(table, KEYS, IDENTITIES, _grid(archive))
    for row in table:
        want = getattr(INSTANCES[row["EntityName"], row["ContainerName"]][0], field)
        assert float(row[column]) == pytest.approx(want, **EXACT), f"{path} {row_label(row, KEYS)}"


KUBEX_WORKLOAD_FILES = [
    f"container/{stem}_{agg}_workload.csv"
    for stem in ("gpu_utilization", "gpu_utilization_gpus", "gpu_mem_utilization", "gpu_mem_used")
    for agg in ("avg", "max")
]


@pytest.mark.parametrize("path", KUBEX_WORKLOAD_FILES)
def test_kubex_workloads_match_the_fixture(archive, path):
    """Per pod first, then the owner's average or maximum over its replicas. Infer's values come from Kubex (not
    DCGM's 90 and 30000), and no power file exists although DCGM serves power."""

    stem = path.rsplit("/", 1)[1].removesuffix("_workload.csv")
    key, agg = stem.rsplit("_", 1)
    aggregate = {"avg": statistics.mean, "max": max}[agg]
    column = "".join(word.capitalize() for word in stem.split("_"))
    table = archive.csv(path)
    assert_metric_grid(table, KEYS, IDENTITIES, _grid(archive))
    for row in table:
        want, tolerance = _owner_truth(INSTANCES[row["EntityName"], row["ContainerName"]], aggregate)[key]
        assert float(row[column]) == pytest.approx(want, **tolerance), f"{path} {row_label(row, KEYS)}"


# --- node/ and node_group/: DCGM without a Hostname ------------------------------

DCGM_NODE = {  # one GPU on W1, so its node average and sum are its own values (node/node.go:218-228)
    "gpu_utilization": DCGM["util"],
    "gpu_mem_utilization": 100 * DCGM["used"] / DCGM_FB_TOTAL,
    "gpu_mem_used": DCGM["used"],
    "gpu_power_usage": DCGM["power"],
}


def _check_dcgm_series(archive, scope, keys, identity, stem):
    column = "".join(word.capitalize() for word in stem.split("_")) + "Avg"
    table = archive.csv(f"{scope}/{stem}_avg.csv")
    assert_metric_grid(table, keys, [identity], _grid(archive))
    for row in table:
        assert float(row[column]) == pytest.approx(DCGM_NODE[stem], **EXACT), f"{scope}/{stem} at {row['MetricTime']}"


@pytest.mark.parametrize("stem", DCGM_NODE)
def test_dcgm_node_series_keep_the_scrape_node(archive, stem):
    """Without a Hostname label the series still belong to the node the scrape gave them."""

    _check_dcgm_series(archive, "node", ["ClusterName", "NodeName"], (CLUSTER, W1), stem)


@pytest.mark.parametrize("stem", DCGM_NODE)
def test_dcgm_group_series_keep_the_scrape_node(archive, stem):
    """A node group of that one node averages and sums to the node's values."""

    _check_dcgm_series(archive, "node_group", ["ClusterName", "NodeGroupName"], (CLUSTER, GROUP), stem)
