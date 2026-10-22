# Architecture

How the harness turns a scenario spec into a pass or a fail. For developers who change the harness or
need to know where a value comes from.

## The data path under test

The **collector** is the `container-optimization-data-forwarder` image. One run of it:

1. Queries Prometheus with PromQL range queries over `[AuditTime - interval, AuditTime]`, one sample
   per `sample_rate` minutes. The series come from kube-state-metrics (inventory, requests, limits,
   owners, quotas, HPAs), kubelet cAdvisor (container usage), node exporter (node usage) and optional
   exporters (DCGM, the Kubex GPU exporter, Beyla, the ephemeral-storage exporter).
2. Writes one CSV per entity and metric (`cluster/`, `node/`, `node_group/`, `container/`, `hpa/`,
   `rq/`, `crq/`) plus `log.txt`, and packs them into one archive under the prefix `data/<cluster>`.
3. Uploads the archive over HTTP to the Kubex endpoint `/upload` (the **forwarder** part).

The harness changes only the upload target. The chart is rendered with the upload host
`fake.kubex.ai:8080`. [`scripts/inject_host_aliases.py`](../scripts/inject_host_aliases.py) then adds
a `hostAliases` entry that resolves that name to the in-cluster **fake receiver**
([`scripts/fake_stack_webserver.py`](../scripts/fake_stack_webserver.py)). The receiver records every
request body. Everything upstream of the upload is the real stack.

```mermaid
flowchart LR
  subgraph truth["Truth"]
    K["Kubernetes API objects"]
    F["fixture exporters (known series)"]
  end
  K --> KSM["kube-state-metrics"] --> P["Prometheus"]
  F --> P
  CA["kubelet cAdvisor"] --> P
  P -- "PromQL range queries" --> C["collector Job"]
  C -- "POST /upload (archive)" --> R["fake receiver"]
  R -- "/debug/state" --> AR["Archive (parsed CSVs)"]
  K -- "kubectl get -o json every 60 s" --> S["Snapshots"] --> O["Oracle"]
  F -. "expected values in the test module" .-> T["pytest tests"]
  AR --> T
  O --> T
```

Tests compare the archive (actual) with snapshots, the oracle and fixture definitions (expected).
Collector output is never the source of an expected value.

## Components

The module map is the repo map in [AGENTS.md](../AGENTS.md#repo-map). The harness modules sit between two
fixtures: `run` (provisions or replays the module's `SCENARIO`; `archive` and `oracle` read it) and the
scenario tests, which read only the `Run`.

## Run lifecycle

`execute()` in [`harness/run.py`](../harness/run.py) runs once per scenario module. The module-scoped
`run` fixture in [`conftest.py`](../conftest.py) enters it, the module's tests share the result, and
teardown always runs.

```mermaid
flowchart TD
  A["spec validated at import"] --> B["Kind cluster: create, then labels, roles, taints, extended resources"]
  B --> C["fake receiver + fixture exporters"]
  C --> D["stack: helm template, preload collector, inject hostAliases, apply"]
  D --> E["scenario manifests + lifecycle waits"]
  E --> F["readiness gates: Prometheus queries"]
  F --> G["collection window: snapshot every 60 s"]
  G --> H["kubectl create job --from=cronjob, wait for success"]
  H --> I["capture receiver state, snapshot 'after upload'"]
  I --> J["load archive: validate paths, parse every CSV, extract"]
  J --> K["write snapshots.json + replay.json"]
  K --> L["pytest runs the tests"]
  L --> M["teardown: summary.md, versions.json, diagnostics, delete cluster"]
```

Details that matter when you read results:

- **Collector image.** The order is `--collector-image`, then the chart default. It is pulled and loaded into Kind under a local tag, and the forwarder runs it with
  `imagePullPolicy: Never`, so the tested image is exactly the one that was resolved.
  `helm_overrides` cannot set the image.
- **Harness chart defaults.** Optional subcharts are disabled, the upload URL points at the receiver,
  and the CronJob is suspended. The forwarder runs only when the harness creates a Job from it.
- **Window.** It lasts `ForwarderSpec.window_seconds` (default 180 s). Readiness gates happen before it, so
  the window holds only warm series.
- **No mutation in the window.** The harness changes nothing after readiness, so a rolling update or a
  kube-state-metrics restart in the window is not tested. Snapshots run on a fixed 60 s cadence. Oracle
  queries fail with `OracleDrift` if a value differs between snapshots. Churn that a scenario designs into
  its workload, such as a CronJob, is captured by the snapshots and asserted from them.
- **Grid.** `expected_timestamps(AuditTime, collection)` gives the exact `MetricTime` steps the
  collector must emit. `assert_metric_grid` checks the identity set and the steps per identity.
  `AuditTime` itself is anchored to the forwarder pod's run time by a universal test.

## Artifacts

One directory per scenario: `artifacts/kind-functional/<scenario>/`. CI uploads it as
`kind-functional-<scenario>-<run-id>`.

| Path | Content | Replay needs it |
| --- | --- | --- |
| `extracted-csv/` | The uploaded archive, extracted (CSVs and `log.txt`) | yes |
| `snapshots.json` | Every Kubernetes snapshot (nodes, pods, `snapshot_resources`). Large. | yes |
| `replay.json` | Upload count, Job name, Job failures | yes |
| `summary.md` | Versions and run facts | |
| `versions.json` | Chart, requested image, digest, collector version | |
| `server-state.json` | The receiver's captured requests, credentials removed | |
| `prometheus-targets.json`, `prometheus-queries.json` | Readiness evidence | |
| `kind-config.yaml`, `kubex-stack-rendered.yaml`, `fixture-inputs/` | What was applied | |
| `diagnostics/` | Pods, events, nodes, EndpointSlices, forwarder Job and logs, per-pod logs | |
| `pytest.xml`, `failures.json` | CI only: JUnit report, and the `(test, file, line, message)` failures the roll-up reads | |

## Replay

`--replay DIR` skips the cluster entirely. `replay()` reads `snapshots.json`, `replay.json` and
`extracted-csv/` from DIR, builds the same `Run` (the oracle comes from the snapshots) and never writes to DIR.

This only works if tests read nothing but the `Run`: `run.archive`, `run.oracle`, `run.snapshots`
and the scenario constants. A test that calls `kubectl` or Prometheus would pass live and break on
replay.

## Coverage inventory and universal checks

[`harness/coverage.py`](../harness/coverage.py) lists every CSV path the collector can emit with its
exact header. Each column is classified as one of:

| Value | Meaning |
| --- | --- |
| `key` | Identity or time column (`KEYS`) |
| `test:<path>::<function>[,...]` | The tests that assert this column |
| `excluded:<reason>` | Cannot be asserted on Kind, with the reason |
| `unclassified` | No test asserts it yet |

A unit test in [`tests/test_collection_checks.py`](../tests/test_collection_checks.py) checks that
every `test:` reference resolves to a `def` in the named file.

Every scenario module runs `from harness.universal import *`, which adds these tests:

| Test | Asserts |
| --- | --- |
| `test_single_upload` | The receiver captured exactly one archive upload. |
| `test_job_never_failed` | The forwarder Job had no failed pod attempts. |
| `test_log_is_clean` | `log.txt` has no Prometheus query failures. "No data" skips pass. |
| `test_archive_matches_inventory` | No inventory column was lost, renamed or reordered (error). New CSVs or columns become warnings. |
| `test_expected_files` | The archive's inventory CSVs are exactly `ScenarioSpec.files` (`BASE_FILES` plus what the workload adds), each with rows. |
| `test_audit_time_is_when_the_forwarder_ran` | `AuditTime` lies between the floored start and finish of the forwarder pod that produced the archive. |

Warnings go into `summary.md`. The run roll-up lists them once and emits one annotation titled **Coverage review needed**.

## CI

[`.github/workflows/kind-functional.yaml`](../.github/workflows/kind-functional.yaml):

```mermaid
flowchart LR
  RV["resolve: chart version + collector digest, list scenarios"] --> M
  UT["unit-tests: pytest tests -q"] --> M
  M["scenario matrix: one job per scenarios/* dir, fail-fast off"] --> S["per leg: artifact upload, no summary or annotations"]
  S --> RP["report: run summary and annotations, red only if the roll-up breaks"]
```

- `resolve` runs [`scripts/resolve_versions.py`](../scripts/resolve_versions.py) once and pins the
  collector to a digest, so every leg tests the same chart and image.
- Each leg runs `pytest scenarios/<name>` with both versions passed explicitly and a 30-minute timeout.
  With `FAILURES_JSON` set, `conftest.py` writes the failures next to the artifact; `--junitxml` writes
  `pytest.xml` there too. A red leg stays red but writes no job summary and no annotation.
- `report` runs after the matrix, even when legs failed, and runs `python3 -m harness.report artifacts
  <scenario>...` on the downloaded artifacts. It reads `pytest.xml` (counts), `failures.json` (failures,
  grouped by identical message; `[setup error]` and `[call error]` count as setup errors), `summary.md`
  (a scenario without `collected` is a setup error; the coverage warnings) and `versions.json`. It writes the
  only job summary and at most 10 annotations (the headline as `::error`, or `::notice` when nothing
  failed; failure groups round-robin across scenarios; one `::warning` for coverage). It exits 0 once written,
  and the red legs carry the run's failure. A scenario without an artifact is `no artifact`.

## Scope and limits

What the suite does not prove, across all scenarios. Each scenario doc lists its own gaps under "Not
covered".

- **One bad row per file.** A per-row loop stops at the first bad timestamp. The full CSV is in
  `extracted-csv/`.
- **No independent truth.** Columns marked `excluded:` in [`harness/coverage.py`](../harness/coverage.py)
  give their reason. The cAdvisor working set is one: its grid is checked, not its values.
- **Environments Kind cannot reproduce.** Cloud providers, OpenShift (`crq/` files, `count/pods` quotas,
  machine-set node groups with cluster identifiers), real NVIDIA hardware and KWOK-scale clusters.
- **Silent query failures.** A failing workload query writes no line to `log.txt`, so `test_log_is_clean`
  cannot see it. A missing file shows up in `test_expected_files`.
- **Reporting.** CI has no third-party JUnit reporter. The run roll-up reads only what a leg uploaded, so a
  leg that never uploaded shows as `no artifact` without a reason.
