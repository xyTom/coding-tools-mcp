# Tunnel integration guide

This subtree owns user-facing tunnel launcher scripts.

- Keep provider-specific launchers thin and share common behavior through `tunnel-common.sh`.
- Keep relative script discovery based on `BASH_SOURCE`/the script directory so the launchers work from any current working directory.
- Preserve environment-variable names and CLI provider aliases unless a task explicitly changes the public interface.
- Update user-facing documentation whenever invocation paths or supported providers change.
