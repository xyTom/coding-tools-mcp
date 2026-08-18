# npm launcher guide

This package is a thin distribution wrapper around the Python `coding-tools-mcp` package.

- Keep launcher behavior small; server/runtime behavior belongs in `coding_tools_mcp/`.
- Preserve the published npm package name and launcher CLI contract.
- Run `make check-npm-launcher` after changing this package.
- Keep release metadata paths in `.github/workflows/release.yml` and `scripts/check_release_versions.py` in sync with this directory.
