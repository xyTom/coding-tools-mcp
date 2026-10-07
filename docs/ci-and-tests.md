# CI And Test Commands

This repository uses a local compliance runner plus GitHub Actions.

## One-Command Gates

```bash
make compliance
make ci
```

`make compliance` runs the full compliance suite and writes `reports/compliance/latest.json` and `reports/compliance/latest.md`.

`make ci` mirrors the main CI workflow: lint, typecheck, unittest discovery, npm launcher checks, protocol tests, integration/security tests, required docs checks, schema drift checks, dogfood smoke, and SWE-bench smoke preflight. It requires Python 3.11 or newer plus Node.js 18 or newer and npm; GitHub Actions uses Node.js 24.

Report files are overwritten by whichever suite or benchmark was run most recently. Check `suite` in compliance reports and `conclusion` in benchmark reports before citing them.

## Release from main

`v0.5.0` is sealed. Do not move its tag, delete registry files, or retrofit its
workflow. This pipeline only publishes versions greater than `0.5.0`.

1. Open a release PR that updates `pyproject.toml`, `coding_tools_mcp/__init__.py`,
   the editable package version in `uv.lock`, and a dated CHANGELOG section.
   The npm launcher has an independent version; bump it whenever any packed
   launcher file changes, including its README or package metadata.
   Keep the version and complete release metadata together in one commit. Use
   squash merge or a merge commit for multi-commit release PRs; rebase-merging a
   version bump before its changelog would correctly fail release validation.
   The currently published npm `0.1.0` still records repository directory
   `npm/coding-tools-mcp`; main now records `packages/npm-launcher`. That is a
   real packed-metadata change, so the next release PR must bump the launcher
   to a new unused version (for example `0.1.1`) even if its executable is
   unchanged. The verifier deliberately rejects reusing `0.1.0` for this tree.
2. Merge the PR into `main` after the checks pass. Main must be protected so
   only reviewed release PRs can introduce versions. Do not create a tag.
3. `.github/workflows/release.yml` selects the first-parent main commit that
   introduced that version. Every gate, build and package uses that exact SHA,
   even if the push contains later commits. A push with no version change does
   nothing. Multiple version bumps in one push fail rather than skip a release.
4. Before either publisher starts, a read-only preflight checks both registries
   against the built artifacts. An existing conflict in either registry blocks
   all new publication. Both registries must contain verified package contents before the workflow
   creates the tag and GitHub Release. The tag is a completion receipt.

Hard gates are metadata/version consistency, MCP contract/unit/security and
integration tests (`compliance`), `real-workloads`, package builds, an isolated
wheel installation outside the checkout, and npm launcher/pack verification.
SWE-bench runs separately as advisory evidence: it is not a build or publish
prerequisite. Its infrastructure failure may affect the overall evidence run,
never the package publication dependency chain. Do not make that optional
workflow a required branch-protection status.

### Repository setup before the next release

Before merging the next version PR, a repository administrator must verify these
settings in GitHub. Workflow files do not configure branch protection, rulesets,
or publishing environments.

- Protect `main` with a branch rule or ruleset requiring pull requests and at
  least one approving review. Require the `compliance` check from
  `.github/workflows/compliance.yml`, which includes release metadata validation.
  Select the check from a successful PR run and require branches to be up to date
  before merging so the required checks cover the current base.
- Keep the advisory SWE-bench checks optional. The release workflow's
  post-merge build and publish jobs are not pre-merge required checks;
  `real-workloads` remains a hard gate inside the release dependency chain.
- In Settings → Environments, verify that deployment branch rules for `pypi`
  and `npm` permit `main`. Preserve any required reviewer policy and confirm
  the trusted publishers still match repository `xyTom/coding-tools-mcp`,
  workflow `release.yml`, and their respective environment names. See
  [trusted publishing setup](#trusted-publishing-setup) below.

### Recovery and concurrency

First try **Re-run failed jobs** on the original main run. Artifacts are named
by source SHA, retained for 90 days, and reusable across run attempts. If the
artifacts have expired, rerun all jobs or dispatch `release.yml` **from main**
with `version=0.5.1` (the version to recover). Recovery uses the repaired control
workflow/scripts from main but rebuilds and tests the original version commit.
A later main commit is never silently substituted for that release source.

- Absent registry version: publish the missing files.
- Partial PyPI version: compare every existing file, upload only missing files.
- Existing npm version: compare its full packed payload, then skip publication.
- Complete registries but missing tag/Release: finalize just the missing steps.
- Existing tag alone: verify its target and treat it as incomplete; still verify
  both registries. Existing GitHub Releases are left unchanged.
- Different payload, wrong tag target, yanked/deprecated version, ambiguous
  version history, or registry/network error: stop. A 401/403/429/5xx is never
  interpreted as a missing version. Fix an infrastructure problem and rerun;
  immutable package conflicts require a new version, not deletion or overwrite.

Archive comparison checks every filename, file content and executable bit while
ignoring container timestamps/compression. Build tools are pinned in files from the original source commit (including the
npm pack CLI), and `SOURCE_DATE_EPOCH` comes from the release commit. This permits safe rebuilds
without accepting different code under the same version. Publishing never uses
an unconditional `skip-existing` to conceal conflicts.

The shared preflight prevents avoidable partial releases, including a new Python
version paired with a changed launcher that still uses npm `0.1.0`. Each publisher
repeats verification under its own lock before uploading. The registries cannot
be updated atomically: a later outage, upload failure, or publication outside this
workflow can still leave a partial release. Recover it through the same immutable
source and missing-file checks; never roll back by deleting published packages.

Per-version publish/finalize concurrency groups never cancel an in-progress
publication. npm has one publication lock across all launcher versions so latest-tag
selection and publishing cannot race between workflow runs. An older absent
launcher is published under `release-<version>` without moving npm `latest`
backward; new versions use an explicit `latest` tag. GitHub uses semantic-version
latest selection. Redundant pending reruns may
be coalesced by GitHub; rerun the original release if needed. Merge only one
release version at a time and wait for both registries and GitHub finalization
before introducing another version. Recovery of older versions remains possible
from the first-parent history.

### Trusted publishing setup

Use the existing trusted publishers for repository `xyTom/coding-tools-mcp`,
workflow filename `release.yml`, and environments `pypi` and `npm`. Environment
branch rules must allow `main` because the workflow no longer runs from tags.
Maintainers must review any necessary environment/publisher configuration;
merging this code does not create grants or change account security settings.
One GitHub platform limit remains: creating a release for a historical source
whose `.github/workflows/` tree differs from the live default branch requires
`Workflows:write`. `GITHUB_TOKEN` cannot receive that permission. The finalizer
conservatively detects this and stops with an explicit maintainer-action error,
leaving the original SHA and existing packages/tags untouched. It never changes
the release target or creates a credential to evade this restriction.

For this case, download the source-SHA release plan, Python/npm artifacts, and
both verified registry receipts from the same run. Recheck both artifact payloads
against their registries with `scripts.release_artifacts --require-complete`.
An authorized maintainer then uses their already-approved publishing identity
to run `scripts.finalize_release` with the exact plan `source_sha`, `tag`, and
source CHANGELOG (without `--github-token`, which is specific to Actions' limited
token). This creates only missing objects. If additional credential permissions
are needed, have the maintainer approve/configure them separately; this workflow
does not grant them. Rerun the main recovery workflow afterward to confirm the
now-existing tag and release. See [GitHub's release permission rule](https://docs.github.com/en/rest/releases/releases#create-a-release).

npm provenance attests the executing workflow's `GITHUB_SHA`, which can be the
newer controller commit during recovery or a batched push. It does not alone
prove the historical artifact's source. Preserve the separate source-SHA plan,
artifact hashes, and registry receipts; do not spoof provenance environment values.

Only the two publish jobs request `id-token: write`; only finalization requests
`contents: write`. Checkouts do not persist credentials. npm uses Node 24 and
an OIDC-capable pinned npm CLI, with provenance and no long-lived npm token.

Official setup references: [PyPI trusted publishers](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
and [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/).
`final-audit` remains a manual audit of existing tags, outside the release path.
It validates workflow names, successful run status, and matching commit SHAs;
its generated report does not inspect benchmark artifacts. Individual outcomes
and SWE-bench resolution counts therefore remain `UNKNOWN` in that report until
the linked, current-run evidence is reviewed. Workflow success alone is never a
substitute for successful official evaluation.
The legacy local `scripts/publish-pypi.sh` helper is for deliberately manual
publishing only; it does not coordinate npm, tags or GitHub Releases and is not
the recovery path for this workflow. Prefer the main-based recovery above.

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

Manual SWE-bench workflow:

```text
.github/workflows/swebench-lite.yml
```

The `swebench-lite` workflow defaults to `prediction_source=both`: a separate
reference-patch harness control and a real pinned SymPy repair through MCP
read/apply_patch/edit/read/diff/exec before the official harness judges its output.
This is a deterministic MCP execution replay, not a model-generated solve rate.
Harness, dataset/reference fixture, base commit, reference patch and Docker image
contents are pinned. See [the benchmark runbook](../benchmarks/swebench/README.md)
for pin maintenance, evidence interpretation, and local replay commands. Manual
runs are blocking when explicitly requested; release calls are advisory and
outside the package-publication dependency chain. `checked_in` still requires
real, nonempty predictions and complete official reports to establish a pass.

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
