"""HorizontalPodAutoscalers the collector can attach to a workload, and ones it cannot.

Three HPAs scale Deployments that run containers (one of them with a single metric); two scale
Deployments that never exist. The first three are attached to their target's container rows (container/hpa_*.csv and the Hpa columns of
container/attributes.csv), the second pair can only be reported on their own (hpa/hpa_extra_*.csv).
Every value is a spec value or a Kubernetes replica count, so it is exact.

No metrics-server is installed: current CPU and memory come from the resource metrics API, change on
every HPA sync and have no exact truth, so those series and ScalingLimited, which the controller sets
only after computing a scale from metrics, are not produced and their files must be absent. Without
metrics the controller never rescales the targets, so every value here is stable.
"""

from __future__ import annotations

from collections import Counter

import pytest

from harness.archive import expected_timestamps, label_map, prometheus_labels
from harness.universal import *  # noqa: F403 - collected as this scenario's tests
from harness.spec import (
    BASE_FILES,
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

NAME = "hpa-orphans"
NAMESPACE = "hpa-orphans-workload"
GROUP_LABEL = "topology.kubex.ai/node-group"
_SYSTEM_NODE = {"stack-e2e-system": "system"}
_CONTROL_PLANE_TOLERATION = [
    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"}
]
CLUSTER_SPEC = ClusterSpec(control_planes=[Node(labels=_SYSTEM_NODE)], workers=[Node(labels={GROUP_LABEL: "hpa-workers"})])

MIB = 2**20
# name: (scale target, min, max, {(metric_name, metric_target_type): value}, Kubernetes labels), as workload.yaml
# declares them and kube-state-metrics exports them (percent, cores, bytes)
HPAS = {
    "hpa-a": ("web-a", 2, 4, {("cpu", "utilization"): 50, ("memory", "average"): 64 * MIB}, {"stack-e2e.kubex.ai/hpa": "a"}),
    "hpa-b": ("web-b", 2, 5, {("cpu", "average"): 0.1, ("memory", "utilization"): 60}, {"stack-e2e.kubex.ai/hpa": "b"}),
    "hpa-c": ("web-c", 2, 3, {("cpu", "utilization"): 40}, {"stack-e2e.kubex.ai/hpa": "c"}),
    "ghost-a": ("ghost", 1, 3, {("cpu", "utilization"): 70, ("memory", "average"): 128 * MIB}, {"stack-e2e.kubex.ai/hpa": "ghost-a"}),
    "ghost-b": ("phantom", 3, 6, {("cpu", "average"): 0.2, ("memory", "utilization"): 80}, {"stack-e2e.kubex.ai/hpa": "ghost-b"}),
}
CLASSIFIED, ORPHANS = ("hpa-a", "hpa-b", "hpa-c"), ("ghost-a", "ghost-b")
KEYS = ["ClusterName", "Namespace", "EntityName", "EntityType", "ContainerName", "HpaName"]

# stem: (column, target metric (name, type) or None)
HPA_SERIES = {
    "min_replicas": ("HpaMinReplicas", None),
    "max_replicas": ("HpaMaxReplicas", None),
    "current_replicas": ("HpaCurrentReplicas", None),
    "cpu_target_utilization": ("HpaCpuTargetUtilization", ("cpu", "utilization")),
    "cpu_target_avg": ("HpaCpuTargetAvg", ("cpu", "average")),
    "memory_target_avg": ("HpaMemoryTargetAvg", ("memory", "average")),
    "memory_target_utilization": ("HpaMemoryTargetUtilization", ("memory", "utilization")),
}
# classified HPAs go to container/hpa_<stem>.csv, the others to hpa/hpa_extra_<stem>.csv
SERIES_FILES = [(f"container/hpa_{s}.csv", s, CLASSIFIED) for s in HPA_SERIES] + [
    (f"hpa/hpa_extra_{s}.csv", s, ORPHANS) for s in HPA_SERIES
]
HPA_FILES = [path for path, _, _ in SERIES_FILES] + ["hpa/hpa_extra_config.csv", "hpa/hpa_extra_attributes.csv"]
assert len(HPA_FILES) == 16


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
    resources=["workload.yaml"],
    lifecycle=[Wait("deployment", name, NAMESPACE) for name in ("web-a", "web-b", "web-c")],
    prometheus=PrometheusSpec(
        queries=[
            PrometheusQuery("hpa-info", f'kube_horizontalpodautoscaler_info{{namespace="{NAMESPACE}"}}', min_results=5),
            PrometheusQuery("hpa-spec-targets", f'kube_horizontalpodautoscaler_spec_target_metric{{namespace="{NAMESPACE}"}}', min_results=9),
            # the controller has synced: classified HPAs report their 2 replicas and that no resource metrics are available
            PrometheusQuery("hpa-current-replicas", f'kube_horizontalpodautoscaler_status_current_replicas{{namespace="{NAMESPACE}"}} == 2', min_results=3),
            PrometheusQuery("hpa-no-metrics", f'kube_horizontalpodautoscaler_status_condition{{namespace="{NAMESPACE}",condition="ScalingActive",status="false"}} == 1', min_results=3),
            PrometheusQuery("hpa-status-all", f'kube_horizontalpodautoscaler_status_current_replicas{{namespace="{NAMESPACE}"}}', min_results=5),
            PrometheusQuery("web-running", f'kube_pod_status_phase{{namespace="{NAMESPACE}",phase="Running"}} == 1', min_results=6),
        ],
    ),
    forwarder=ForwarderSpec(
        # history=2 reads [AuditTime-2m, AuditTime-1m]; the HPA series must exist from well before that
        window_seconds=240,
        collection=Collection(interval="minutes", interval_size=1, history=2, sample_rate=1),
    ),
    files=BASE_FILES | frozenset(HPA_FILES),
)


def _identity(hpa):
    return (NAME, NAMESPACE, HPAS[hpa][0], "Deployment", "app", hpa) if hpa in CLASSIFIED else (NAME, NAMESPACE, "", "", "", hpa)


def _replicas(oracle, target):
    """Running pods of the target, which the controller copies into status.currentReplicas even when metrics fail."""

    counts = {
        sum(
            1
            for p in s.pods
            if p["metadata"]["namespace"] == NAMESPACE
            and p["metadata"].get("labels", {}).get("app") == target
            and p.get("status", {}).get("phase") == "Running"
        )
        for s in oracle.snapshots
    }
    assert len(counts) == 1, f"{target} pod count changed during the window: {sorted(counts)}"
    return counts.pop()


def _expected(oracle, stem, hpas):
    """HPA name -> value of the series, for the HPAs whose file this is."""

    metric = HPA_SERIES[stem][1]
    values = {}
    for name in hpas:
        target, low, high, targets, _ = HPAS[name]
        if metric is not None:
            if metric in targets:
                values[name] = targets[metric]
        elif stem == "min_replicas":
            values[name] = low
        elif stem == "max_replicas":
            values[name] = high
        else:
            values[name] = _replicas(oracle, target) if name in CLASSIFIED else 0
    return values


def _audit_time(archive):
    return archive.csv("cluster/config.csv").one(Name=NAME)["AuditTime"]


@pytest.mark.parametrize("path,stem,hpas", SERIES_FILES, ids=[case[0].removesuffix(".csv") for case in SERIES_FILES])
def test_hpa_series_identities(archive, oracle, path, stem, hpas):
    """Classified HPAs only in container/, orphans only in hpa/, each in the files for the metrics it targets."""

    seen = {tuple(row[k] for k in KEYS) for row in archive.csv(path)}
    assert seen == {_identity(name) for name in _expected(oracle, stem, hpas)}, path


def test_hpa_series_cover_every_history_interval(archive, oracle):
    """history=2 runs one range query per interval, each inclusive at both ends: [AT-2m, AT-1m] and
    [AT-1m, AT]. Every identity therefore has one sample at AT-2m, two at AT-1m (the shared boundary)
    and one at AT (internal/common/prometheus.go:254-273, container/collection.go:831-886)."""

    first, middle, last = expected_timestamps(_audit_time(archive), Collection(interval="minutes", interval_size=2, sample_rate=1))
    expected = Counter({first: 1, middle: 2, last: 1})
    failures = []
    for path, stem, hpas in SERIES_FILES:
        times = {}
        for row in archive.csv(path):
            times.setdefault(tuple(row[k] for k in KEYS), []).append(row["MetricTime"])
        for name in _expected(oracle, stem, hpas):
            observed = Counter(times.get(_identity(name), []))
            if observed != expected:
                failures.append(f"{path} {name}: expected {dict(sorted(expected.items()))}, observed {dict(sorted(observed.items()))}")
    assert not failures, "MetricTime counts differ in {} identities:\n{}".format(len(failures), "\n".join(failures))


@pytest.mark.parametrize("path,stem,hpas", SERIES_FILES, ids=[case[0].removesuffix(".csv") for case in SERIES_FILES])
def test_hpa_series_values(archive, oracle, path, stem, hpas):
    column = HPA_SERIES[stem][0]
    expected = _expected(oracle, stem, hpas)
    rows = list(archive.csv(path))
    assert rows, f"{path} has no rows"
    for row in rows:
        assert float(row[column]) == pytest.approx(expected[row["HpaName"]], abs=1e-6), f"{path} {row['HpaName']} at {row['MetricTime']}"


def _target_metrics(name):
    """HpaTargetMetrics entries, "<metric>|<type>|<value %.3f>"."""

    return {f"{metric}|{kind}|{value:.3f}" for (metric, kind), value in HPAS[name][3].items()}


def test_hpa_attributes_on_target_containers(archive):
    attributes = archive.csv("container/attributes.csv")
    for name in CLASSIFIED:
        target, labels = HPAS[name][0], HPAS[name][4]
        row = attributes.one(Namespace=NAMESPACE, EntityName=target, ContainerName="app")
        assert row["HpaName"] == name
        assert prometheus_labels(labels).items() <= label_map(row["HpaLabels"]).items(), name
        assert set(row["HpaTargetMetricName"].split("###")) == {metric for metric, _ in HPAS[name][3]}, name
        if len(HPAS[name][3]) > 1:  # two target metrics reset the single-metric type and value (container/collection.go:565-569)
            assert (row["HpaTargetMetricType"], row["HpaTargetMetricValue"]) == ("", ""), name
        assert set(row["HpaTargetMetrics"].split("###")) == _target_metrics(name), name
    targets = {(NAMESPACE, HPAS[name][0]) for name in CLASSIFIED}
    columns = ["HpaName", "HpaLabels", "HpaTargetMetricName", "HpaTargetMetricType", "HpaTargetMetricValue", "HpaTargetMetrics"]
    others = [row for row in attributes if (row["Namespace"], row["EntityName"]) not in targets]
    assert others
    for row in others:
        assert not any(row[c] for c in columns), f"{row['Namespace']}/{row['EntityName']} has HPA columns"


def test_single_metric_hpa_keeps_its_target_type_and_value(archive):
    """One target metric: the collector prints its type and value (%f) rather than resetting them."""

    ((metric, kind), value), = HPAS["hpa-c"][3].items()
    row = archive.csv("container/attributes.csv").one(Namespace=NAMESPACE, EntityName=HPAS["hpa-c"][0], ContainerName="app")
    assert (row["HpaTargetMetricName"], row["HpaTargetMetricType"], row["HpaTargetMetricValue"]) == (metric, kind, f"{value:.6f}")


def test_orphan_config(archive):
    """Every column of exactly one row per orphan."""

    audit = _audit_time(archive)
    rows = Counter(
        (r["AuditTime"], r["ClusterName"], r["Namespace"], r["EntityName"], r["EntityType"], r["ContainerName"], r["HpaName"], r["OsName"], r["HwManufacturer"])
        for r in archive.csv("hpa/hpa_extra_config.csv")
    )
    assert rows == Counter((audit, NAME, NAMESPACE, "", "", "", name, "Linux", "HPA") for name in ORPHANS)


def test_orphan_attributes_describe_their_targets(archive):
    """The HPA spec is the truth. An orphan has no scale target to attach to, so the collector creates it
    without its target metrics (container/collection.go:593-596,633) and prints the unknown marker."""

    attributes = archive.csv("hpa/hpa_extra_attributes.csv")
    for name in ORPHANS:
        row = attributes.one(HpaName=name)
        assert (row["EntityName"], row["EntityType"], row["ContainerName"]) == ("", "", ""), name
        assert prometheus_labels(HPAS[name][4]).items() <= label_map(row["HpaLabels"]).items(), name
        assert set(row["HpaTargetMetricName"].split("###")) == {"cpu", "memory"}, name
        assert (row["HpaTargetMetricType"], row["HpaTargetMetricValue"]) == ("", ""), name
        assert set(row["HpaTargetMetrics"].split("###")) == _target_metrics(name), name
