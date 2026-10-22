"""GPU nodes the way a device plugin leaves them: extended resources, GPU labels and a DCGM exporter.

No NVIDIA device plugin runs. The harness advertises `nvidia.com/gpu` by patching node status capacity and
the kubelet mirrors it into allocatable. That is all the collector reads, through kube-state-metrics, so it
is equivalent for every collected metric. Worker 1 carries the GPU feature discovery labels of a 4-GPU A100
node, and a DCGM exporter fixture serves its four GPUs: two bound to the `trainer` container and one to each of
the two `infer` replicas, whose values differ. Worker 2 is labelled like a GKE GPU node with one T4 GPU; the fixture serves that GPU too, busy but bound
to no container. Worker 3 is a 1-GPU A100 node time-sliced four ways, as GPU feature discovery labels it: it advertises four
`nvidia.com/gpu` but holds one physical GPU, which the fixture serves unbound.
"""

from __future__ import annotations

import statistics
from collections import namedtuple

import pytest

from harness.archive import assert_metric_grid, expected_timestamps, label_map, prometheus_labels, row_label
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.oracle import quantity
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

NAME = "gpu-device-plugin"
NAMESPACE = "gdp-workload"
GROUP_LABEL = "topology.kubex.ai/node-group"
GROUP = "gdp-workers"
GPU = "nvidia.com/gpu"
_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]
# Without nvidia.com/gpu.present the collector ignores every other NVIDIA label (node/gpu.go:73-104)
W1_GPU_LABELS = {
    "nvidia.com/gpu.present": "true",
    "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-40GB",
    "nvidia.com/gpu.count": "4",
    "nvidia.com/gpu.memory": "40960",
    "nvidia.com/gpu.replicas": "1",
    "nvidia.com/gpu.sharing-strategy": "none",
    "nvidia.com/mig.capable": "true",
    "nvidia.com/mig.strategy": "none",
    "nvidia.com/mps.capable": "true",  # true, unlike Go's zero value, so ignoring the label fails
    "nvidia.com/vgpu.present": "false",
}
W2_GPU_LABELS = {"cloud.google.com/gke-accelerator": "nvidia-tesla-t4"}
# Time-slicing: gpu.count stays the physical GPUs, gpu.replicas is the slices per GPU and the product gains -SHARED
W3_GPU_LABELS = {
    "nvidia.com/gpu.present": "true",
    "nvidia.com/gpu.product": "NVIDIA-A100-SXM4-40GB-SHARED",
    "nvidia.com/gpu.count": "1",
    "nvidia.com/gpu.memory": "40960",
    "nvidia.com/gpu.replicas": "4",
    "nvidia.com/gpu.sharing-strategy": "time-slicing",
}
CLUSTER_SPEC = ClusterSpec(
    control_planes=[Node(labels=_SYSTEM_NODE)],
    workers=[
        Node(labels={GROUP_LABEL: GROUP, **W1_GPU_LABELS}, extended_resources={GPU: "4"}),
        Node(labels={GROUP_LABEL: GROUP, **W2_GPU_LABELS}, extended_resources={GPU: "1"}),
        Node(labels={GROUP_LABEL: GROUP, **W3_GPU_LABELS}, extended_resources={GPU: "4"}),  # 1 GPU x 4 replicas
    ],
)
NODES = CLUSTER_SPEC.node_names(CLUSTER_NAME_PREFIX + NAME)
CP, W1, W2, W3 = NODES  # workload.yaml pins W1 by hostname
GPU_LABELS = {CP: {}, W1: W1_GPU_LABELS, W2: W2_GPU_LABELS, W3: W3_GPU_LABELS}

# --- fixture inputs ----------------------------------------------------------

# DCGM modelName and FB_USED + FB_FREE in MiB (total less the driver's reserve, which the chart drops)
A100 = ("NVIDIA A100-SXM4-40GB", 40326)  # GFD's product label spells the model with dashes
T4 = ("Tesla T4", 15360)
MODEL, FB_TOTAL = A100
Gpu = namedtuple("Gpu", "node index card pod container util used power")  # util %, used MiB, power W
GPUS = [
    Gpu(W1, 0, A100, "trainer-0", "train", 30, 10000, 120),
    Gpu(W1, 1, A100, "trainer-0", "train", 50, 14000, 180),
    Gpu(W1, 2, A100, "infer-0", "serve", 20, 6000, 90),
    Gpu(W1, 3, A100, "infer-1", "serve", 60, 9000, 110),
    Gpu(W2, 0, T4, None, None, 70, 4000, 60),  # busy, but bound to no container the fixture names
    Gpu(W3, 0, A100, None, None, 50, 8000, 70),  # the one physical GPU behind W3's four slices, bound to nothing
]
CONTAINERS = {("trainer", "train"): ["trainer-0"], ("infer", "serve"): ["infer-0", "infer-1"]}  # (entity, container) -> pods


def _dcgm_series():
    series = []
    for g in GPUS:
        model, total = g.card
        labels = {
            "gpu": str(g.index),
            "UUID": f"GPU-{g.node[-1]}000000{g.index}-4000-8000-00000000000{g.index}",
            "device": f"nvidia{g.index}",
            "modelName": model,
            "Hostname": g.node,
        }
        if g.pod:
            labels |= {"namespace": NAMESPACE, "pod": g.pod, "container": g.container}
        for name, value in (
            ("DCGM_FI_DEV_GPU_UTIL", g.util),
            ("DCGM_FI_DEV_FB_USED", g.used),
            ("DCGM_FI_DEV_FB_FREE", total - g.used),
            ("DCGM_FI_DEV_POWER_USAGE", g.power),
        ):
            series.append(Metric(name, "gauge", float(value), labels=labels))
    return series


def _query(name, query, n):
    return PrometheusQuery(name, query, min_results=n)


# --- expectations ------------------------------------------------------------

CLUSTER = NAME
ALL_NODES = {"kubernetes.io/os": "linux"}
EXACT = {"rel": 0, "abs": 1e-6}  # gauges and their ratios, written with %f (print error 5e-7)
KEYS = ["ClusterName", "Namespace", "EntityName", "EntityType", "ContainerName"]
IDENTITIES = [(CLUSTER, NAMESPACE, e, "StatefulSet", c) for e, c in CONTAINERS]
GPU_COLUMNS = ["GpuLimit", "GpuRequest", "GpuLimitFloat", "GpuRequestFloat", "GpuModel", "GpuSharingStrategy"]
DCGM_STEMS = ["gpu_utilization", "gpu_utilization_gpus", "gpu_mem_utilization", "gpu_mem_used", "gpu_power_usage"]


def _bound(pod, container):
    return [g for g in GPUS if (g.pod, g.container) == (pod, container)]


def _on(node):
    return [g for g in GPUS if g.node == node]


# Truth for a container bound to N GPUs is per container instance (pod): utilization is the mean over its
# GPUs, memory used and power are sums, and memory utilization is total used over total memory. The owner
# aggregation (avg or max) then runs across the owner's replicas. This is the per-container aggregation the
# collector applies on the Kubex path (container/container.go:990-1001) and to a container's memory total
# (container/container.go:521,556). The gauges are constant, so a series is its own average and maximum.
def _container_truth(gpus, requested):
    util = statistics.mean(g.util for g in gpus)
    return {
        "gpu_utilization": util,
        "gpu_utilization_gpus": util * requested / 100,
        # sum used / sum total; equals the mean of per-GPU percentages only because a container's GPUs are identical here
        "gpu_mem_utilization": 100 * sum(g.used for g in gpus) / sum(g.card[1] for g in gpus),
        "gpu_mem_used": sum(g.used for g in gpus),
        "gpu_power_usage": sum(g.power for g in gpus),
    }


def _owner_truth(instances, aggregate):
    """``instances``: one (gpus, requested) per replica of the owner."""

    per_pod = [_container_truth(gpus, requested) for gpus, requested in instances]
    return {k: aggregate(p[k] for p in per_pod) for k in per_pod[0]}


def _node_truth(gpus):
    """Per node, from that node's GPUs (node/node.go:218-228)."""

    util = statistics.mean(g.util for g in gpus)
    return {
        "gpu_utilization": util,
        "gpu_utilization_gpus": util * len(gpus) / 100,  # physical GPUs: the node's DCGM series; under time-slicing allocatable counts slices
        "gpu_mem_utilization": statistics.mean(100 * g.used / g.card[1] for g in gpus),
        "gpu_mem_used": sum(g.used for g in gpus),
        "gpu_power_usage": sum(g.power for g in gpus),
    }


def _group_truth():
    """Members with DCGM series: utilization, gpus and mem utilization averaged over them, mem used and power summed
    (nodegroup/nodegroup.go:443-456). The gpus value is the per-node GPUs-busy averaged over members, in the same
    loop and wrapper as the group utilization; a group total would be 2.3."""

    nodes = [_node_truth(_on(n)) for n in (W1, W2, W3)]
    mean = {"gpu_utilization", "gpu_utilization_gpus", "gpu_mem_utilization"}
    return {k: (statistics.mean if k in mean else sum)(n[k] for n in nodes) for k in nodes[0]}


_TRAINER = [(_bound("trainer-0", "train"), 2)]  # requests from workload.yaml
_INFER = [(_bound("infer-0", "serve"), 1), (_bound("infer-1", "serve"), 1)]
# The formulas above, checked against hand-computed values
assert _owner_truth(_TRAINER, statistics.mean) == _owner_truth(_TRAINER, max) == pytest.approx(
    {"gpu_utilization": 40, "gpu_utilization_gpus": 0.8, "gpu_mem_utilization": 29.757476565987204,
     "gpu_mem_used": 24000, "gpu_power_usage": 300}
)  # fmt: skip
assert _owner_truth(_INFER, statistics.mean) == pytest.approx(
    {"gpu_utilization": 40, "gpu_utilization_gpus": 0.4, "gpu_mem_utilization": 18.598422853742,
     "gpu_mem_used": 7500, "gpu_power_usage": 100}
)  # fmt: skip
assert _owner_truth(_INFER, max) == pytest.approx(
    {"gpu_utilization": 60, "gpu_utilization_gpus": 0.6, "gpu_mem_utilization": 22.318107424490403,
     "gpu_mem_used": 9000, "gpu_power_usage": 110}
)  # fmt: skip
assert _node_truth(_on(W1)) == pytest.approx(
    {"gpu_utilization": 40, "gpu_utilization_gpus": 1.6, "gpu_mem_utilization": 24.177949709864606,
     "gpu_mem_used": 39000, "gpu_power_usage": 500}
)  # fmt: skip
assert _node_truth(_on(W2)) == pytest.approx(
    {"gpu_utilization": 70, "gpu_utilization_gpus": 0.7, "gpu_mem_utilization": 26.041666666666668,
     "gpu_mem_used": 4000, "gpu_power_usage": 60}
)  # fmt: skip
assert _node_truth(_on(W3)) == pytest.approx(
    {"gpu_utilization": 50, "gpu_utilization_gpus": 0.5, "gpu_mem_utilization": 19.838317710658135,
     "gpu_mem_used": 8000, "gpu_power_usage": 70}
)  # fmt: skip
assert _group_truth() == pytest.approx(
    {"gpu_utilization": 53.333333333333336, "gpu_utilization_gpus": 0.9333333333333332, "gpu_mem_utilization": 23.352644695729804,
     "gpu_mem_used": 51000, "gpu_power_usage": 630}
)  # fmt: skip

GPU_FILES = (
    "cluster/gpu_requests.csv",
    "cluster/gpu_reservation_percent.csv",
    "node/gpu_limit.csv",
    "node/gpu_request.csv",
    "node/gpu_utilization_avg.csv",
    "node/gpu_utilization_gpus_avg.csv",
    "node/gpu_mem_utilization_avg.csv",
    "node/gpu_mem_used_avg.csv",
    "node/gpu_power_usage_avg.csv",
    "node_group/gpu_requests.csv",
    "node_group/gpu_reservation_percent.csv",
    "node_group/gpu_utilization_avg.csv",
    "node_group/gpu_utilization_gpus_avg.csv",
    "node_group/gpu_mem_utilization_avg.csv",
    "node_group/gpu_mem_used_avg.csv",
    "node_group/gpu_power_usage_avg.csv",
    "container/gpu_limit.csv",
    "container/gpu_request.csv",
    *(f"container/{stem}_{agg}_workload.csv" for stem in DCGM_STEMS for agg in ("avg", "max")),
)
assert len(GPU_FILES) == 28

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
    # The Service name puts its EndpointSlice under the chart's .*dcgm.* keep rule.
    fixtures=[FixtureService(name="dcgm-exporter", metrics=_dcgm_series())],
    resources=["workload.yaml"],
    prometheus=PrometheusSpec(
        queries=[
            _query("gpu-allocatable", 'kube_node_status_allocatable{resource="nvidia_com_gpu"}', 3),
            _query("pods-running", f'kube_pod_status_phase{{namespace="{NAMESPACE}",phase="Running"}} == 1', 3),
            _query("owners", f'kube_pod_owner{{namespace="{NAMESPACE}",owner_kind="StatefulSet"}}', 3),
            _query("dcgm-gpus", "DCGM_FI_DEV_GPU_UTIL", len(GPUS)),  # four on W1, one each on W2 and W3
        ],
    ),
    forwarder=ForwarderSpec(
        # gauges only: avg_over_time(...[1m0s]) at AuditTime-60s needs samples from AuditTime-120s
        window_seconds=180,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    # No lifecycle Wait: the StatefulSet pods may not exist yet when kubectl wait runs; the queries wait instead.
    files=BASE_FILES | frozenset(GPU_FILES),
)

pytestmark = pytest.mark.pr


# --- Kubernetes truth, read from the snapshots --------------------------------


def _node(snapshot, name):
    return next(n for n in snapshot.nodes if n["metadata"]["name"] == name)


def _amount(node, field):
    """The node's GPU count as the collector prints it, "" when the node advertises none."""

    amounts = node["status"].get(field, {})
    return str(int(quantity(amounts[GPU], node["metadata"]["name"]))) if GPU in amounts else ""


def _gpu_quantity(oracle, pod, container, kind):
    spec = next(
        p for p in oracle.primary.pods if (p["metadata"]["namespace"], p["metadata"]["name"]) == (NAMESPACE, pod)
    )["spec"]
    resources = next(c for c in spec["containers"] if c["name"] == container)["resources"]
    return quantity(resources[kind][GPU], f"pod {pod} container {container} {kind}")


def _short(node):
    return node.removeprefix(f"{CLUSTER_NAME_PREFIX}{NAME}-")  # control-plane, worker, worker2


# --- archive helpers ----------------------------------------------------------


def _grid(archive):
    return expected_timestamps(archive.csv("cluster/config.csv").one(Name=NAME)["AuditTime"], SCENARIO.forwarder.collection)


def _series(path):
    """(truth key, value column) of a DCGM file: node/gpu_mem_used_avg.csv -> ("gpu_mem_used", "GpuMemUsedAvg")."""

    stem = path.rsplit("/", 1)[1].removesuffix(".csv").removesuffix("_workload")
    return stem.rsplit("_", 1)[0], "".join(word.capitalize() for word in stem.split("_"))


def _check_rows(archive, path, keys, identities, column, expected):
    """The grid is exactly the identities at the collector's times; each row's value is ``expected(row)``."""

    table = archive.csv(path)
    assert_metric_grid(table, keys, identities, _grid(archive))
    for row in table:
        assert float(row[column]) == pytest.approx(expected(row), **EXACT), f"{path} {row_label(row, keys)}"


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


# --- extended resources -------------------------------------------------------


def test_extended_resources_persist_through_the_window(run):
    """The kubelet never restores a capacity it did not register, so a reset at any point shows in every later snapshot."""

    labels = [s.label for s in run.snapshots]
    assert labels[0] == "collection window start" and labels[-1] == "after upload" and len(labels) >= 3, labels
    declared = {name: spec.extended_resources[GPU] for name, spec in zip((W1, W2, W3), CLUSTER_SPEC.workers)}
    for snapshot in run.snapshots:
        for field in ("capacity", "allocatable"):
            for name, count in declared.items():
                amounts = _node(snapshot, name)["status"][field]
                assert GPU in amounts, f"{name} {field} lost {GPU} at {snapshot.label}"
                assert quantity(amounts[GPU], name) == quantity(count, name), f"{name} {field} at {snapshot.label}"
            assert GPU not in _node(snapshot, CP)["status"][field], f"{CP} {field} at {snapshot.label}"


# --- node/ --------------------------------------------------------------------


def _expected_node_attributes(oracle, node):
    labels, gpus = GPU_LABELS[node], _on(node)
    selector = {"kubernetes.io/hostname": node}
    spec = _node(oracle.primary, node)
    allocatable = _amount(spec, "allocatable")

    def whole(value):  # no GPU requests means no series, so a blank attribute (node/collection.go:118-134)
        return str(int(value)) if value else ""

    return {
        "GpuLimit": whole(oracle.limits(GPU, selector)),
        "GpuRequest": whole(oracle.requests(GPU, selector)),
        "CapacityGpu": _amount(spec, "capacity"),
        "AllocatableGpu": allocatable,
        # W2 has no gpu.count label: its allocatable is the fallback (node/collection.go:85)
        "GpuTotal": labels.get("nvidia.com/gpu.count", allocatable),
        # W1: the label wins over DCGM's FB sum (node/collection.go:155-158). W2: no label, so DCGM's sum over its GPU
        "GpuMemoryTotal": labels.get("nvidia.com/gpu.memory", str(sum(g.card[1] for g in gpus)) if gpus else ""),
        # W2: no gpu.replicas label, so the allocatable fallback again (node/collection.go:86)
        "GpuReplicas": labels.get("nvidia.com/gpu.replicas", allocatable),
        # the GFD and GKE labels carry the vendor (node/gpu.go:82,92); DCGM plays no part
        "GpuVendor": "nvidia" if labels else "",
        # DCGM's modelName overrides the GFD product and the GKE accelerator labels (node/node.go:103-108)
        "GpuModel": gpus[0].card[0] if gpus else "",
        "GpuSharingStrategy": labels.get("nvidia.com/gpu.sharing-strategy", ""),
        "GpuMpsCapable": labels.get("nvidia.com/mps.capable", "false"),
        "GpuVgpuPresent": labels.get("nvidia.com/vgpu.present", "false"),
        "GpuMigCapable": labels.get("nvidia.com/mig.capable", "false"),
        "GpuMigStrategy": labels.get("nvidia.com/mig.strategy", ""),
    }


@pytest.mark.parametrize("node", NODES, ids=[_short(n) for n in NODES])
def test_node_gpu_attributes(archive, oracle, node):
    row = archive.csv("node/attributes.csv").one(ClusterName=CLUSTER, NodeName=node)
    expected = _expected_node_attributes(oracle, node)
    assert {c: row[c] for c in expected} == expected


@pytest.mark.parametrize("node", NODES, ids=[_short(n) for n in NODES])
def test_node_gpu_labels(archive, node):
    """GPU labels are split out of NodeLabels."""

    row = archive.csv("node/attributes.csv").one(ClusterName=CLUSTER, NodeName=node)
    assert label_map(row["GpuLabels"]) == prometheus_labels(GPU_LABELS[node])
    assert not set(label_map(row["GpuLabels"])) & set(label_map(row["NodeLabels"]))


@pytest.mark.parametrize("path", ["node/gpu_request.csv", "node/gpu_limit.csv"])
def test_node_gpu_request_and_limit_series(archive, oracle, path):
    column = "GpuRequest" if path.endswith("request.csv") else "GpuLimit"
    total = (oracle.requests if column == "GpuRequest" else oracle.limits)(GPU, {"kubernetes.io/hostname": W1})
    _check_rows(archive, path, ["ClusterName", "NodeName"], [(CLUSTER, W1)], column, lambda row: total)


DCGM_NODE_FILES = [f"node/{stem}_avg.csv" for stem in DCGM_STEMS]
DCGM_GROUP_FILES = [f"node_group/{stem}_avg.csv" for stem in DCGM_STEMS]


@pytest.mark.parametrize("path", DCGM_NODE_FILES)
def test_node_dcgm_series(archive, path):
    """The control plane has no DCGM series. W3 is time-sliced: GPUs busy count its one physical GPU, not its four slices."""

    key, column = _series(path)
    _check_rows(
        archive, path, ["ClusterName", "NodeName"], [(CLUSTER, n) for n in (W1, W2, W3)], column,
        lambda row: _node_truth(_on(row["NodeName"]))[key],
    )  # fmt: skip


# --- node_group/ and cluster/ --------------------------------------------------


@pytest.mark.parametrize("path", ["node_group/gpu_requests.csv", "node_group/gpu_reservation_percent.csv"])
def test_group_gpu_requests_and_reservation(archive, oracle, path):
    selector = {GROUP_LABEL: GROUP}
    column, total = (
        ("GpuRequests", oracle.requests(GPU, selector))
        if path.endswith("requests.csv")
        else ("GpuReservationPercent", oracle.reservation_percent(GPU, selector))
    )
    _check_rows(archive, path, ["ClusterName", "NodeGroupName"], [(CLUSTER, GROUP)], column, lambda row: total)


@pytest.mark.parametrize("path", DCGM_GROUP_FILES)
def test_group_dcgm_series(archive, path):
    """Utilization, gpus and memory utilization are the mean of the node values, memory used and power their sum."""

    key, column = _series(path)
    truth = _group_truth()[key]
    _check_rows(archive, path, ["ClusterName", "NodeGroupName"], [(CLUSTER, GROUP)], column, lambda row: truth)


@pytest.mark.parametrize("path", ["cluster/gpu_requests.csv", "cluster/gpu_reservation_percent.csv"])
def test_cluster_gpu_requests_and_reservation(archive, oracle, path):
    column, total = (
        ("GpuRequests", oracle.requests(GPU, ALL_NODES))
        if path.endswith("requests.csv")
        else ("GpuReservationPercent", oracle.reservation_percent(GPU, ALL_NODES))
    )
    _check_rows(archive, path, ["Name"], [(CLUSTER,)], column, lambda row: total)


# --- container/ ---------------------------------------------------------------


@pytest.mark.parametrize("target", TARGETS, ids=TARGET_IDS)
def test_container_gpu_attributes(archive, oracle, target):
    """The W2 GPU is attributed to nobody, and system containers have no GPU columns. The replicas of an owner
    request the same, so the first one speaks for the container."""

    expected = {c: "" for c in GPU_COLUMNS}
    if target != "others":
        container = target[1]
        pod = CONTAINERS[target][0]
        limit, request = (_gpu_quantity(oracle, pod, container, kind) for kind in ("limits", "requests"))
        expected = {
            "GpuLimit": str(int(limit)),
            "GpuRequest": str(int(request)),
            "GpuLimitFloat": f"{float(limit):f}",
            "GpuRequestFloat": f"{float(request):f}",
            "GpuModel": MODEL,
            "GpuSharingStrategy": _node(oracle.primary, W1)["metadata"]["labels"]["nvidia.com/gpu.sharing-strategy"],
        }
    for row in _rows(archive.csv("container/attributes.csv"), target):
        assert {c: row[c] for c in GPU_COLUMNS} == expected


@pytest.mark.parametrize("target", TARGETS, ids=TARGET_IDS)
def test_container_gpu_memory_total(archive, target):
    """One (node, modelName) series per pod sums all of that container's GPUs; the collector averages the series with
    integer division (container/output.go:61-64)."""

    totals = [] if target == "others" else [sum(g.card[1] for g in _bound(pod, target[1])) for pod in CONTAINERS[target]]
    expected = "" if target == "others" else str(sum(totals) // len(totals))
    for row in _rows(archive.csv("container/config.csv"), target):
        assert row["GpuMemoryTotal"] == expected


@pytest.mark.parametrize("path", ["container/gpu_request.csv", "container/gpu_limit.csv"])
def test_container_gpu_request_and_limit_series(archive, oracle, path):
    kind, column = ("requests", "GpuRequest") if path.endswith("request.csv") else ("limits", "GpuLimit")
    _check_rows(
        archive, path, KEYS, IDENTITIES, column,
        lambda row: float(_gpu_quantity(oracle, CONTAINERS[row["EntityName"], row["ContainerName"]][0], row["ContainerName"], kind)),
    )  # fmt: skip


CONTAINER_WORKLOAD_FILES = [
    f"container/{stem}_{agg}_workload.csv" for stem in DCGM_STEMS for agg in ("avg", "max")
]


@pytest.mark.parametrize("path", CONTAINER_WORKLOAD_FILES)
def test_container_dcgm_workloads_match_the_fixture(archive, oracle, path):
    """Per pod first, then the owner's average or maximum over its replicas: infer's two replicas differ, so
    average, maximum and sum are three different numbers."""

    key, column = _series(path)
    aggregate = {"Avg": statistics.mean, "Max": max}[column[-3:]]

    def expected(row):
        entity, container = row["EntityName"], row["ContainerName"]
        instances = [
            (_bound(pod, container), float(_gpu_quantity(oracle, pod, container, "requests")))
            for pod in CONTAINERS[entity, container]
        ]
        return _owner_truth(instances, aggregate)[key]

    _check_rows(archive, path, KEYS, IDENTITIES, column, expected)
