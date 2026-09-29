# Kubex Stack E2E

This repository runs an end-to-end validation of the Kubex Automation Stack
and its Prometheus data forwarder.

## Workflow

The `stack-validation` workflow runs on pushes to `main`, nightly at 02:00 UTC,
and manual dispatches. It:

1. Creates a kind-backed KWOK cluster with a real control-plane node.
2. Resolves and records the latest published Kubex Automation Stack chart, then deploys a fake upload server and the stack with Beyla enabled.
3. Creates a real-node resource fixture for cAdvisor and node-exporter data.
4. Scales to 100 simulated KWOK nodes.
5. Applies 25 Deployments, 25 StatefulSets, 10 CronJobs, and one DaemonSet.
6. Runs Go, Java, Node.js, Python, and .NET HTTP applications on the real node and sends traffic to them.
7. Checks Beyla's runtime survey data in Prometheus for all five applications.
8. Waits for 30 minutes of Prometheus history, runs the data forwarder, and validates the uploaded CSV archive.

The workflow summary reports upload status and row counts for the required
cluster, node, and container CSVs. It also records the exact chart version and
Beyla runtime detection results. The complete diagnostic and CSV output is
available in the `stack-validation-${run_id}` artifact.

## Local Tests

Run the unit tests with:

```text
python3 -m unittest discover -s tests
```

The generated workloads target nodes labeled `type=kwok` and include resource
requests and limits. KWOK nodes provide Kubernetes inventory and scheduling
metrics through kube-state-metrics, but do not run real node-exporter or
cAdvisor processes. The real control-plane fixture supplies those usage
metrics.

## Fixed Test Versions

- Kubex Automation Stack Helm chart: latest published version at run time, recorded in the artifact
- KWOK: `v0.5.1`
- Kubernetes: `v1.30.0`
- Helm: `v3.16.3`

The workflow intentionally does not pin the automation-stack chart. It resolves
the newest chart from the Kubex Helm repository at the start of each run and
stores the selected chart metadata in the artifact. This keeps the test aligned
with the chart that customers receive, while leaving a precise record when a
chart changes.
