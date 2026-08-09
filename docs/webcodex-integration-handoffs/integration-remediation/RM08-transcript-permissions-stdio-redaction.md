# RM08 Handoff — Transcript Permissions and Stdio Target Redaction

## Status

- Result: implementation complete; POSIX evidence remains unavailable on this host
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Implementation commit: `fc95365`
- Handoff commit: pending

## Defects Closed by the Implementation

- `ServerSettingsStore.write()` now creates or tightens its settings directory to `0700` on POSIX before writing the atomically replaced settings file.
- `load_workspace_startup()` now creates or tightens the startup config directory to `0700` before reading settings.
- `TranscriptStore` now leaves `:memory:` untouched, tightens its storage directory to `0700` on POSIX, and tightens the SQLite database plus `-journal`, `-wal`, and `-shm` files to `0600` during managed connection lifecycles.
- `safe_target()` now exposes only the stdio executable and, when present, an argument count such as `uvx (3 args)`. It does not expose argv flags or values. HTTP target query/fragment redaction is unchanged.

No public schema, Core 25 tool, database schema, ADR, catalog mutation, or replay semantic was changed. No real credential, transcript, or external upstream was used.

## Tests Added

- `tests/compliance/test_chat_persistence.py`
  - standard umask `022` coverage for existing/new settings and transcript directories;
  - settings and transcript database mode assertions;
  - SQLite WAL/SHM and rollback-journal sidecar mode assertions;
  - startup config-directory coverage;
  - POSIX-only mode assertions are skipped on Windows.
- `tests/compliance/test_mcp_admin.py`
  - Admin gateway status coverage proving `--token SUPER-SECRET --verbose` is absent and the target is `uvx (3 args)`.

## Validation Evidence

| Command | Exit | Result |
| --- | ---: | --- |
| `python -m unittest tests.compliance.test_mcp_admin.AdminServiceTests.test_admin_gateway_status_redacts_stdio_argv_values_and_flags` before production fix | 1 | Stable red test: leaked `uvx --token SUPER-SECRET --verbose`. |
| `python -m unittest tests.compliance.test_chat_persistence.ChatPersistenceTests.test_startup_settings_transcript_and_sqlite_sidecars_are_private` on Windows | 0 | 1 test skipped because POSIX modes are not portable to Windows. |
| `python -m compileall -q coding_tools_mcp/settings_store.py coding_tools_mcp/transcript.py coding_tools_mcp/server.py coding_tools_mcp/upstream.py tests/compliance/test_chat_persistence.py tests/compliance/test_mcp_admin.py` | 0 | Passed. |
| `python -m unittest tests.compliance.test_chat_persistence tests.compliance.test_mcp_admin tests.compliance.test_upstream_admin_observability` | 0 | 40 tests passed, 1 Windows POSIX-permission skip. |
| `python -m unittest tests.test_settings_foundation` | 0 | 18 passed. |
| `python -m unittest tests.test_workspace_session_binding` | 0 | 5 passed. |
| `python -m unittest tests.compliance.test_upstream_gateway` | 0 | 34 passed. |
| `python -m unittest tests.test_upstream_resilience` | 0 | 12 passed. |
| `git diff --check` | 0 | Passed before commit. |
| `wsl.exe --cd /mnt/g/LLM/coding-tools-mcp/.worktrees/webcodex-runtime-platform -- python -m unittest tests.compliance.test_chat_persistence` | 1 | WSL had no usable Linux distribution; unittest did not run. |

The requested Linux permission evidence therefore does not exist. RM08 must not be reported as “POSIX permission verified” until the same permission test runs successfully on Linux CI/host. Phase 18 and Phase 22 remain `in remediation`.

## Scope and Remaining Risk

- Only the RM08 allowlist was modified, plus this handoff.
- The execution book remains untracked and was not staged.
- Windows ACL behavior was intentionally not changed or represented as POSIX mode compliance.
- Linux-side SQLite directory, database, and sidecar modes require external Linux CI/host verification.

## Next Card Preconditions

- Start the next remediation card from handoff commit recorded after this file is committed.
- Preserve the execution book as untracked.
- Do not treat the missing Linux run as a release or POSIX-security pass.
