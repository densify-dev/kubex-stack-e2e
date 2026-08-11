# Kubex Stack E2E

This repository runs an end-to-end validation of the Kubex Automation Stack
and its Prometheus data forwarder.

## Workflow

The `stack-validation` workflow runs on pushes to `main`, nightly at 02:00 UTC,
and manual dispatches. It:

1. Creates a kind-backed KWOK cluster with a real control-plane node.
2. Deploys a fake upload server and the Kubex Automation Stack.
3. Creates a real-node resource fixture for cAdvisor and node-exporter data.
4. Scales to 100 simulated KWOK nodes.
5. Applies 25 Deployments, 25 StatefulSets, 10 CronJobs, and one DaemonSet.
6. Waits for 30 minutes of Prometheus history.
7. Runs the data forwarder and validates the uploaded CSV archive.

The workflow summary reports upload status and row counts for the required
cluster, node, and container CSVs. The complete diagnostic and CSV output is
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

- Kubex Automation Stack Helm chart: `1.0.18`
- KWOK: `v0.5.1`
- Kubernetes: `v1.30.0`
- Helm: `v3.16.3`
