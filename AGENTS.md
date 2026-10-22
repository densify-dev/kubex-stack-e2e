# AGENTS.md

Instructions for coding agents working in this repo. This is a pytest harness that runs the Kubex
Automation Stack on Kind and checks the collector's uploaded CSV archive against Kubernetes and fixture
truth. Humans start at [README.md](README.md).

## Commands

```sh
.venv/bin/python -m pytest tests -q                                          # unit tests, no cluster, ~1 s
.venv/bin/python -m pytest scenarios/<name> --replay <artifact-dir> -q --tb=short   # assertions on saved artifacts, ~1 s
.venv/bin/python -m pytest scenarios/<name> --replay <artifact-dir> -q --tb=short -k <test>
.venv/bin/python -m pytest scenarios/<name> -v --tb=short                    # live Kind run, minutes; needs docker, kind, kubectl, helm
.venv/bin/python -m pytest scenarios --collect-only -q                       # list every test, no cluster
```

Prefer replay to live runs. Local artifacts live in `artifacts/kind-functional/<scenario>/`. A CI
artifact is downloaded with `gh run download <run-id> -n kind-functional-<scenario>-<run-id> -D <dir>`.

## Repo map

| Path | What |
| --- | --- |
| `conftest.py` | CLI options, the module-scoped `run`/`archive`/`oracle` fixtures, failure headlines, `failures.json` |
| `harness/report.py` | CI roll-up over all scenario artifacts: run summary and annotations |
| `harness/run.py` | Run lifecycle (`execute`), `replay`, the `Run` object tests receive |
| `harness/spec.py` | `ScenarioSpec` and the other typed setup dataclasses |
| `harness/archive.py` | `Archive`, `Csv`, and the assertion helpers |
| `harness/oracle.py` | Snapshots and the `Oracle` (spec-derived totals, drift detection) |
| `harness/coverage.py` | `INVENTORY`: every collector CSV column and the tests that cover it |
| `harness/universal.py` | Tests every scenario imports |
| `harness/{cluster,stack,receiver,fixtures,prometheus,artifacts,command,versions}.py` | Provisioning, Helm, upload capture, fixtures, readiness, output, pins |
| `scenarios/<name>/test_<name>.py`, `workload.yaml` | One scenario: spec, truth helpers, tests |
| `tests/` | Harness unit tests |
| `scripts/` | Fake receiver, host-alias injection, version resolver, legacy stack-validation scripts |
| `docs/` | `architecture.md`, `extending.md`, `scenarios/` (index, template, per-scenario docs) |

## Keep context small

Measured sizes. Do not read the large ones whole.

| File | Size | How to read |
| --- | --- | --- |
| `tests/test_harness.py` | ~1020 lines, 47 KB | `grep -n "def test_"`, then `sed -n 'A,Bp'` |
| `scenarios/container-resources/test_container_resources.py` | ~900 lines, 45 KB | Read `docs/scenarios/container-resources.md` first, then grep the test |
| `harness/coverage.py` | ~360 lines, 38 KB, lines up to 870 chars | `grep -n '"node/attributes.csv"' harness/coverage.py \| cut -c1-300` |
| `scenarios/gpu-device-plugin/`, `node-pressure/`, `gpu-telemetry-fixtures/` tests | 400-560 lines, 18-26 KB | Docstring and `SCENARIO`, then grep |
| `artifacts/.../snapshots.json` | ~1.1 MB | `jq -r '.[].label'`, `jq '.[0].pods[] \| select(.metadata.name=="solo")'` |
| `artifacts/.../prometheus-targets.json` | ~190 KB | `jq` only |
| `artifacts/.../server-state.json` | ~100 KB, base64 archive body | Use `extracted-csv/` instead |
| `artifacts/.../diagnostics/pod-logs/` | ~250 KB | `grep` one pod's log |
| `artifacts/.../extracted-csv/*.csv` | small each | `head -1` for the header, `grep` for a row |

Order of reading: scenario doc, module docstring and `SCENARIO`, then the one test you need. Run
pytest with `-q --tb=short`. Use `-k` to narrow.

## Design conventions

Always:

- Write `assert actual == expected`, collector value on the left. `conftest.py` prints "actual X,
  expected Y" and the CI headline relies on that.
- Derive expected values from Kubernetes objects (snapshots, `oracle`) or fixture `Metric`
  constants. Never from collector output.
- Compare exactly. When you need a tolerance, define it as a named constant with the reason in a
  comment (e.g. `TOLERANCE = 1e-6  # %f print error`).
- Read only from the `Run` (`run.archive`, `run.oracle`, `run.snapshots`), so replay reproduces every
  assertion. No `kubectl` or HTTP calls in tests.
- Use the shared helpers in `harness/archive.py`: `assert_metric_grid` for series files, `Csv.one` /
  `Csv.where` for rows, `row_label` in messages, `label_map` / `prometheus_labels` for label columns,
  `expected_timestamps` for the grid.
- Give each assertion a message with file, identity and `MetricTime`:
  `f"{path} {row_label(row, KEYS)}"`. Keep it short; pytest adds the values.
- Map every column you assert in `harness/coverage.py` (`test:<path>::<function>`). The inventory must
  list every column the collector emits.
- Add a readiness gate (a `PrometheusQuery`) for any state a test depends on.

Never:

- `xfail`, `skip` or weaken an assertion to pass a collector defect. A red test is the finding.
- Mutate the cluster during the collection window. Designed churn belongs in the workload, with truth
  from snapshots.
- Copy a value from the archive into an expectation.

## When you change something, update the docs

| Change | Update |
| --- | --- |
| Scenario setup or tests | `docs/scenarios/<name>.md` (including its Not covered list) and its row in `docs/scenarios/README.md` |
| New scenario | New doc from `docs/scenarios/_template.md`, index row, `max-parallel` in the workflow if needed |
| Harness module, lifecycle, artifacts, CLI option, known limit | `docs/architecture.md`, and `README.md` if user-facing |
| Process for a common task | `docs/extending.md` |
| Repo map or conventions | This file |

Keep docs short, about 200 lines at most: tables and small Mermaid diagrams over prose. Do not list which tests currently fail.
Check every name and path you write with `grep`.

## Repo rules

- Commits follow Conventional Commits with a single subject line and no body
  (e.g. `test(container-resources): cover restart events`).
- Never put internal bug or ticket IDs, review notes, planning documents or phase labels in repo files,
  commit messages or docs. Untracked review or planning files in the repo root are local-only. Do not
  link, cite or commit them.
- A local `graphify-out/` (git-excluded) may exist. Its `GRAPH_REPORT.md` summarizes module
  relationships.
