"""Kind cluster provisioning from a ``ClusterSpec``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from harness.command import HarnessError, kubectl, run, scalar
from harness.spec import ClusterSpec


def render_config(cluster: ClusterSpec) -> str:
    """Render kind's cluster config.

    Only the first control plane takes an InitConfiguration patch; every later
    node joins, so kubelet settings there go through JoinConfiguration.
    """

    document: dict[str, Any] = {"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4", "nodes": []}
    control_plane_seen = 0
    for role, node in cluster.nodes():
        item: dict[str, Any] = {"role": role}
        settings = {**cluster.kubelet, **node.kubelet}
        if settings:
            if role == "control-plane":
                control_plane_seen += 1
            patch_kind = (
                "InitConfiguration" if role == "control-plane" and control_plane_seen == 1 else "JoinConfiguration"
            )
            item["kubeadmConfigPatches"] = [
                yaml.safe_dump(
                    {
                        "kind": patch_kind,
                        "nodeRegistration": {
                            "kubeletExtraArgs": {key: scalar(value) for key, value in settings.items()}
                        },
                    },
                    sort_keys=False,
                ).rstrip()
            ]
        document["nodes"].append(item)
    return yaml.safe_dump(document, sort_keys=False)


def assert_absent(cluster_name: str, repo_root: Path) -> None:
    """Refuse to adopt an existing deterministic cluster name."""

    existing = {
        line.strip() for line in run(["kind", "get", "clusters"], cwd=repo_root, merge_stderr=False).splitlines()
    }
    if cluster_name in existing:
        raise HarnessError(f"Kind cluster already exists: {cluster_name}")


def create(cluster: ClusterSpec, cluster_name: str, config_path: Path, repo_root: Path) -> None:
    """Create a cluster after ``assert_absent`` establishes ownership."""

    args = ["kind", "create", "cluster", "--name", cluster_name, "--config", str(config_path), "--wait", "5m"]
    if cluster.node_image:
        args.extend(["--image", cluster.node_image])
    run(args, cwd=repo_root, timeout=900)


def delete(cluster_name: str, repo_root: Path) -> None:
    run(["kind", "delete", "cluster", "--name", cluster_name], cwd=repo_root, timeout=300)


def configure_nodes(cluster: ClusterSpec, cluster_name: str, repo_root: Path) -> None:
    """Apply labels, roles, and taints to the nodes kind just created."""

    for node_name, (_, node) in zip(cluster.node_names(cluster_name), cluster.nodes()):
        for key, value in node.labels.items():
            kubectl(["label", "node", node_name, f"{key}={scalar(value)}", "--overwrite"], repo_root)
        for role_name in node.roles:
            kubectl(["label", "node", node_name, f"node-role.kubernetes.io/{role_name}=", "--overwrite"], repo_root)
        for taint in node.taints:
            kubectl(["taint", "node", node_name, taint.assignment(), "--overwrite"], repo_root)
