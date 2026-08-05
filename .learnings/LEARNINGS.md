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
