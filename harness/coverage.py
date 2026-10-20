"""Coverage inventory: every CSV the collector can emit, its exact header, and what covers each column.

Source of truth is collector v4.7.5-beta1 (file names from NewWorkloadMetricHolder in
internal/common/workload.go and the per-entity writers; headers from the Fprintln literals and
generateCsvHeaderFormat in internal/common/output.go). Paths the live archive emits were checked
against it. A column is one of: "key" (identity or time), "test:<path>::<function>[,<path>::<function>...]"
(asserted by those tests today), "excluded:<reason>", or "unclassified" (no scenario asserts it yet). The runtime
check fails on missing, renamed or reordered columns and warns on additions; it never requires
zero "unclassified".
"""

from __future__ import annotations

from typing import Sequence

from harness.archive import Archive

KEYS = {
    "AuditTime", "MetricTime", "EventTime", "ClusterName", "Name", "NodeName", "NodeGroupName",
    "Namespace", "EntityName", "EntityType", "ContainerName", "HpaName", "RqName", "CrqName",
}
_NGL = "scenarios/nodegroup-labels/test_nodegroup_labels.py::"
_NP = "scenarios/node-pressure/test_node_pressure.py::"
_NGR = "scenarios/nodegroup-roles/test_nodegroup_roles.py::"
_NE = "scenarios/node-exporter/test_node_exporter.py::"
_EPH = "scenarios/ephemeral-storage/test_ephemeral_storage.py::"
_CR = "scenarios/container-resources/test_container_resources.py::"
_HPA = "scenarios/hpa-orphans/test_hpa_orphans.py::"
_CAD = "scenarios/cadvisor/test_cadvisor.py::"


def _t(*refs: str) -> str:
    """Several asserting tests for one column: "test:<ref>,<ref>"."""

    return "test:" + ",".join(refs)


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


_NODE_SERIES = _t(_NP + "test_node_series_match_the_cluster")
_NODE_SERIES_TERMINAL = _t(_NP + "test_node_series_match_the_cluster", _CR + "test_terminal_pods_are_excluded_from_series")
_TERMINAL_ATTRIBUTES = _CR + "test_terminal_pods_are_excluded_from_attributes"
_CONTAINER_ATTRIBUTES = {
    **dict.fromkeys(
        ("VirtualTechnology", "VirtualDomain", "VirtualDatacenter", "VirtualCluster", "ContainerType", "CreatedByKind", "CreatedByName"),
        _t(_CR + "test_attribute_identity_columns"),
    ),
    **dict.fromkeys(
        ("HpaLabels", "HpaTargetMetricName", "HpaTargetMetricType", "HpaTargetMetricValue", "HpaTargetMetrics"),
        _t(_HPA + "test_hpa_attributes_on_target_containers"),
    ),
    "ContainerLabels": _t(_CR + "test_container_labels_carry_the_image"),
    "PodLabels": _t(_CR + "test_pod_labels"),
    **dict.fromkeys(("CpuLimit", "CpuRequest", "MemoryLimit", "MemoryRequest"), _t(_CR + "test_request_and_limit_attributes")),
    "CurrentNodes": _t(_CR + "test_current_nodes"),
    "PowerState": _t(_CR + "test_power_state"),
    "CurrentSize": _t(_CR + "test_current_size_attribute"),
    "CreateTime": _t(_CR + "test_create_time"),
    "ContainerRestarts": _t(_CR + "test_container_restarts"),
    "QosClass": _t(_CR + "test_qos_class"),
    "Runtimes": _t(_CR + "test_runtimes_come_from_the_survey"),
    "NamespaceLabels": _t(_CR + "test_namespace_labels"),
    **dict.fromkeys(
        ("NamespaceCpuRequest", "NamespaceCpuLimit", "NamespaceMemoryRequest", "NamespaceMemoryLimit", "NamespacePodsLimit"),
        _t(_CR + "test_namespace_quota_columns"),
    ),
}
_HPA_NO_CURRENT = "excluded:needs the resource metrics API; metrics-server is not installed because real usage changes on every HPA sync and has no exact truth, so the file is asserted absent"
_HPA_NO_SCALING_LIMITED = "excluded:the HPA controller sets ScalingLimited only after computing a scale from metrics, which needs metrics-server (not installed, see the current-metric files); the file is asserted absent"
_HPA_EXTRA_NO_METRICS = "excluded:an unclassified HPA's scale target has no running containers, so its controller never reports current metrics or ScalingLimited; the file is asserted absent here"
_CAD_USAGE = _t(_CAD + "test_usage_matches_the_fixture")
_RQ_SERIES = _t(_CR + "test_quota_series_match_status_used")
_NODE_CONFIG = {
    **dict.fromkeys(("HwTotalCpus", "HwTotalPhysicalCpus", "HwCoresPerCpu", "HwThreadsPerCore", "HwTotalMemory"), _t(_NP + "test_node_config_hardware")),
    "OsName": _t(_NP + "test_node_config_os_name"),
    "HwModel": _t(_NP + "test_node_config_model_and_network"),
    "HwMaxNetworkIoBps": _t(_NP + "test_node_config_model_and_network", _NE + "test_node_network_speed_and_memory_total"),
}
_NODE_ATTRIBUTES = {
    **dict.fromkeys(("VirtualTechnology", "VirtualDomain", "OsArchitecture", "ProviderId", "K8sVersion"), _t(_NP + "test_node_attributes_identify_the_node")),
    **dict.fromkeys(("CpuLimit", "CpuRequest", "MemoryLimit", "MemoryRequest"), _t(_NP + "test_node_attribute_requests_and_limits", _TERMINAL_ATTRIBUTES)),
    **dict.fromkeys(
        [f"{kind}{name}" for kind in ("Capacity", "Allocatable") for name in ("Pods", "Cpu", "Memory", "EphemeralStorage")],
        _t(_NP + "test_node_attribute_capacity_and_allocatable"),
    ),
    "NodeTaints": _t(_NP + "test_node_attribute_taints"),
    **dict.fromkeys(("NetworkSpeed", "MemoryTotalBytes"), _t(_NE + "test_node_network_speed_and_memory_total")),
    "NodeLabels": _t(_NP + "test_node_attribute_labels"),
}
_GROUP_CONFIG = {
    "HwTotalCpus": _t(_NGL + "test_config_total_cpus", _NP + "test_group_config_hardware", _NGR + "test_config_hardware"),
    **dict.fromkeys(("HwTotalPhysicalCpus", "HwCoresPerCpu", "HwThreadsPerCore"), _t(_NP + "test_group_config_hardware", _NGR + "test_config_hardware")),
    "HwTotalMemory": _t(_NGL + "test_config_total_memory_in_bytes", _NP + "test_group_config_hardware", _NGR + "test_config_hardware"),
    "HwModel": _t(_NP + "test_group_config_os_and_model", _NGR + "test_config_os_and_model"),
    "OsName": _t(_NGL + "test_config_os_name", _NP + "test_group_config_os_and_model", _NGR + "test_config_os_and_model"),
}
_GROUP_ATTRIBUTES = {
    **dict.fromkeys(("VirtualTechnology", "VirtualDomain"), _t(_NGL + "test_attributes_describe_a_node_group", _NP + "test_node_group_membership", _NGR + "test_group_membership")),
    **dict.fromkeys(("CurrentSize", "CurrentNodes"), _t(_NGL + "test_both_workers_are_in_the_group", _NP + "test_node_group_membership", _NGR + "test_group_membership")),
    "CpuLimit": _t(_NGL + "test_attribute_cpu_limit", _NP + "test_group_attribute_requests_and_limits", _NGR + "test_attribute_requests_and_limits", _TERMINAL_ATTRIBUTES),
    "CpuRequest": _t(_NGL + "test_attribute_cpu_request", _NP + "test_group_attribute_requests_and_limits", _NGR + "test_attribute_requests_and_limits", _TERMINAL_ATTRIBUTES),
    "MemoryLimit": _t(_NGL + "test_attribute_memory_limit", _NP + "test_group_attribute_requests_and_limits", _NGR + "test_attribute_requests_and_limits", _TERMINAL_ATTRIBUTES),
    "MemoryRequest": _t(_NGL + "test_attribute_memory_request", _NP + "test_group_attribute_requests_and_limits", _NGR + "test_attribute_requests_and_limits", _TERMINAL_ATTRIBUTES),
    "NodeLabels": _t(_NP + "test_group_attribute_labels", _NGR + "test_attribute_labels"),
}
_GROUP_RESERVATION = _t(_NGL + "test_reservation_percent_matches_the_cluster", _NP + "test_group_reservation_percent_matches_the_cluster", _NGR + "test_reservation_percent_matches_the_cluster")


INVENTORY: dict[str, dict[str, str]] = {
    "cluster/config.csv": _csv("AuditTime,Name"),
    "cluster/attributes.csv": _csv("Name,VirtualTechnology,VirtualDomain,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,K8sVersion", **dict.fromkeys(("VirtualTechnology", "VirtualDomain", "K8sVersion"), _t(_NP + "test_cluster_attributes_match_the_cluster")), **dict.fromkeys(("CpuLimit", "CpuRequest", "MemoryLimit", "MemoryRequest"), _t(_NP + "test_cluster_attributes_match_the_cluster", _TERMINAL_ATTRIBUTES))),
    "cluster/cpu_requests.csv": _csv(CL + "CpuRequests", CpuRequests=_t(_NP + "test_cluster_requests_match_the_cluster", _CR + "test_terminal_pods_are_excluded_from_series")),
    "cluster/cpu_reservation_percent.csv": _csv(CL + "CpuReservationPercent", CpuReservationPercent=_t(_NP + "test_cluster_reservation_percent_matches_the_cluster")),
    "cluster/memory_requests.csv": _csv(CL + "MemoryRequests", MemoryRequests=_t(_NP + "test_cluster_requests_match_the_cluster", _CR + "test_terminal_pods_are_excluded_from_series")),
    "cluster/memory_reservation_percent.csv": _csv(CL + "MemoryReservationPercent", MemoryReservationPercent=_t(_NP + "test_cluster_reservation_percent_matches_the_cluster")),
    "cluster/gpu_requests.csv": _csv(CL + "GpuRequests"),
    "cluster/gpu_reservation_percent.csv": _csv(CL + "GpuReservationPercent"),
    "cluster/cpu_utilization.csv": _csv(CL + "CpuUtilization", CpuUtilization=_t(_NE + "test_values_match_the_fixture")),
    "cluster/memory_raw_bytes.csv": _csv(CL + "MemoryBytes", MemoryBytes=_t(_NE + "test_values_match_the_fixture")),
    "cluster/memory_actual_workload.csv": _csv(CL + "MemoryActualWorkload", MemoryActualWorkload=_t(_NE + "test_values_match_the_fixture")),
    "cluster/disk_read_bytes.csv": _csv(CL + "DiskReadBytes", DiskReadBytes=_t(_NE + "test_values_match_the_fixture")),
    "cluster/disk_write_bytes.csv": _csv(CL + "DiskWriteBytes", DiskWriteBytes=_t(_NE + "test_values_match_the_fixture")),
    "cluster/disk_total_bytes.csv": _csv(CL + "DiskTotalBytes", DiskTotalBytes=_t(_NE + "test_values_match_the_fixture")),
    "cluster/disk_read_ops.csv": _csv(CL + "DiskReadOps", DiskReadOps=_t(_NE + "test_values_match_the_fixture")),
    "cluster/disk_write_ops.csv": _csv(CL + "DiskWriteOps", DiskWriteOps=_t(_NE + "test_values_match_the_fixture")),
    "cluster/disk_total_ops.csv": _csv(CL + "DiskTotalOps", DiskTotalOps=_t(_NE + "test_values_match_the_fixture")),
    "cluster/net_received_bytes.csv": _csv(CL + "NetReceivedBytes", NetReceivedBytes=_t(_NE + "test_values_match_the_fixture")),
    "cluster/net_sent_bytes.csv": _csv(CL + "NetSentBytes", NetSentBytes=_t(_NE + "test_values_match_the_fixture")),
    "cluster/net_total_bytes.csv": _csv(CL + "NetTotalBytes", NetTotalBytes=_t(_NE + "test_values_match_the_fixture")),
    "cluster/net_received_packets.csv": _csv(CL + "NetReceivedPackets", NetReceivedPackets=_t(_NE + "test_values_match_the_fixture")),
    "cluster/net_sent_packets.csv": _csv(CL + "NetSentPackets", NetSentPackets=_t(_NE + "test_values_match_the_fixture")),
    "cluster/net_total_packets.csv": _csv(CL + "NetTotalPackets", NetTotalPackets=_t(_NE + "test_values_match_the_fixture")),
    "node/config.csv": _csv("AuditTime,ClusterName,NodeName,HwModel,OsName,HwTotalCpus,HwTotalPhysicalCpus,HwCoresPerCpu,HwThreadsPerCore,HwTotalMemory,HwMaxNetworkIoBps", **_NODE_CONFIG),
    "node/attributes.csv": _csv("ClusterName,NodeName,VirtualTechnology,VirtualDomain,VirtualDatacenter,VirtualCluster,OsArchitecture,NetworkSpeed,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,GpuLimit,GpuRequest,CapacityPods,CapacityCpu,CapacityMemory,CapacityGpu,CapacityEphemeralStorage,CapacityHugePages,AllocatablePods,AllocatableCpu,AllocatableMemory,AllocatableGpu,AllocatableEphemeralStorage,AllocatableHugePages,MemoryTotalBytes,GpuTotal,GpuMemoryTotal,GpuReplicas,ProviderId,K8sVersion,NodeLabels,GpuLabels,NodeTaints,GpuVendor,GpuModel,GpuSharingStrategy,GpuMpsCapable,GpuVgpuPresent,GpuMigCapable,GpuMigStrategy", **_NODE_ATTRIBUTES),
    "node/cpu_limit.csv": _csv(NO + "CpuLimit", CpuLimit=_NODE_SERIES_TERMINAL),
    "node/cpu_request.csv": _csv(NO + "CpuRequest", CpuRequest=_NODE_SERIES_TERMINAL),
    "node/memory_limit.csv": _csv(NO + "MemoryLimit", MemoryLimit=_NODE_SERIES_TERMINAL),
    "node/memory_request.csv": _csv(NO + "MemoryRequest", MemoryRequest=_NODE_SERIES_TERMINAL),
    "node/gpu_limit.csv": _csv(NO + "GpuLimit"),
    "node/gpu_request.csv": _csv(NO + "GpuRequest"),
    "node/ephemeral_storage_limit.csv": _csv(NO + "EphemeralStorageLimit", EphemeralStorageLimit=_t(_EPH + "test_node_request_limit_and_reservation")),
    "node/ephemeral_storage_request.csv": _csv(NO + "EphemeralStorageRequest", EphemeralStorageRequest=_t(_EPH + "test_node_request_limit_and_reservation")),
    "node/cpu_reservation_percent.csv": _csv(NO + "CpuReservationPercent", CpuReservationPercent=_NODE_SERIES),
    "node/memory_reservation_percent.csv": _csv(NO + "MemoryReservationPercent", MemoryReservationPercent=_NODE_SERIES),
    "node/ephemeral_storage_reservation_percent.csv": _csv(NO + "EphemeralStorageReservationPercent", EphemeralStorageReservationPercent=_t(_EPH + "test_node_request_limit_and_reservation")),
    "node/pod_count.csv": _csv(NO + "PodCount", PodCount=_NODE_SERIES_TERMINAL),
    "node/ephemeral_storage_usage_bytes.csv": _csv(NO + "EphemeralStorageUsageBytes", EphemeralStorageUsageBytes=_t(_EPH + "test_node_usage_matches_the_fixture")),
    "node/ephemeral_storage_usage_utilization.csv": _csv(NO + "EphemeralStorageUsageUtilization", EphemeralStorageUsageUtilization=_t(_EPH + "test_node_usage_matches_the_fixture")),
    "node/gpu_utilization_avg.csv": _csv(NO + "GpuUtilizationAvg"),
    "node/gpu_utilization_gpus_avg.csv": _csv(NO + "GpuUtilizationGpusAvg"),
    "node/gpu_mem_utilization_avg.csv": _csv(NO + "GpuMemUtilizationAvg"),
    "node/gpu_mem_used_avg.csv": _csv(NO + "GpuMemUsedAvg"),
    "node/gpu_power_usage_avg.csv": _csv(NO + "GpuPowerUsageAvg"),
    "node/cpu_utilization.csv": _csv(NO + "CpuUtilization", CpuUtilization=_t(_NE + "test_values_match_the_fixture")),
    "node/memory_raw_bytes.csv": _csv(NO + "MemoryBytes", MemoryBytes=_t(_NE + "test_values_match_the_fixture")),
    "node/memory_utilization.csv": _csv(NO + "MemoryUtilization", MemoryUtilization=_t(_NE + "test_values_match_the_fixture")),
    "node/memory_actual_workload.csv": _csv(NO + "MemoryActualWorkload", MemoryActualWorkload=_t(_NE + "test_values_match_the_fixture")),
    "node/memory_actual_utilization.csv": _csv(NO + "MemoryActualUtilization", MemoryActualUtilization=_t(_NE + "test_values_match_the_fixture")),
    "node/memory_ws.csv": _csv(NO + "MemoryWs", MemoryWs="excluded:real cAdvisor root-cgroup working set with no fixture or Kubernetes truth; its grid is asserted and MemoryWsUtilization is checked against it"),
    "node/memory_ws_utilization.csv": _csv(NO + "MemoryWsUtilization", MemoryWsUtilization=_t(_NE + "test_working_set_utilization_uses_allocatable_memory")),
    "node/oom_kill_events.csv": _csv(NO + "OomKillEvents", OomKillEvents=_t(_NE + "test_values_match_the_fixture")),
    "node/cpu_throttling_events.csv": _csv(NO + "CpuThrottlingEvents", CpuThrottlingEvents=_t(_NE + "test_values_match_the_fixture")),
    "node/disk_read_bytes.csv": _csv(NO + "DiskReadBytes", DiskReadBytes=_t(_NE + "test_values_match_the_fixture")),
    "node/disk_write_bytes.csv": _csv(NO + "DiskWriteBytes", DiskWriteBytes=_t(_NE + "test_values_match_the_fixture")),
    "node/disk_total_bytes.csv": _csv(NO + "DiskTotalBytes", DiskTotalBytes=_t(_NE + "test_values_match_the_fixture")),
    "node/disk_read_ops.csv": _csv(NO + "DiskReadOps", DiskReadOps=_t(_NE + "test_values_match_the_fixture")),
    "node/disk_write_ops.csv": _csv(NO + "DiskWriteOps", DiskWriteOps=_t(_NE + "test_values_match_the_fixture")),
    "node/disk_total_ops.csv": _csv(NO + "DiskTotalOps", DiskTotalOps=_t(_NE + "test_values_match_the_fixture")),
    "node/net_received_bytes.csv": _csv(NO + "NetReceivedBytes", NetReceivedBytes=_t(_NE + "test_values_match_the_fixture")),
    "node/net_sent_bytes.csv": _csv(NO + "NetSentBytes", NetSentBytes=_t(_NE + "test_values_match_the_fixture")),
    "node/net_total_bytes.csv": _csv(NO + "NetTotalBytes", NetTotalBytes=_t(_NE + "test_values_match_the_fixture")),
    "node/net_received_packets.csv": _csv(NO + "NetReceivedPackets", NetReceivedPackets=_t(_NE + "test_values_match_the_fixture")),
    "node/net_sent_packets.csv": _csv(NO + "NetSentPackets", NetSentPackets=_t(_NE + "test_values_match_the_fixture")),
    "node/net_total_packets.csv": _csv(NO + "NetTotalPackets", NetTotalPackets=_t(_NE + "test_values_match_the_fixture")),
    "node_group/config.csv": _csv("AuditTime,ClusterName,NodeGroupName,HwTotalCpus,HwTotalPhysicalCpus,HwCoresPerCpu,HwThreadsPerCore,HwTotalMemory,HwModel,OsName", **_GROUP_CONFIG),
    "node_group/attributes.csv": _csv("ClusterName,NodeGroupName,VirtualTechnology,VirtualDomain,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,CurrentSize,CurrentNodes,NodeLabels", **_GROUP_ATTRIBUTES),
    "node_group/current_size.csv": _csv(NG + "CurrentSize", CurrentSize=_t(_NGL + "test_current_size_holds_for_every_sample", _NP + "test_current_size_holds_for_every_sample", _NGR + "test_current_size_holds_for_every_sample")),
    "node_group/cpu_requests.csv": _csv(NG + "CpuRequests", CpuRequests=_t(_NGL + "test_requests_match_the_cluster", _NP + "test_group_requests_match_the_cluster", _NGR + "test_requests_match_the_cluster", _CR + "test_terminal_pods_are_excluded_from_series")),
    "node_group/cpu_reservation_percent.csv": _csv(NG + "CpuReservationPercent", CpuReservationPercent=_GROUP_RESERVATION),
    "node_group/memory_requests.csv": _csv(NG + "MemoryRequests", MemoryRequests=_t(_NGL + "test_requests_match_the_cluster", _NP + "test_group_requests_match_the_cluster", _NGR + "test_requests_match_the_cluster", _CR + "test_terminal_pods_are_excluded_from_series")),
    "node_group/memory_reservation_percent.csv": _csv(NG + "MemoryReservationPercent", MemoryReservationPercent=_GROUP_RESERVATION),
    "node_group/gpu_requests.csv": _csv(NG + "GpuRequests"),
    "node_group/gpu_reservation_percent.csv": _csv(NG + "GpuReservationPercent"),
    "node_group/cpu_utilization.csv": _csv(NG + "CpuUtilization", CpuUtilization=_t(_NE + "test_values_match_the_fixture")),
    "node_group/memory_raw_bytes.csv": _csv(NG + "MemoryBytes", MemoryBytes=_t(_NE + "test_values_match_the_fixture")),
    "node_group/memory_actual_workload.csv": _csv(NG + "MemoryActualWorkload", MemoryActualWorkload=_t(_NE + "test_values_match_the_fixture")),
    "node_group/disk_read_bytes.csv": _csv(NG + "DiskReadBytes", DiskReadBytes=_t(_NE + "test_values_match_the_fixture")),
    "node_group/disk_write_bytes.csv": _csv(NG + "DiskWriteBytes", DiskWriteBytes=_t(_NE + "test_values_match_the_fixture")),
    "node_group/disk_total_bytes.csv": _csv(NG + "DiskTotalBytes", DiskTotalBytes=_t(_NE + "test_values_match_the_fixture")),
    "node_group/disk_read_ops.csv": _csv(NG + "DiskReadOps", DiskReadOps=_t(_NE + "test_values_match_the_fixture")),
    "node_group/disk_write_ops.csv": _csv(NG + "DiskWriteOps", DiskWriteOps=_t(_NE + "test_values_match_the_fixture")),
    "node_group/disk_total_ops.csv": _csv(NG + "DiskTotalOps", DiskTotalOps=_t(_NE + "test_values_match_the_fixture")),
    "node_group/net_received_bytes.csv": _csv(NG + "NetReceivedBytes", NetReceivedBytes=_t(_NE + "test_values_match_the_fixture")),
    "node_group/net_sent_bytes.csv": _csv(NG + "NetSentBytes", NetSentBytes=_t(_NE + "test_values_match_the_fixture")),
    "node_group/net_total_bytes.csv": _csv(NG + "NetTotalBytes", NetTotalBytes=_t(_NE + "test_values_match_the_fixture")),
    "node_group/net_received_packets.csv": _csv(NG + "NetReceivedPackets", NetReceivedPackets=_t(_NE + "test_values_match_the_fixture")),
    "node_group/net_sent_packets.csv": _csv(NG + "NetSentPackets", NetSentPackets=_t(_NE + "test_values_match_the_fixture")),
    "node_group/net_total_packets.csv": _csv(NG + "NetTotalPackets", NetTotalPackets=_t(_NE + "test_values_match_the_fixture")),
    "node_group/gpu_utilization_avg.csv": _csv(NG + "GpuUtilizationAvg"),
    "node_group/gpu_utilization_gpus_avg.csv": _csv(NG + "GpuUtilizationGpusAvg"),
    "node_group/gpu_mem_utilization_avg.csv": _csv(NG + "GpuMemUtilizationAvg"),
    "node_group/gpu_mem_used_avg.csv": _csv(NG + "GpuMemUsedAvg"),
    "node_group/gpu_power_usage_avg.csv": _csv(NG + "GpuPowerUsageAvg"),
    "container/config.csv": _csv("AuditTime,ClusterName,Namespace,EntityName,EntityType,ContainerName,HwTotalMemory,GpuMemoryTotal,OsName,HwManufacturer", **dict.fromkeys(("HwTotalMemory", "OsName", "HwManufacturer"), _t(_CR + "test_container_config"))),
    "container/attributes.csv": _csv("ClusterName,Namespace,EntityName,EntityType,ContainerName,ContainerType,VirtualTechnology,VirtualDomain,VirtualDatacenter,VirtualCluster,ContainerLabels,PodLabels,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,GpuLimit,GpuRequest,GpuLimitFloat,GpuRequestFloat,CurrentNodes,PowerState,CreatedByKind,CreatedByName,CurrentSize,CreateTime,ContainerRestarts,NamespaceLabels,NamespaceCpuRequest,NamespaceCpuLimit,NamespaceMemoryRequest,NamespaceMemoryLimit,NamespacePodsLimit,HpaName,HpaLabels,HpaTargetMetricName,HpaTargetMetricType,HpaTargetMetricValue,HpaTargetMetrics,QosClass,GpuModel,GpuSharingStrategy,EphemeralStorageRequest,EphemeralStorageLimit,Runtimes", **dict.fromkeys(("EphemeralStorageRequest", "EphemeralStorageLimit"), _t(_EPH + "test_container_attributes_in_mib")), **_CONTAINER_ATTRIBUTES),
    "container/events.csv": _csv("ClusterName,Namespace,EntityName,EntityType,ContainerName,EventTime,ExitCode,IsPid1"),
    "container/current_size.csv": _csv(CO + "CurrentSize", CurrentSize=_t(_CR + "test_current_size_series_match_running_pods")),
    "container/cpu_limit.csv": _csv(CO + "CpuLimit", CpuLimit=_t(_CR + "test_request_and_limit_series_match_the_spec")),
    "container/cpu_request.csv": _csv(CO + "CpuRequest", CpuRequest=_t(_CR + "test_request_and_limit_series_match_the_spec")),
    "container/memory_limit.csv": _csv(CO + "MemoryLimit", MemoryLimit=_t(_CR + "test_request_and_limit_series_match_the_spec")),
    "container/memory_request.csv": _csv(CO + "MemoryRequest", MemoryRequest=_t(_CR + "test_request_and_limit_series_match_the_spec")),
    "container/gpu_limit.csv": _csv(CO + "GpuLimit"),
    "container/gpu_request.csv": _csv(CO + "GpuRequest"),
    "container/ephemeral_storage_limit.csv": _csv(CO + "EphemeralStorageLimit", EphemeralStorageLimit=_t(_EPH + "test_container_request_and_limit_series_in_bytes")),
    "container/ephemeral_storage_request.csv": _csv(CO + "EphemeralStorageRequest", EphemeralStorageRequest=_t(_EPH + "test_container_request_and_limit_series_in_bytes")),
    "container/max_restarts.csv": _csv(CO + "MaxRestarts"),
    "container/avg_cpu_mcores_workload.csv": _csv(CO + "AvgCpuMcores", AvgCpuMcores=_CAD_USAGE),
    "container/avg_custom_cpu_mcores_workload.csv": _csv(CO + "AvgCustomCpuMcores", AvgCustomCpuMcores=_CAD_USAGE),
    "container/avg_mem_workload.csv": _csv(CO + "AvgMem", AvgMem=_CAD_USAGE),
    "container/avg_rss_workload.csv": _csv(CO + "AvgRss", AvgRss=_CAD_USAGE),
    "container/avg_ws_workload.csv": _csv(CO + "AvgWs", AvgWs=_CAD_USAGE),
    "container/avg_custom_memory_workload.csv": _csv(CO + "AvgCustomMemory", AvgCustomMemory=_CAD_USAGE),
    "container/avg_disk_workload.csv": _csv(CO + "AvgDisk", AvgDisk=_CAD_USAGE),
    "container/avg_cpu_throttling_percent_workload.csv": _csv(CO + "AvgCpuThrottlingPercent", AvgCpuThrottlingPercent=_CAD_USAGE),
    "container/ephemeral_storage_usage_bytes_avg_workload.csv": _csv(CO + "EphemeralStorageUsageBytesAvg", EphemeralStorageUsageBytesAvg=_t(_EPH + "test_container_usage_counts_each_volume_once", _EPH + "test_container_usage_average_equals_maximum")),
    "container/gpu_utilization_avg_workload.csv": _csv(CO + "GpuUtilizationAvg"),
    "container/gpu_utilization_gpus_avg_workload.csv": _csv(CO + "GpuUtilizationGpusAvg"),
    "container/gpu_mem_utilization_avg_workload.csv": _csv(CO + "GpuMemUtilizationAvg"),
    "container/gpu_mem_used_avg_workload.csv": _csv(CO + "GpuMemUsedAvg"),
    "container/gpu_power_usage_avg_workload.csv": _csv(CO + "GpuPowerUsageAvg"),
    "container/max_cpu_mcores_workload.csv": _csv(CO + "MaxCpuMcores", MaxCpuMcores=_CAD_USAGE),
    "container/max_custom_cpu_mcores_workload.csv": _csv(CO + "MaxCustomCpuMcores", MaxCustomCpuMcores=_CAD_USAGE),
    "container/max_mem_workload.csv": _csv(CO + "MaxMem", MaxMem=_CAD_USAGE),
    "container/max_rss_workload.csv": _csv(CO + "MaxRss", MaxRss=_CAD_USAGE),
    "container/max_ws_workload.csv": _csv(CO + "MaxWs", MaxWs=_CAD_USAGE),
    "container/max_custom_memory_workload.csv": _csv(CO + "MaxCustomMemory", MaxCustomMemory=_CAD_USAGE),
    "container/max_disk_workload.csv": _csv(CO + "MaxDisk", MaxDisk=_CAD_USAGE),
    "container/max_cpu_throttling_percent_workload.csv": _csv(CO + "MaxCpuThrottlingPercent", MaxCpuThrottlingPercent=_CAD_USAGE),
    "container/ephemeral_storage_usage_bytes_max_workload.csv": _csv(CO + "EphemeralStorageUsageBytesMax", EphemeralStorageUsageBytesMax=_t(_EPH + "test_container_usage_counts_each_volume_once", _EPH + "test_container_usage_average_equals_maximum")),
    "container/gpu_utilization_max_workload.csv": _csv(CO + "GpuUtilizationMax"),
    "container/gpu_utilization_gpus_max_workload.csv": _csv(CO + "GpuUtilizationGpusMax"),
    "container/gpu_mem_utilization_max_workload.csv": _csv(CO + "GpuMemUtilizationMax"),
    "container/gpu_mem_used_max_workload.csv": _csv(CO + "GpuMemUsedMax"),
    "container/gpu_power_usage_max_workload.csv": _csv(CO + "GpuPowerUsageMax"),
    "container/sum_cpu_throttling_seconds_workload.csv": _csv(CO + "SumCpuThrottlingSeconds", SumCpuThrottlingSeconds=_CAD_USAGE),
    "container/hpa_max_replicas.csv": _csv(HP + "HpaMaxReplicas", HpaMaxReplicas=_t(_HPA + "test_hpa_series_values")),
    "container/hpa_min_replicas.csv": _csv(HP + "HpaMinReplicas", HpaMinReplicas=_t(_HPA + "test_hpa_series_values")),
    "container/hpa_current_replicas.csv": _csv(HP + "HpaCurrentReplicas", HpaCurrentReplicas=_t(_HPA + "test_hpa_series_values")),
    "container/hpa_condition_scaling_limited.csv": _csv(HP + "HpaConditionScalingLimited", HpaConditionScalingLimited=_HPA_NO_SCALING_LIMITED),
    "container/hpa_cpu_target_avg.csv": _csv(HP + "HpaCpuTargetAvg", HpaCpuTargetAvg=_t(_HPA + "test_hpa_series_values")),
    "container/hpa_cpu_target_utilization.csv": _csv(HP + "HpaCpuTargetUtilization", HpaCpuTargetUtilization=_t(_HPA + "test_hpa_series_values")),
    "container/hpa_cpu_current_avg.csv": _csv(HP + "HpaCpuCurrentAvg", HpaCpuCurrentAvg=_HPA_NO_CURRENT),
    "container/hpa_cpu_current_utilization.csv": _csv(HP + "HpaCpuCurrentUtilization", HpaCpuCurrentUtilization=_HPA_NO_CURRENT),
    "container/hpa_memory_target_avg.csv": _csv(HP + "HpaMemoryTargetAvg", HpaMemoryTargetAvg=_t(_HPA + "test_hpa_series_values")),
    "container/hpa_memory_target_utilization.csv": _csv(HP + "HpaMemoryTargetUtilization", HpaMemoryTargetUtilization=_t(_HPA + "test_hpa_series_values")),
    "container/hpa_memory_current_avg.csv": _csv(HP + "HpaMemoryCurrentAvg", HpaMemoryCurrentAvg=_HPA_NO_CURRENT),
    "container/hpa_memory_current_utilization.csv": _csv(HP + "HpaMemoryCurrentUtilization", HpaMemoryCurrentUtilization=_HPA_NO_CURRENT),
    "hpa/hpa_extra_config.csv": _csv("AuditTime,ClusterName,Namespace,EntityName,EntityType,ContainerName,HpaName,OsName,HwManufacturer", **dict.fromkeys(("OsName", "HwManufacturer"), _t(_HPA + "test_orphan_config"))),
    "hpa/hpa_extra_attributes.csv": _csv("ClusterName,Namespace,EntityName,EntityType,ContainerName,HpaName,HpaLabels,HpaTargetMetricName,HpaTargetMetricType,HpaTargetMetricValue,HpaTargetMetrics", **dict.fromkeys(("HpaLabels", "HpaTargetMetricName", "HpaTargetMetricType", "HpaTargetMetricValue", "HpaTargetMetrics"), _t(_HPA + "test_orphan_attributes_describe_their_targets"))),
    "hpa/hpa_extra_max_replicas.csv": _csv(HP + "HpaMaxReplicas", HpaMaxReplicas=_t(_HPA + "test_hpa_series_values")),
    "hpa/hpa_extra_min_replicas.csv": _csv(HP + "HpaMinReplicas", HpaMinReplicas=_t(_HPA + "test_hpa_series_values")),
    "hpa/hpa_extra_current_replicas.csv": _csv(HP + "HpaCurrentReplicas", HpaCurrentReplicas=_t(_HPA + "test_hpa_series_values")),
    "hpa/hpa_extra_condition_scaling_limited.csv": _csv(HP + "HpaConditionScalingLimited", HpaConditionScalingLimited=_HPA_EXTRA_NO_METRICS),
    "hpa/hpa_extra_cpu_target_avg.csv": _csv(HP + "HpaCpuTargetAvg", HpaCpuTargetAvg=_t(_HPA + "test_hpa_series_values")),
    "hpa/hpa_extra_cpu_target_utilization.csv": _csv(HP + "HpaCpuTargetUtilization", HpaCpuTargetUtilization=_t(_HPA + "test_hpa_series_values")),
    "hpa/hpa_extra_cpu_current_avg.csv": _csv(HP + "HpaCpuCurrentAvg", HpaCpuCurrentAvg=_HPA_EXTRA_NO_METRICS),
    "hpa/hpa_extra_cpu_current_utilization.csv": _csv(HP + "HpaCpuCurrentUtilization", HpaCpuCurrentUtilization=_HPA_EXTRA_NO_METRICS),
    "hpa/hpa_extra_memory_target_avg.csv": _csv(HP + "HpaMemoryTargetAvg", HpaMemoryTargetAvg=_t(_HPA + "test_hpa_series_values")),
    "hpa/hpa_extra_memory_target_utilization.csv": _csv(HP + "HpaMemoryTargetUtilization", HpaMemoryTargetUtilization=_t(_HPA + "test_hpa_series_values")),
    "hpa/hpa_extra_memory_current_avg.csv": _csv(HP + "HpaMemoryCurrentAvg", HpaMemoryCurrentAvg=_HPA_EXTRA_NO_METRICS),
    "hpa/hpa_extra_memory_current_utilization.csv": _csv(HP + "HpaMemoryCurrentUtilization", HpaMemoryCurrentUtilization=_HPA_EXTRA_NO_METRICS),
    "rq/config.csv": _csv("AuditTime,ClusterName,Namespace,RqName"),
    "rq/attributes.csv": _csv("ClusterName,Namespace,RqName,VirtualTechnology,VirtualDomain,VirtualDatacenter,CreateTime,ResourceMetadata,CpuLimit,CpuRequest,MemoryLimit,MemoryRequest,CurrentSize,NamespaceCpuLimit,NamespaceCpuRequest,NamespaceMemoryLimit,NamespaceMemoryRequest,NamespacePodsLimit", **dict.fromkeys(("VirtualTechnology", "VirtualDomain", "VirtualDatacenter", "CreateTime", "ResourceMetadata"), _t(_CR + "test_quota_description")), **dict.fromkeys(("CpuLimit", "CpuRequest", "MemoryLimit", "MemoryRequest", "CurrentSize", "NamespaceCpuLimit", "NamespaceCpuRequest", "NamespaceMemoryLimit", "NamespaceMemoryRequest", "NamespacePodsLimit"), _t(_CR + "test_quota_attributes"))),
    "rq/cpu_limits.csv": _csv(RQ + "CpuLimits", CpuLimits=_RQ_SERIES),
    "rq/cpu_requests.csv": _csv(RQ + "CpuRequests", CpuRequests=_RQ_SERIES),
    "rq/mem_limits.csv": _csv(RQ + "MemLimits", MemLimits=_RQ_SERIES),
    "rq/mem_requests.csv": _csv(RQ + "MemRequests", MemRequests=_RQ_SERIES),
    "rq/pods.csv": _csv(RQ + "PodsLimits", PodsLimits=_RQ_SERIES),
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
        if path not in INVENTORY:
            warnings.append(f"{path}: not in the coverage inventory (new CSV, columns {headers!r})")
            continue
        known = list(INVENTORY[path])
        if [h for h in headers if h in known] != known:
            errors.append(f"{path}: header {headers!r} lost, renamed or reordered columns of {known!r}")
        elif len(headers) > len(known):
            warnings.append(f"{path}: new column(s) {[h for h in headers if h not in known]!r} not in the inventory")
    return errors, warnings


def workflow_warning(warnings: Sequence[str]) -> str:
    """One GitHub Actions annotation line, escaped per the workflow-command rules."""

    message = "; ".join(warnings).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title=Coverage review needed::{message}"
