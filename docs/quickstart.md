# Quickstart

Install the runtime in editable mode:

```bash
python -m pip install -e ".[dev]"
```

Start Streamable HTTP against a workspace:

```bash
codex-tool-runtime-mcp --workspace /path/to/repo --host 127.0.0.1 --port 8765
```

Endpoint:

```text
http://127.0.0.1:8765/mcp
```

Start stdio:

```bash
codex-tool-runtime-mcp --stdio --workspace /path/to/repo
```

Run the acceptance gate:

```bash
make compliance
```

For local trace debugging:

```bash
CODEX_TOOL_RUNTIME_TRACE=1 codex-tool-runtime-mcp --workspace /path/to/repo
```

Trace JSON lines are written to stderr.
