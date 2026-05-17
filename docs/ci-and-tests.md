# CI And Test Commands

This repository uses a local compliance runner plus GitHub Actions.

## One-Command Gates

```bash
make compliance
make ci
```

`make compliance` runs the full compliance suite and writes `reports/compliance/latest.json` and `reports/compliance/latest.md`.

`make ci` mirrors the main CI workflow: lint, typecheck, unittest discovery, protocol tests, integration/security tests, required docs checks, schema drift checks, dogfood smoke, and SWE-bench smoke preflight.

Report files are overwritten by whichever suite or benchmark was run most recently. Check `suite` in compliance reports and `conclusion` in benchmark reports before citing them.

## Individual Gates

```bash
make test-mcp-contract
make test-tool-golden
make test-security
make test-e2e
make test-codex-compat
make test-docs-required
make test-schema-drift
make dogfood-mcp
make dogfood-runner
make dogfood-smoke
make benchmark-smoke
make benchmark-real-workloads
```

| Command | Coverage |
| --- | --- |
| `make test-mcp-contract` | MCP initialize, `tools/list`, schemas, annotations, structured success/error envelopes, protocol errors |
| `make test-tool-golden` | Golden behavior for read/list/search/patch/exec/stdin/kill/git/image paths |
| `make test-security` | Traversal, symlink escape, command workdir escape, risky env, shell-expansion gating, Linux Landlock confinement, direct syscall denial, timeout/watchdog, buffer caps |
| `make test-e2e` | End-to-end coding loops through the runtime |
| `make test-codex-compat` | Codex-compatible patch/session/image behavior vectors |
| `make test-docs-required` | Required docs, evidence artifacts, and CI workflow gate checks |
| `make test-schema-drift` | Live tool schema/annotation names compared against checked-in profile/docs |
| `make dogfood-mcp` | Unittest MCP-only dogfood cases |
| `make dogfood-runner` | Full deterministic HTTP dogfood transcript and report |
| `make dogfood-smoke` | Both dogfood suites |
| `make benchmark-smoke` | SWE-bench smoke preflight and placeholder prediction validation |
| `make benchmark-real-workloads` | MCP runtime smoke over real Python, Node, Rust, Go, and monorepo checkouts plus large file/output and long command cases |

Valid runner suites include `all`, `mcp-contract`, `tool-golden`, `security`, `e2e`, `codex-compat`, `dogfood`, `compliance-report`, `docs-required`, and `schema-drift`.

## GitHub Actions

Main workflow:

```text
.github/workflows/compliance.yml
```

Manual SWE-bench workflow:

```text
.github/workflows/swebench-lite.yml
```

The manual `swebench-lite` workflow can install the official harness, record Docker diagnostics, run selected Lite instance IDs, and upload `reports/benchmark/**`. It defaults to `prediction_source=reference_patch`, which generates non-empty SWE-bench reference-patch predictions for official harness sanity. It fails by default unless official harness results include parsed resolved counts with `candidate_mcp_resolved >= baseline_native_resolved`. Use `prediction_source=checked_in` only after replacing the scaffold files with model-generated predictions.

Manual real-workload workflow:

```text
.github/workflows/real-workloads.yml
```

The manual `real-workloads` workflow installs Python, Node, Go, and Rust toolchains, runs `make benchmark-real-workloads`, and uploads `reports/benchmark/real-workloads**`.
