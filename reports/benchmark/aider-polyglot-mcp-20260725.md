# Coding Tools MCP: Aider Polyglot Tool-Chain Evaluation

- Date: 2026-07-25
- Branch: `benchmark/aider-polyglot-mcp-20260725` (original run branch, cut from `eebe9be`)
- Base commit: `9ad493525ee86c3913cd75c8a5d33739e151a619` (evaluation sandbox checkout; this commit is not present in this repository)
- Benchmark corpus: Aider Polyglot, 225 Exercism tasks across six languages
- Evaluation type: deterministic tool-chain evaluation using repository reference implementations

## Executive summary

This run evaluates the Coding Tools MCP file-editing and command-execution path independently of model problem-solving ability:

```text
read_file -> construct reference patch -> apply_patch -> read_file verification -> exec_command tests
```

Every one of the 225 task patches was accepted and applied by `apply_patch`. File reads succeeded for all tasks. Post-edit verification matched the reference implementation for 224 of 225 tasks; the single mismatch was an extra blank line at end-of-file that was normalized away. The official task tests passed on 211 of 225 tasks. All 14 test failures were traced to missing benchmark fixtures or missing sandbox dependencies, rather than incorrect patch application.

### Aggregate results

| Metric | Result |
|---|---:|
| Tasks | 225 |
| `read_file` success | 225/225 (100.00%) |
| `apply_patch` success | 225/225 (100.00%) |
| Post-edit reference match | 224/225 (99.56%) |
| Official tests passed | 211/225 (93.78%) |
| Tests passed after excluding confirmed fixture/dependency gaps | 211/211 (100.00%) |
| Strict read + patch + exact verification + tests | 210/225 (93.33%) |

### Results by language

| Language | Tasks | Patch success | Exact verification | Tests passed |
|---|---:|---:|---:|---:|
| C++ | 26 | 26 | 26 | 24 |
| Go | 39 | 39 | 39 | 39 |
| Java | 47 | 47 | 46 | 41 |
| JavaScript | 49 | 49 | 49 | 49 |
| Python | 34 | 34 | 34 | 34 |
| Rust | 30 | 30 | 30 | 24 |
| **Total** | **225** | **225** | **224** | **211** |

## Scope and interpretation

This is not an official Aider model score. Aider's normal Polyglot evaluation combines:

1. natural-language problem solving,
2. edit-format compliance,
3. edit parsing and file mutation,
4. test execution, and
5. optional retry based on test feedback.

This evaluation deliberately supplies the repository reference implementation and therefore measures the tool layer, not model intelligence. The 93.78% raw test-pass rate must not be compared directly with a model's Aider `Percent correct` score.

## Tool findings

### `read_file`

Result: 225/225 successful reads with no observed content truncation or incorrect path resolution in the benchmark workload.

Assessment: strong.

### `apply_patch`

Result: 225/225 patches accepted and applied, including multi-file C++ and Java edits.

Observed patch sizes included approximately:

- C++: median 2.1 KB, maximum 9.2 KB; 23 multi-file tasks.
- Java: median 3.1 KB, maximum 11.7 KB.
- JavaScript: median 2.1 KB, maximum 7.4 KB.

Observed issues:

1. **End-of-file blank-line normalization**

   Java `circular-buffer` expected two trailing newline characters. The applied result retained one. Code and tests were unaffected, but exact byte/line fidelity was 224/225 rather than 225/225.

2. **Delete and add the same path in one patch**

   A smoke test using `*** Delete File: path` followed by `*** Add File: path` in the same envelope was rejected with `Cannot add file that already exists`. Expressing the replacement as `*** Update File` worked.

Assessment: first-tier for normal text-edit workloads, but not yet fully byte-preserving or fully compatible with all patch-replacement idioms.

### `exec_command`

Functional test execution worked after the necessary runtime dependencies and environment roots were configured. The main issues were operational rather than command correctness.

Observed issues:

1. **Completed-session race**

   A long command may initially return `status=running`. If the command exits before the next poll, `write_stdin` and `read_output` can return `SESSION_NOT_FOUND`, losing the final exit result unless the caller independently wrote status and logs to disk.

2. **Toolchain environment inheritance**

   The default `shell_env_inherit=core` omitted Rust variables including `CARGO_HOME` and `RUSTUP_HOME`. Rust commands initially failed until the environment was explicitly supplied.

3. **Writable package caches**

   `/usr/local/cargo` was readable but not writable for crate downloads. A workspace-local `CARGO_HOME` was required.

4. **JDK Landlock read roots**

   Java initially failed with `java.lang.InternalError: Error loading java.security file`. Adding `/etc/java-17-openjdk` and `/etc/maven` to `CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS` resolved the problem.

5. **Control-plane starvation under concurrent compilation**

   Running C++, Java, and JavaScript benchmark groups concurrently in a 2-vCPU sandbox caused authenticated MCP calls to return sustained HTTP 502 responses while the unauthenticated endpoint probe still reached the tunnel. Sequential execution on a fresh sandbox succeeded.

Assessment: command execution is capable, but session retention, default toolchain discovery, and control-plane resource isolation require hardening.

### `write_stdin` and `read_output`

The present lifecycle behaves approximately as:

```text
running -> process exits -> session immediately disappears
```

A more reliable agent-facing lifecycle would be:

```text
running -> exited with retained final result -> TTL cleanup
```

Recommended behavior:

- retain completed sessions and their output for 5-15 minutes;
- let `write_stdin` return the final completed result for an exited session;
- keep `read_output` references valid independently of interactive-session retention.

### Runtime diagnostics

One Rust dependency failure was diagnosed as `LANDLOCK_READ_ROOT_BLOCKED`, even though the underlying error was an undeclared Cargo dependency such as `can't find crate` or `unresolved import`.

Recommended change: require explicit permission-denial evidence (`Permission denied`, `Operation not permitted`, or a Landlock denial signal) before emitting the Landlock diagnostic.

## Non-tool test failures

### C++: 2 tasks

- `gigasecond`
- `meetup`

Cause: sandbox missing Boost Date Time headers/libraries.

```text
Could NOT find Boost (missing: Boost_INCLUDE_DIR date_time)
```

### Rust: 6 tasks

Reference implementations use crates absent from the supplied `Cargo.toml`:

- `alphametics`: `itertools`, `permutohedron`
- `decimal`: `num_bigint`, `num_traits`
- `grep`: `thiserror`
- `pig-latin`: `regex`
- `poker`: `counter`
- `robot-name`: `rand`

### Java: 6 tasks

Reference implementations depend on helper classes not included in the configured solution-file set:

- `bowling`: `Frame`
- `connect`: `Board`
- `forth`: `Token`
- `mazy-mice`: `Dimensions`
- `ocr-numbers`: `Digit`
- `poker`: `Hand`

## External comparison

### Aider Polyglot leaderboard

Aider's public Polyglot leaderboard evaluates 225 tasks and reports both task correctness and correct edit-format usage. The published leaderboard snapshot lists GPT-5 high at 88.0% task correctness and 91.6% correct edit format. Some other model runs reach approximately 99.6% correct edit format while solving substantially fewer tasks.

Source: https://aider.chat/docs/leaderboards/

The correct comparison is therefore:

- Coding Tools MCP `apply_patch` acceptance on already-valid patches: **100.0%**.
- Model ability to emit a valid Aider edit format: typically below 100%, with strong runs ranging from the low 90s to approximately 99.6%.
- Model end-to-end task correctness: not measured by this tool-only run.

This supports classifying the normal `apply_patch` path as first-tier, but it does not establish a model/agent leaderboard position.

### Mature patch engines

`git apply` is designed to check patch applicability and, by default, fail the whole patch atomically if any hunk does not apply. It is a mature baseline for traditional unified diffs, but it uses a different format and no standardized public benchmark reports a tool-only Aider-225 score for `git apply`, GNU patch, Aider's internal edit applicator, Cursor, Claude Code, Codex, or similar systems.

Source: https://git-scm.com/docs/git-apply

Consequently, the 100% patch-acceptance result is strong, but a claim of outright first place would not be evidence-based without running the same generated patch corpus through competing applicators.

### Broader coding-agent benchmarks

SWE-bench evaluates complete agents on real repository issues, not just patch application. The current official leaderboard uses 500 human-filtered Verified tasks and reports end-to-end resolved percentages. These scores are not comparable to the deterministic patch-application result here.

Source: https://www.swebench.com/

Recent research also shows that harness design can move end-to-end coding-agent results by tens of percentage points even with a fixed model, reinforcing the need to separate model capability, agent orchestration, patch transport, and execution environment.

Source: https://arxiv.org/abs/2606.12344

## Overall assessment

### What is already first-tier

- Normal text patch parsing and application.
- Multi-file transaction success in this workload.
- Read-after-write consistency for ordinary source files.
- Six-language coverage when toolchains are configured.

### What prevents a stronger claim

- One byte-level EOF fidelity difference.
- Same-path delete-plus-add incompatibility.
- Completed-session results can disappear before polling.
- Java and Rust need manual environment/read-root configuration.
- Concurrent builds can starve the MCP control plane.
- There is no public, standardized patch-applicator leaderboard for direct ranking.

## Prioritized recommendations

1. **P0:** retain completed command sessions and final output for a TTL.
2. **P0:** isolate MCP server resources from child build processes and cap execution concurrency.
3. **P1:** auto-detect and allow standard Java/Rust toolchain roots and environment variables.
4. **P1:** preserve exact EOF newline counts when replacing file content.
5. **P2:** support delete-plus-add of the same path as an atomic replacement, or return a precise compatibility hint.
6. **P2:** tighten Landlock diagnostic classification.
7. **P2:** add this deterministic 225-task run as an optional regression benchmark, with dependency preflight and incremental reports.

## Bottom line

The editing engine itself performed very well: 225/225 patch applications succeeded. That is a first-tier result for the tested valid-patch workload. The meaningful weaknesses are now mostly around command-session lifecycle, sandbox toolchain configuration, resource isolation, and exact file-byte fidelity—not basic patch landing.
