# Competitive Analysis

This summary condenses the subagent research in [../reports/subagents/competitor-research.md](../reports/subagents/competitor-research.md).

| Tool | Useful Pattern | Not Borrowed |
| --- | --- | --- |
| Claude Code | Subagents with scoped tools, MCP integration, permission modes, hooks, deny-first policy | Product-layer account/session features and bypass modes |
| Aider | Repo map, strict edit formats, git diff/test workflow | Auto-commit/undo behavior in a shared runtime |
| OpenCode | Low-level read/list/grep/edit/bash/apply_patch style tool split | Default broad tool enablement |
| Gemini CLI | Root-directory model, filesystem tools, shell confirmation, sandbox options, MCP servers | Model-assisted edits inside deterministic patch application |
| OpenHands | Agent-computer interface, sandboxed dev environments, issue-fixing loops | Browser/product orchestration as P0 runtime tools |
| Cline | MCP client usage, explicit tool approval, file editing loops | UI-centric approval as the server security boundary |
| SWE-agent and mini-SWE-agent | Benchmark discipline, Docker/Singularity environments, patch submission through diffs | A single unrestricted bash interface as the public P0 surface |

Project decisions:

- Keep P0 small: read/list/search/patch/exec/stdin/kill/git/permissions.
- Keep subagent orchestration internal to validation, not exposed as MCP tools.
- Treat MCP annotations as hints, not enforcement.
- Use server-side workspace, environment, permission, timeout, output, and Landlock controls.
- Do not claim SWE-bench pass without official harness evidence.
