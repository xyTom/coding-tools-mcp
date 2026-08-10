# Learning Log

## [LRN-20260802-001] correction

**Logged**: 2026-08-02T22:40:14+08:00
**Priority**: high
**Status**: pending
**Area**: tests

### Summary

Reviews of a dirty shared worktree must pin content hashes, not only branch and HEAD.

### Details

The feature branch HEAD remained `60ef13d` while its uncommitted files changed during several review rounds. A conclusion based on an earlier read was later reported as current, producing stale findings after the user had already updated the working tree.

### Suggested Action

At the start of a dirty-worktree review, record SHA-256 hashes for every relevant modified/untracked file. Recompute them before reporting; if any hash changed, discard affected findings and reread those files before concluding.

### Metadata

- Source: user_feedback
- Related Files: coding_tools_mcp/upstream.py, docs/integration-contract-v0.2.2.md, docs/upstream-broker-handoffs/T12-post-review-remediation.md
- Tags: review, dirty-worktree, stale-snapshot, verification
- Pattern-Key: review.pin_dirty_worktree_content
- Recurrence-Count: 1
- First-Seen: 2026-08-02
- Last-Seen: 2026-08-02

---

## [LRN-20260810-002] correction

**Logged**: 2026-08-10T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: docs

### Summary

User onboarding must be reachable inside the running product and must name the exact startup credential expected by `/app`.

### Details

Repository Markdown links were not a usable navigation surface for the user, and the Operator dialog called its input only a bearer token without explaining where to configure it. `/app` has no separate password: the normal static path uses `CODING_TOOLS_MCP_AUTH_TOKEN`, which is read at server startup and is intentionally different from the Admin token.

### Suggested Action

For user-facing runtime features, provide an in-product help route with copyable platform-specific commands. Authentication prompts must name the source environment variable, role boundary, restart requirement, and whether the WebUI can bootstrap the credential.

### Metadata

- Source: user_feedback
- Related Files: coding_tools_mcp/webui.py, webui/src/wiki.html, webui/src/app.html
- Tags: onboarding, authentication, wiki, documentation

### Resolution

- **Resolved**: 2026-08-10T00:00:00+08:00
- **Notes**: Added the bilingual `/wiki` route and linked the Operator authentication dialog directly to its token setup section.

---

## [LRN-20260810-001] correction

**Logged**: 2026-08-10T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: docs

### Summary

The public project name must remain Coding Tools MCP; WebCodex is an internal integration codename only.

### Details

New user-facing guide titles and README navigation briefly presented WebCodex as a product name. The package, command, repository, and public documentation must continue to use Coding Tools MCP / `coding-tools-mcp`. Internal historical plans and handoffs may retain their codename filenames.

### Suggested Action

Before committing user-facing documentation, verify both English and Chinese README navigation and keep internal phase codenames out of public product titles.

### Metadata

- Source: user_feedback
- Related Files: README.md, README.zh-CN.md, docs/coding-tools-mcp-agent-runner-guide.md
- Tags: naming, documentation, localization

### Resolution

- **Resolved**: 2026-08-10T00:00:00+08:00
- **Notes**: Added English and Chinese guides under Coding Tools MCP naming and corrected all user-facing navigation.

---

## [LRN-20260806-001] best_practice

**Logged**: 2026-08-06T12:00:00+08:00
**Priority**: high
**Status**: pending
**Area**: backend

### Summary

Merging divergent persistence implementations requires startup tests against real legacy schema and encrypted-record fixtures.

### Details

The merged branch passed fresh-database tests but rejected valid state written by the pre-merge branch. Legacy OAuth records omitted the newer `cipher` metadata, used truncated signing-key fingerprints, and referenced `oauth-signing/...`; the transcript database used schema version 4 with table names that collided with the replacement workspace-partitioned schema.

### Suggested Action

Before merging persistence rewrites, capture sanitized legacy fixtures and require restart/migration tests. Keep schema versions monotonic across both histories, preserve conflicting legacy tables before creating replacements, and migrate cryptographic metadata only after verifying the legacy record with its original integrity checks.

### Metadata

- Source: error
- Related Files: coding_tools_mcp/oauth.py, coding_tools_mcp/secret_vault.py, coding_tools_mcp/transcript.py
- Tags: migration, sqlite, oauth, secret-vault, merge
- Pattern-Key: persistence.test_legacy_state_before_merge
- Recurrence-Count: 1
- First-Seen: 2026-08-06
- Last-Seen: 2026-08-06

---

## [LRN-20260806-002] best_practice

**Logged**: 2026-08-06T12:30:00+08:00
**Priority**: high
**Status**: pending
**Area**: frontend

### Summary

MutationObserver callbacks must make idempotent DOM writes to avoid browser main-thread starvation.

### Details

The Admin WebUI observer watched `title` and `aria-label`, then unconditionally rewrote those attributes and a language-toggle text node at the end of every callback. Each callback queued another callback indefinitely, so the HTTP response completed while navigation and browser control timed out.

### Suggested Action

Before writing observed text or attributes, compare the current and desired values. Add a bounded fake-observer regression that proves the mutation queue settles after initialization.

### Metadata

- Source: error
- Related Files: webui/src/i18n.js, webui/tests/i18n.test.mjs
- Tags: mutation-observer, browser-freeze, idempotence, webui
- Pattern-Key: frontend.mutation_observer_idempotent_writes
- Recurrence-Count: 1
- First-Seen: 2026-08-06
- Last-Seen: 2026-08-06

---
