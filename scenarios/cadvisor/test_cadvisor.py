"""Container usage files: CPU, memory, disk, throttling and the two custom sizing families.

Real kubelet cAdvisor values are never exact, so the fixture replaces the cAdvisor scrape job: a
Service serves known counters and gauges for the pods of a two-replica StatefulSet and a bare Pod,
labelled like cAdvisor's (namespace, pod, container), and Prometheus scrapes it under the production
job name. The custom families are served at twice the native values, so they cannot be satisfied by
reading native data. Pod-level cgroup series (no container label) are served too; the collector must
ignore them.
"""

from __future__ import annotations

import statistics
from collections import namedtuple
from pathlib import Path

import pytest

from harness.archive import assert_metric_grid, expected_timestamps
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.spec import (
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
NAME = "cadvisor"
NAMESPACE = "cadvisor-workload"
GROUP_LABEL = "topology.kubex.ai/node-group"
_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]
CLUSTER_SPEC = ClusterSpec(control_planes=[Node(labels=_SYSTEM_NODE)], workers=[Node(labels={GROUP_LABEL: "cad-workers"})])
# Its EndpointSlice does not match the service-endpoints keep rule, so only the job below scrapes it.
FIXTURE = "cadvisor-fixture"

# --- fixture inputs ----------------------------------------------------------

MIB = 2**20
START = 1000.0  # counters start positive, so increase() never clamps at zero
PERIODS = 10.0  # container_cpu_cfs_periods_total per second (100ms CFS period), every container
CUSTOM = 2  # custom_container_cpu_sizing_seconds_total = 2 x cpu rate; custom_container_memory_sizing_bytes = 2 x working set
OWNERS = {"burst-0": ("burst", "StatefulSet"), "burst-1": ("burst", "StatefulSet"), "solo": ("solo", "Pod")}
Usage = namedtuple("Usage", "cpu mem ws rss fs throttled throttled_s")
# cpu: cores/s; mem, ws, rss, fs: MiB; throttled: throttled periods/s; throttled_s: throttled seconds/s
USAGE = {
    ("burst-0", "app"): Usage(0.250, 96, 80, 64, 200, 2.5, 0.125),
    ("burst-1", "app"): Usage(0.150, 48, 40, 32, 100, 1.0, 0.050),
    ("burst-0", "sidecar"): Usage(0.040, 24, 20, 16, 8, 0.5, 0.025),
    ("burst-1", "sidecar"): Usage(0.020, 12, 10, 8, 4, 0.2, 0.010),
    ("solo", "app"): Usage(0.075, 30, 25, 20, 60, 4.0, 0.200),
}


def _cadvisor_series():
    series = []

    def add(pod, container):
        """One pod's series. A container is one set of values; the pod cgroup (container=None) is their sum."""

        labels = {"namespace": NAMESPACE, "pod": pod, **({} if container is None else {"container": container})}
        members = [u for (p, _), u in USAGE.items() if p == pod] if container is None else [USAGE[pod, container]]

        def total(field):
            return sum(getattr(u, field) for u in members)

        for name, rate in (
            ("container_cpu_usage_seconds_total", total("cpu")),
            ("container_cpu_cfs_periods_total", PERIODS * len(members)),
            ("container_cpu_cfs_throttled_periods_total", total("throttled")),
            ("container_cpu_cfs_throttled_seconds_total", total("throttled_s")),
        ):
            series.append(Metric(name, "counter", START, labels=labels, rate=rate))
        for name, value in (
            ("container_memory_usage_bytes", total("mem") * MIB),
            ("container_memory_working_set_bytes", total("ws") * MIB),
            ("container_memory_rss", total("rss") * MIB),
            ("container_fs_usage_bytes", total("fs") * MIB),
        ):
            series.append(Metric(name, "gauge", float(value), labels=labels))
        if container is not None:  # the custom families exist per container only
            series.append(Metric("custom_container_cpu_sizing_seconds_total", "counter", START, labels=labels, rate=CUSTOM * total("cpu")))
            series.append(Metric("custom_container_memory_sizing_bytes", "gauge", float(CUSTOM * total("ws") * MIB), labels=labels))

    for pod, container in USAGE:
        add(pod, container)
    for pod in OWNERS:
        add(pod, None)  # pod-level cgroup decoy: no container label
    return series


# --- expectations ------------------------------------------------------------

# Tolerances. RATE: rate, irate and increase of a constant-rate counter differ from the rate by
# scrape-latency jitter (about 1e-4); a defect moves a value by at least 10 mcores or a factor of 2.
# EXACT: gauges that are MiB multiples, their means, and ratios of increases from one scrape (the
# extrapolation factor cancels), written with %f (5e-7).
RATE = {"rel": 1e-2}
EXACT = {"rel": 0, "abs": 1e-6}

# stem, column, value per pod container, how the owner's pods combine, tolerance
FAMILIES = [
    ("avg_cpu_mcores_workload", "AvgCpuMcores", lambda u: 1000 * u.cpu, statistics.mean, RATE),
    ("max_cpu_mcores_workload", "MaxCpuMcores", lambda u: 1000 * u.cpu, max, RATE),
    ("avg_custom_cpu_mcores_workload", "AvgCustomCpuMcores", lambda u: 1000 * CUSTOM * u.cpu, statistics.mean, RATE),
    ("max_custom_cpu_mcores_workload", "MaxCustomCpuMcores", lambda u: 1000 * CUSTOM * u.cpu, max, RATE),
    # Averages of memory are in MiB, maxima in bytes; disk is in bytes for both
    ("avg_mem_workload", "AvgMem", lambda u: u.mem, statistics.mean, EXACT),
    ("max_mem_workload", "MaxMem", lambda u: u.mem * MIB, max, EXACT),
    ("avg_ws_workload", "AvgWs", lambda u: u.ws, statistics.mean, EXACT),
    ("max_ws_workload", "MaxWs", lambda u: u.ws * MIB, max, EXACT),
    ("avg_rss_workload", "AvgRss", lambda u: u.rss, statistics.mean, EXACT),
    ("max_rss_workload", "MaxRss", lambda u: u.rss * MIB, max, EXACT),
    ("avg_custom_memory_workload", "AvgCustomMemory", lambda u: CUSTOM * u.ws, statistics.mean, EXACT),
    ("max_custom_memory_workload", "MaxCustomMemory", lambda u: CUSTOM * u.ws * MIB, max, EXACT),
    ("avg_disk_workload", "AvgDisk", lambda u: u.fs * MIB, statistics.mean, EXACT),
    ("max_disk_workload", "MaxDisk", lambda u: u.fs * MIB, max, EXACT),
    ("avg_cpu_throttling_percent_workload", "AvgCpuThrottlingPercent", lambda u: 100 * u.throttled / PERIODS, statistics.mean, EXACT),
    ("max_cpu_throttling_percent_workload", "MaxCpuThrottlingPercent", lambda u: 100 * u.throttled / PERIODS, max, EXACT),
    # each row is the total for its 1-minute sample: the throttled seconds in 60s
    ("sum_cpu_throttling_seconds_workload", "SumCpuThrottlingSeconds", lambda u: 60 * u.throttled_s, sum, RATE),
]
IDENTITIES = [("burst", "StatefulSet", "app"), ("burst", "StatefulSet", "sidecar"), ("solo", "Pod", "app")]
KEYS = ["ClusterName", "Namespace", "EntityName", "EntityType", "ContainerName"]


def _expected(per_pod, combine, owner, container):
    return combine(per_pod(u) for (pod, c), u in USAGE.items() if OWNERS[pod][0] == owner and c == container)


CASES = [
    (f"container/{stem}.csv", column, {(o, c): _expected(per_pod, combine, o, c) for o, _, c in IDENTITIES}, tolerance)
    for stem, column, per_pod, combine, tolerance in FAMILIES
]
# The formulas above, checked against the hand-computed values for burst/app (200 mcores average, 250 maximum, ...)
assert [expected["burst", "app"] for _, _, expected, _ in CASES] == [
    200, 250, 400, 500, 72, 100663296, 60, 83886080, 48, 67108864, 120, 167772160,
    157286400, 209715200, 17.5, 25, 10.5,
]  # fmt: skip
USAGE_FILES = [case[0] for case in CASES]

NEW_FILES = [
    "container/avg_custom_cpu_mcores_workload.csv",
    "container/max_custom_cpu_mcores_workload.csv",
    "container/avg_custom_memory_workload.csv",
    "container/max_custom_memory_workload.csv",
    "container/avg_disk_workload.csv",
    "container/max_disk_workload.csv",
]
# Same files as node-pressure produces
BASE_FILES = dict.fromkeys(
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
)

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
                # The fixture replaces the kubelet cAdvisor job and keeps its name, so the job label is unchanged.
                "scrapeConfigs": {
                    "kubernetes-nodes-cadvisor": {"enabled": False},
                    FIXTURE: {
                        "job_name": "kubernetes-nodes-cadvisor",
                        "static_configs": [{"targets": [f"{FIXTURE}.kubex.svc.cluster.local:9100"]}],
                    },
                },
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
    fixtures=[FixtureService(name=FIXTURE, metrics=_cadvisor_series())],
    resources=["workload.yaml"],
    prometheus=PrometheusSpec(
        queries=[
            # 5 containers + 3 pod cgroups, scraped under the production job name
            PrometheusQuery("fixture-cpu", f'container_cpu_usage_seconds_total{{namespace="{NAMESPACE}",job="kubernetes-nodes-cadvisor"}}', min_results=8),
            PrometheusQuery("fixture-custom", f'custom_container_memory_sizing_bytes{{namespace="{NAMESPACE}"}}', min_results=5),
            PrometheusQuery("pods-running", f'kube_pod_status_phase{{namespace="{NAMESPACE}",phase="Running"}} == 1', min_results=3),
            PrometheusQuery("burst-owner", f'kube_pod_owner{{namespace="{NAMESPACE}",owner_kind="StatefulSet",owner_name="burst"}}', min_results=2),
        ],
    ),
    forwarder=ForwarderSpec(
        job_name="cadvisor-forwarder",
        # the collector queries increase(...[1m20s]), which needs samples from 80s before the first grid point
        window_seconds=240,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    # No lifecycle Wait: the StatefulSet pods may not exist yet when kubectl wait runs; the queries wait instead.
    files={**BASE_FILES, **dict.fromkeys(NEW_FILES, "populated")},
)

pytestmark = pytest.mark.pr


@pytest.fixture(scope="session")
def run(stack_run):
    with stack_run(SCENARIO, HERE) as active:
        yield active


def _grid(archive):
    return expected_timestamps(archive.csv("cluster/config.csv").one(Name=NAME)["AuditTime"], SCENARIO.forwarder.collection)


@pytest.mark.parametrize("path", USAGE_FILES, ids=[path.removesuffix(".csv") for path in USAGE_FILES])
def test_usage_follows_the_collector_grid(archive, path):
    """Exactly the three fixture identities: system containers would show up if the real kubelet job were on,
    and a pod cgroup would show up as a container if the collector kept series without a container label."""

    identities = [(NAME, NAMESPACE, owner, kind, container) for owner, kind, container in IDENTITIES]
    assert_metric_grid(archive.csv(path), KEYS, identities, _grid(archive))


@pytest.mark.parametrize(
    "path,column,expected,tolerance", CASES, ids=[case[0].removesuffix(".csv") for case in CASES]
)
def test_usage_matches_the_fixture(archive, path, column, expected, tolerance):
    rows = list(archive.csv(path))
    assert rows, f"{path} has no rows"
    for row in rows:
        want = expected[row["EntityName"], row["ContainerName"]]
        assert float(row[column]) == pytest.approx(want, **tolerance), (
            f"{path} {row['EntityName']}/{row['ContainerName']} at {row['MetricTime']}"
        )
