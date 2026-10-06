# Documentation guide

Use one authoritative home for each fact. Prefer links over duplicated long-form explanations.

Start with `docs/README.md` when locating source-coupled documentation by topic. Human-oriented tutorials are maintained in `https://github.com/coding-tools-mcp/docs`.

## Documentation roles

- Root `README.md` / `README.zh-CN.md`: product overview, quick entry points, and navigation.
- Component `README.md`: local setup and component-specific usage.
- `docs/`: runtime contracts, schema/reference material, engineering/evidence docs, and compatibility pointers for moved user guides.
- `AGENTS.md`: stable instructions for coding agents, not end-user documentation.

When moving an existing user guide to the docs repository, leave a short compatibility pointer here when the old path is already public, and update inbound links in the same change.
