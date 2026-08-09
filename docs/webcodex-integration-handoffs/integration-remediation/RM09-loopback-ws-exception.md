# RM09 Handoff — Formal loopback `ws://` exception

## Status

- Result: complete
- Branch: `integration/webcodex-runtime-platform`
- Worktree: `G:\LLM\coding-tools-mcp\.worktrees\webcodex-runtime-platform`
- Started from: `63f22732`
- Implementation commit: `d478a21`

## Defect Closed

- ADR 0004 Decision 9 stated authenticated WebSocket over HTTPS but did not
  record the loopback `ws://` development exception, leaving the ADR literally
  inconsistent with the Runner CLI behavior.
- The exception is now written directly into ADR Decision 9 and made explicit
  in `docs/webcodex-runner-troubleshooting.md`: production Runner connections
  must use authenticated `wss://`; `ws://` is allowed only for local
  development, tests, and the loopback hop in front of a tunnel; it requires
  the explicit `--allow-insecure-ws` flag; hosts are limited to `127.0.0.1`,
  `::1`, and `localhost`; non-loopback `ws://` fails closed; the exception is
  not a public deployment recommendation and does not weaken Runner credential
  requirements.
- Product behavior was not changed. `validate_runner_url` already enforces the
  documented contract.

## Files Changed

- `docs/adr/0004-webcodex-runtime-platform-boundaries.md`: Decision 9 now
  records the loopback `ws://` exception directly.
- `docs/webcodex-runner-troubleshooting.md`: replaced the implicit sentence
  with explicit `wss://` / `ws://` rules.
- `tests/test_runner_remote.py`: added five `RunnerUrlValidationTests`
  characterization regressions for the URL validation contract (missing
  coverage was present).

## Invariants Preserved

- Current ambiguous `tools/call` replayed: no
- Cross-principal live Session shared: no
- Runtime catalog changed dynamically: no
- Runtime/network close under global lock: no
- Public schema, Core 25 tools, DB schema, or replay semantics expanded: no

## Validation

| Command | Exit code | Passed / skipped | Notes |
| --- | ---: | --- | --- |
| `python -m unittest tests.test_runner_remote.RunnerUrlValidationTests` | 0 | 5 passed | New URL validation regressions; pass on unchanged production code because the defect was documentation inconsistency, not behavior. |
| `python -m unittest tests.test_runner_remote tests.test_runner_websocket` | 0 | 33 passed | RM09 targeted suites. |
| `git diff --check` | 0 | Passed | LF will be replaced by CRLF warnings only. |
| `git commit -m "docs(webcodex): formalize loopback ws:// exception"` | 0 | Commit `d478a21` | Implementation commit. |

No red-green test was possible for this card without changing production
behavior, which RM09 explicitly forbids ("保留当前产品行为"). The added tests
are stable characterization regressions: they fail if the fail-closed
loopback/flag contract regresses.

## Security Review

- Full Session ID/token/argv secret exposed: no
- Real credential/data used: no
- Only synthetic loopback hosts and `.test` hostnames were used in tests.

## Remaining Risks

- `wss://` remains accepted for any host by design; production authentication
  still comes from the required Runner credential handshake, not URL validation.
- The ADR/troubleshooting wording is documentation only; reviewers should
  re-read the changed sections if they expect code-level enforcement changes.

## Next Card Preconditions

- RM10 must produce real 500 full-Runtime capacity evidence, not Session-manager
  numbers; a lightweight Session-manager 500-point measurement does not
  substitute for full Runtime results.
