# Documentation guide

Use one authoritative home for each fact. Prefer links over duplicated long-form explanations.

Start with `docs/README.md` when locating documentation by topic. Existing document paths are intentionally kept stable; organize navigation through the index before moving published files.

## Documentation roles

- Root `README.md` / `README.zh-CN.md`: product overview, quick entry points, and navigation.
- Component `README.md`: local setup and component-specific usage.
- `docs/`: detailed user guides, architecture/reference material, migrations, troubleshooting, and contributor-facing explanations.
- `AGENTS.md`: stable instructions for coding agents, not end-user documentation.

When moving an existing document, preserve or update inbound links in the same change. Avoid large documentation-path migrations unless redirects or compatibility are available.
