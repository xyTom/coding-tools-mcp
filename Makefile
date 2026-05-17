PYTHON ?= python3
COMPLIANCE_RUNNER := PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m tests.compliance.runner
PYTHON_SOURCES := codex_tool_runtime_mcp tests benchmarks
MYPY_TARGETS := codex_tool_runtime_mcp benchmarks/mcp_http.py benchmarks/runtime_latency.py benchmarks/swebench/run_smoke.py benchmarks/swebench/generate_reference_predictions.py benchmarks/real_workloads.py
REPORT_FLAG ?= --report
SWE_BENCH_ARGS ?=
DOGFOOD_PORT ?= 8765
RUFF_FLAGS ?= --exclude benchmarks/dogfood --ignore=E501
MYPY_FLAGS ?= --python-version 3.11 --disable-error-code union-attr --disable-error-code assignment --disable-error-code arg-type --disable-error-code no-untyped-def

.PHONY: lint typecheck test ci compliance test-protocol test-integration test-mcp-contract test-tool-golden test-security test-e2e test-codex-compat test-docs-required test-schema-drift dogfood-mcp dogfood-runner dogfood-smoke benchmark-latency benchmark-smoke benchmark-real-workloads swebench-reference-predictions swebench-preflight swebench-evaluate report

lint:
	$(PYTHON) -m ruff check $(RUFF_FLAGS) $(PYTHON_SOURCES)

typecheck:
	$(PYTHON) -m mypy $(MYPY_FLAGS) $(MYPY_TARGETS)

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -p 'test_*.py'

ci: lint typecheck test test-protocol test-integration test-docs-required test-schema-drift dogfood-smoke benchmark-latency benchmark-smoke

compliance:
	$(COMPLIANCE_RUNNER) --suite all $(REPORT_FLAG)

test-protocol: test-mcp-contract

test-integration: test-tool-golden test-security test-e2e test-codex-compat

test-mcp-contract:
	$(COMPLIANCE_RUNNER) --suite mcp-contract $(REPORT_FLAG)

test-tool-golden:
	$(COMPLIANCE_RUNNER) --suite tool-golden $(REPORT_FLAG)

test-security:
	$(COMPLIANCE_RUNNER) --suite security $(REPORT_FLAG)

test-e2e:
	$(COMPLIANCE_RUNNER) --suite e2e $(REPORT_FLAG)

test-codex-compat:
	$(COMPLIANCE_RUNNER) --suite codex-compat $(REPORT_FLAG)

test-docs-required:
	$(COMPLIANCE_RUNNER) --suite docs-required $(REPORT_FLAG)

test-schema-drift:
	$(COMPLIANCE_RUNNER) --suite schema-drift $(REPORT_FLAG)

dogfood-mcp:
	$(COMPLIANCE_RUNNER) --suite dogfood $(REPORT_FLAG)

dogfood-runner:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/dogfood/mcp_deterministic_runner.py \
		--endpoint http://127.0.0.1:$(DOGFOOD_PORT)/mcp \
		--server-command "codex-tool-runtime-mcp --workspace {workspace} --host 127.0.0.1 --port $(DOGFOOD_PORT)"

dogfood-smoke: dogfood-mcp dogfood-runner

benchmark-latency:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/runtime_latency.py

benchmark-smoke: swebench-preflight

benchmark-real-workloads:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) benchmarks/real_workloads.py $(REAL_WORKLOAD_ARGS)

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

report:
	$(COMPLIANCE_RUNNER) --write-report-only
