# coding-tools-mcp (npm launcher)

npm launcher for [coding-tools-mcp](https://github.com/xyTom/coding-tools-mcp), the model-neutral coding-agent runtime MCP server. The server itself is a Python package published on [PyPI](https://pypi.org/project/coding-tools-mcp/); this package starts it through `uvx` (preferred) or `pipx run`, forwarding all arguments and stdio.

```bash
npx coding-tools-mcp --stdio --workspace /path/to/repo
```

Requires `uv` or `pipx` on PATH. The launcher runs the latest PyPI release; pin a specific server version with:

```bash
CODING_TOOLS_MCP_VERSION=0.2.0 npx coding-tools-mcp --stdio --workspace /path/to/repo
```

The launcher's own version is independent of the server version. Documentation, configuration, and issues live in the [main repository](https://github.com/xyTom/coding-tools-mcp).
