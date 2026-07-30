# Phase 11 Handoff

## Status

- Result: complete
- Integration branch: `integration/upstream-v0.2.2`
- Started from: `cc5b9cb0a6cd33f88a64ca1a3f7c6c061a24857e`
- Packaging commit: `7dd8bb1a184e6657b9fb9e4d18d12bf67f21d28d`
- Admin telemetry commit: `f74e890a515748ce8dacdb18b320be4f4e003679`
- npm test portability commit: `610b02ff5c88a787125bbf526c4534762326c47b`
- Handoff commit: filled by the next agent from `git log`
- Phase 12 remains pending and was not started.

## Scope Completed

- Added the generated Admin WebUI to Python package data without removing upstream Desktop packaging.
- Preserved the upstream Desktop optional dependencies, entry point, package discovery roots, locale package data, and dev/image extras.
- Added packaging and current-release metadata regression tests.
- Exposed the effective upstream telemetry mode through the authenticated Admin status response.
- Kept the Admin telemetry response to the effective mode and documentation entry only.
- Updated Admin API/WebUI documentation for the privacy-safe telemetry status.
- Made the upstream npm launcher contract tests portable across Windows and POSIX while keeping test fixtures inside the repository worktree.
- Verified Desktop, npm launcher, Cloudflare dispatch, release metadata, Python wheel, and WebUI compatibility without deploying or changing secrets.

## Packaging Boundary

`pyproject.toml` now preserves both package-data sets:

```toml
[tool.setuptools.package-data]
coding_tools_mcp = ["webui_dist/*"]
mcp_desktop_client = ["locales/*.qm", "locales/*.ts"]
```

The following upstream packaging contract remains unchanged:

- `desktop` optional dependencies include PySide6 and psutil.
- `coding-tools-mcp-desktop = "mcp_desktop_client.app:main"` remains registered.
- package discovery still uses `where = [".", "apps/desktop-client"]`.
- both `coding_tools_mcp*` and `mcp_desktop_client*` remain included.
- upstream `dev` and `image` extras remain present.
- package and module versions remain `0.2.2`.
- npm launcher version remains `0.1.0`.
- `uv.lock` is unchanged.

An actual wheel build confirmed that the wheel contains:

- `coding_tools_mcp/webui_dist/admin.html`
- `mcp_desktop_client/locales/app_zh_CN.qm`
- `mcp_desktop_client/locales/app_zh_CN.ts`
- both Python console entry points in package metadata

## Telemetry Boundary

`GET /admin/api/status` now includes:

```json
{
  "telemetry": {
    "mode": "on",
    "docs": "docs/telemetry.md"
  }
}
```

- `mode` is calculated by the existing upstream `telemetry_mode()` function and is `on`, `off`, or `debug`.
- The upstream v0.2.2 default remains enabled; Phase 11 does not change default policy.
- Existing `CODING_TOOLS_MCP_TELEMETRY=off`, `DO_NOT_TRACK=1`, `CI`, and debug behavior remain unchanged.
- Admin status adds no path, Workspace ID, Agent ID, Client ID, command, arguments, file content, or event payload.
- The WebUI already accepts the structured telemetry object and required no source or dist rebuild.
- The telemetry install-id test now patches `Path.home()` directly and checks POSIX permission bits only on POSIX, preserving the real security assertion without Windows-only false failures.

## Desktop Boundary

- `apps/desktop-client/**` production source is unchanged.
- Desktop profile and secret storage remain owned by the Desktop client and are not connected to Admin Settings or the server Secret Vault.
- The Desktop runtime still resolves the local Python module fallback and passes bearer credentials through the environment only.
- With the `desktop` extra installed, all 15 Desktop tests passed in the required focused run.

## npm Launcher Boundary

- `npm/coding-tools-mcp/bin/coding-tools-mcp.js` is unchanged.
- Pinned Python package selection, argument/stdio forwarding, uvx-to-pipx fallback, missing-runner diagnostics, and child exit-code propagation remain unchanged.
- Test fixtures now live under `npm/coding-tools-mcp/.tmp` and clean themselves after each test.
- POSIX continues to use executable shell fixtures.
- Windows uses temporary native Node executable stubs with a preload that records arguments or exits with the requested code; no POSIX shell is required.
- All four launcher tests execute on Windows with no skips.

## Cloudflare and Release Boundary

- `cloudflare/sandbox-control/**` is unchanged.
- `.github/workflows/**` is unchanged.
- No deployment was performed and no secret was read or modified.
- Dispatch verification confirmed that the Worker sends 9 inputs and both `workflow_dispatch` and `workflow_call` declare the same 9 inputs.
- Release metadata verification confirmed Python `0.2.2`, tag `v0.2.2`, and stable npm launcher `0.1.0`.

## Files Changed

- `pyproject.toml`
- `coding_tools_mcp/admin.py`
- `docs/admin-api.md`
- `docs/admin-webui.md`
- `npm/coding-tools-mcp/test/launcher.test.js`
- `tests/compliance/test_mcp_admin.py`
- `tests/test_integration_contract_v022.py`
- `tests/test_phase11_packaging.py`
- `tests/test_release_checks.py`
- `tests/test_telemetry.py`

No Desktop production source, npm launcher production source, WebUI source/dist, OAuth Store/schema, OAuth/Refresh implementation, Gateway lifecycle, Server routing, Settings Store, Secret Vault format, tool registry, Cloudflare code, workflow, or lockfile was changed.

## Validation Performed

| Command | Exit code | Result |
| --- | ---: | --- |
| `uv sync --extra dev --extra desktop` | 0 | Resolved 16 packages and installed the Desktop stack. The stale generated `uv.lock` rewrite was deliberately restored; lockfile is unchanged. |
| `uv run python -m unittest tests.test_desktop_client` | 0 | Ran 15 tests; all passed. |
| `uv run python -m unittest tests.test_telemetry` | 0 | Ran 16 privacy/default/off/debug/install-ID tests; all passed. |
| `uv run python scripts/check_dispatch_inputs.py` | 0 | Worker, workflow dispatch, and workflow call each agree on 9 inputs. |
| `uv run python scripts/check_release_versions.py --tag v0.2.2` | 0 | Python/module/tag/npm release metadata is consistent. |
| `npm --prefix npm/coding-tools-mcp test` | 0 | Ran 4 launcher tests on Windows; all passed with no skips. |
| `npm pack --dry-run --json` from `npm/coding-tools-mcp` | 0 | Produced the four-file `coding-tools-mcp@0.1.0` package manifest. |
| `uv build --wheel --out-dir .tmp/phase11-wheel` | 0 | Built `coding_tools_mcp-0.2.2-py3-none-any.whl`; WebUI and Desktop locales are present. Temporary build artifacts were removed. |
| Phase 11 Python focused suite | 0 | Ran 65 packaging/release/Desktop/telemetry/Admin/contract tests; all passed. |
| `npm --prefix webui test` | 0 | Ran 15 WebUI model/security/DOM tests; all passed. |
| Ruff over changed Phase 11 Python source/tests | 0 | All checks passed. |
| `py_compile` over changed Phase 11 Python source/tests | 0 | All modules compiled. |
| Mypy over `coding_tools_mcp/admin.py` | 0 | No issues found. |
| `node --check npm/coding-tools-mcp/test/launcher.test.js` | 0 | JavaScript syntax is valid. |
| security compliance runner | 0 | Native Windows runner exited 0; 15 bodies skipped by the known `/dev/null` preflight. |
| MCP-contract compliance runner | 0 | Native Windows runner exited 0; 37 bodies skipped by the same preflight. |
| `git diff --check` and staged whitespace checks | 0 | No whitespace errors. |
| scope, lockfile, and high-risk credential scans | 0 | No forbidden production change, lockfile change, PAT, private key, or long bearer credential found. |

The documented repository-root command `npm --prefix npm/coding-tools-mcp pack --dry-run --json` does not apply `--prefix` as the default package spec with the installed npm version and looked for a repository-root `package.json`. Running the same dry-run from the package directory is the semantically equivalent successful check and produced the expected manifest.

## Additional Windows Regression Observation

An extra one-process Phase 03–11 cumulative run executed 138 tests. Product assertions reached 137 passes, but cleanup ended with transient `WinError 145` on an already-empty `TemporaryDirectory`. An isolated retry of the first affected Settings test passed; a second cumulative run moved the same cleanup-only error to a Desktop temporary directory. This matches the Phase 01/03 Windows temporary-directory baseline and does not reference Phase 11 production code. The required focused Desktop, telemetry, packaging, Admin, release, npm, and WebUI suites all executed and passed. Linux CI remains required for the compliance bodies skipped by `/dev/null` preflight.

## Remaining Risks and Deferred Work

- Phase 05's Refresh rotation/access-token insertion cross-transaction availability risk remains unchanged.
- Native Windows may intermittently report `WinError 145` while deleting freshly emptied temporary directories in very large one-process suites.
- The installed npm version requires package-directory execution (or an explicit package spec) for the dry-run pack check; the package content itself is valid.
- Final integrated Linux CI must execute the security and MCP-contract bodies skipped by the native-Windows `/dev/null` fixture.

## Phase 12 Preconditions

- Start from a clean worktree with Phase 11 marked complete.
- Preserve the Python `0.2.2` and npm `0.1.0` release metadata unless Phase 12 explicitly requires a version action.
- Keep both WebUI and Desktop package data in the wheel.
- Do not connect Desktop secret/profile storage to Admin Settings or the server Secret Vault.
- Do not expand Admin telemetry status beyond mode and documentation without a new privacy review.
- Preserve the immutable Gateway/tool-catalog behavior and all earlier OAuth/Workspace boundaries.

## Secret Check

- No real Admin token, OAuth credential, Desktop secret, npm token, Cloudflare secret, Gateway credential, Vault value, private key, or release credential was committed.
- Test credentials and bearer strings remain synthetic canaries only.
