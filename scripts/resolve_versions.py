#!/usr/bin/env python3
"""Print the chart version and default collector image a run would use, as GITHUB_OUTPUT lines.

CI resolves these once so every scenario leg tests the same chart and collector.
Usage: python3 -m scripts.resolve_versions [chart-version]
"""

from __future__ import annotations

import sys
from pathlib import Path
import yaml

from harness.command import run
from harness.spec import ClusterSpec, Node, ScenarioSpec
from harness.stack import CHART, REPOSITORY, find_forwarder, resolve_chart_version, template_args

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    scenario = ScenarioSpec(name="resolve", cluster=ClusterSpec(workers=[Node()]))
    stack = scenario.stack
    run(["helm", "repo", "add", CHART.split("/")[0], REPOSITORY, "--force-update"], cwd=REPO_ROOT)
    chart_version = sys.argv[1] if len(sys.argv) > 1 else ""
    chart_path = sys.argv[2] if len(sys.argv) > 2 else None
    if chart_path:
        version = str(yaml.safe_load(Path(chart_path, "Chart.yaml").read_text(encoding="utf-8"))["version"])
    else:
        version = chart_version or resolve_chart_version(CHART, REPO_ROOT)
    rendered = run(template_args(scenario.name, stack, scenario.forwarder, version if not chart_path else None, chart_path=chart_path), cwd=REPO_ROOT, timeout=900)
    print(f"chart_version={version}")
    print(f"collector_image={find_forwarder(rendered)[1]}")


if __name__ == "__main__":
    main()
