# Coding Tools MCP repository guide

This repository is a monorepo. Keep changes inside the narrowest owning subtree and avoid creating cross-component coupling unless the feature requires it.

## Repository map

- `coding_tools_mcp/`: core Python MCP runtime and public server behavior.
- `integrations/tunnels/`: user-facing tunnel launchers for remote MCP access.
- `infra/cloudflare/sandbox-control/`: Cloudflare Worker control plane paired with `.github/workflows/start-sandbox.yml`.
- `packages/npm-launcher/`: thin npm launcher for the Python package.
- `media/promo-video/`: Remotion source for the project promo video.
- `benchmarks/`: benchmark runners and fixtures.
- `reports/`: generated/published benchmark and compliance results.
- `docs/`: source-coupled contracts, engineering documentation, evidence, and compatibility pointers to the external docs site.
- `scripts/`: repository maintenance, validation, installation, release, and report-generation scripts. Do not put user-facing runtime integrations here.

## Standing rules

1. Preserve public CLI names, Python import paths, protocol schemas, and release behavior unless a task explicitly requires a breaking change.
2. Keep one authoritative home for each fact; link to it instead of copying long explanations between documents.
3. Changes to `infra/cloudflare/sandbox-control/` and `.github/workflows/start-sandbox.yml` may form one interface contract. Update and validate them together.
4. Prefer subtree-specific instructions when present.
5. Run the narrowest relevant checks first, then broader checks when tooling is available.

## Ecosystem repositories

- Desktop application: `https://github.com/coding-tools-mcp/desktop`
- Public documentation site: `https://github.com/coding-tools-mcp/docs`

Do not reintroduce desktop application code into this repository. Human-oriented tutorials and client setup belong in the documentation repository unless a source-coupled compatibility pointer is required here.
