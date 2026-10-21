"""Container requests, limits and attributes, namespace quotas and resource quota files.

One namespace with a ResourceQuota holds every kind of owner the collector resolves (Deployment,
StatefulSet, DaemonSet, Job, CronJob and bare Pods), a native sidecar and a standard init container,
a pod that restarted once, and a completed Job and a failed Pod that stay in the API. Every expected
value comes from what Kubernetes reports about those objects (pod specs, owner objects, quota status),
captured in the snapshots, or from the fixture that stands in for Beyla.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from harness.archive import Csv, assert_metric_grid, expected_timestamps, label_map, prometheus_labels
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.oracle import quantity
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
NAME = "container-resources"
NAMESPACE = "cr-workload"
CHURN = "cr-churn"
GROUP_LABEL = "topology.kubex.ai/node-group"
GROUP = "cr-workers"
ENTITY_LABEL = "stack-e2e.kubex.ai/entity"
_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]
CLUSTER_SPEC = ClusterSpec(
    control_planes=[Node(labels=_SYSTEM_NODE)],
    workers=[Node(labels={GROUP_LABEL: GROUP}), Node(labels={GROUP_LABEL: GROUP})],
    kubelet_configuration={"crashLoopBackOff": {"maxContainerRestartPeriod": "1s"}},
)
NODES = CLUSTER_SPEC.node_names(CLUSTER_NAME_PREFIX + NAME)
CP, W1, W2 = NODES  # workload.yaml pins pods by these hostnames

# --- fixture inputs ----------------------------------------------------------

# Beyla's survey: one language per container
RUNTIMES = {("web", "app"): "go", ("db", "db"): "java"}
KINDS = {"web": "Deployment", "db": "StatefulSet"}


def _survey(entity, container, language):
    return Metric(
        "survey_info",
        "gauge",
        1.0,
        labels={
            "k8s_node_name": W1,
            "k8s_namespace_name": NAMESPACE,
            "k8s_kind": KINDS[entity],
            "k8s_owner_name": entity,
            "k8s_container_name": container,
            "telemetry_sdk_language": language,
        },
    )


def _query(name, query, n):
    return PrometheusQuery(name, query, min_results=n)


# status.used of cr-quota once every pod is admitted and the completed and failed pods have released theirs, as
# kube-state-metrics exports it: CPU in cores, memory in bytes, pods as a count
MIB = 2**20
QUOTA_USED = {
    "cpu": 2.501,
    "requests.cpu": 2.501,
    "limits.cpu": 4.001,
    "requests.memory": 864 * MIB,
    "limits.memory": 1472 * MIB,
    "pods": 9,
}

CONTAINER_FILES = (
    "attributes", "config", "current_size", "cpu_limit", "cpu_request", "memory_limit", "memory_request",
    "max_restarts", "avg_cpu_mcores_workload", "max_cpu_mcores_workload", "avg_cpu_throttling_percent_workload",
    "max_cpu_throttling_percent_workload", "sum_cpu_throttling_seconds_workload", "avg_mem_workload",
    "max_mem_workload", "avg_rss_workload", "max_rss_workload", "avg_ws_workload", "max_ws_workload",
)  # fmt: skip

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
    # The chart's Beyla is disabled; a Service with its name is a kept target (EndpointSlice "kubex-beyla-...").
    fixtures=[FixtureService(name="kubex-beyla", metrics=[_survey(e, c, lang) for (e, c), lang in RUNTIMES.items()])],
    prometheus=PrometheusSpec(
        queries=[
            _query("running", f'kube_pod_status_phase{{namespace="{NAMESPACE}",phase="Running"}} == 1', 9),
            _query("succeeded", f'kube_pod_status_phase{{namespace="{NAMESPACE}",phase="Succeeded"}} == 1', 1),
            _query("failed", f'kube_pod_status_phase{{namespace="{NAMESPACE}",phase="Failed"}} == 1', 1),
            _query("terminal-requests", f'kube_pod_container_resource_requests{{namespace="{NAMESPACE}",pod=~"done-.*|failed"}}', 4),
            _query("sidecars", f'kube_pod_init_container_info{{namespace="{NAMESPACE}",restart_policy="Always"}}', 2),
            _query("cronjob-job", f'kube_job_owner{{namespace="{NAMESPACE}",owner_kind="CronJob",owner_name="tick"}}', 1),
            _query("agent-available", f'kube_daemonset_status_number_available{{namespace="{NAMESPACE}",daemonset="agent"}} == 2', 1),
            _query("solo-restarted", f'kube_pod_container_status_restarts_total{{namespace="{NAMESPACE}",pod="solo"}} == 1', 1),
            _query("churn-running", f'kube_pod_status_phase{{namespace="{CHURN}",pod=~"churn-.*",phase="Running"}} == 1', 1),
            _query("mixed-running", f'kube_pod_status_phase{{namespace="{CHURN}",pod=~"mixed-0-.*",phase="Running"}} == 1', 1),
            _query("mixed-finished", f'kube_pod_status_phase{{namespace="{CHURN}",pod=~"mixed-1-.*",phase="Succeeded"}} == 1', 1),
            _query("crash-restarted", f'kube_pod_container_status_restarts_total{{namespace="{CHURN}",pod="crash"}} >= 1', 1),
            # the quota has finished accounting: every used quantity holds its final value (the first snapshot must not see less)
            *(
                _query(f"quota-used-{key}", f'abs(kube_resourcequota{{namespace="{NAMESPACE}",type="used",resource="{key}"}} - {value}) < 1e-6', 1)
                for key, value in QUOTA_USED.items()
            ),
            _query("survey", f'survey_info{{k8s_namespace_name="{NAMESPACE}"}}', 2),
        ],
    ),
    forwarder=ForwarderSpec(
        job_name="container-resources-forwarder",
        window_seconds=240,
        collection=Collection(interval="minutes", interval_size=3, history=1, sample_rate=1),
    ),
    resources=["workload.yaml"],
    lifecycle=[
        Wait(kind="deployment", name="web", namespace=NAMESPACE, condition="Available", timeout=600),
        Wait(kind="job", name="done", namespace=NAMESPACE, condition="Complete", timeout=600),
    ],
    snapshot_resources=("resourcequotas", "namespaces", "deployments", "statefulsets", "daemonsets", "jobs", "cronjobs"),
    files={
        **dict.fromkeys(
            (
                "cluster/attributes.csv",
                "cluster/config.csv",
                "cluster/cpu_requests.csv",
                "cluster/cpu_reservation_percent.csv",
                "cluster/memory_requests.csv",
                "cluster/memory_reservation_percent.csv",
                *(f"container/{name}.csv" for name in (*CONTAINER_FILES, "events", "ephemeral_storage_request")),
                "node/attributes.csv",
                "node/config.csv",
                "node/cpu_limit.csv",
                "node/cpu_request.csv",
                "node/cpu_reservation_percent.csv",
                "node/ephemeral_storage_request.csv",
                "node/ephemeral_storage_reservation_percent.csv",
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
                "rq/attributes.csv",
                "rq/config.csv",
                "rq/cpu_limits.csv",
                "rq/cpu_requests.csv",
                "rq/mem_limits.csv",
                "rq/mem_requests.csv",
                "rq/pods.csv",
            ),
            "populated",
        ),
    },
)

pytestmark = pytest.mark.pr

CLUSTER = NAME
TOLERANCE = 1e-6  # %f series: worst-case print error 5e-7
ALL_NODES = {"kubernetes.io/os": "linux"}
CONTAINERS = [
    ("web", "app"), ("web", "proxy"), ("db", "db"), ("agent", "agent"), ("batch", "work"),
    ("tick", "tick"), ("solo", "solo"), ("done", "work"), ("failed", "fail"),
]  # fmt: skip
ENTITY_TYPES = {
    "web": "Deployment", "db": "StatefulSet", "agent": "DaemonSet", "batch": "Job",
    "tick": "CronJob", "solo": "Pod", "done": "Job", "failed": "Pod",
}  # fmt: skip
OWNER_RESOURCE = {
    "Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets",
    "Job": "jobs", "CronJob": "cronjobs",
}  # fmt: skip
TERMINAL = {"done", "failed"}
CONTROLLERS = ["web", "db", "agent", "batch", "tick"]  # entities the collector sizes
KEYS = ["ClusterName", "Namespace", "EntityName", "EntityType", "ContainerName"]
CONTAINER_IDS = [f"{e}-{c}" for e, c in CONTAINERS]


@pytest.fixture(scope="session")
def run(stack_run):
    with stack_run(SCENARIO, HERE) as active:
        yield active


# --- Kubernetes truth, read from the snapshots --------------------------------


def _pods(snapshot, entity):
    return [
        p
        for p in snapshot.pods
        if p["metadata"]["namespace"] == NAMESPACE and p["metadata"].get("labels", {}).get(ENTITY_LABEL) == entity
    ]


def _running(snapshot, entity):
    return [p for p in _pods(snapshot, entity) if p["status"].get("phase") == "Running"]


def _spec(oracle, entity, container):
    """The container (regular or init) of the entity's first pod."""

    spec = _pods(oracle.primary, entity)[0]["spec"]
    return next(c for c in spec["containers"] + spec.get("initContainers", []) if c["name"] == container)


def _statuses(pod, container):
    status = pod["status"]
    return [s for s in status.get("containerStatuses", []) + status.get("initContainerStatuses", []) if s["name"] == container]


def _object(oracle, resource, name):
    return next(
        item
        for item in oracle.primary.objects[resource]
        if item["metadata"]["name"] == name
        and (resource == "namespaces" or item["metadata"].get("namespace") == NAMESPACE)
    )


def _owner(oracle, entity):
    """The object the collector reports the entity as: the pod itself for a bare Pod."""

    kind = ENTITY_TYPES[entity]
    return _pods(oracle.primary, entity)[0] if kind == "Pod" else _object(oracle, OWNER_RESOURCE[kind], entity)


def _quota(oracle, snapshot=None):
    return next(
        q
        for q in (snapshot or oracle.primary).objects["resourcequotas"]
        if q["metadata"]["namespace"] == NAMESPACE and q["metadata"]["name"] == "cr-quota"
    )


# Kubernetes CPU is whole millicores, so the Decimal product is exact (int(1.001 * 1000) is 1000).
def _mcores(value, where):
    return int(quantity(value, where) * 1000)


def _mib(value, where):
    """The collector floors MiB (common/value.go:27-29)."""

    return int(quantity(value, where)) // MIB


def _count(value, where):
    return int(quantity(value, where))


# --- archive helpers ----------------------------------------------------------


def _ours(archive, path, namespace=NAMESPACE):
    """The rows of one of this scenario's namespaces; the system pods in other namespaces are not under test."""

    table = archive.csv(path)
    return Csv(path, table.headers, table.where(Namespace=namespace))


def _row(archive, path, entity, container):
    return _ours(archive, path).one(EntityName=entity, ContainerName=container)


def _grid(archive):
    return expected_timestamps(archive.csv("cluster/config.csv").one(Name=CLUSTER)["AuditTime"], SCENARIO.forwarder.collection)


# --- premise: the cluster is what the expectations assume ---------------------


def test_terminal_pods_stay_terminal_and_reserve_resources(run):
    """Without a completed and a failed pod holding requests on W2 the exclusion tests could not fail."""

    for snapshot in run.snapshots:
        done, failed = _pods(snapshot, "done"), _pods(snapshot, "failed")
        assert [(p["status"]["phase"], p["spec"]["nodeName"]) for p in done] == [("Succeeded", W2)], snapshot.label
        assert [(p["status"]["phase"], p["spec"]["nodeName"]) for p in failed] == [("Failed", W2)], snapshot.label
        for resource, expected in (("cpu", Decimal(1)), ("memory", 512 * MIB)):
            total = sum(
                quantity(c["resources"]["requests"][resource], f"{p['metadata']['name']} {resource}")
                for p in done + failed
                for c in p["spec"]["containers"]
            )
            assert total == expected, f"{resource} requested by the terminal pods at {snapshot.label}"


def test_workload_is_steady_through_the_window(run):
    def state(snapshot):
        pods = {
            entity: sorted(
                (
                    p["metadata"]["name"],
                    p["status"].get("phase"),
                    p["spec"].get("nodeName"),
                    sorted((s["name"], s["restartCount"]) for c in ("containerStatuses", "initContainerStatuses") for s in p["status"].get(c, [])),
                )
                for p in _pods(snapshot, entity)
            )
            for entity in ENTITY_TYPES
        }
        return pods, _quota(run.oracle, snapshot)["status"]["used"]

    first = state(run.snapshots[0])
    for snapshot in run.snapshots[1:]:
        assert state(snapshot) == first, f"workload changed between {run.snapshots[0].label} and {snapshot.label}"


def test_quota_status_reports_cpu_under_both_keys(oracle):
    """The rq CPU request series sums the cpu and requests.cpu keys (rq/rq.go:224), which is only right if
    Kubernetes reports one quantity under both."""

    status = _quota(oracle)["status"]
    for field in ("hard", "used"):
        values = status[field]
        assert quantity(values["cpu"], f"{field} cpu") == quantity(values["requests.cpu"], f"{field} requests.cpu")


# --- container/ ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["container/attributes.csv", "container/config.csv"])
def test_container_identities(archive, path):
    """One row per container; the standard init container `setup` has none."""

    rows = _ours(archive, path).rows
    assert sorted((r["EntityName"], r["EntityType"], r["ContainerName"]) for r in rows) == sorted(
        (e, ENTITY_TYPES[e], c) for e, c in CONTAINERS
    )
    assert {r["ClusterName"] for r in rows} == {CLUSTER}


@pytest.mark.parametrize("entity,container", CONTAINERS, ids=CONTAINER_IDS)
def test_attribute_identity_columns(archive, entity, container):
    row = _row(archive, "container/attributes.csv", entity, container)
    assert row["ContainerType"] == ("NativeSidecarInit" if (entity, container) == ("web", "proxy") else "Regular")
    assert (row["VirtualTechnology"], row["VirtualDomain"], row["VirtualDatacenter"]) == ("Containers", CLUSTER, NAMESPACE)
    assert row["VirtualCluster"] == entity
    assert (row["CreatedByKind"], row["CreatedByName"]) == (ENTITY_TYPES[entity], entity)


SPEC_COLUMNS = [
    ("CpuRequest", "requests", "cpu"),
    ("CpuLimit", "limits", "cpu"),
    ("MemoryRequest", "requests", "memory"),
    ("MemoryLimit", "limits", "memory"),
]
ATTRIBUTE_CASES = [(e, c, *column) for e, c in CONTAINERS for column in SPEC_COLUMNS]


# Whole mcores and MiB from the last sample; a terminated container has no request or limit series.
@pytest.mark.parametrize(
    "entity,container,column,kind,resource", ATTRIBUTE_CASES, ids=["-".join(case[:3]) for case in ATTRIBUTE_CASES]
)
def test_request_and_limit_attributes(archive, oracle, entity, container, column, kind, resource):
    row = _row(archive, "container/attributes.csv", entity, container)
    if entity in TERMINAL:
        expected = ""
    else:
        value = _spec(oracle, entity, container)["resources"][kind][resource]
        where = f"{entity} {container} {kind} {resource}"
        expected = str(_mcores(value, where) if resource == "cpu" else _mib(value, where))
    assert row[column] == expected


SPEC_SERIES = [
    ("container/cpu_request.csv", "CpuRequest", "requests", "cpu"),
    ("container/cpu_limit.csv", "CpuLimit", "limits", "cpu"),
    ("container/memory_request.csv", "MemoryRequest", "requests", "memory"),
    ("container/memory_limit.csv", "MemoryLimit", "limits", "memory"),
]


@pytest.mark.parametrize("path", [case[0] for case in SPEC_SERIES])
def test_request_and_limit_series_follow_the_grid(archive, path):
    identities = [(CLUSTER, NAMESPACE, e, ENTITY_TYPES[e], c) for e, c in CONTAINERS if e not in TERMINAL]
    assert_metric_grid(_ours(archive, path), KEYS, identities, _grid(archive))


# Series keep the raw value: mcores for CPU, MiB for memory. db's 1001m is 1001.000000 here (%f rounds
# 1000.9999999999999) although its attribute is truncated to 1000.
@pytest.mark.parametrize("path,column,kind,resource", SPEC_SERIES, ids=[case[0] for case in SPEC_SERIES])
def test_request_and_limit_series_match_the_spec(archive, oracle, path, column, kind, resource):
    for row in _ours(archive, path):
        entity, container = row["EntityName"], row["ContainerName"]
        value = float(quantity(_spec(oracle, entity, container)["resources"][kind][resource], f"{entity} {container}"))
        expected = value * 1000 if resource == "cpu" else value / MIB
        assert float(row[column]) == pytest.approx(expected, abs=TOLERANCE), f"{path} {entity}/{container} at {row['MetricTime']}"


@pytest.mark.parametrize("entity,container", CONTAINERS, ids=CONTAINER_IDS)
def test_power_state(archive, entity, container):
    row = _row(archive, "container/attributes.csv", entity, container)
    assert row["PowerState"] == ("Terminated" if entity in TERMINAL else "Running")


def test_current_size_series_follow_the_grid(archive):
    identities = [(CLUSTER, NAMESPACE, e, ENTITY_TYPES[e], c) for e, c in CONTAINERS if e in CONTROLLERS]
    assert_metric_grid(_ours(archive, "container/current_size.csv"), KEYS, identities, _grid(archive))


def test_current_size_series_match_running_pods(archive, oracle):
    for row in _ours(archive, "container/current_size.csv"):
        expected = len(_running(oracle.primary, row["EntityName"]))
        assert float(row["CurrentSize"]) == pytest.approx(expected), f"{row['EntityName']} at {row['MetricTime']}"


# Bare pods have no size. Jobs report their running pods, so a finished Job reports 0.
@pytest.mark.parametrize("entity", CONTROLLERS + ["solo", "failed", "done"])
def test_current_size_attribute(archive, oracle, entity):
    expected = "" if entity in ("solo", "failed") else str(len(_running(oracle.primary, entity)))
    rows = _ours(archive, "container/attributes.csv").where(EntityName=entity)
    assert rows
    for row in rows:
        assert row["CurrentSize"] == expected, f"{entity}/{row['ContainerName']}"


@pytest.mark.parametrize("entity,container", CONTAINERS, ids=CONTAINER_IDS)
def test_container_restarts(archive, oracle, entity, container):
    """The lifetime counter: solo exited once at startup, nothing else restarted."""

    expected = sum(s["restartCount"] for p in _pods(oracle.primary, entity) for s in _statuses(p, container))
    assert _row(archive, "container/attributes.csv", entity, container)["ContainerRestarts"] == str(expected)


@pytest.mark.parametrize("entity", list(ENTITY_TYPES))
def test_qos_class(archive, oracle, entity):
    (expected,) = {p["status"]["qosClass"] for p in _pods(oracle.primary, entity)}
    rows = _ours(archive, "container/attributes.csv").where(EntityName=entity)
    assert [r["QosClass"] for r in rows] == [expected] * len(rows) != []


@pytest.mark.parametrize("entity", list(ENTITY_TYPES))
def test_create_time(archive, oracle, entity):
    expected = datetime.fromisoformat(_owner(oracle, entity)["metadata"]["creationTimestamp"])
    rows = _ours(archive, "container/attributes.csv").where(EntityName=entity)
    assert rows
    for row in rows:
        assert datetime.fromisoformat(row["CreateTime"]) == expected, f"{entity}/{row['ContainerName']}"


@pytest.mark.parametrize("entity", list(ENTITY_TYPES))
def test_current_nodes(archive, oracle, entity):
    """Every node of every pod of the entity, terminal pods included."""

    expected = sorted({p["spec"]["nodeName"] for p in _pods(oracle.primary, entity)})
    for row in _ours(archive, "container/attributes.csv").where(EntityName=entity):
        assert sorted(row["CurrentNodes"].split("|")) == expected, f"{entity}/{row['ContainerName']}"


@pytest.mark.parametrize("entity", list(ENTITY_TYPES))
def test_pod_labels(archive, oracle, entity):
    """The owner's labels merged with every pod's; a label whose value differed would be ';'-joined and fail."""

    expected = prometheus_labels(_owner(oracle, entity)["metadata"].get("labels", {}))
    for pod in _pods(oracle.primary, entity):
        expected.update(prometheus_labels(pod["metadata"].get("labels", {})))
    rows = _ours(archive, "container/attributes.csv").where(EntityName=entity)
    assert rows
    for row in rows:
        collected = {k: v for k, v in label_map(row["PodLabels"]).items() if k.startswith("label_")}
        assert collected == expected, f"{entity}/{row['ContainerName']}"


@pytest.mark.parametrize("entity,container", CONTAINERS, ids=CONTAINER_IDS)
def test_container_labels_carry_the_image(archive, oracle, entity, container):
    """The rest of ContainerLabels is raw series labels (container id, scrape labels) with no further truth."""

    row = _row(archive, "container/attributes.csv", entity, container)
    assert label_map(row["ContainerLabels"])["image_spec"] == _spec(oracle, entity, container)["image"]


NAMESPACE_QUOTA = {  # column: (quota key, conversion), read from status.hard
    "NamespaceCpuRequest": ("requests.cpu", _mcores),
    "NamespaceCpuLimit": ("limits.cpu", _mcores),
    "NamespaceMemoryRequest": ("requests.memory", _mib),
    "NamespaceMemoryLimit": ("limits.memory", _mib),
    "NamespacePodsLimit": ("pods", _count),
}


@pytest.mark.parametrize("column", NAMESPACE_QUOTA)
def test_namespace_quota_columns(archive, oracle, column):
    key, convert = NAMESPACE_QUOTA[column]
    expected = str(convert(_quota(oracle)["status"]["hard"][key], f"hard {key}"))
    rows = _ours(archive, "container/attributes.csv").rows
    assert rows
    for row in rows:
        assert row[column] == expected, f"{row['EntityName']}/{row['ContainerName']}"


def test_namespace_labels(archive, oracle):
    """Namespace labels and annotations; the last-applied annotation is dropped."""

    namespace = _object(oracle, "namespaces", NAMESPACE)["metadata"]
    annotations = {k: v for k, v in namespace["annotations"].items() if k != "kubectl.kubernetes.io/last-applied-configuration"}
    expected = prometheus_labels(namespace["labels"]) | {
        "annotation_" + k.removeprefix("label_"): v for k, v in prometheus_labels(annotations).items()
    }
    rows = _ours(archive, "container/attributes.csv").rows
    assert rows
    for row in rows:
        collected = {k: v for k, v in label_map(row["NamespaceLabels"]).items() if k.startswith(("label_", "annotation_"))}
        assert collected == expected, f"{row['EntityName']}/{row['ContainerName']}"


@pytest.mark.parametrize("entity,container", CONTAINERS, ids=CONTAINER_IDS)
def test_runtimes_come_from_the_survey(archive, entity, container):
    expected = [{"name": RUNTIMES[entity, container]}] if (entity, container) in RUNTIMES else []
    assert json.loads(_row(archive, "container/attributes.csv", entity, container)["Runtimes"]) == expected


@pytest.mark.parametrize("entity,container", CONTAINERS, ids=CONTAINER_IDS)
def test_container_config(archive, oracle, entity, container):
    """HwTotalMemory is cAdvisor's memory limit in MiB; a terminated container has no cAdvisor series."""

    row = _row(archive, "container/config.csv", entity, container)
    limit = _spec(oracle, entity, container)["resources"]["limits"]["memory"]
    assert row["HwTotalMemory"] == ("" if entity in TERMINAL else str(_mib(limit, f"{entity} {container} limit")))
    assert row["GpuMemoryTotal"] == ""
    assert (row["OsName"], row["HwManufacturer"]) == ("Linux", "CONTAINERS")
    assert row["AuditTime"] == archive.csv("cluster/config.csv").one(Name=CLUSTER)["AuditTime"]


# --- rq/ ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["rq/config.csv", "rq/attributes.csv"])
def test_quota_is_described_once(archive, path):
    assert [(r["ClusterName"], r["Namespace"], r["RqName"]) for r in archive.csv(path)] == [(CLUSTER, NAMESPACE, "cr-quota")]


def test_quota_description(archive, oracle):
    row = archive.csv("rq/attributes.csv").one(Namespace=NAMESPACE, RqName="cr-quota")
    quota = _quota(oracle)
    assert (row["VirtualTechnology"], row["VirtualDomain"], row["VirtualDatacenter"]) == ("ResourceQuota", CLUSTER, NAMESPACE)
    assert datetime.fromisoformat(row["CreateTime"]) == datetime.fromisoformat(quota["metadata"]["creationTimestamp"])
    hard = quota["status"]["hard"]
    assert set(filter(None, row["ResourceMetadata"].split("|"))) == {
        f"{k}: {float(quantity(v, f'hard {k}')):.2f}" for k, v in hard.items()
    }


QUOTA_ATTRIBUTES = {  # column: (status field, quota key, conversion)
    "CpuLimit": ("used", "limits.cpu", _mcores),
    "CpuRequest": ("used", "requests.cpu", _mcores),
    "MemoryLimit": ("used", "limits.memory", _mib),
    "MemoryRequest": ("used", "requests.memory", _mib),
    "CurrentSize": ("used", "pods", _count),
    **{column: ("hard", key, convert) for column, (key, convert) in NAMESPACE_QUOTA.items()},
}


@pytest.mark.parametrize("column", QUOTA_ATTRIBUTES)
def test_quota_attributes(archive, oracle, column):
    field, key, convert = QUOTA_ATTRIBUTES[column]
    expected = str(convert(_quota(oracle)["status"][field][key], f"{field} {key}"))
    assert archive.csv("rq/attributes.csv").one(Namespace=NAMESPACE, RqName="cr-quota")[column] == expected


QUOTA_SERIES = [  # path, column, quota key, factor from Kubernetes' base unit
    ("rq/cpu_limits.csv", "CpuLimits", "limits.cpu", 1000),
    ("rq/cpu_requests.csv", "CpuRequests", "requests.cpu", 1000),
    ("rq/mem_limits.csv", "MemLimits", "limits.memory", 1),
    ("rq/mem_requests.csv", "MemRequests", "requests.memory", 1 / MIB),
    ("rq/pods.csv", "PodsLimits", "pods", 1),
]


@pytest.mark.parametrize("path", [case[0] for case in QUOTA_SERIES])
def test_quota_series_follow_the_grid(archive, path):
    assert_metric_grid(archive.csv(path), ["ClusterName", "Namespace", "RqName"], [(CLUSTER, NAMESPACE, "cr-quota")], _grid(archive))


@pytest.mark.parametrize("path,column,key,factor", QUOTA_SERIES, ids=[case[0] for case in QUOTA_SERIES])
def test_quota_series_match_status_used(archive, oracle, path, column, key, factor):
    expected = float(quantity(_quota(oracle)["status"]["used"][key], f"cr-quota used {key}")) * factor
    for row in archive.csv(path):
        assert float(row[column]) == pytest.approx(expected, abs=TOLERANCE), f"{path} at {row['MetricTime']}"


# --- node/, node_group/, cluster/: terminated pods are excluded ---------------
# The oracle counts only Running pods' live containers and running native sidecars, the collector's
# FilterTerminatedContainers rule (common/workload.go:135-156). done and failed would add 1000m and
# 512Mi of requests and 2000m and 1024Mi of limits to W2, the group and the cluster if they were counted.

W2_SELECTOR, GROUP_SELECTOR = {"kubernetes.io/hostname": W2}, {GROUP_LABEL: GROUP}
EXCLUDED_SERIES = [  # path, identity column, identity, selector, column, calculation, resource, factor from cores and bytes
    ("node/cpu_request.csv", "NodeName", W2, W2_SELECTOR, "CpuRequest", "requests", "cpu", 1000),
    ("node/memory_request.csv", "NodeName", W2, W2_SELECTOR, "MemoryRequest", "requests", "memory", 1 / MIB),
    ("node/cpu_limit.csv", "NodeName", W2, W2_SELECTOR, "CpuLimit", "limits", "cpu", 1000),
    ("node/memory_limit.csv", "NodeName", W2, W2_SELECTOR, "MemoryLimit", "limits", "memory", 1 / MIB),
    ("node/pod_count.csv", "NodeName", W2, W2_SELECTOR, "PodCount", "pod_count", None, 1),
    ("node_group/cpu_requests.csv", "NodeGroupName", GROUP, GROUP_SELECTOR, "CpuRequests", "requests", "cpu", 1),
    ("node_group/memory_requests.csv", "NodeGroupName", GROUP, GROUP_SELECTOR, "MemoryRequests", "requests", "memory", 1 / MIB),
    ("cluster/cpu_requests.csv", "Name", CLUSTER, ALL_NODES, "CpuRequests", "requests", "cpu", 1),
    ("cluster/memory_requests.csv", "Name", CLUSTER, ALL_NODES, "MemoryRequests", "requests", "memory", 1 / MIB),
]  # fmt: skip


@pytest.mark.parametrize("path,identity_column,identity,selector,column,calculation,resource,factor", EXCLUDED_SERIES, ids=[case[0] for case in EXCLUDED_SERIES])
def test_terminal_pods_are_excluded_from_series(archive, oracle, path, identity_column, identity, selector, column, calculation, resource, factor):
    expected = (oracle.pod_count(selector) if calculation == "pod_count" else getattr(oracle, calculation)(resource, selector)) * factor
    rows = archive.csv(path).where(**{identity_column: identity})
    assert sorted(r["MetricTime"] for r in rows) == _grid(archive), f"{path} {identity}"
    for row in rows:
        assert float(row[column]) == pytest.approx(expected, abs=TOLERANCE), f"{path} {identity} at {row['MetricTime']}"


EXCLUDED_ATTRIBUTES = [  # path, identity column, identity, selector
    ("node/attributes.csv", "NodeName", W2, W2_SELECTOR),
    ("node_group/attributes.csv", "NodeGroupName", GROUP, GROUP_SELECTOR),
    ("cluster/attributes.csv", "Name", CLUSTER, ALL_NODES),
]
EXCLUDED_ATTRIBUTE_CASES = [(*entity, *column) for entity in EXCLUDED_ATTRIBUTES for column in SPEC_COLUMNS]


# The true totals in whole mcores and MiB; cpu is rounded so float error in the oracle sum cannot hide a lost millicore.
@pytest.mark.parametrize(
    "path,identity_column,identity,selector,column,kind,resource",
    EXCLUDED_ATTRIBUTE_CASES,
    ids=[f"{case[0]}-{case[4]}" for case in EXCLUDED_ATTRIBUTE_CASES],
)
def test_terminal_pods_are_excluded_from_attributes(archive, oracle, path, identity_column, identity, selector, column, kind, resource):
    total = getattr(oracle, kind)(resource, selector)
    expected = round(total * 1000) if resource == "cpu" else int(total) // MIB
    assert int(archive.csv(path).one(**{identity_column: identity})[column]) == expected


# --- container/: restart events, max_restarts and the request series of a churning CronJob ------
# crash restarts every ~102s and churn starts a pod every minute, both pinned to W1 in cr-churn. Truth
# is the snapshots: Kubernetes reports each restart as a lastState and a new running startedAt.

LAG = timedelta(seconds=30)  # a restart can be up to half a snapshot interval older than the snapshot that sees it
WINDOW = timedelta(minutes=3)


def _time(value):
    return datetime.fromisoformat(value)


def _audit_time(archive):
    return _time(archive.csv("cluster/config.csv").one(Name=CLUSTER)["AuditTime"])


def _crash(oracle):
    """The crash container's status in every snapshot, oldest first."""

    return [
        s
        for snapshot in oracle.snapshots
        for p in snapshot.pods
        if (p["metadata"]["namespace"], p["metadata"]["name"]) == (CHURN, "crash")
        for s in _statuses(p, "crash")
    ]


def _finished(oracle):
    """finishedAt and exit code of every termination a snapshot saw as lastState."""

    terminated = (s.get("lastState", {}).get("terminated") for s in _crash(oracle))
    return {_time(t["finishedAt"]): t["exitCode"] for t in terminated if t}


def _restarts(oracle):
    """startedAt of every restarted container a snapshot saw running."""

    return {_time(s["state"]["running"]["startedAt"]) for s in _crash(oracle) if s["restartCount"] >= 1 and "running" in s["state"]}


def test_crash_cadence(oracle):
    """Without the capped back-off the restarts spread out to 300s and the tests below would have nothing to match."""

    counts = [s["restartCount"] for s in _crash(oracle)]
    assert all(0 <= b - a <= 1 for a, b in zip(counts, counts[1:])), f"restartCount between snapshots: {counts}"
    finished = _finished(oracle)
    assert len(_restarts(oracle)) >= 2
    assert set(finished.values()) == {42}
    times = sorted(finished)
    assert all(b - a <= timedelta(seconds=115) for a, b in zip(times, times[1:])), f"finishedAt: {times}"


def test_restart_events(archive, oracle):
    finished, at = _finished(oracle), _audit_time(archive)
    rows = _ours(archive, "container/events.csv", CHURN)
    crash = [r for r in rows if r["EntityName"] == "crash"]
    for row in crash:
        assert (row["EntityType"], row["ContainerName"]) == ("Pod", "crash")
        assert row["ExitCode"] == "42"
        assert row["IsPid1"] == "true"  # decode check only: the restart query encodes no pid1 (container/events.go:143-147)
        assert _time(row["EventTime"]) in finished, f"no termination at {row['EventTime']}"
    times = [_time(r["EventTime"]) for r in crash]
    assert len(set(times)) == len(times), f"repeated EventTime in {sorted(times)}"
    # events come from a 40s increase evaluated every 20s; 60s covers exit to counter, scrape and step
    certain = [f for f in finished if at - WINDOW <= f <= at - timedelta(seconds=60)]
    assert certain
    assert all(times.count(f) == 1 for f in certain), f"certain terminations {sorted(certain)} against events {sorted(times)}"
    assert [r for r in rows if r["EntityName"] != "crash"] == []
    assert _ours(archive, "container/events.csv").rows == []


def test_max_restarts_match_restarts(archive, oracle):
    """Each 1 is one observed restart within the last minute plus the widened query window (container/container.go:793-797)."""

    restarts, at = _restarts(oracle), _audit_time(archive)
    table = archive.csv("container/max_restarts.csv")
    crash = Csv(table.path, table.headers, table.where(Namespace=CHURN, EntityName="crash"))
    assert_metric_grid(crash, KEYS, [(CLUSTER, CHURN, "crash", "Pod", "crash")], _grid(archive))
    series = sorted((_time(r["MetricTime"]), float(r["MaxRestarts"])) for r in crash)
    assert {v for _, v in series} <= {0.0, 1.0}, series
    matched = set()
    for step, value in series:
        if value:
            near = [r for r in sorted(restarts) if step - timedelta(seconds=60) - LAG < r <= step and r not in matched]
            assert near, f"MaxRestarts 1 at {step} with no unmatched restart in {sorted(restarts)}"
            matched.add(near[0])
    lost = [r for r in restarts if at - timedelta(seconds=240) < r <= at - LAG and r not in matched]
    assert not lost, f"restarts {sorted(lost)} left no MaxRestarts 1: {series}"
    quiet = [r for r in table if r["Namespace"] == NAMESPACE or (r["Namespace"] == CHURN and r["EntityName"] != "crash")]
    assert quiet
    assert all(float(r["MaxRestarts"]) == 0 for r in quiet), [r for r in quiet if float(r["MaxRestarts"])]


def _churn_pods(oracle, entity):
    """Pods of an owner in cr-churn, the latest status per uid over all snapshots."""

    return {p["metadata"]["uid"]: p for s in oracle.snapshots for p in s.pods if p["metadata"]["namespace"] == CHURN and p["metadata"]["name"].startswith(f"{entity}-")}


def _spans(oracle, entity):
    """The (startedAt, finishedAt or None) of the owner's container in each of its cr-churn pods; the container is named like the owner."""

    spans = []
    for pod in _churn_pods(oracle, entity).values():
        state = next((s["state"].get("running") or s["state"].get("terminated") for s in _statuses(pod, entity)), None)
        if state:  # no state while the pod is still pending
            spans.append((_time(state["startedAt"]), _time(state["finishedAt"]) if "finishedAt" in state else None))
    return spans


def test_churn_request_series_cover_the_grid(archive, oracle):
    """One churn pod is always running, so a series that spans the grid exists; the collector writes only the first pod's."""

    spans = _spans(oracle, "churn")
    for step in map(_time, _grid(archive)):
        assert any(start <= step and (end is None or end > step) for start, end in spans), f"no churn pod running at {step}: {spans}"
    rows = _ours(archive, "container/ephemeral_storage_request.csv", CHURN)
    rows = Csv(rows.path, rows.headers, rows.where(EntityName="churn"))
    assert_metric_grid(rows, KEYS, [(CLUSTER, CHURN, "churn", "CronJob", "churn")], _grid(archive))
    container = next(c for c in next(iter(_churn_pods(oracle, "churn").values()))["spec"]["containers"] if c["name"] == "churn")
    expected = float(quantity(container["resources"]["requests"]["ephemeral-storage"], "churn ephemeral-storage request"))
    for row in rows:
        assert float(row["EphemeralStorageRequest"]) == pytest.approx(expected, abs=TOLERANCE), f"at {row['MetricTime']}"


FINISHED_AND_RUNNING = {"churn": "CronJob", "mixed": "Job"}  # owners in cr-churn that keep finished pods next to a running one


@pytest.mark.parametrize("entity", FINISHED_AND_RUNNING)
def test_power_state_of_owners_with_finished_pods(archive, oracle, entity):
    """Running while any pod of the owner runs the container, however many finished pods the owner keeps. The collector
    keeps the state of the owner's last pod series (container/collection.go:285-286); mixed's finished pod sorts last."""

    spans, at = _spans(oracle, entity), _audit_time(archive)
    assert any(end is not None and end <= at for _, end in spans), f"no {entity} pod finished before {at}: {spans}"
    assert any(start <= at and (end is None or end > at) for start, end in spans), f"no {entity} pod running at {at}: {spans}"
    rows = _ours(archive, "container/attributes.csv", CHURN).where(EntityName=entity)
    assert [(r["EntityType"], r["ContainerName"], r["PowerState"]) for r in rows] == [(FINISHED_AND_RUNNING[entity], entity, "Running")]
