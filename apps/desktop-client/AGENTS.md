# Desktop client guide

This subtree owns the desktop application. Keep desktop-only behavior here and keep the core MCP runtime in `coding_tools_mcp/` independent of the GUI.

## Boundaries

- Preserve the `mcp_desktop_client` import/package name and existing console entry point.
- Do not duplicate core server logic in the desktop client; invoke or configure the core runtime instead.
- User-facing remote-access helpers belong under `integrations/`, not under desktop UI modules or root `scripts/`.
- When changing UI strings, run the desktop i18n check in `scripts/check_desktop_i18n.py` when dependencies permit.
- When changing runtime discovery or process management, run `tests/test_desktop_client.py`.
