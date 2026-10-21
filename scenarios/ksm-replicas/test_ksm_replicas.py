"""Duplicate kube-state-metrics series.

kube-state-metrics runs two replicas, as an HA install does. Each serves every object, so Prometheus holds two
copies of every kube-state-metrics series that differ only in `instance`. Each container, quota and node must
still be reported once: a container's requests, its power state, a quota's usage and a node's reservation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.archive import Csv, assert_metric_grid, expected_timestamps
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.oracle import quantity
from harness.spec import (
    CLUSTER_NAME_PREFIX,
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
NAME = "ksm-replicas"
NAMESPACE = "ksmr-workload"
GROUP_LABEL, GROUP = "topology.kubex.ai/node-group", "ksmr-workers"
ENTITY_LABEL = "stack-e2e.kubex.ai/entity"
_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]
CLUSTER_SPEC = ClusterSpec(control_planes=[Node(labels=_SYSTEM_NODE)], workers=[Node(labels={GROUP_LABEL: GROUP})])
CP, W1 = CLUSTER_SPEC.node_names(CLUSTER_NAME_PREFIX + NAME)
QUOTA_USED_CPU = 0.25  # web's 250m from workload.yaml; done has finished and released its share

BASE_FILES = (
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
)
RQ_FILES = ("rq/attributes.csv", "rq/config.csv", "rq/cpu_requests.csv")  # the quota sets only requests.cpu (rq/rq.go:221-233)
assert len(BASE_FILES) == 41

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
                    "replicas": 2,
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
            # every node is served by both replicas, so both targets are scraped
            PrometheusQuery("ksm-twice", "count by (node) (kube_node_info) == 2", min_results=2),
            PrometheusQuery(
                "done-succeeded", f'kube_pod_status_phase{{namespace="{NAMESPACE}",pod=~"done-.*",phase="Succeeded"}} == 1'
            ),
            PrometheusQuery(
                "quota-used",
                f'abs(max(kube_resourcequota{{namespace="{NAMESPACE}",type="used",resource="requests.cpu"}}) - {QUOTA_USED_CPU}) < 1e-6',
            ),
        ],
    ),
    forwarder=ForwarderSpec(
        job_name="ksm-replicas-forwarder",
        window_seconds=180,
        collection=Collection(interval="minutes", interval_size=1, history=1, sample_rate=1),
    ),
    resources=["workload.yaml"],
    lifecycle=[
        Wait(kind="deployment", name="web", namespace=NAMESPACE),
        Wait(kind="job", name="done", namespace=NAMESPACE, condition="Complete"),
    ],
    snapshot_resources=("resourcequotas",),
    files=dict.fromkeys((*BASE_FILES, *RQ_FILES), "populated"),
)

pytestmark = pytest.mark.pr

CLUSTER = NAME
TOLERANCE = 1e-6  # %f series: worst-case print error 5e-7
KEYS = ["ClusterName", "Namespace", "EntityName", "EntityType", "ContainerName"]
CONTAINERS = [("web", "app", "Deployment"), ("done", "work", "Job")]
CONTAINER_IDS = [f"{e}-{c}" for e, c, _ in CONTAINERS]


@pytest.fixture(scope="session")
def run(stack_run):
    with stack_run(SCENARIO, HERE) as active:
        yield active


def _grid(archive):
    return expected_timestamps(archive.csv("cluster/config.csv").one(Name=CLUSTER)["AuditTime"], SCENARIO.forwarder.collection)


def _pods(snapshot, entity):
    return [
        p
        for p in snapshot.pods
        if p["metadata"]["namespace"] == NAMESPACE and p["metadata"].get("labels", {}).get(ENTITY_LABEL) == entity
    ]


def _spec(oracle, entity, container):
    """The container of the entity's first pod."""

    return next(c for c in _pods(oracle.primary, entity)[0]["spec"]["containers"] if c["name"] == container)


def test_kube_state_metrics_runs_twice(run):
    for snapshot in run.snapshots:
        pods = [
            p
            for p in snapshot.pods
            if p["metadata"]["namespace"] == "kubex"
            and p["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "kube-state-metrics"
            and p["status"].get("phase") == "Running"
        ]
        assert len(pods) == 2, f"{len(pods)} running kube-state-metrics pods at {snapshot.label}"


@pytest.mark.parametrize("entity,container,kind", CONTAINERS, ids=CONTAINER_IDS)
def test_container_cpu_request_attribute(archive, oracle, entity, container, kind):
    """A terminated container has no request (it is excluded from the request series)."""

    rows = archive.csv("container/attributes.csv").where(Namespace=NAMESPACE, EntityName=entity, ContainerName=container)
    expected = "" if entity == "done" else str(int(quantity(_spec(oracle, entity, container)["resources"]["requests"]["cpu"], entity) * 1000))
    assert [r["CpuRequest"] for r in rows] == [expected]


def test_container_cpu_request_series(archive, oracle):
    rows = archive.csv("container/cpu_request.csv")
    rows = Csv(rows.path, rows.headers, rows.where(Namespace=NAMESPACE))
    assert_metric_grid(rows, KEYS, [(CLUSTER, NAMESPACE, "web", "Deployment", "app")], _grid(archive))
    expected = float(quantity(_spec(oracle, "web", "app")["resources"]["requests"]["cpu"], "web cpu request")) * 1000
    for row in rows:
        assert float(row["CpuRequest"]) == pytest.approx(expected, abs=TOLERANCE), f"at {row['MetricTime']}"


@pytest.mark.parametrize("entity,container,kind", CONTAINERS, ids=CONTAINER_IDS)
def test_power_state(archive, oracle, entity, container, kind):
    phase = _pods(oracle.primary, entity)[0]["status"]["phase"]
    rows = archive.csv("container/attributes.csv").where(Namespace=NAMESPACE, EntityName=entity, ContainerName=container)
    assert [r["PowerState"] for r in rows] == [{"Running": "Running", "Succeeded": "Terminated"}[phase]]


def test_quota_cpu_requests_series(archive, oracle):
    quota = next(q for q in oracle.primary.objects["resourcequotas"] if q["metadata"]["namespace"] == NAMESPACE)
    expected = float(quantity(quota["status"]["used"]["requests.cpu"], "ksmr-quota used requests.cpu")) * 1000
    table = archive.csv("rq/cpu_requests.csv")
    assert_metric_grid(table, ["ClusterName", "Namespace", "RqName"], [(CLUSTER, NAMESPACE, "ksmr-quota")], _grid(archive))
    for row in table:
        assert float(row["CpuRequests"]) == pytest.approx(expected, abs=TOLERANCE), f"at {row['MetricTime']}"


# The node request series deduplicates (common/workload.go:135-141); the reservation divides by allocatable summed over
# every scrape target (node/node.go:198), so only the reservation is wrong.
WORKER_SERIES = [
    ("node/cpu_request.csv", "CpuRequest", lambda oracle, selector: oracle.requests("cpu", selector) * 1000),
    ("node/cpu_reservation_percent.csv", "CpuReservationPercent", lambda oracle, selector: oracle.reservation_percent("cpu", selector)),
]


@pytest.mark.parametrize("path,column,truth", WORKER_SERIES, ids=[case[0] for case in WORKER_SERIES])
def test_worker_cpu_series(archive, oracle, path, column, truth):
    rows = archive.csv(path).where(NodeName=W1)
    assert sorted(r["MetricTime"] for r in rows) == _grid(archive)
    expected = truth(oracle, {"kubernetes.io/hostname": W1})
    for row in rows:
        assert float(row[column]) == pytest.approx(expected, abs=TOLERANCE), f"{path} at {row['MetricTime']}"
