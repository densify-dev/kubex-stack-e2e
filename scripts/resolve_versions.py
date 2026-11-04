#!/usr/bin/env python3
"""Print the chart version and default collector image a run would use, as GITHUB_OUTPUT lines.

CI resolves these once so every scenario leg tests the same chart and collector.
Usage: python3 -m scripts.resolve_versions [chart-version | chart-directory]
A chart directory is printed back as chart_version, so every leg renders the same checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness.command import run
from harness.spec import ClusterSpec, Node, ScenarioSpec
from harness.stack import find_forwarder, is_local_chart, prepare_chart, template_args

REPO_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    scenario = ScenarioSpec(name="resolve", cluster=ClusterSpec(workers=[Node()]))
    stack = scenario.stack
    requested = sys.argv[1] if len(sys.argv) > 1 else ""
    version = prepare_chart(requested, REPO_ROOT)
    chart = requested if is_local_chart(requested) else version
    rendered = run(template_args(scenario.name, stack, scenario.forwarder, chart), cwd=REPO_ROOT, timeout=900)
    print(f"chart_version={chart}")
    print(f"collector_image={find_forwarder(rendered)[1]}")


if __name__ == "__main__":
    main()
