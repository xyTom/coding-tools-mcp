# Embedding coding-tools-mcp In Your App Or Agent

This is the reference template for programs that spawn `coding-tools-mcp`
themselves instead of configuring it in an MCP host. It covers the three things
every embedder must get right:

1. **Start** the server over stdio (default) or against Docker/HTTP.
2. **Speak** newline-delimited JSON-RPC (`initialize` → `tools/call`).
3. **Stop** it — close stdin and terminate the child so no background
   `coding-tools-mcp` process outlives your app.

A production-grade embedder keeps the same skeleton and adds request
timeouts, EPIPE-safe writes, reconnect loops, and a SIGTERM→SIGKILL close
escalation on top.

## Minimal Node.js Client

```js
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

class CodingToolsClient {
  #child;
  #pending = new Map();
  #nextId = 1;

  /** cmd example: ["uvx", "coding-tools-mcp", "--stdio", "--workspace", repo] */
  async start(cmd) {
    const [command, ...args] = cmd;
    this.#child = spawn(command, args, { stdio: ["pipe", "pipe", "inherit"] });
    // Both handlers are required: without them a backend that dies early
    // surfaces as an uncaught EPIPE and crashes the embedder.
    this.#child.stdin.on("error", () => this.#failAll(new Error("backend closed")));
    this.#child.once("exit", () => this.#failAll(new Error("backend exited")));
    createInterface({ input: this.#child.stdout }).on("line", (line) => {
      const message = JSON.parse(line);
      const pending = this.#pending.get(message.id);
      if (!pending) return;
      this.#pending.delete(message.id);
      message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result);
    });

    await this.#request("initialize", {
      protocolVersion: "2025-11-25",
      capabilities: {},
      clientInfo: { name: "my-agent", version: "0.1.0" },
    });
    this.#notify("notifications/initialized", {});
  }

  callTool(name, args) {
    return this.#request("tools/call", { name, arguments: args });
  }

  /**
   * Shutdown order matters: stdin EOF lets the server exit cleanly (and lets a
   * `docker run --rm -i` backend stop + auto-remove); SIGTERM covers servers
   * that ignore EOF. Always call this before your app exits — otherwise the
   * spawned coding-tools-mcp keeps running in the background.
   */
  async close() {
    if (!this.#child) return;
    this.#child.stdin.end();
    this.#child.kill();
    this.#failAll(new Error("client closed"));
  }

  #request(method, params, timeoutMs = 60_000) {
    const id = this.#nextId++;
    const promise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.#pending.delete(id);
        reject(new Error(`${method} timed out`));
      }, timeoutMs);
      timer.unref();
      this.#pending.set(id, {
        resolve: (v) => (clearTimeout(timer), resolve(v)),
        reject: (e) => (clearTimeout(timer), reject(e)),
      });
    });
    this.#child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    return promise;
  }

  #notify(method, params) {
    this.#child.stdin.write(JSON.stringify({ jsonrpc: "2.0", method, params }) + "\n");
  }

  #failAll(error) {
    for (const pending of this.#pending.values()) pending.reject(error);
    this.#pending.clear();
  }
}

const client = new CodingToolsClient();
// Tie the server's lifetime to yours before doing any work.
process.on("exit", () => void client.close());
process.on("SIGINT", () => process.exit(130));
process.on("SIGTERM", () => process.exit(143));

await client.start(["uvx", "coding-tools-mcp", "--stdio", "--workspace", process.cwd()]);
console.log(await client.callTool("server_info", {}));
console.log(await client.callTool("exec_command", { cmd: "node --version", timeout_ms: 10_000 }));
await client.close();
```

## Minimal Python Client

```python
import json
import subprocess
import sys


class CodingToolsClient:
    def __init__(self, cmd: list[str]) -> None:
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.next_id = 1
        self.request("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "my-agent", "version": "0.1.0"},
        })
        self.notify("notifications/initialized", {})

    def request(self, method: str, params: dict, timeout: float = 60.0):
        rpc_id, self.next_id = self.next_id, self.next_id + 1
        self._write({"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params})
        # coding-tools-mcp answers strictly in request order over stdio.
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("backend exited")
        message = json.loads(line)
        if "error" in message:
            raise RuntimeError(message["error"]["message"])
        return message["result"]

    def call_tool(self, name: str, args: dict):
        return self.request("tools/call", {"name": name, "arguments": args})

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def close(self) -> None:
        """EOF first, then terminate: no orphaned coding-tools-mcp afterwards."""
        self.proc.stdin.close()
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        finally:
            self.proc.stdout.close()


client = CodingToolsClient(["uvx", "coding-tools-mcp", "--stdio", "--workspace", "."])
try:
    print(client.call_tool("server_info", {}))
finally:
    client.close()
```

## Backend Variants

The same client shape works for every deployment; only the spawn/connect step
changes:

| Backend | Start | Stop |
| --- | --- | --- |
| Local stdio (default) | `uvx coding-tools-mcp --stdio --workspace <repo>` | close stdin, then terminate |
| Docker stdio | `docker run --rm -i -v "$PWD:/workspace" <image> --stdio` | close stdin — the server exits on EOF and `--rm` removes the container |
| Docker HTTP | `docker run --rm --init -p 8765:8765 <image>` (see [docker.md](docker.md)) | `docker stop` — the server handles SIGTERM and exits promptly |
| Remote HTTP | POST JSON-RPC to `http://host:8765/mcp` with `Authorization: Bearer <token>` | server lifetime is managed by whoever started it |

## Cleanup Checklist

- Close the server's stdin when you are done; the stdio server exits on EOF.
- Send SIGTERM as a follow-up and SIGKILL only as a last resort.
- Register the cleanup on *your* exit paths (`process.on("exit")`, `atexit`,
  signal handlers) so a crash in your agent does not orphan the server.
- If you expose the server to a model loop, add per-request timeouts so a hung
  backend cannot wedge your agent.

## Environment Notes

`exec_command` inherits only a core environment (`PATH`, `HOME`*, locale) by
default — see [permission-modes.md](permission-modes.md). Two practical
consequences for embedders:

- Launch your app from a login shell, or pass an explicit `PATH`, so version
  managers (nvm, pyenv) resolve the same toolchain the user sees in their
  terminal. A robust embedder asks the user's login shell for its `PATH` once
  at startup and reuses it for spawned backends.
- Set `CODING_TOOLS_MCP_SHELL_ENV_INHERIT=all` (or `--shell-env-inherit all`)
  when the workload genuinely needs the full host environment; sensitive
  variables are still filtered outside dangerous mode.

*`HOME` is redirected to a per-runtime directory; see the
[runtime contract](runtime-contract-v0.2.md).
