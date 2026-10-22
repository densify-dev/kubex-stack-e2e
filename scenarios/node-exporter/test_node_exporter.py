"""Node exporter metrics: every node, node group and cluster file the exporter feeds.

A fixture Service named like the chart's real node exporter serves known series for every Kind node,
each with a `node` label, so the collector attributes them by node (internal/node/node.go:401-434).
The two workers form one node group and the control plane is in none, so group results must exclude
the control plane while cluster results include it. Disk and network devices the collector filters
out are served too, with rates far above the real ones, so a missing filter changes the result.
"""

from __future__ import annotations

import statistics

import pytest

from harness.archive import assert_metric_grid, expected_timestamps
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

NAME = "node-exporter"
GROUP_LABEL = "topology.kubex.ai/node-group"
GROUP = "ne-workers"
_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]
CLUSTER_SPEC = ClusterSpec(
    control_planes=[Node(labels=_SYSTEM_NODE)],
    workers=[Node(labels={GROUP_LABEL: GROUP}), Node(labels={GROUP_LABEL: GROUP})],
)
NODES = CLUSTER_SPEC.node_names(CLUSTER_NAME_PREFIX + NAME)
CP, W1, W2 = NODES
WORKERS = [W1, W2]

# --- fixture inputs ----------------------------------------------------------

GIB, MIB = 2**30, 2**20
SCALE = {CP: 1, W1: 2, W2: 4}  # multiplies every disk, NIC, OOM and throttle rate below
# (user, system) CPU-seconds per second for each node's two CPUs; idle = 1 - user - system
CPU = {CP: [(0.10, 0.05), (0.10, 0.05)], W1: [(0.40, 0.10), (0.20, 0.10)], W2: [(0.50, 0.20), (0.30, 0.20)]}
MEMORY = {  # node_memory_<field>_bytes gauges
    CP: {"MemTotal": 8 * GIB, "MemFree": 2 * GIB, "Cached": 1 * GIB, "Buffers": 256 * MIB, "SReclaimable": 128 * MIB},
    W1: {"MemTotal": 16 * GIB, "MemFree": 5 * GIB, "Cached": 3 * GIB, "Buffers": 512 * MIB, "SReclaimable": 256 * MIB},
    W2: {"MemTotal": 32 * GIB, "MemFree": 12 * GIB, "Cached": 6 * GIB, "Buffers": 1 * GIB, "SReclaimable": 512 * MIB},
}
SPEED = {CP: 125_000_000, W1: 1_250_000_000, W2: 3_125_000_000}  # node_network_speed_bytes{device="eth0"}
# per-second base rates (x SCALE): read_bytes, written_bytes, reads_completed, writes_completed
DISKS = {"vda": (100_000, 300_000, 10, 30), "vdb": (50_000, 100_000, 5, 10)}
# per-second base rates (x SCALE): receive_bytes, transmit_bytes, receive_packets, transmit_packets
NICS = {"eth0": (1_000_000, 500_000, 1_000, 500)}
# Same on every node, not scaled. The collector excludes dm-.* disks and veth.* NICs
# (node.go:253-268, 271-286), and the fake speed is above every eth0 so a broken filter wins the max.
DISK_DECOY = ("dm-0", (10_000_000, 10_000_000, 1_000, 1_000))
NIC_DECOY = ("veth1a2b3c", (50_000_000, 50_000_000, 50_000, 50_000))
NIC_DECOY_SPEED = 5_000_000_000
# The real exporter always exports the loopback device and exports no speed for it.
LOOPBACK = ("lo", (300_000, 300_000, 300, 300))
OOM_KILLS = 0.05  # node_vmstat_oom_kill per second (x SCALE)
THROTTLES = (0.05, 0.10)  # node_cpu_core_throttles_total per second for core 0 and core 1 (x SCALE)
START = 1000.0  # every counter starts positive, so the zero clamp of increase() never applies


def _node_exporter_series():
    series = []
    for n in NODES:
        k = SCALE[n]

        def gauge(name, value, **labels):
            series.append(Metric(name, "gauge", float(value), labels={"node": n, **labels}))

        def counter(name, rate, **labels):
            series.append(Metric(name, "counter", START, labels={"node": n, **labels}, rate=rate))

        for cpu, (user, system) in enumerate(CPU[n]):
            for mode, rate in (("user", user), ("system", system), ("idle", 1 - user - system)):
                counter("node_cpu_seconds_total", rate, cpu=str(cpu), mode=mode)
        for field, value in MEMORY[n].items():
            gauge(f"node_memory_{field}_bytes", value)
        gauge("node_network_speed_bytes", SPEED[n], device="eth0")
        gauge("node_network_speed_bytes", NIC_DECOY_SPEED, device=NIC_DECOY[0])
        for device, rates, scale in [*((d, r, k) for d, r in NICS.items()), (*NIC_DECOY, 1), (*LOOPBACK, 1)]:
            for what, rate in zip(("receive_bytes", "transmit_bytes", "receive_packets", "transmit_packets"), rates):
                counter(f"node_network_{what}_total", rate * scale, device=device)
        for device, rates, scale in [*((d, r, k) for d, r in DISKS.items()), (*DISK_DECOY, 1)]:
            for what, rate in zip(("read_bytes", "written_bytes", "reads_completed", "writes_completed"), rates):
                counter(f"node_disk_{what}_total", rate * scale, device=device)
        counter("node_vmstat_oom_kill", OOM_KILLS * k)
        for core, rate in enumerate(THROTTLES):
            counter("node_cpu_core_throttles_total", rate * k, core=str(core), package="0")
    return series


# --- expectations ------------------------------------------------------------

# Tolerances. RATE: irate of a constant-rate counter differs from the rate by scrape-time jitter (about
# 1e-4); every defect moves a value by at least 10%. EXACT: gauges and ratios of exact integers, written
# with %f (5e-7). COUNT: round(increase(c[1m])) over the 60s sample, which Prometheus extrapolates to
# exactly 60 r. The collector widens the range to [1m20s] (common/prometheus.go:675-697) and reports
# 80 r, so the two COUNT families are expected to fail until that is fixed.
RATE = {"rel": 1e-2}
EXACT = {"rel": 0, "abs": 1e-6}
COUNT = {"rel": 0, "abs": 0}


def _count(rate):
    n = round(60 * rate)
    assert abs(n - 60 * rate) < 1e-9, f"{rate} per second does not give a whole count over 60s"
    return n


def _sum_rates(index, devices=DISKS):
    return lambda n: sum(rates[index] for rates in devices.values()) * SCALE[n]


def _memory(n, *subtract):
    m = MEMORY[n]
    return m["MemTotal"] - sum(m[f] for f in subtract)


_ACTUAL = ("MemFree", "Cached", "Buffers", "SReclaimable")  # SReclaimable is added once (node.go:633-644)
# stem, column, value per node, how node_group/ and cluster/ combine nodes (None: node/ only), tolerance
FAMILIES = [
    # CPU utilization is averaged over nodes on purpose (nodegroup.go:387)
    ("cpu_utilization", "CpuUtilization", lambda n: 100 * sum(u + s for u, s in CPU[n]) / len(CPU[n]), statistics.mean, RATE),
    ("memory_raw_bytes", "MemoryBytes", lambda n: _memory(n, "MemFree"), sum, EXACT),
    ("memory_utilization", "MemoryUtilization", lambda n: 100 * _memory(n, "MemFree") / MEMORY[n]["MemTotal"], None, EXACT),
    ("memory_actual_workload", "MemoryActualWorkload", lambda n: _memory(n, *_ACTUAL), sum, EXACT),
    ("memory_actual_utilization", "MemoryActualUtilization", lambda n: 100 * _memory(n, *_ACTUAL) / MEMORY[n]["MemTotal"], None, EXACT),
    ("oom_kill_events", "OomKillEvents", lambda n: _count(OOM_KILLS * SCALE[n]), None, COUNT),
    ("cpu_throttling_events", "CpuThrottlingEvents", lambda n: sum(_count(r * SCALE[n]) for r in THROTTLES), None, COUNT),
    ("disk_read_bytes", "DiskReadBytes", _sum_rates(0), sum, RATE),
    ("disk_write_bytes", "DiskWriteBytes", _sum_rates(1), sum, RATE),
    ("disk_total_bytes", "DiskTotalBytes", lambda n: _sum_rates(0)(n) + _sum_rates(1)(n), sum, RATE),
    ("disk_read_ops", "DiskReadOps", _sum_rates(2), sum, RATE),
    ("disk_write_ops", "DiskWriteOps", _sum_rates(3), sum, RATE),
    ("disk_total_ops", "DiskTotalOps", lambda n: _sum_rates(2)(n) + _sum_rates(3)(n), sum, RATE),
    # Only eth0: the loopback never leaves the host, so it is not traffic the NIC speed bounds
    ("net_received_bytes", "NetReceivedBytes", _sum_rates(0, NICS), sum, RATE),
    ("net_sent_bytes", "NetSentBytes", _sum_rates(1, NICS), sum, RATE),
    ("net_total_bytes", "NetTotalBytes", lambda n: _sum_rates(0, NICS)(n) + _sum_rates(1, NICS)(n), sum, RATE),
    ("net_received_packets", "NetReceivedPackets", _sum_rates(2, NICS), sum, RATE),
    ("net_sent_packets", "NetSentPackets", _sum_rates(3, NICS), sum, RATE),
    ("net_total_packets", "NetTotalPackets", lambda n: _sum_rates(2, NICS)(n) + _sum_rates(3, NICS)(n), sum, RATE),
]

NODE_KEYS = (["ClusterName", "NodeName"], [(NAME, n) for n in NODES])
GROUP_KEYS = (["ClusterName", "NodeGroupName"], [(NAME, GROUP)])
CLUSTER_KEYS = (["Name"], [(NAME,)])


def _cases():
    """(path, identity column, expected value per identity, value column, tolerance) for every fixture-valued file."""

    for stem, column, per_node, combine, tolerance in FAMILIES:
        values = {n: per_node(n) for n in NODES}
        yield f"node/{stem}.csv", "NodeName", values, column, tolerance
        if combine:
            yield f"node_group/{stem}.csv", "NodeGroupName", {GROUP: combine(values[n] for n in WORKERS)}, column, tolerance
            yield f"cluster/{stem}.csv", "Name", {NAME: combine(values.values())}, column, tolerance


CASES = list(_cases())
# memory_ws is the real cAdvisor root cgroup and memory_ws_utilization divides it by allocatable memory
EXPORTER_FILES = [case[0] for case in CASES] + ["node/memory_ws.csv", "node/memory_ws_utilization.csv"]
assert len(EXPORTER_FILES) == 51

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
    # The chart's real node exporter is disabled; this Service has its name, so Prometheus keeps its target.
    fixtures=[FixtureService(name="kubex-prometheus-node-exporter", metrics=_node_exporter_series())],
    prometheus=PrometheusSpec(
        queries=[
            # 3 only if the exposition node label survived
            PrometheusQuery("exporter-node-labels", "count by (node) (node_memory_MemTotal_bytes)", min_results=3),
            PrometheusQuery("exporter-cpus", 'node_cpu_seconds_total{mode="idle"}', min_results=6),
            PrometheusQuery("group-labels", f'kube_node_labels{{label_topology_kubex_ai_node_group="{GROUP}"}}', min_results=2),
            PrometheusQuery("root-working-set", 'container_memory_working_set_bytes{id="/"}', min_results=3),
            PrometheusQuery("allocatable-memory", 'kube_node_status_allocatable{resource="memory"}', min_results=3),
        ],
    ),
    forwarder=ForwarderSpec(
        # the collector queries increase(...[1m20s]), which needs samples from 80s before the first grid point
        window_seconds=240,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    files=BASE_FILES | frozenset(EXPORTER_FILES),
)


def _grid(archive):
    return expected_timestamps(archive.csv("cluster/config.csv").one(Name=NAME)["AuditTime"], SCENARIO.forwarder.collection)


def _keys(path):
    return {"node": NODE_KEYS, "node_group": GROUP_KEYS, "cluster": CLUSTER_KEYS}[path.split("/")[0]]


def test_node_exporter_is_detected(archive):
    """Without it the collector logs this once per entity and writes no exporter file (node.go:233-236)."""

    assert "prometheus node exporter metrics not present" not in archive.raw("log.txt").decode()


@pytest.mark.parametrize("path", EXPORTER_FILES)
def test_exporter_series_follow_the_collector_grid(archive, path):
    keys, identities = _keys(path)
    assert_metric_grid(archive.csv(path), keys, identities, _grid(archive))


@pytest.mark.parametrize(
    "path,identity,expected,column,tolerance", CASES, ids=[case[0].removesuffix(".csv") for case in CASES]
)
def test_values_match_the_fixture(archive, path, identity, expected, column, tolerance):
    rows = list(archive.csv(path))
    assert rows, f"{path} has no rows"
    for row in rows:
        assert float(row[column]) == pytest.approx(expected[row[identity]], **tolerance), (
            f"{path} {row[identity]} at {row['MetricTime']}"
        )


def test_working_set_utilization_uses_allocatable_memory(archive, oracle):
    """Divided by Kubernetes allocatable memory, not the exporter's MemTotal (node.go:625,651,660-668)."""

    working_set = {(r["NodeName"], r["MetricTime"]): float(r["MemoryWs"]) for r in archive.csv("node/memory_ws.csv")}
    rows = list(archive.csv("node/memory_ws_utilization.csv"))
    assert rows
    for row in rows:
        node = next(n for n in oracle.primary.nodes if n["metadata"]["name"] == row["NodeName"])
        allocatable = float(quantity(node["status"]["allocatable"]["memory"], row["NodeName"]))
        expected = working_set[row["NodeName"], row["MetricTime"]] / allocatable * 100
        assert float(row["MemoryWsUtilization"]) == pytest.approx(expected, **EXACT), (
            f"{row['NodeName']} at {row['MetricTime']}"
        )


# The collector reads the last sample of max(<metric>) by (node, instance) and truncates it with int()
# (node.go:90-100). The veth decoy's larger speed is excluded from the max (node.go:94).
@pytest.mark.parametrize("node", NODES)
def test_node_network_speed_and_memory_total(archive, node):
    assert archive.csv("node/config.csv").one(NodeName=node)["HwMaxNetworkIoBps"] == str(SPEED[node])
    attributes = archive.csv("node/attributes.csv").one(NodeName=node)
    assert attributes["NetworkSpeed"] == str(SPEED[node])
    assert attributes["MemoryTotalBytes"] == str(MEMORY[node]["MemTotal"])
