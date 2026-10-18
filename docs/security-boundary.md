# Security Boundary

This page describes default `--execution-isolation compatibility` behavior.
For the opt-in strict mode, its supported capabilities, and fail-closed
requirements, see [cross-platform execution isolation](cross-platform-sandbox.md).

Coding Tools MCP exposes primitives, not an agent workflow engine.

The boundary is:

- direct file tools accept workspace-relative paths only
- `exec_command` starts in a workspace cwd
- safe/trusted modes filter secrets and loader/startup env
- safe/trusted modes block destructive commands
- safe mode blocks network-looking commands, shell expansion, and inline scripts
- Landlock confines filesystem access when available

The boundary is not:

- a complete OS sandbox on every platform
- a package-manager policy engine
- a project build/test/install orchestrator
- an active network egress firewall

For untrusted workspaces or untrusted MCP clients, run the server inside an external container or VM with no host secrets and restricted network egress.
