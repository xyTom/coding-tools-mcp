# Exec Command Recipes

These recipes intentionally use explicit `exec_command` commands. The MCP server does not infer project type, install dependencies automatically, or choose package-manager cache policy.

## Foreground and background results

The server always exposes the same four process tools: `exec_command`,
`write_stdin`, `read_output`, and `kill_session`. It does not dynamically add a
tool after a command starts.

`exec_command` waits up to 10 seconds by default. If the command exits in that
window, the result is complete and no polling call is needed. If it is still
running, the result contains a `session_id` and an exact `next_action`, for
example:

```json
{
  "status": "running",
  "session_id": "sess_123",
  "next_action": {
    "tool": "write_stdin",
    "arguments": {
      "session_id": "sess_123",
      "chars": "",
      "yield_time_ms": 10000
    }
  }
}
```

Calling `write_stdin` with empty `chars` means “wait/poll”; non-empty `chars`
interacts with the process. `read_output` is for paging retained stdout/stderr
when a result explicitly says output was truncated (or when compact verbosity
was requested). It is not an extra step for every command.

Use the external runtime `HOME`, `TMPDIR`, or `cache_dir` reported by `server_info` when you want dependency caches without adding files to the Git worktree. These shell examples assume trusted mode because they use environment expansion:

```bash
MAVEN_USER_HOME="$HOME/.cache/m2" mvn test
GRADLE_USER_HOME="$HOME/.cache/gradle" ./gradlew test
npm_config_cache="$HOME/.cache/npm" npm ci && npm test
PIP_CACHE_DIR="$HOME/.cache/pip" python -m pip install -r requirements.txt && python -m pytest
GOCACHE="$HOME/.cache/go-build" GOMODCACHE="$HOME/.cache/go-mod" go test ./...
CARGO_HOME="$HOME/.cache/cargo" cargo test
cmake -S . -B "$TMPDIR/cmake-build" && cmake --build "$TMPDIR/cmake-build" && ctest --test-dir "$TMPDIR/cmake-build"
```

Primitive toolchain checks:

```bash
java -version
javac -version
mvn -version
gcc --version
g++ --version
make --version
cmake --version
node --version
npm --version
python --version
pip --version
go version
cargo --version
rustc --version
```

If dependencies need network access, start with:

```bash
coding-tools-mcp --permission-mode trusted --workspace /path/to/repo
```
