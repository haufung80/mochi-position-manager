# Convenience targets. The OpenAPI dump is the Phase-0 review/CI-gate artifact.
#
# `openapi` regenerates docs/openapi-{existing,funding-arb}.{json,yaml}. The dump
# script forces DRY_RUN=1 + a dummy FUNDING_ARB_SECRET internally, so it never
# touches an exchange or needs real creds. CI runs this then drift-gates ONLY
# docs/openapi-funding-arb.* (the contract-grade file).

.PHONY: openapi test

openapi:
	.venv/bin/python scripts/dump_openapi.py

test:
	.venv/bin/python -m pytest tests/ -q --cov=app --cov-report=term-missing --cov-fail-under=75
