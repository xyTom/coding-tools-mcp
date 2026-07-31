# Troubleshooting Exec Command

`exec_command` preserves raw `stdout`, `stderr`, and `exit_code`. It may also return `diagnostics` with common failure codes.

Common codes:

- `DEV_NULL_DENIED`: Landlock special device rules are wrong for `/dev/null`.
- `TMPDIR_NOT_WRITABLE`: the configured temp directory is not writable.
- `HOME_NOT_WRITABLE`: the configured home directory is not writable.
- `DNS_RESOLUTION_FAILED`: resolver configuration or network/DNS failed.
- `NETWORK_PERMISSION_REQUIRED`: safe mode blocked a network-looking command.
- `SHELL_EXPANSION_PERMISSION_REQUIRED`: safe mode blocked shell expansion.
- `INLINE_SCRIPT_PERMISSION_REQUIRED`: safe mode blocked inline interpreter or shell code.
- `LANDLOCK_READ_ROOT_BLOCKED`: a toolchain file path is missing from read roots.
- `SECRET_ENV_REJECTED`: secret-looking or loader/startup env was rejected.
- `COMMAND_TIMED_OUT`: the command exceeded `timeout_ms`.
- `OUTPUT_TRUNCATED`: stdout or stderr exceeded output limits.

Useful explicit probes:

```bash
dd if=/dev/null of=/dev/null bs=1 count=0
echo hi >/dev/null
printf ok > "$HOME/coding-tools-write-test"
printf ok > "$TMPDIR/coding-tools-write-test"
cat /etc/resolv.conf && getent hosts repo.maven.apache.org
```

## Wrong Toolchain Version (nvm, pyenv, rbenv, asdf)

Symptom: `node --version` in your terminal prints v24, but the same command
through `exec_command` prints the system Node (for example v18).

Cause: version managers only prepend their shim/bin directories to `PATH` in
*interactive shell rc files* (`~/.zshrc`, `~/.bashrc`). When the MCP host that
spawned the server was launched from a GUI (desktop app, IDE), it inherited the
minimal system `PATH`, and `exec_command` — which inherits `PATH` from the
server process under the default `core` policy — resolves `node` to the system
copy.

Fixes, in preference order:

1. **Resolve the login-shell `PATH` in your launcher.** If you control the
   process that spawns the server, ask the user's login shell for its `PATH`
   once at startup (the same trick VS Code and kimi-code use) and spawn the
   server with it, so nvm-selected toolchains work no matter how the host app
   was launched.
2. **Pass the PATH explicitly** in your MCP host config `env` block, or start
   the server with an absolute command path (for example the nvm-versioned
   `.../versions/node/v24.x/bin/node`).
3. **Broaden inheritance** with `--shell-env-inherit all` /
   `CODING_TOOLS_MCP_SHELL_ENV_INHERIT=all` when commands also need variables
   beyond the core set (`NVM_DIR`, `GOPATH`, `JAVA_HOME`, …). Sensitive-looking
   variables are still filtered outside dangerous mode. This mirrors Codex's
   `shell_environment_policy.inherit = "all"` default while keeping this
   server's stricter `core` default.
