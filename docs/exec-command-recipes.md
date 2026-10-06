<a id="exec-command-recipes"></a>

# Exec command recipes moved

The maintained recipes are published in the [execution guide](https://coding-tools-mcp.github.io/docs/guides/exec-command-recipes/).
Their source is maintained at:

https://github.com/coding-tools-mcp/docs/blob/main/content/docs/guides/exec-command-recipes.mdx

## Foreground and background results

For the behavior of this checkout, use the source-coupled
[command lifecycle](runtime-contract-v0.3.md#command-lifecycle) and
[command/output reference](tools-and-schemas.md#command-and-output-behavior).
They cover `next_action`, polling, truncated output, process lifetime, and
`keep_stdin_open` for input without a TTY on every platform.
Pass an explicit workspace-relative `workdir` when targeting a subdirectory;
see the [exec_command contract](runtime-contract-v0.3.md#exec_command).

The public recipes remain the home for toolchain commands and cache examples.
This file preserves the existing section link while the contract records
runtime changes independently of the tutorial.
