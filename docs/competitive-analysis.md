# Competitive Analysis

`coding-tools-mcp` is a coding-tool runtime, not a complete agent product. Codex,
OpenCode, and similar applications also own the model loop, prompt/context
selection, approvals, compaction, planning, and UI. Therefore tool-contract
quality can be compared directly; end-to-end agent parity cannot be claimed
from MCP unit tests alone.

| Concern | This runtime in 0.2 | Practical comparison |
| --- | --- | --- |
| Tool choice | One stable catalog of 20 low-level coding tools; no profiles or dynamic process tools | A fixed catalog reduces discovery and routing variance, but a host agent can still add its own tools |
| Editing | `apply_patch` is the sole direct mutation primitive; it stages all files, checks baselines, preserves mode/BOM/newlines, and rolls back partial commits | A whole-file `edit_file` can be simpler for a model, while patching sends fewer unchanged bytes and gives stronger conflict/rollback behavior |
| Results | Concise bounded `content`, complete `structuredContent`, image bytes once | Avoids paying model context for duplicated JSON, diffs, and base64 |
| Commands | Ten-second default foreground yield; fixed `write_stdin`, `read_output`, and `kill_session`; bounded sessions and real POSIX PTY | Short tests normally finish in one call; background/interactive work has explicit next actions |
| Project context | Root `AGENTS.md`/`CLAUDE.md` content is injected at initialize; nested instruction paths are indexed | Removes a separate workspace-opening call without injecting every nested rule into every task |
| Isolation | Workspace path checks, permission modes, environment filtering, process groups, output limits, and Linux Landlock where available | MCP annotations remain hints; enforcement is server-side |
| Transport | Independent HTTP runtimes, negotiated protocol versions, bounded sessions, OAuth DCR + PKCE, bearer auth, and stdio | Suitable as a reusable backend; it is not an agent UI or account system |

Other useful reference patterns remain outside this runtime layer: Claude Code
combines permissions, hooks, and scoped agent orchestration; Aider emphasizes
repo maps and disciplined edit/diff/test loops; OpenHands adds a broader
agent-computer sandbox; and Cline couples MCP and approvals to an editor UI.
Those product-level workflows can host or complement this MCP server, but are
not additional tools in its fixed catalog.

## Interpreting the Devspace speed claim

“Devspace changes code faster” can be true for a particular model and task, but
the cause must be measured. Fewer visible tools, a direct whole-file editor, and
automatic repository context can reduce tool-selection and round-trip overhead.
They do not prove that the editor itself is universally faster or more reliable.

Version 0.2 addresses the same overhead without adding `edit_file`: the catalog
is fixed, project instructions arrive during initialization, result duplication
is removed, and commands wait long enough for most tests to finish in one call.
The remaining tradeoff is deliberate: `apply_patch` asks the model for anchored
context, in return for smaller writes, conflict detection, and multi-file
rollback.

The deterministic dogfood report records completion, elapsed time, tool calls,
request/result bytes, first-patch success, polling calls, and tool p50/p95. It is
regression evidence for this runtime. A defensible Devspace/Codex/OpenCode rank
still requires the same model, prompt, repository snapshot, machine, permissions,
and repeated task set. No SWE-bench or competitor-parity claim is made without
that controlled run.

The five-run [0.1.7 versus 0.2.0 deterministic comparison](../reports/benchmark/dogfood-v0.1-v0.2.md)
shows the concrete effect: both versions completed every case with the same 18
calls, two successful first patch attempts, and zero empty polls; median result
bytes dropped 37.279%. Median wall time was effectively unchanged (+0.301%), so
the evidence supports “less model-context traffic,” not “proven faster agent.”
