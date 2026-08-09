# Phase 18 Session Resilience Retrospective

## Scope and historical qualification

This document was created by RM11. It is a retrospective, not a handoff that existed during the RS implementation work. The canonical integration branch did not preserve a continuous per-card handoff chain for RS00-RS07. RS00 carried its source-era card in `c7b1563`; RS01-RS07 cards were written together by the later documentation commit `70cfa42`. Those facts must not be presented as contemporaneous release evidence.

All source commits below are from `integration/webcodex-runner`. For every row, `git merge-base --is-ancestor <source> d8c1cc8` was run and returned exit 1. The implementation content was later incorporated, rewritten, or represented by the canonical squash commit `d8c1cc855f71303a525da288a6c8e895a76a10b8` (whose parent was `252c2f0b5508e6b6c23a330e3417943960249eca`); the source commit itself was not an ancestor of that squash.

## RS provenance matrix

| RS | Actual source implementation commit | Source-era implementation area | What later entered the `d8c1cc8` squash | Handoff state at the time | Current RM/test supplementation |
| --- | --- | --- | --- | --- | --- |
| RS00 | `c7b1563` | ADR 0002, status/handoff material, RS00 card, `tests/test_http_session_resilience.py` | Session-resilience baseline, catalog-template ADR content, and HTTP resilience tests were integrated/reworked into the canonical baseline. | RS00 produced its own source-era card, but the canonical branch did not retain a verified per-RS chain. | RM00 checkpointing; RM05/RM07 close-cleanup and HTTP matrix evidence; `tests/test_http_session_resilience.py`. |
| RS01 | `0160376` | `server.py`, `transport_http.py`, session-resilience tests | HTTP admission, lease, close, and session transport behavior was incorporated into the squash. | No contemporaneous canonical handoff was preserved; the RS01 card was later batch-written by `70cfa42`. | RM01/RM05/RM07 recovery, cleanup, and matrix checks; `tests/test_http_session_resilience.py`, `tests/test_session_resilience_http_integration.py`. |
| RS02 | `0160376` | Same underlying commit as RS01: lease/close race and admission behavior | The shared RS01/RS02 implementation was incorporated into the squash; there was no separate source commit to preserve. | RS02 was documented as sharing the RS01 implementation, but its card was part of the later batch documentation. | RM04 create-call-close race checks and RM05 cleanup checks; HTTP resilience and integration tests. |
| RS03 | `86aaf12` | `upstream.py`, `tests/compliance/test_upstream_lifecycle.py` | Upstream lifecycle and shutdown behavior was incorporated into the squash. | No contemporaneous canonical handoff was preserved; the RS03 card was later batch-written by `70cfa42`. | RM01 recovery and RM06 aggregate observability; `tests/compliance/test_upstream_lifecycle.py`. |
| RS04 | `8f1b59d` | `upstream.py`, `upstream_resilience.py`, `test_upstream_resilience.py` | Upstream backoff/resilience behavior was incorporated into the squash. | No contemporaneous canonical handoff was preserved; the RS04 card was later batch-written by `70cfa42`. | RM01 recovery and RM06 observability; `tests/test_upstream_resilience.py`. |
| RS05 | `32edfa8` | `server.py`, `upstream.py`, `test_upstream_lazy_catalog.py` | Lazy catalog and upstream/server integration behavior was incorporated into the squash. | No contemporaneous canonical handoff was preserved; the RS05 card was later batch-written by `70cfa42`. | RM02 frozen-catalog checks and RM06 aggregate observability; `tests/test_upstream_lazy_catalog.py`, schema/catalog compliance tests. |
| RS06 | `85375a1` | `runner/__init__.py`, `runner/routing.py`, `runner/transport.py`, `test_runner_mcp_routing.py` | Runner routing and transport behavior was incorporated into the squash. | No contemporaneous canonical handoff was preserved; the RS06 card was later batch-written by `70cfa42`. | RM09 loopback WebSocket exception path and runner routing tests; `tests/test_runner_mcp_routing.py`, Runner WebSocket/remote tests. |
| RS07 | `0dcbde9` | `admin.py`, `runner/routing.py`, `scripts/benchmark_session_resilience.py`, resilience tests | Admin/Runner observability and resilience benchmark material was incorporated into the squash. | No contemporaneous canonical handoff was preserved; the RS07 card was later batch-written by `70cfa42`. | RM03 role isolation, RM06 observability, RM09 loopback validation, and RM10 full-Runtime capacity evidence; related admin/Runner/resilience tests and `scripts/benchmark_webcodex_release.py`. |

## Evidence added after the source work

RM01-RM12 provide current remediation evidence rather than retroactively changing the RS history. The relevant additions include HTTP/upstream recovery, frozen-catalog and role-isolation checks, create/call/close race coverage, close cleanup, aggregate observability, the HTTP matrix, transcript/stdio controls, loopback WebSocket exception handling, two completed RM10 measurements of 0/100/128/500 full Runtime working-set points, the RM12 Core 25 and aggregate gates, and independent Ubuntu WSL POSIX sidecar evidence. The RM10 measurements are capacity evidence only; the production default remains 128.

The historical source work and the later remediation are therefore separate evidence layers. RM11 recorded the provenance gap; it did not claim that the missing handoffs existed earlier or close the release gate.

## Current RM Handoffs

- `integration-remediation/RM00-canonical-checkpoint.md`
- `integration-remediation/RM01-http-upstream-recovery.md`
- `integration-remediation/RM02-deep-freeze-catalog-template.md`
- `integration-remediation/RM03-operator-admin-role-isolation.md`
- `integration-remediation/RM04-runner-mcp-create-call-close-race.md`
- `integration-remediation/RM05-http-session-close-cleanup.md`
- `integration-remediation/RM06-upstream-aggregate-observability.md`
- `integration-remediation/RM07-session-resilience-http-matrix.md`
- `integration-remediation/RM08-transcript-permissions-stdio-redaction.md`
- `integration-remediation/RM09-loopback-ws-exception.md`
- `integration-remediation/RM10-full-runtime-capacity.md`
- `integration-remediation/RM11-retrospective-status-honesty.md`
- `integration-remediation/RM12-release-aggregate-blocked.md` (historical blocked result)
- `integration-remediation/RM12A-local-gate-remediation.md`
- `integration-remediation/RM12B-linux-posix-evidence.md`
- `integration-remediation/RM13-final-release-handoff.md`

RM12 passed its Core 25, resilience, security/persistence, Linux POSIX, and
`main...HEAD` range-diff gates for candidate commit `81072da`. RM13 records the
final Phase 18/22 status; this list does not rewrite the historical blocked RM12
handoff.
