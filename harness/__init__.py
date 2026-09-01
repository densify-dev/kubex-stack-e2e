"""Kind functional E2E harness.

Scenario modules import from here. Everything in this package is infrastructure:
cluster provisioning, stack rendering, fixture deployment, capture, extraction,
and the Kubernetes oracle. Assertions live in the scenario modules themselves.
"""
