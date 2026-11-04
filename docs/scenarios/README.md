# Scenarios

One row per scenario under [`scenarios/`](../../scenarios/). Use this page to find the scenario that
covers a collector file or behaviour. Each scenario also runs the
[universal checks](../architecture.md#coverage-inventory-and-universal-checks).

Every cluster has one control plane, which runs Prometheus, kube-state-metrics and the collector. The
shape column lists the workers.

| Scenario | Proves | Workers | Fixtures |
| --- | --- | --- | --- |
| [cadvisor](cadvisor.md) | Container usage files (CPU, memory, disk, throttling, custom sizing families) match cAdvisor-shaped series. Pod-level cgroup series are ignored. | 1 | `cadvisor-fixture`, which replaces the `kubernetes-nodes-cadvisor` scrape job |
| [container-resources](container-resources.md) | Container requests, limits and attributes for every owner kind. Namespace quota and `rq/` files. Terminal pods excluded from node, group and cluster totals. Restart events and pod churn. JMX-exporter JVM heap attributes and workload files. | 2, one node group, kubelet back-off capped at 1 s | `kubex-beyla` (`survey_info` runtimes), `jvm-app` (JMX exporter series) |
| [ephemeral-storage](ephemeral-storage.md) | Node ephemeral usage and reservation. Container ephemeral request, limit and usage, with a shared volume counted once per pod. | 2, one with 1Gi `system-reserved` | `k8s-ephemeral-storage-metrics` |
| [gpu-device-plugin](gpu-device-plugin.md) | Node, group, cluster and container GPU columns from extended resources, GPU feature discovery labels and DCGM. | 3: 4x A100, GKE-style 1x T4, 1 A100 time-sliced four ways | `dcgm-exporter` |
| [gpu-telemetry-fixtures](gpu-telemetry-fixtures.md) | Container GPU columns come from the Kubex GPU exporter (`kubex_gpu_container_*`), including `exported_*` relabelled series. DCGM without `Hostname` keeps the scrape node. | 1, time-slicing labels, no extended resource | `gpu-process-exporter`, `dcgm-exporter` |
| [hpa-orphans](hpa-orphans.md) | HPAs attach to their target's container rows, with one or two target metrics. HPAs with a missing target go to `hpa/hpa_extra_*`. Metric-dependent files are absent. | 1, no metrics-server, `history=2` | none |
| [ksm-replicas](ksm-replicas.md) | Two kube-state-metrics replicas (duplicate series) still give one value per container, quota and node. | 1 | none |
| [node-exporter](node-exporter.md) | Every node, group and cluster file fed by node exporter matches the fixture. Filtered devices are ignored, and groups exclude the control plane. | 2, one node group | `kubex-prometheus-node-exporter` |
| [node-pressure](node-pressure.md) | Node, group and cluster requests, limits, capacity, allocatable, region, zone and reservation percent match Kubernetes when kubelets reserve resources. | 3: two reserving in one group, one tainted in another | none |
| [nodegroup-labels](nodegroup-labels.md) | A node exporter scraped by pod IP (no `node` label) maps to its node, in node, group and cluster files. | 2, sharing one group label | `pod-ip-node-exporter-1`, `-2`, each pinned to a worker |
| [nodegroup-roles](nodegroup-roles.md) | Role-derived groups: first configured role wins, an unconfigured role forms its own group, a node without a role is in no group. | 3: `infra`+`worker`, `gpu`, none | none |

Run one scenario with `pytest scenarios/<name>`. Each module's docstring is the authoritative one-paragraph
description. New scenario docs follow [_template.md](_template.md).
