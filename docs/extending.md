# Extending the suite

Task-by-task steps for changing scenarios, tests, fixtures, the coverage inventory and versions. For
the background behind each step, see [architecture.md](architecture.md). To diagnose a failure, see
[Reading a red run](../README.md#reading-a-red-run).

## Ground rules

These apply to every task below.

- Write `assert actual == expected`. The collector output goes on the left, the truth on the right.
- Take expected values from Kubernetes objects (snapshots, oracle) or from fixture definitions, never
  from collector output.
- Compare exactly. Use a tolerance only with a stated reason, e.g. `TOLERANCE = 1e-6` for `%f` print
  error.
- When the collector is wrong, the test stays red. Do not use `xfail` and do not weaken the assertion.
- The harness never changes the cluster during the collection window. Churn must be part of the
  workload, and its truth must come from the snapshots.
- Tests read only from the `Run` (`run.archive`, `run.oracle`, `run.snapshots`), so replay gives the
  same result.

## If you want to add a scenario

1. Create `scenarios/<name>/test_<name_with_underscores>.py` and, if it needs workloads,
   `scenarios/<name>/workload.yaml`. Names must be lowercase DNS labels.
2. Define the spec at module level. It is validated at import, before any cluster exists:

   ```python
   CLUSTER_SPEC = ClusterSpec(control_planes=[Node(labels=_SYSTEM_NODE)], workers=[Node(labels={GROUP_LABEL: "my-workers"})])
   NODES = CLUSTER_SPEC.node_names(CLUSTER_NAME_PREFIX + NAME)  # hostnames for nodeSelector pins
   SCENARIO = ScenarioSpec(
       name=NAME, cluster=CLUSTER_SPEC,
       stack=StackSpec(helm_overrides={...}),          # copy the block from a similar scenario
       fixtures=[...], resources=["workload.yaml"], lifecycle=[Wait(...)],
       prometheus=PrometheusSpec(queries=[PrometheusQuery("ready", "<promql>", min_results=1)]),
       forwarder=ForwarderSpec(job_name=f"{NAME}-forwarder", window_seconds=180, collection=Collection()),
       files={"node/attributes.csv": "populated", ...},
   )
   pytestmark = pytest.mark.pr
   ```

3. Wire it to the harness and import the universal checks:

   ```python
   from harness.universal import *  # noqa: F403 - collected as this scenario's tests

   @pytest.fixture(scope="session")
   def run(stack_run):
       with stack_run(SCENARIO, HERE) as active:
           yield active
   ```

4. Add a `PrometheusQuery` readiness gate for every series and object state your tests rely on. The
   window starts only after all the gates pass.
5. Run it live once:
   `.venv/bin/python -m pytest scenarios/<name> -v --tb=short`. Fill `files` from the run's
   `archive-paths.json`, then iterate on the assertions with `--replay`.
6. CI finds the new directory on its own. If the scenario count goes above `max-parallel: 11` in
   [`kind-functional.yaml`](../.github/workflows/kind-functional.yaml), raise that value.
7. Write `docs/scenarios/<name>.md` from [the template](scenarios/_template.md), add a row to
   [the index](scenarios/README.md), and map the columns you assert in the coverage inventory.

## If you want to add or change a test

1. Read the scenario doc, then the scenario module's constants and helpers.
2. Find the truth first: a snapshot field, an oracle query or a fixture constant. If no truth exists
   (real usage, for example), add a fixture or leave the column unasserted.
3. Use the shared helpers in [`harness/archive.py`](../harness/archive.py):

   | Helper | Use |
   | --- | --- |
   | `Csv.where(**cols)`, `Csv.one(**cols)` | Select rows. `one` fails unless exactly one row matches. |
   | `assert_metric_grid(table, keys, identities, timestamps)` | Exact identity set and `MetricTime` grid of a series file |
   | `expected_timestamps(audit_time, collection)` | The grid the collector must emit |
   | `row_label(row, keys)` | Row identity for failure messages |
   | `label_map(cell)`, `prometheus_labels(labels)` | Compare label columns with Kubernetes labels |
   | `oracle.requests/limits/capacity/reservation_percent/pod_count` | Totals per node selector, drift-checked |

4. Make the message say where the failure is. pytest prints the values:

   ```python
   assert float(row[column]) == pytest.approx(expected, abs=TOLERANCE), f"{path} {row_label(row, KEYS)}"
   ```

5. Show that the test can fail. Edit a value in a copy of `extracted-csv/` and replay, or run it
   against a collector image with the known defect (`--collector-image`).
6. Update the column's entry in [`harness/coverage.py`](../harness/coverage.py) to `test:<path>::<function>`.
   If you rename a test, update every reference to it. Then run `.venv/bin/python -m pytest tests -q`,
   which checks that every reference resolves.
7. Update the scenario doc's test table.

## If you want to add a fixture exporter

A fixture is a small HTTP server ([`harness/metric_fixture_server.py`](../harness/metric_fixture_server.py))
that serves exact series. Each one replaces a real exporter whose values cannot be predicted.

1. Declare the series as typed `Metric`s. A gauge is a constant. A counter starts at `value` and
   grows by `rate` per second:

   ```python
   FixtureService(
       name="kubex-prometheus-node-exporter",          # must pass the chart's scrape filter, see below
       metrics=[Metric("node_memory_MemTotal_bytes", "gauge", 8 * 2**30, labels={"node": W1}),
                Metric("node_cpu_seconds_total", "counter", 0, labels={"node": W1, "cpu": "0", "mode": "idle"}, rate=0.5)],
       node=W1,                                         # optional: pin the pod to one node
   )
   ```

2. Make Prometheus scrape it. Pick one:
   - **Service discovery.** The chart's `kubernetes-service-endpoints` job keeps only Services named
     like stack exporters and only allow-listed metric names. Read both `keep` regexes in the run's
     `kubex-stack-rendered.yaml` and name the Service to match (see `node-exporter`,
     `ephemeral-storage`, `gpu-*`).
   - **Own scrape job.** Replace a chart job via `prometheus.scrapeConfigs` (see `cadvisor`) or add one
     via `prometheus.extraScrapeConfigs` (see `nodegroup-labels`).
3. Disable the real exporter in `helm_overrides`, so its series do not mix with the fixture's.
4. Add a readiness gate for the fixture's series.
5. Compute expected values from the same `Metric` constants in the test module. The served input is
   saved in `fixture-inputs/<name>.json`.

## Update the coverage inventory

[`harness/coverage.py`](../harness/coverage.py) maps every collector CSV column. Change it when the
collector's output changes:

| Signal | Action |
| --- | --- |
| Warning: `<path>: not in the coverage inventory (new CSV ...)` | Add `"<path>": _csv("<exact header>", Col=...)` to `INVENTORY`. Key columns are classified automatically. Columns you do not name are `unclassified`. |
| Warning: `<path>: new column(s) [...]` | Insert the columns into the header string in the emitted order, then classify them. A new column gets a value test, or `excluded:<reason>`. |
| Error: `lost, renamed or reordered columns` | Confirm in the collector source or release that the change is intended. Then update the header and every test that reads the column. |

The check warns on a new CSV or column and fails on a lost one. The JVM columns and files added in
collector 4.8.0 went in this way: the header and entries in `coverage.py`, value tests in
[container-resources](scenarios/container-resources.md).

After any inventory change:

- Add the CSV to `ScenarioSpec.files` in each scenario that emits it, as `populated` or
  `header-only`. Otherwise `test_expected_files` fails.
- Classify each column as `test:...` when a test asserts it, or `excluded:<reason>` when Kind cannot
  produce truth for it. `unclassified` is allowed but should shrink.
- Update the collector version in the module docstring, and recheck `expected_timestamps` in
  [`harness/archive.py`](../harness/archive.py) if query timing changed.
- Run `.venv/bin/python -m pytest tests -q`.

## Bump versions

| What | Where | Default |
| --- | --- | --- |
| Chart | `STACK_CHART_VERSION` in [`harness/versions.py`](../harness/versions.py), `--stack-chart-version`, CI input `stack_chart_version` | `None`: latest stable |
| Collector | `COLLECTOR_IMAGE` in `versions.py`, per scenario `StackSpec(collector_image=...)`, `--collector-image`, CI input `collector_image` | `None`: the chart's default image |
| Kind node image | `KIND_NODE_IMAGE` in `versions.py` (by digest). Keep `KUBECTL_VERSION` in the workflow on the same Kubernetes minor. | pinned |
| Fixture and helper image | `FIXTURE_IMAGE` in `versions.py`, plus the same digest written into some `workload.yaml` files (grep for it) | pinned |
| Kind, kubectl, Helm, Python | `env` in [`kind-functional.yaml`](../.github/workflows/kind-functional.yaml), with sha256 | pinned |

To test an unreleased collector, dispatch CI with `collector_image`. Do not edit the spec. CI always
passes `--collector-image` (the resolved digest), and that flag beats `StackSpec.collector_image`. A
spec pin therefore only affects local runs that omit the flag.

## If you change the harness

- Add or adjust a unit test under [`tests/`](../tests/), then run
  `.venv/bin/python -m pytest tests -q`. These tests need no cluster.
- Keep replay working. Anything a test needs must be in `snapshots.json`, `replay.json` or
  `extracted-csv/`.
- Update [architecture.md](architecture.md) when a module's role, the lifecycle, the artifacts or a CLI
  option changes.
