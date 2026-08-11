#!/usr/bin/env python3
"""Generate a small resource-bearing workload mix for stack validation."""

from __future__ import annotations

import argparse
from pathlib import Path


NAMESPACE = "stack-validation-workload"
LABEL = "stack-validation-mixed"


def pod_spec(indent: str = "      ", restart_policy: str | None = None) -> list[str]:
    lines = [
        f"{indent}affinity:",
        f"{indent}  nodeAffinity:",
        f"{indent}    requiredDuringSchedulingIgnoredDuringExecution:",
        f"{indent}      nodeSelectorTerms:",
        f"{indent}        - matchExpressions:",
        f"{indent}          - key: type",
        f"{indent}            operator: In",
        f"{indent}            values:",
        f"{indent}              - kwok",
        f"{indent}tolerations:",
        f"{indent}        - key: kwok.x-k8s.io/node",
        f"{indent}          operator: Exists",
        f"{indent}          effect: NoSchedule",
        f"{indent}topologySpreadConstraints:",
        f"{indent}  - maxSkew: 1",
        f"{indent}    topologyKey: kubernetes.io/hostname",
        f"{indent}    whenUnsatisfiable: ScheduleAnyway",
        f"{indent}    labelSelector:",
        f"{indent}      matchLabels:",
        f"{indent}        app.kubernetes.io/name: {LABEL}",
        f"{indent}containers:",
        f"{indent}  - name: workload",
        f"{indent}    image: registry.k8s.io/pause:3.9",
        f"{indent}    resources:",
        f"{indent}      requests:",
        f"{indent}        cpu: 25m",
        f"{indent}        memory: 32Mi",
        f"{indent}      limits:",
        f"{indent}        cpu: 100m",
        f"{indent}        memory: 64Mi",
    ]
    if restart_policy:
        lines.append(f"{indent}restartPolicy: {restart_policy}")
    return lines


def workload(kind: str, name: str, replicas: int = 1) -> str:
    labels = [
        f"      app.kubernetes.io/name: {LABEL}",
        f"      app.kubernetes.io/component: {kind.lower()}",
    ]
    if kind == "Deployment":
        return "\n".join([
            "apiVersion: apps/v1", "kind: Deployment", "metadata:", f"  name: {name}", f"  namespace: {NAMESPACE}",
            "  labels:", *labels, "spec:", f"  replicas: {replicas}", "  selector:", "    matchLabels:",
            f"      app.kubernetes.io/name: {LABEL}", f"      app.kubernetes.io/component: {kind.lower()}",
            "  template:", "    metadata:", "      labels:", f"        app.kubernetes.io/name: {LABEL}",
            f"        app.kubernetes.io/component: {kind.lower()}", "    spec:", *pod_spec("      ")]) + "\n"
    if kind == "StatefulSet":
        return "\n".join([
            "apiVersion: apps/v1", "kind: StatefulSet", "metadata:", f"  name: {name}", f"  namespace: {NAMESPACE}",
            "  labels:", *labels, "spec:", f"  serviceName: {name}", f"  replicas: {replicas}", "  selector:", "    matchLabels:",
            f"      app.kubernetes.io/name: {LABEL}", f"      app.kubernetes.io/component: {kind.lower()}",
            "  template:", "    metadata:", "      labels:", f"        app.kubernetes.io/name: {LABEL}",
            f"        app.kubernetes.io/component: {kind.lower()}", "    spec:", *pod_spec("      ")]) + "\n"
    if kind == "DaemonSet":
        return "\n".join([
            "apiVersion: apps/v1", "kind: DaemonSet", "metadata:", f"  name: {name}", f"  namespace: {NAMESPACE}",
            "  labels:", *labels, "spec:", "  selector:", "    matchLabels:", f"      app.kubernetes.io/name: {LABEL}",
            f"      app.kubernetes.io/component: {kind.lower()}", "  template:", "    metadata:", "      labels:",
            f"        app.kubernetes.io/name: {LABEL}", f"        app.kubernetes.io/component: {kind.lower()}",
            "    spec:", *pod_spec("      ")]) + "\n"
    if kind == "CronJob":
        return "\n".join([
            "apiVersion: batch/v1", "kind: CronJob", "metadata:", f"  name: {name}", f"  namespace: {NAMESPACE}",
            "  labels:", *labels, "spec:", '  schedule: "*/5 * * * *"', "  concurrencyPolicy: Allow", "  jobTemplate:",
            "    spec:", "      backoffLimit: 0", "      template:", "        metadata:", "          labels:",
            f"            app.kubernetes.io/name: {LABEL}", f"            app.kubernetes.io/component: {kind.lower()}",
            "        spec:", *pod_spec("          ", "Never")]) + "\n"
    raise ValueError(kind)


def build() -> str:
    documents = [
        "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: " + NAMESPACE + "\n",
        workload("DaemonSet", "stack-validation-daemon"),
    ]
    documents.extend(workload("Deployment", f"stack-validation-deployment-{i:02d}") for i in range(1, 26))
    documents.extend(workload("StatefulSet", f"stack-validation-statefulset-{i:02d}") for i in range(1, 26))
    documents.extend(workload("CronJob", f"stack-validation-cronjob-{i:02d}") for i in range(1, 11))
    return "---\n".join(documents) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build(), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
