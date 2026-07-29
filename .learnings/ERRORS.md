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
