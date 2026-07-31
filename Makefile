PYTHON ?= python3
PROJECT_VERSION := $(shell $(PYTHON) -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
RELEASE_TAG ?= v$(PROJECT_VERSION)
COMPLIANCE_RUNNER := PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m tests.compliance.runner
PYTHON_SOURCES := coding_tools_mcp apps/desktop-client/mcp_desktop_client tests benchmarks
MYPY_TARGETS := coding_tools_mcp benchmarks/mcp_http.py benchmarks/runtime_latency.py benchmarks/swebench/run_smoke.py benchmarks/swebench/generate_reference_predictions.py benchmarks/real_workloads.py
REPORT_FLAG ?= --report
SWE_BENCH_ARGS ?=
DOGFOOD_PORT ?= 18772
MCP_WORKSPACE ?= .
MCP_HOST ?= 127.0.0.1
MCP_PORT ?= 8765
MCP_ARGS ?=
RUFF_FLAGS ?= --exclude benchmarks/dogfood --ignore=E501
MYPY_FLAGS ?= --python-version 3.11 --disable-error-code union-attr --disable-error-code assignment --disable-error-code arg-type --disable-error-code no-untyped-def
PYSIDE6_LUPDATE ?= pyside6-lupdate
PYSIDE6_LRELEASE ?= pyside6-lrelease
DESKTOP_PACKAGE := apps/desktop-client/mcp_desktop_client
DESKTOP_TS := $(DESKTOP_PACKAGE)/locales/app_zh_CN.ts
DESKTOP_QM := $(DESKTOP_PACKAGE)/locales/app_zh_CN.qm

.PHONY: start lint typecheck test ci check-dispatch-inputs check-npm-launcher check-release compliance test-protocol test-integration test-mcp-contract test-tool-golden test-security test-e2e test-runtime-semantics test-docs-required test-schema-drift dogfood-mcp dogfood-runner dogfood-smoke benchmark-latency benchmark-smoke benchmark-real-workloads swebench-reference-predictions swebench-preflight swebench-evaluate desktop-i18n-update desktop-i18n-release desktop-i18n-check install-user publish-testpypi publish-pypi publish-all report

start:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m coding_tools_mcp --workspace "$(MCP_WORKSPACE)" --host "$(MCP_HOST)" --port "$(MCP_PORT)" $(MCP_ARGS)

lint:
	$(PYTHON) -m ruff check $(RUFF_FLAGS) $(PYTHON_SOURCES)
	$(PYTHON) scripts/check_desktop_i18n.py

check-dispatch-inputs:
	$(PYTHON) scripts/check_dispatch_inputs.py

check-npm-launcher:
	cd npm/coding-tools-mcp && npm test
	cd npm/coding-tools-mcp && npm pack --dry-run --json >/dev/null

check-release:
	$(PYTHON) scripts/check_release_versions.py --tag "$(RELEASE_TAG)"

typecheck:
	$(PYTHON) -m mypy $(MYPY_FLAGS) $(MYPY_TARGETS)

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -p 'test_*.py'

ci: lint typecheck test check-dispatch-inputs check-npm-launcher test-protocol test-integration test-docs-required test-schema-drift dogfood-smoke benchmark-latency benchmark-smoke

compliance:
	$(COMPLIANCE_RUNNER) --suite all $(REPORT_FLAG)

test-protocol: test-mcp-contract

test-integration: test-tool-golden test-security test-e2e test-runtime-semantics

test-mcp-contract:
	$(COMPLIANCE_RUNNER) --suite mcp-contract $(REPORT_FLAG)

test-tool-golden:
	$(COMPLIANCE_RUNNER) --suite tool-golden $(REPORT_FLAG)

test-security:
	$(COMPLIANCE_RUNNER) --suite security $(REPORT_FLAG)

test-e2e:
	$(COMPLIANCE_RUNNER) --suite e2e $(REPORT_FLAG)

test-runtime-semantics:
	$(COMPLIANCE_RUNNER) --suite runtime-semantics $(REPORT_FLAG)

test-docs-required:
	$(COMPLIANCE_RUNNER) --suite docs-required $(REPORT_FLAG)

test-schema-drift:
	$(COMPLIANCE_RUNNER) --suite schema-drift $(REPORT_FLAG)

dogfood-mcp:
	$(COMPLIANCE_RUNNER) --suite dogfood $(REPORT_FLAG)

dogfood-runner:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/dogfood/mcp_deterministic_runner.py \
		--endpoint http://127.0.0.1:$(DOGFOOD_PORT)/mcp \
		--server-command "$(PYTHON) -m coding_tools_mcp --workspace {workspace} --host 127.0.0.1 --port $(DOGFOOD_PORT)"

dogfood-smoke: dogfood-mcp dogfood-runner

benchmark-latency:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/runtime_latency.py

benchmark-smoke: swebench-preflight

benchmark-real-workloads:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/real_workloads.py $(REAL_WORKLOAD_ARGS)

desktop-i18n-update:
	$(PYSIDE6_LUPDATE) -tr-function-alias 'translate+=tr' -extensions py $(DESKTOP_PACKAGE) \
		-source-language en_US -target-language zh_CN -locations relative -ts $(DESKTOP_TS)

desktop-i18n-release:
	$(PYSIDE6_LRELEASE) $(DESKTOP_TS) -qm $(DESKTOP_QM)

desktop-i18n-check: desktop-i18n-update desktop-i18n-release
	$(PYTHON) scripts/check_desktop_i18n.py
	git diff --exit-code -- $(DESKTOP_TS) $(DESKTOP_QM)

swebench-reference-predictions:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/swebench/generate_reference_predictions.py \
		--instance-id sympy__sympy-12419 \
		--baseline-output reports/benchmark/swebench-reference-predictions/baseline_reference.jsonl \
		--candidate-output reports/benchmark/swebench-reference-predictions/candidate_reference.jsonl \
		--metadata-output reports/benchmark/swebench-reference-predictions/metadata.json

swebench-preflight:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/swebench/run_smoke.py $(SWE_BENCH_ARGS)

swebench-evaluate:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/swebench/run_smoke.py --run-evaluation $(SWE_BENCH_ARGS)

install-user:
	scripts/install.sh

publish-testpypi:
	PYTHON=$(PYTHON) scripts/publish-pypi.sh --testpypi

publish-pypi:
	PYTHON=$(PYTHON) scripts/publish-pypi.sh --pypi

publish-all:
	PYTHON=$(PYTHON) scripts/publish-pypi.sh --both

report:
	$(COMPLIANCE_RUNNER) --write-report-only
