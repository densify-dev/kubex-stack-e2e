"""Pinned defaults for the Kind functional test suite."""

KIND_NODE_IMAGE = (
    "kindest/node:v1.37.0@sha256:"
    "a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
)
# None = latest stable chart / the chart's default collector image, resolved per run.
STACK_CHART_VERSION = None
COLLECTOR_IMAGE = None
FIXTURE_IMAGE = (
    "python@sha256:"
    "656d12e70054d5fda18a045e2494c96701e9792dd1445f95b3d038df954f57e9"
)
