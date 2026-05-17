# Compliance

The one-command acceptance gate is:

```bash
make compliance
```

It runs protocol, golden tool, security, E2E, Codex compatibility, dogfood, compliance-report, required docs/evidence/workflow, and schema-drift checks. Report files:

- [reports/compliance/latest.json](reports/compliance/latest.json)
- [reports/compliance/latest.md](reports/compliance/latest.md)

Always inspect `suite`, `passed`, `tests_run`, `security`, `e2e`, `codex_dogfood`, and `required_tools`. If `suite` is not `all`, required tool coverage is `not_measured` by design.

The CI-shaped local gate is:

```bash
make ci
```

It adds lint, typecheck, unittest discovery, required docs checks, schema-drift checks, full deterministic dogfood runner, and SWE-bench preflight.

## Coverage

- MCP initialize, `tools/list`, `tools/call`, schemas, annotations, structured success/failure output, unknown tool behavior, protocol errors, trace redaction, and stdout cleanliness.
- Tool golden cases for read/list/search/patch/exec/stdin/kill/git status/git diff/image.
- Security cases for traversal, absolute paths, symlink escape, command workdir escape, direct and interpreter-mediated outside reads, direct syscall outside reads and writes, destructive command policy, shell-expansion gating, obfuscated network access, risky env rejection, Linux Landlock confinement, session timeout enforcement, watchdog cleanup, bounded output buffers, request-permission non-grants, and concurrent read-only calls.
- Deterministic E2E loops for JavaScript bugfix, Python function add, long-running stdin, session close behavior, workspace escape denial, and image viewing.
- MCP-only dogfood without direct filesystem or shell bypass during task execution.
- Compliance report generation semantics, including non-overclaiming partial-suite tool coverage.

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
```

GitHub Actions workflows:

- [.github/workflows/compliance.yml](.github/workflows/compliance.yml)
- [.github/workflows/swebench-lite.yml](.github/workflows/swebench-lite.yml)
