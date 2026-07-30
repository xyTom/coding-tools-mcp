# Phase 03 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Implementation commit: `aa3a0f481be7ef13809b318e3c30ac102ab810ea`
- Handoff commit: filled by next agent from git log
- Started from: `a522cc096d7b2361207a63ada7d63872b556abb3`

## Scope Completed

- Ported the standalone versioned settings store.
- Ported canonical settings validation and legacy settings migration rules.
- Ported the validated multi-workspace catalog without connecting it to Runtime.
- Ported the encrypted server Secret Vault without connecting it to OAuth or Admin.
- Added focused tests for settings round-trip, migrations, atomic replacement,
  workspace validation, secret redaction, vault encryption, wrong-key handling,
  and atomic vault replacement.
- Replaced the Phase 03 contract-test skip with an actual passing migration test.

## Files Changed

- `coding_tools_mcp/settings_store.py`: adds schema-versioned atomic JSON storage,
  stable user config directory resolution, migration metadata, secret redaction,
  and rejection of new plaintext secret settings.
- `coding_tools_mcp/settings_definition.py`: adds shared normalization,
  restart-field comparison, workspace catalog canonicalization, and legacy
  `tool_profile` removal with `legacy_tool_profile_ignored`.
- `coding_tools_mcp/workspace_catalog.py`: adds canonical workspace entries,
  stable IDs, one enabled default, duplicate/nested-root rejection, and disabled
  workspace filtering.
- `coding_tools_mcp/secret_vault.py`: adds versioned authenticated encrypted
  records and atomic vault persistence.
- `tests/test_settings_foundation.py`: adds focused Phase 03 module coverage.
- `tests/test_integration_contract_v022.py`: removes the Phase 03 skip and binds
  the contract migration cases to the implementation.
- `docs/integration-handoffs/STATUS.md`: marks Phase 03 complete.
- `docs/integration-handoffs/phase-03.md`: records this handoff.

## Decisions Applied

- `tool_profile` is migration input only. Known and unknown legacy values are
  removed, emit the same stable warning code, and never control `tools/list`.
- New settings writes may contain secret references but reject plaintext secret
  material; legacy plaintext can still be read for a later explicit migration.
- Settings writes and vault writes use temporary files, fsync, and atomic
  replacement so a failed replacement preserves the prior file.
- Workspace IDs and roots must be unique, nested roots are rejected, only one
  enabled default is allowed, and disabled entries are unavailable to callers.
- Phase 03 does not inject Workspace Catalog into Runtime and does not connect
  Secret Vault to OAuth, Admin, WebUI, desktop storage, or the tool registry.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `.\.venv\Scripts\python.exe -m unittest tests.test_settings_foundation tests.test_integration_contract_v022` | 0 | Ran 22 tests: 20 passed; only the Phase 04 and Phase 06 implementation tests remain explicitly skipped. |
| `.\.venv\Scripts\python.exe -m ruff check --ignore=E501 coding_tools_mcp/settings_store.py coding_tools_mcp/settings_definition.py coding_tools_mcp/workspace_catalog.py coding_tools_mcp/secret_vault.py tests/test_settings_foundation.py tests/test_integration_contract_v022.py` | 0 | All checks passed. |
| `.\.venv\Scripts\python.exe -m mypy --python-version 3.11 --disable-error-code union-attr --disable-error-code assignment --disable-error-code arg-type --disable-error-code no-untyped-def coding_tools_mcp/settings_store.py coding_tools_mcp/settings_definition.py coding_tools_mcp/workspace_catalog.py coding_tools_mcp/secret_vault.py` | 0 | No issues found in the four source modules. |
| `.\.venv\Scripts\python.exe -m unittest tests.compliance.test_runtime_helpers` | 1 | Reproduced the documented native-Windows upstream baseline: 77 tests, 7 failures, 3 errors, 8 skips. Failures are the same POSIX command/path, CRLF, environment-key casing, and temporary-handle diagnostics recorded in Phase 01; none imports or references the new Phase 03 modules. |
| `.\.venv\Scripts\python.exe -m tests.compliance.runner --suite security` | 0 | Runner exited 0; 15 tests skipped because the native-Windows fixture preflight requires `/dev/null`, matching the Phase 01 platform diagnosis. |
| `git diff --cached --check` | 0 | No whitespace errors after the final staged review. |
| high-risk credential pattern scan over staged files | 0 | No GitHub PAT, private-key marker, or long Bearer-token pattern found. |

## Known Baseline Failures

- Native Windows full `tests.compliance.test_runtime_helpers` is not the upstream
  Windows gate and retains the POSIX-only failures documented in
  `phase-01-unblock.md`. The Phase 03 focused tests and static checks pass.
- Native Windows security compliance bodies remain skipped by the upstream
  `/dev/null` preflight; final integrated validation still requires the Linux
  compliance gate.

## Remaining Risks

- The four modules are deliberately not connected to server startup, Runtime,
  OAuth HTTP, Admin, WebUI, desktop profiles, or the tool registry yet.
- The vault file format is versioned and integrity-tested, but its cryptographic
  construction should receive a dedicated security review before a fork release.

## Next Phase Preconditions

- Phase 04 must remain store-only: port `oauth_store.py` and focused persistence
  tests without changing `server.py`, OAuth endpoints, Admin, or WebUI.
- Reuse the Phase 03 Secret Vault reference boundary; do not store signing-key or
  refresh-token plaintext in SQLite or ordinary settings.
- Keep the Phase 04 contract test skipped until persistent OAuth Store reopen and
  idempotent transactional migration are implemented and actually pass.

## Secret Check

- No credentials, OAuth databases, bearer tokens, signing secrets, or real vault
  files were added.
- Test values are synthetic canaries only and are never committed as plaintext
  persisted artifacts.
