# Coding Tools MCP repository guide

This repository is a monorepo. Keep changes inside the narrowest owning subtree and avoid creating cross-component coupling unless the feature requires it.

## Repository map

- `coding_tools_mcp/`: core Python MCP runtime and public server behavior.
- `apps/desktop-client/`: desktop client UI and desktop runtime integration.
- `integrations/tunnels/`: user-facing tunnel launchers for remote MCP access.
- `infra/cloudflare/sandbox-control/`: Cloudflare Worker control plane paired with `.github/workflows/start-sandbox.yml`.
- `packages/npm-launcher/`: thin npm launcher for the Python package.
- `media/promo-video/`: Remotion source for the project promo video.
- `benchmarks/`: benchmark runners and fixtures.
- `reports/`: generated/published benchmark and compliance results.
- `docs/`: user, contributor, architecture, and reference documentation.
- `scripts/`: repository maintenance, validation, installation, release, and report-generation scripts. Do not put user-facing runtime integrations here.

## Standing rules

1. Preserve public CLI names, Python import paths, protocol schemas, and release behavior unless a task explicitly requires a breaking change.
2. Keep one authoritative home for each fact; link to it instead of copying long explanations between documents.
3. Changes to `infra/cloudflare/sandbox-control/` and `.github/workflows/start-sandbox.yml` may form one interface contract. Update and validate them together.
4. Prefer subtree-specific instructions when present.
5. Run the narrowest relevant checks first, then broader checks when tooling is available.
