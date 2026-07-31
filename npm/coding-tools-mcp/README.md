# coding-tools-mcp (npm launcher)

npm launcher for [coding-tools-mcp](https://github.com/xyTom/coding-tools-mcp), the model-neutral coding-agent runtime MCP server. The server itself is a Python package published on [PyPI](https://pypi.org/project/coding-tools-mcp/); this package starts it through `uvx` (preferred) or `pipx run`, forwarding all arguments and stdio.

```bash
npx coding-tools-mcp --stdio --workspace /path/to/repo
```

Requires `uv` or `pipx` on PATH. Stable launchers run the latest PyPI release; development launchers pin the matching Python development build. Override either behavior with:

```bash
CODING_TOOLS_MCP_VERSION=0.3.0.dev0 npx coding-tools-mcp --stdio --workspace /path/to/repo
```

For the current fork development release, launcher SemVer `0.3.0-dev.0` automatically pins Python `0.3.0.dev0`; `CODING_TOOLS_MCP_VERSION` can still override it. Stable launchers remain unpinned unless the environment variable is set, so later server-only releases can be discovered. Documentation, configuration, and issues live in the [main repository](https://github.com/xyTom/coding-tools-mcp).
