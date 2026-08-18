# CI And Test Commands

This repository uses a local compliance runner plus GitHub Actions.

## One-Command Gates

```bash
make compliance
make ci
```

`make compliance` runs the full compliance suite and writes `reports/compliance/latest.json` and `reports/compliance/latest.md`.

`make ci` mirrors the main CI workflow: lint, typecheck, unittest discovery, npm launcher checks, protocol tests, integration/security tests, required docs checks, schema drift checks, dogfood smoke, and SWE-bench smoke preflight. It requires Python 3.11 or newer plus Node.js 18 or newer and npm; GitHub Actions uses Node.js 22.

Report files are overwritten by whichever suite or benchmark was run most recently. Check `suite` in compliance reports and `conclusion` in benchmark reports before citing them.

## PyPI Release

Releasing is one action: push the version tag.

1. Merge the release commit (version bumped in `pyproject.toml` and
   `coding_tools_mcp/__init__.py`, CHANGELOG `Unreleased` folded into a dated
   `## <version> - YYYY-MM-DD` heading).
2. `git tag v<version> && git push origin v<version>`

Pushing the tag triggers `.github/workflows/release.yml`, which runs everything
from that single commit: release-metadata validation
(`scripts/check_release_versions.py`), the `compliance`, `real-workloads`, and
`swebench-lite` evidence workflows as called jobs, the wheel/sdist build with
content and clean-install verification, PyPI trusted publishing, npm trusted
publishing with provenance, and finally the GitHub Release with notes taken
from the CHANGELOG section. There are no inputs, no run ids to copy, and no
ref choices: evidence and publishes are jobs of one workflow run, so the
same-release-commit property holds by construction, and a failed evidence job
blocks both registries.

The npm launcher keeps its own version. The pipeline publishes it only when
`npm/coding-tools-mcp/package.json` names a version that is not yet on the
registry, so server-only releases skip the npm jobs automatically; bump the
launcher version whenever its source changes (npm versions cannot be
overwritten).

PyPI and npm trusted publishing must both be configured with workflow filename
`release.yml` and the `pypi` / `npm` environments. The `final-audit` workflow
remains available as a manual, dispatch-only audit of an existing tag; it is
no longer part of the release path.

For local or recovery publishing, use the release helper so the same build,
check, upload, and install-verification flow is used every time:

```bash
make publish-testpypi
make publish-pypi
```

`make publish-testpypi` uploads to TestPyPI only. `make publish-pypi` uploads to production PyPI and asks for an irreversible-release confirmation. To run both in sequence:

```bash
make publish-all
```

The helper expects `TWINE_USERNAME`/`TWINE_PASSWORD` or `~/.pypirc` credentials. For token auth, use `__token__` as the username. After a production upload, bump `[project].version` and `coding_tools_mcp.__version__` before the next release because PyPI files cannot be overwritten.

## Individual Gates

```bash
make check-dispatch-inputs
make check-npm-launcher
make check-release
make test-mcp-contract
make test-tool-golden
make test-security
make test-e2e
make test-runtime-semantics
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
| `make check-dispatch-inputs` | Cloudflare Worker dispatch body compared with the sandbox workflow inputs |
| `make check-npm-launcher` | npm launcher argument forwarding, runner fallback, exit behavior, and package contents |
| `make check-release` | Python/module/npm versions and release changelog checked against `RELEASE_TAG`, which defaults from `pyproject.toml` |
| `make test-mcp-contract` | Both protocol eras per method: the handshake, `2026-07-28` `_meta` validation and mirror headers, `tools/list`, schemas, annotations, structured success/error envelopes, protocol errors and their HTTP statuses |
| `make test-dual-era` | What only shows up with both eras on one server: handshake-era responses carry no modern field, a modern client works without ever handshaking, concurrent clients of either era, workspace races, and the official MCP python SDK driving both transports |
| `make test-tool-golden` | Golden behavior for read/list/search/patch/exec/stdin/kill/git/image paths |
| `make test-security` | Traversal, symlink escape, command workdir escape, risky env, shell-expansion gating, Linux Landlock fallback behavior, direct syscall denial where Landlock is available, timeout/watchdog, buffer caps |
| `make test-e2e` | End-to-end coding loops through the runtime |
| `make test-runtime-semantics` | Patch/command/image behavior vectors |
| `make test-docs-required` | Required docs, evidence artifacts, and CI workflow gate checks |
| `make test-schema-drift` | Live tool schema/annotation names compared against the checked-in runtime contract/docs |
| `make dogfood-mcp` | Unittest MCP-only dogfood cases |
| `make dogfood-runner` | Full deterministic HTTP dogfood transcript and report |
| `make dogfood-smoke` | Both dogfood suites |
| `make benchmark-smoke` | SWE-bench smoke preflight and placeholder prediction validation |
| `make benchmark-real-workloads` | MCP runtime smoke over real Python, Node, Rust, Go, and monorepo checkouts plus large file/output and long command cases |

Valid runner suites include `all`, `mcp-contract`, `dual-era`, `tool-golden`, `security`, `e2e`, `runtime-semantics`, `dogfood`, `compliance-report`, `docs-required`, and `schema-drift`.

## GitHub Actions

Main workflow:

```text
.github/workflows/compliance.yml
```

The main workflow also includes a `windows-msvc-smoke` job. It verifies that
Windows reports unsupported TTY requests explicitly, force-kills a background
command without relying on POSIX `SIGKILL`, initializes Visual Studio with
`vcvarsall.bat x64`, checks the narrow default `core` environment, and confirms
that `--shell-env-inherit all` can compile and run a single-file `cl.exe` smoke.
It also exercises PowerShell 7 selection, the trusted `cmd.exe` compatibility
fallback, and the shell-specific safe-mode policy gates.

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

Docker workflows:

```text
.github/workflows/docker-image.yml
.github/workflows/docker-smoke.yml
```

`docker-image` builds and publishes the sandbox image to GHCR. `docker-smoke` builds the image, starts `coding-tools-mcp --permission-mode trusted` in a container, verifies MCP metadata and `tools/list`, checks `server_info`, and runs explicit `exec_command` toolchain version commands.
