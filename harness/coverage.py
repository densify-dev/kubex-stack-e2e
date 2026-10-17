"""Coverage inventory: every CSV the collector can emit, its exact header, and what covers each column.

Source of truth is collector v4.7.5-beta1 (file names from NewWorkloadMetricHolder in
internal/common/workload.go and the per-entity writers; headers from the Fprintln literals and
generateCsvHeaderFormat in internal/common/output.go). Paths the live archive emits were checked
against it. A column is one of: "key" (identity or time), "test:<path>::<function>" (asserted by
that test today), "excluded:<reason>", "pending SME #<n>" (unit ruling outstanding), or
"unclassified" (no scenario asserts it yet). The runtime check fails on missing, renamed or
reordered columns and warns on additions; it never requires zero "unclassified".
"""

from __future__ import annotations

import re
from typing import Sequence

from harness.archive import Archive

KEYS = {
    "AuditTime", "MetricTime", "EventTime", "ClusterName", "Name", "NodeName", "NodeGroupName",
    "Namespace", "EntityName", "EntityType", "ContainerName", "HpaName", "RqName", "CrqName",
}
_T = "test:scenarios/nodegroup-labels/test_nodegroup_labels.py::"
CLASSIFICATION = re.compile(r"key|unclassified|test:[^:]+\.py::test_\w+|excluded:.+|pending SME #\d+[ab]?")


def _csv(header: str, **cols: str) -> dict[str, str]:
    names = header.split(",")
    assert set(cols) <= set(names), f"unknown column(s) {set(cols) - set(names)} in {header}"
    return {c: "key" if c in KEYS else cols.get(c, "unclassified") for c in names}


CL = "Name,MetricTime,"
NO = "ClusterName,NodeName,MetricTime,"
NG = "ClusterName,NodeGroupName,MetricTime,"
CO = "ClusterName,Namespace,EntityName,EntityType,ContainerName,MetricTime,"
HP = "ClusterName,Namespace,EntityName,EntityType,ContainerName,HpaName,MetricTime,"
RQ = "ClusterName,Namespace,RqName,MetricTime,"
CR = "ClusterName,CrqName,MetricTime,"
_OPENSHIFT = "excluded:OpenShift-only, openshift_clusterresourcequota_* is not available on Kind"
_DISK = "excluded:container_fs_usage_bytes is not exported for containerd (cadvisor#2785); Kind runs containerd"


INVENTORY: dict[str, dict[str, str]] = {
    "cluster/config.csv": _csv("AuditTime,Name"),
    "cluster/attributes.csv": _csv("Name,VirtualTechnology,VirtualDomain,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,K8sVersion"),
    "cluster/cpu_requests.csv": _csv(CL + "CpuRequests"),
    "cluster/cpu_reservation_percent.csv": _csv(CL + "CpuReservationPercent"),
    "cluster/memory_requests.csv": _csv(CL + "MemoryRequests"),
    "cluster/memory_reservation_percent.csv": _csv(CL + "MemoryReservationPercent"),
    "cluster/gpu_requests.csv": _csv(CL + "GpuRequests"),
    "cluster/gpu_reservation_percent.csv": _csv(CL + "GpuReservationPercent"),
    "cluster/cpu_utilization.csv": _csv(CL + "CpuUtilization"),
    "cluster/memory_raw_bytes.csv": _csv(CL + "MemoryBytes"),
    "cluster/memory_actual_workload.csv": _csv(CL + "MemoryActualWorkload"),
    "cluster/disk_read_bytes.csv": _csv(CL + "DiskReadBytes"),
    "cluster/disk_write_bytes.csv": _csv(CL + "DiskWriteBytes"),
    "cluster/disk_total_bytes.csv": _csv(CL + "DiskTotalBytes"),
    "cluster/disk_read_ops.csv": _csv(CL + "DiskReadOps"),
    "cluster/disk_write_ops.csv": _csv(CL + "DiskWriteOps"),
    "cluster/disk_total_ops.csv": _csv(CL + "DiskTotalOps"),
    "cluster/net_received_bytes.csv": _csv(CL + "NetReceivedBytes"),
    "cluster/net_sent_bytes.csv": _csv(CL + "NetSentBytes"),
    "cluster/net_total_bytes.csv": _csv(CL + "NetTotalBytes"),
    "cluster/net_received_packets.csv": _csv(CL + "NetReceivedPackets"),
    "cluster/net_sent_packets.csv": _csv(CL + "NetSentPackets"),
    "cluster/net_total_packets.csv": _csv(CL + "NetTotalPackets"),
    "node/config.csv": _csv("AuditTime,ClusterName,NodeName,HwModel,OsName,HwTotalCpus,HwTotalPhysicalCpus,HwCoresPerCpu,HwThreadsPerCore,HwTotalMemory,HwMaxNetworkIoBps"),
    "node/attributes.csv": _csv("ClusterName,NodeName,VirtualTechnology,VirtualDomain,VirtualDatacenter,VirtualCluster,OsArchitecture,NetworkSpeed,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,GpuLimit,GpuRequest,CapacityPods,CapacityCpu,CapacityMemory,CapacityGpu,CapacityEphemeralStorage,CapacityHugePages,AllocatablePods,AllocatableCpu,AllocatableMemory,AllocatableGpu,AllocatableEphemeralStorage,AllocatableHugePages,MemoryTotalBytes,GpuTotal,GpuMemoryTotal,GpuReplicas,ProviderId,K8sVersion,NodeLabels,GpuLabels,NodeTaints,GpuVendor,GpuModel,GpuSharingStrategy,GpuMpsCapable,GpuVgpuPresent,GpuMigCapable,GpuMigStrategy"),
    "node/cpu_limit.csv": _csv(NO + "CpuLimit"),
    "node/cpu_request.csv": _csv(NO + "CpuRequest"),
    "node/memory_limit.csv": _csv(NO + "MemoryLimit"),
    "node/memory_request.csv": _csv(NO + "MemoryRequest"),
    "node/gpu_limit.csv": _csv(NO + "GpuLimit"),
    "node/gpu_request.csv": _csv(NO + "GpuRequest"),
    "node/ephemeral_storage_limit.csv": _csv(NO + "EphemeralStorageLimit", EphemeralStorageLimit="pending SME #4"),
    "node/ephemeral_storage_request.csv": _csv(NO + "EphemeralStorageRequest", EphemeralStorageRequest="pending SME #4"),
    "node/cpu_reservation_percent.csv": _csv(NO + "CpuReservationPercent"),
    "node/memory_reservation_percent.csv": _csv(NO + "MemoryReservationPercent"),
    "node/ephemeral_storage_reservation_percent.csv": _csv(NO + "EphemeralStorageReservationPercent"),
    "node/pod_count.csv": _csv(NO + "PodCount"),
    "node/ephemeral_storage_usage_bytes.csv": _csv(NO + "EphemeralStorageUsageBytes"),
    "node/ephemeral_storage_usage_utilization.csv": _csv(NO + "EphemeralStorageUsageUtilization"),
    "node/gpu_utilization_avg.csv": _csv(NO + "GpuUtilizationAvg"),
    "node/gpu_utilization_gpus_avg.csv": _csv(NO + "GpuUtilizationGpusAvg"),
    "node/gpu_mem_utilization_avg.csv": _csv(NO + "GpuMemUtilizationAvg"),
    "node/gpu_mem_used_avg.csv": _csv(NO + "GpuMemUsedAvg"),
    "node/gpu_power_usage_avg.csv": _csv(NO + "GpuPowerUsageAvg"),
    "node/cpu_utilization.csv": _csv(NO + "CpuUtilization"),
    "node/memory_raw_bytes.csv": _csv(NO + "MemoryBytes"),
    "node/memory_utilization.csv": _csv(NO + "MemoryUtilization"),
    "node/memory_actual_workload.csv": _csv(NO + "MemoryActualWorkload"),
    "node/memory_actual_utilization.csv": _csv(NO + "MemoryActualUtilization"),
    "node/memory_ws.csv": _csv(NO + "MemoryWs"),
    "node/memory_ws_utilization.csv": _csv(NO + "MemoryWsUtilization"),
    "node/oom_kill_events.csv": _csv(NO + "OomKillEvents"),
    "node/cpu_throttling_events.csv": _csv(NO + "CpuThrottlingEvents"),
    "node/disk_read_bytes.csv": _csv(NO + "DiskReadBytes"),
    "node/disk_write_bytes.csv": _csv(NO + "DiskWriteBytes"),
    "node/disk_total_bytes.csv": _csv(NO + "DiskTotalBytes"),
    "node/disk_read_ops.csv": _csv(NO + "DiskReadOps"),
    "node/disk_write_ops.csv": _csv(NO + "DiskWriteOps"),
    "node/disk_total_ops.csv": _csv(NO + "DiskTotalOps"),
    "node/net_received_bytes.csv": _csv(NO + "NetReceivedBytes"),
    "node/net_sent_bytes.csv": _csv(NO + "NetSentBytes"),
    "node/net_total_bytes.csv": _csv(NO + "NetTotalBytes"),
    "node/net_received_packets.csv": _csv(NO + "NetReceivedPackets"),
    "node/net_sent_packets.csv": _csv(NO + "NetSentPackets"),
    "node/net_total_packets.csv": _csv(NO + "NetTotalPackets"),
    "node_group/config.csv": _csv("AuditTime,ClusterName,NodeGroupName,HwTotalCpus,HwTotalPhysicalCpus,HwCoresPerCpu,HwThreadsPerCore,HwTotalMemory,HwModel,OsName", HwTotalCpus=_T + "test_config_total_cpus", HwTotalMemory="pending SME #9", OsName=_T + "test_config_os_name"),
    "node_group/attributes.csv": _csv("ClusterName,NodeGroupName,VirtualTechnology,VirtualDomain,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,CurrentSize,CurrentNodes,NodeLabels", VirtualTechnology=_T + "test_attributes_describe_a_node_group", CpuLimit=_T + "test_attribute_cpu_limit", CpuRequest=_T + "test_attribute_cpu_request", MemoryLimit=_T + "test_attribute_memory_limit", MemoryRequest=_T + "test_attribute_memory_request", VirtualDomain=_T + "test_attributes_describe_a_node_group", CurrentSize=_T + "test_both_workers_are_in_the_group", CurrentNodes=_T + "test_both_workers_are_in_the_group"),
    "node_group/current_size.csv": _csv(NG + "CurrentSize", CurrentSize=_T + "test_current_size_holds_for_every_sample"),
    "node_group/cpu_requests.csv": _csv(NG + "CpuRequests", CpuRequests=_T + "test_requests_match_the_cluster"),
    "node_group/cpu_reservation_percent.csv": _csv(NG + "CpuReservationPercent", CpuReservationPercent=_T + "test_reservation_percent_matches_the_cluster"),
    "node_group/memory_requests.csv": _csv(NG + "MemoryRequests", MemoryRequests=_T + "test_requests_match_the_cluster"),
    "node_group/memory_reservation_percent.csv": _csv(NG + "MemoryReservationPercent", MemoryReservationPercent=_T + "test_reservation_percent_matches_the_cluster"),
    "node_group/gpu_requests.csv": _csv(NG + "GpuRequests"),
    "node_group/gpu_reservation_percent.csv": _csv(NG + "GpuReservationPercent"),
    "node_group/cpu_utilization.csv": _csv(NG + "CpuUtilization"),
    "node_group/memory_raw_bytes.csv": _csv(NG + "MemoryBytes"),
    "node_group/memory_actual_workload.csv": _csv(NG + "MemoryActualWorkload"),
    "node_group/disk_read_bytes.csv": _csv(NG + "DiskReadBytes"),
    "node_group/disk_write_bytes.csv": _csv(NG + "DiskWriteBytes"),
    "node_group/disk_total_bytes.csv": _csv(NG + "DiskTotalBytes"),
    "node_group/disk_read_ops.csv": _csv(NG + "DiskReadOps"),
    "node_group/disk_write_ops.csv": _csv(NG + "DiskWriteOps"),
    "node_group/disk_total_ops.csv": _csv(NG + "DiskTotalOps"),
    "node_group/net_received_bytes.csv": _csv(NG + "NetReceivedBytes"),
    "node_group/net_sent_bytes.csv": _csv(NG + "NetSentBytes"),
    "node_group/net_total_bytes.csv": _csv(NG + "NetTotalBytes"),
    "node_group/net_received_packets.csv": _csv(NG + "NetReceivedPackets"),
    "node_group/net_sent_packets.csv": _csv(NG + "NetSentPackets"),
    "node_group/net_total_packets.csv": _csv(NG + "NetTotalPackets"),
    "node_group/gpu_utilization_avg.csv": _csv(NG + "GpuUtilizationAvg"),
    "node_group/gpu_utilization_gpus_avg.csv": _csv(NG + "GpuUtilizationGpusAvg"),
    "node_group/gpu_mem_utilization_avg.csv": _csv(NG + "GpuMemUtilizationAvg"),
    "node_group/gpu_mem_used_avg.csv": _csv(NG + "GpuMemUsedAvg"),
    "node_group/gpu_power_usage_avg.csv": _csv(NG + "GpuPowerUsageAvg"),
    "container/config.csv": _csv("AuditTime,ClusterName,Namespace,EntityName,EntityType,ContainerName,HwTotalMemory,GpuMemoryTotal,OsName,HwManufacturer"),
    "container/attributes.csv": _csv("ClusterName,Namespace,EntityName,EntityType,ContainerName,ContainerType,VirtualTechnology,VirtualDomain,VirtualDatacenter,VirtualCluster,ContainerLabels,PodLabels,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,GpuLimit,GpuRequest,GpuLimitFloat,GpuRequestFloat,CurrentNodes,PowerState,CreatedByKind,CreatedByName,CurrentSize,CreateTime,ContainerRestarts,NamespaceLabels,NamespaceCpuRequest,NamespaceCpuLimit,NamespaceMemoryRequest,NamespaceMemoryLimit,NamespacePodsLimit,HpaName,HpaLabels,HpaTargetMetricName,HpaTargetMetricType,HpaTargetMetricValue,HpaTargetMetrics,QosClass,GpuModel,GpuSharingStrategy,EphemeralStorageRequest,EphemeralStorageLimit,Runtimes", EphemeralStorageRequest="pending SME #4", EphemeralStorageLimit="pending SME #4"),
    "container/events.csv": _csv("ClusterName,Namespace,EntityName,EntityType,ContainerName,EventTime,ExitCode,IsPid1"),
    "container/current_size.csv": _csv(CO + "CurrentSize"),
    "container/cpu_limit.csv": _csv(CO + "CpuLimit"),
    "container/cpu_request.csv": _csv(CO + "CpuRequest"),
    "container/memory_limit.csv": _csv(CO + "MemoryLimit"),
    "container/memory_request.csv": _csv(CO + "MemoryRequest"),
    "container/gpu_limit.csv": _csv(CO + "GpuLimit"),
    "container/gpu_request.csv": _csv(CO + "GpuRequest"),
    "container/ephemeral_storage_limit.csv": _csv(CO + "EphemeralStorageLimit", EphemeralStorageLimit="pending SME #4"),
    "container/ephemeral_storage_request.csv": _csv(CO + "EphemeralStorageRequest", EphemeralStorageRequest="pending SME #4"),
    "container/max_restarts.csv": _csv(CO + "MaxRestarts"),
    "container/avg_cpu_mcores_workload.csv": _csv(CO + "AvgCpuMcores"),
    "container/avg_custom_cpu_mcores_workload.csv": _csv(CO + "AvgCustomCpuMcores"),
    "container/avg_mem_workload.csv": _csv(CO + "AvgMem", AvgMem="pending SME #6a"),
    "container/avg_rss_workload.csv": _csv(CO + "AvgRss", AvgRss="pending SME #6a"),
    "container/avg_ws_workload.csv": _csv(CO + "AvgWs", AvgWs="pending SME #6a"),
    "container/avg_custom_memory_workload.csv": _csv(CO + "AvgCustomMemory", AvgCustomMemory="pending SME #6a"),
    "container/avg_disk_workload.csv": _csv(CO + "AvgDisk", AvgDisk=_DISK),
    "container/avg_cpu_throttling_percent_workload.csv": _csv(CO + "AvgCpuThrottlingPercent"),
    "container/ephemeral_storage_usage_bytes_avg_workload.csv": _csv(CO + "EphemeralStorageUsageBytesAvg"),
    "container/gpu_utilization_avg_workload.csv": _csv(CO + "GpuUtilizationAvg"),
    "container/gpu_utilization_gpus_avg_workload.csv": _csv(CO + "GpuUtilizationGpusAvg"),
    "container/gpu_mem_utilization_avg_workload.csv": _csv(CO + "GpuMemUtilizationAvg"),
    "container/gpu_mem_used_avg_workload.csv": _csv(CO + "GpuMemUsedAvg"),
    "container/gpu_power_usage_avg_workload.csv": _csv(CO + "GpuPowerUsageAvg"),
    "container/max_cpu_mcores_workload.csv": _csv(CO + "MaxCpuMcores"),
    "container/max_custom_cpu_mcores_workload.csv": _csv(CO + "MaxCustomCpuMcores"),
    "container/max_mem_workload.csv": _csv(CO + "MaxMem", MaxMem="pending SME #6b"),
    "container/max_rss_workload.csv": _csv(CO + "MaxRss", MaxRss="pending SME #6b"),
    "container/max_ws_workload.csv": _csv(CO + "MaxWs", MaxWs="pending SME #6b"),
    "container/max_custom_memory_workload.csv": _csv(CO + "MaxCustomMemory", MaxCustomMemory="pending SME #6b"),
    "container/max_disk_workload.csv": _csv(CO + "MaxDisk", MaxDisk=_DISK),
    "container/max_cpu_throttling_percent_workload.csv": _csv(CO + "MaxCpuThrottlingPercent"),
    "container/ephemeral_storage_usage_bytes_max_workload.csv": _csv(CO + "EphemeralStorageUsageBytesMax"),
    "container/gpu_utilization_max_workload.csv": _csv(CO + "GpuUtilizationMax"),
    "container/gpu_utilization_gpus_max_workload.csv": _csv(CO + "GpuUtilizationGpusMax"),
    "container/gpu_mem_utilization_max_workload.csv": _csv(CO + "GpuMemUtilizationMax"),
    "container/gpu_mem_used_max_workload.csv": _csv(CO + "GpuMemUsedMax"),
    "container/gpu_power_usage_max_workload.csv": _csv(CO + "GpuPowerUsageMax"),
    "container/sum_cpu_throttling_seconds_workload.csv": _csv(CO + "SumCpuThrottlingSeconds"),
    "container/hpa_max_replicas.csv": _csv(HP + "HpaMaxReplicas"),
    "container/hpa_min_replicas.csv": _csv(HP + "HpaMinReplicas"),
    "container/hpa_current_replicas.csv": _csv(HP + "HpaCurrentReplicas"),
    "container/hpa_condition_scaling_limited.csv": _csv(HP + "HpaConditionScalingLimited"),
    "container/hpa_cpu_target_avg.csv": _csv(HP + "HpaCpuTargetAvg"),
    "container/hpa_cpu_target_utilization.csv": _csv(HP + "HpaCpuTargetUtilization"),
    "container/hpa_cpu_current_avg.csv": _csv(HP + "HpaCpuCurrentAvg"),
    "container/hpa_cpu_current_utilization.csv": _csv(HP + "HpaCpuCurrentUtilization"),
    "container/hpa_memory_target_avg.csv": _csv(HP + "HpaMemoryTargetAvg"),
    "container/hpa_memory_target_utilization.csv": _csv(HP + "HpaMemoryTargetUtilization"),
    "container/hpa_memory_current_avg.csv": _csv(HP + "HpaMemoryCurrentAvg"),
    "container/hpa_memory_current_utilization.csv": _csv(HP + "HpaMemoryCurrentUtilization"),
    "hpa/hpa_extra_config.csv": _csv("AuditTime,ClusterName,Namespace,EntityName,EntityType,ContainerName,HpaName,OsName,HwManufacturer"),
    "hpa/hpa_extra_attributes.csv": _csv("ClusterName,Namespace,EntityName,EntityType,ContainerName,HpaName,HpaLabels,HpaTargetMetricName,HpaTargetMetricType,HpaTargetMetricValue,HpaTargetMetrics"),
    "hpa/hpa_extra_max_replicas.csv": _csv(HP + "HpaMaxReplicas"),
    "hpa/hpa_extra_min_replicas.csv": _csv(HP + "HpaMinReplicas"),
    "hpa/hpa_extra_current_replicas.csv": _csv(HP + "HpaCurrentReplicas"),
    "hpa/hpa_extra_condition_scaling_limited.csv": _csv(HP + "HpaConditionScalingLimited"),
    "hpa/hpa_extra_cpu_target_avg.csv": _csv(HP + "HpaCpuTargetAvg"),
    "hpa/hpa_extra_cpu_target_utilization.csv": _csv(HP + "HpaCpuTargetUtilization"),
    "hpa/hpa_extra_cpu_current_avg.csv": _csv(HP + "HpaCpuCurrentAvg"),
    "hpa/hpa_extra_cpu_current_utilization.csv": _csv(HP + "HpaCpuCurrentUtilization"),
    "hpa/hpa_extra_memory_target_avg.csv": _csv(HP + "HpaMemoryTargetAvg"),
    "hpa/hpa_extra_memory_target_utilization.csv": _csv(HP + "HpaMemoryTargetUtilization"),
    "hpa/hpa_extra_memory_current_avg.csv": _csv(HP + "HpaMemoryCurrentAvg"),
    "hpa/hpa_extra_memory_current_utilization.csv": _csv(HP + "HpaMemoryCurrentUtilization"),
    "rq/config.csv": _csv("AuditTime,ClusterName,Namespace,RqName"),
    "rq/attributes.csv": _csv("ClusterName,Namespace,RqName,VirtualTechnology,VirtualDomain,VirtualDatacenter,CreateTime,ResourceMetadata,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,CurrentSize,NamespaceCpuLimit,NamespaceCpuRequest,NamespaceMemoryLimit,NamespaceMemoryRequest,NamespacePodsLimit"),
    "rq/cpu_limits.csv": _csv(RQ + "CpuLimits"),
    "rq/cpu_requests.csv": _csv(RQ + "CpuRequests"),
    "rq/mem_limits.csv": _csv(RQ + "MemLimits", MemLimits="pending SME #3"),
    "rq/mem_requests.csv": _csv(RQ + "MemRequests"),
    "rq/pods.csv": _csv(RQ + "PodsLimits"),
    "crq/config.csv": _csv("AuditTime,ClusterName,CrqName"),
    "crq/attributes.csv": _csv("ClusterName,CrqName,VirtualTechnology,VirtualDomain,VirtualDatacenter,VirtualCluster,SelectorType,SelectorKey,SelectorValue,CreateTime,NamespaceLabels,ResourceMetadata,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,CurrentSize,NamespaceCpuLimit,NamespaceCpuRequest,NamespaceMemoryLimit,NamespaceMemoryRequest,NamespacePodsLimit,Namespaces"),
    "crq/cpu_limits.csv": _csv(CR + "CpuLimits"),
    "crq/cpu_requests.csv": _csv(CR + "CpuRequests"),
    "crq/mem_limits.csv": _csv(CR + "MemLimits"),
    "crq/mem_requests.csv": _csv(CR + "MemRequests"),
    "crq/pods.csv": _csv(CR + "PodsLimits"),
}

for _path, _cols in INVENTORY.items():
    if _path.startswith("crq/"):
        _cols.update({c: _OPENSHIFT for c, v in _cols.items() if v == "unclassified"})


def check_archive(archive: Archive) -> tuple[list[str], list[str]]:
    """(errors, warnings) for the archive's CSV paths against INVENTORY. log.txt and non-CSV are ignored."""

    errors: list[str] = []
    warnings: list[str] = []
    for path in archive.paths:
        if not path.lower().endswith(".csv"):
            continue
        headers = archive.csv(path).headers
        known = list(INVENTORY.get(path, ()))
        if path not in INVENTORY:
            warnings.append(f"{path}: not in the coverage inventory (new CSV, columns {headers!r})")
            continue
        if [h for h in headers if h in known] != known:
            errors.append(f"{path}: header {headers!r} lost, renamed or reordered columns of {known!r}")
        elif len(headers) > len(known):
            warnings.append(f"{path}: new column(s) {[h for h in headers if h not in known]!r} not in the inventory")
    return errors, warnings


def workflow_warning(warnings: Sequence[str]) -> str:
    """One GitHub Actions annotation line, escaped per the workflow-command rules."""

    message = "; ".join(warnings).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title=Coverage review needed::{message}"
