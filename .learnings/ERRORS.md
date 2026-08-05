# Error Log

## [ERR-20260729-001] powershell-rg-markdown-backtick

**Logged**: 2026-07-29T23:39:58+08:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary

A PowerShell `rg` verification command failed because a Markdown backtick inside a double-quoted command was interpreted as PowerShell escaping.

### Error

```text
rg: the literal "\n" is not allowed in a regex
```

### Context

- The command attempted to search the generated integration plan for several strings in one regular expression.
- A literal Markdown backtick before `npm` escaped the following character/newline at the PowerShell parsing layer.

### Suggested Fix

Use single-quoted PowerShell patterns without embedded Markdown backticks, or split verification into several simple `rg` calls.

### Metadata

- Reproducible: yes
- Related Files: docs/upstream-v0.2.2-integration-agent-execution-plan.md

### Resolution

- **Resolved**: 2026-07-29T23:39:58+08:00
- **Notes**: Continued with simple patterns that do not include Markdown backticks.

---

## [ERR-20260803-009] uv-cache-denied-in-isolated-localappdata

**Logged**: 2026-08-03T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

`uv run --frozen` could not initialize its cache after `LOCALAPPDATA` was redirected to the review sandbox.

### Error

```text
Failed to initialize cache at G:\LLM\coding-tools-mcp\.tmp\broker-v6-final-review\localappdata\uv\cache: access denied (os error 5)
```

### Context

- The command was a read-only focused review run against `G:\LLM\coding-tools-mcp-broker-v6`.
- HOME/APPDATA/LOCALAPPDATA/TEMP were isolated under the writable main repository.
- The failure occurred before test discovery; it is not a product test failure.

### Suggested Fix

Reuse the project's existing `uv` cache or invoke the existing virtual-environment Python directly while keeping bytecode writes disabled.

### Metadata

- Reproducible: unknown
- Related Files: pyproject.toml

### Resolution

- **Resolved**: 2026-08-03T00:00:00+08:00
- **Notes**: Bypassed `uv` cache initialization and ran the same five focused tests with the worktree's existing `.venv` Python; all five passed.

---

## [ERR-20260802-005] npm-wrapper-user-prefix-access-denied

**Logged**: 2026-08-02T08:25:00+08:00
**Priority**: low
**Status**: pending
**Area**: config

### Summary

The PowerShell npm wrapper reported an access-denied warning for the user-level npm prefix after the project build had completed successfully.

### Error

```text
Test-Path: Access to the path 'C:\Users\YING\AppData\Roaming\npm\node_modules\npm\bin\npm-cli.js' is denied.
```

### Context

- `npm run build` completed and generated `coding_tools_mcp/webui_dist/admin.html`.
- The wrapper warning appeared after npm output and did not change the command exit code.
- The repository-local build does not require the denied user-level npm path.

### Suggested Fix

Prefer `npm.cmd` for project scripts in this managed PowerShell environment, and treat this warning as non-blocking when the native command exit code and expected artifacts confirm success.

### Metadata

- Reproducible: unknown
- Related Files: webui/package.json, webui/scripts/build.mjs

---

## [ERR-20260802-001] rg-unavailable-in-review-environment

**Logged**: 2026-08-02T01:07:48+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

The review environment did not provide the `rg` executable, so repository file discovery failed.

### Error

```text
The term 'rg' is not recognized as a name of a cmdlet, function, script file, or executable program.
```

### Context

- The command attempted to enumerate repository standards files before reviewing `main...feat/upstream-tool-broker-v6`.
- The active shell was native Windows PowerShell.

### Suggested Fix

Fall back immediately to `Get-ChildItem` and `Select-String` when `Get-Command rg` reports that ripgrep is unavailable.

### Metadata

- Reproducible: yes
- Related Files: none

### Resolution

- **Resolved**: 2026-08-02T01:07:48+08:00
- **Notes**: Continued with PowerShell-native repository discovery.

---

## [ERR-20260802-002] uv-user-cache-denied-during-review

**Logged**: 2026-08-02T01:13:53+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The Broker review test command could not initialize uv's default user cache under the managed filesystem policy.

### Error

```text
Failed to initialize cache at C:\Users\YING\AppData\Local\uv\cache: access denied.
```

### Context

- The attempted command was the authoritative `uv run --frozen python -m unittest discover` gate.
- Test TEMP/TMP were already repository-local, but `UV_CACHE_DIR` was not set.

### Suggested Fix

Set `UV_CACHE_DIR` to a repository-local `.tmp` directory before invoking uv in managed Windows sessions.

### Metadata

- Reproducible: yes
- Related Files: docs/upstream-broker-handoffs/T11-release-validation.md

### Resolution

- **Resolved**: 2026-08-02T01:15:00+08:00
- **Notes**: A repository-local `UV_CACHE_DIR` was also denied when uv was launched from the external worktree. Validation continued with the worktree's existing `.venv\Scripts\python.exe`, avoiding uv cache initialization entirely.

---

## [ERR-20260802-003] unittest-module-resolution-used-main-tests

**Logged**: 2026-08-02T01:22:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

Running named unittest modules from the main worktree resolved the main worktree's `tests` package instead of the external feature worktree's tests.

### Error

```text
ModuleNotFoundError: No module named 'tests.compliance.test_upstream_broker_calls'
```

### Context

- `PYTHONPATH` pointed at the feature worktree, but the main worktree current directory appeared earlier on `sys.path`.
- The main branch does not contain the feature branch's newly added test modules.

### Suggested Fix

Use unittest discovery with explicit feature-worktree `-s` and `-t` paths instead of named `tests.*` modules from the main worktree.

### Metadata

- Reproducible: yes
- Related Files: tests/compliance/test_upstream_broker_calls.py

### Resolution

- **Resolved**: 2026-08-02T01:22:00+08:00
- **Notes**: Switched targeted validation to explicit discovery roots.

---

## [ERR-20260802-004] managed-policy-blocked-review-temp-cleanup

**Logged**: 2026-08-02T01:25:00+08:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary

The managed command policy rejected cleanup of the repository-local Broker review temporary directory.

### Error

```text
Remove-Item ... rejected: blocked by policy
```

### Context

- The resolved target was `G:\LLM\coding-tools-mcp\.tmp\broker-review`.
- The same rejection recurred for `G:\LLM\coding-tools-mcp\.tmp\broker-v6-t12-verify` after filesystem permissions were relaxed.
- The command validated that the target was under the repository's `.tmp` directory before requesting recursive deletion.

### Suggested Fix

Delete the known review directory through an approved workspace cleanup mechanism when available.

### Metadata

- Reproducible: yes
- Related Files: .tmp/broker-review, .tmp/broker-v6-t12-verify
- Recurrence-Count: 2
- Last-Seen: 2026-08-02

---

## [ERR-20260802-005] powershell-home-variable-collision

**Logged**: 2026-08-02T10:40:07+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary

A Windows validation command declared `$home`, which is the same case-insensitive read-only variable as PowerShell `$HOME`, so the intended isolated HOME was not applied.

### Error

```text
Cannot overwrite variable HOME because it is read-only or constant.
```

### Context

- The command was independently rerunning the Broker T12 Windows unittest discovery.
- Because the command continued after the assignment error, one instructions test read the real user-home `AGENTS.md` and failed for environmental contamination.

### Suggested Fix

Always use a task-specific variable such as `$verifyHome`; after environment setup commands, stop immediately on any PowerShell error before starting tests.

### Metadata

- Reproducible: yes
- Related Files: tests/compliance/test_runtime_helpers.py
- See Also: repository AGENTS.md Windows command construction rules

### Resolution

- **Resolved**: 2026-08-02T10:40:07+08:00
- **Notes**: The failed test is being rerun with `$verifyHome` and explicit environment verification before discovery.

---

## [ERR-20260802-006] external-worktree-process-cannot-use-main-temp

**Logged**: 2026-08-02T11:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A Python process launched from the repository-external Broker worktree could not create `TemporaryDirectory` entries under the writable main-repository `.tmp` directory in managed mode.

### Error

```text
FileNotFoundError: No usable temporary directory found
```

### Context

- Five read-only regression tests passed before the test requiring `TemporaryDirectory` failed during setup.
- The configured TEMP/TMP path was under `G:\LLM\coding-tools-mcp\.tmp`.

### Suggested Fix

Launch the feature interpreter from the writable main worktree with `-P` and set `PYTHONPATH` explicitly to the feature worktree, then verify the imported module path before running tests.

### Metadata

- Reproducible: yes
- Related Files: tests/compliance/test_upstream_broker_calls.py
- See Also: ERR-20260802-002, ERR-20260802-003

### Resolution

- **Resolved**: 2026-08-02T11:00:00+08:00
- **Notes**: The remaining regression passed when launched from the main worktree with isolated import resolution.

---

## [ERR-20260802-007] incorrect-unittest-method-name

**Logged**: 2026-08-02T11:20:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A targeted final-review command referenced a guessed integration-contract test method that did not exist.

### Error

```text
AttributeError: IntegrationContractTests has no attribute test_upstream_gateway_contract_is_frozen_and_explicit
```

### Context

- Three preceding adversarial regression tests passed.
- The failure occurred during unittest test selection, before any contract assertion ran.

### Suggested Fix

List exact test method names with `Select-String` before composing targeted unittest commands.

### Metadata

- Reproducible: yes
- Related Files: tests/test_integration_contract_v022.py

### Resolution

- **Resolved**: 2026-08-02T11:20:00+08:00
- **Notes**: Re-ran the actual `test_phase07_gateway_snapshot_contract_is_machine_readable` method successfully.

---

## [ERR-20260802-008] unbounded-recursive-select-string-timeout

**Logged**: 2026-08-02T11:30:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A recursive PowerShell text search traversed environment/cache directories and timed out before reporting whether a machine-contract key existed.

### Error

```text
command timed out after 11216 milliseconds
```

### Context

- The search started at the external feature-worktree root.
- The worktree contains `.venv` and cache directories that were irrelevant to the contract-key lookup.

### Suggested Fix

Scope `Get-ChildItem` to `docs`, `tests`, and source directories and exclude `__pycache__`; use `rg` first when available.

### Metadata

- Reproducible: yes
- Related Files: docs/integration-contract-v0.2.2.md

### Resolution

- **Resolved**: 2026-08-02T11:30:00+08:00
- **Notes**: The scoped search completed and confirmed that `upstream_stdio_output_unicode` is absent.

---

## [ERR-20260802-006] unittest-class-name-assumed

**Logged**: 2026-08-02T08:30:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A focused unittest command assumed the test class name instead of reading it from the test file.

### Error

```text
AttributeError: module 'tests.compliance.test_mcp_admin' has no attribute 'McpAdminContractTests'.
```

### Context

- The actual class is `McpAdminConfigTests`.
- The corrected focused test and the complete module both passed.

### Suggested Fix

Read declared unittest class names before constructing a focused module path.

### Metadata

- Reproducible: yes
- Related Files: tests/compliance/test_mcp_admin.py

### Resolution

- **Resolved**: 2026-08-02T08:31:00+08:00
- **Notes**: Used the declared class name and then ran all 24 module tests successfully.

---

## [ERR-20260802-007] webui-npm-command-ran-from-repository-root

**Logged**: 2026-08-02T08:36:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The WebUI npm test was invoked from the repository root, which has no package.json.

### Error

```text
npm error enoent Could not read package.json: ENOENT, open 'G:\LLM\coding-tools-mcp\package.json'
```

### Context

- The frontend package root is `webui/`.
- The JavaScript syntax check that preceded npm completed successfully.

### Suggested Fix

Run npm scripts with `webui/` as the working directory or use an explicit prefix.

### Metadata

- Reproducible: yes
- Related Files: webui/package.json

### Resolution

- **Resolved**: 2026-08-02T08:37:00+08:00
- **Notes**: Re-ran `npm.cmd run test` from `webui/`; both tests passed.

---

## [ERR-20260802-008] ripgrep-windows-glob-path

**Logged**: 2026-08-02T09:12:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

An `rg` command passed a Windows wildcard as a literal path, which ripgrep rejected.

### Error

```text
rg: webui\src\*.js: IO error ... 文件名、目录名或卷标语法不正确。
```

### Context

- Native PowerShell did not expand the wildcard for ripgrep.
- The search was intended to locate JavaScript `confirm(...)` calls.

### Suggested Fix

Pass the directory as the path and use ripgrep's `-g '*.js'` file filter.

### Metadata

- Reproducible: yes
- Related Files: webui/src/admin.js

### Resolution

- **Resolved**: 2026-08-02T09:13:00+08:00
- **Notes**: Re-ran the search with `rg -g '*.js'` and completed the localization update.

---

## [ERR-20260803-010] full-discovery-sandbox-temp-unusable

**Logged**: 2026-08-03T00:00:00+08:00
**Priority**: low
**Status**: pending
**Area**: tests

### Summary

Windows full discovery could not use the repository-controlled temporary directory under the current read-only cross-worktree sandbox.

### Error

```text
FileNotFoundError: No usable temporary directory found
Ran 438 tests in 52.168s
FAILED (errors=263, skipped=12)
```

### Context

- All reported errors originated from `tempfile` setup before the affected test logic ran.
- `TEMP` and `TMP` pointed to `G:\LLM\coding-tools-mcp\.tmp\broker-v6-final-review\temp`.
- The feature worktree is intentionally read-only in this session, and project rules prohibit moving test work to a system or repository-external temp directory.

### Suggested Fix

Run authoritative full discovery in a writable repository-controlled worktree with its isolated HOME/APPDATA/TEMP, as recorded by T12. Keep focused read-only regressions in this review session.

### Metadata

- Reproducible: yes
- Related Files: tests

---

## [ERR-20260803-011] external-review-script-missing-pythonpath

**Logged**: 2026-08-03T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

A temporary review script outside the feature worktree could not import the feature worktree's `tests` package.

### Error

```text
ModuleNotFoundError: No module named 'tests'
```

### Context

- The script was stored in the writable main repository `.tmp` directory while reviewing a read-only sibling worktree.
- Python placed the script directory, rather than the feature worktree, first on `sys.path`.

### Suggested Fix

Set `PYTHONPATH` explicitly to the reviewed worktree when running external temporary review scripts.

### Metadata

- Reproducible: yes
- Related Files: tests/compliance/test_upstream_gateway.py

### Resolution

- **Resolved**: 2026-08-03T00:00:00+08:00
- **Notes**: Set `PYTHONPATH` to `G:\LLM\coding-tools-mcp-broker-v6`; the reproduction script then ran successfully.

---
