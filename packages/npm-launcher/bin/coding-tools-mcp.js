#!/usr/bin/env node
// npm launcher for the coding-tools-mcp Python package published on PyPI.
// It starts the real server through uvx (preferred) or pipx, forwarding all
// arguments and stdio, so MCP clients configured with the npm name work
// unchanged. Pin a server version with CODING_TOOLS_MCP_VERSION=x.y.z.
import { spawn } from "node:child_process";

const args = process.argv.slice(2);
const version = process.env.CODING_TOOLS_MCP_VERSION;
const spec = version ? `coding-tools-mcp==${version}` : "coding-tools-mcp";
const candidates = [
  { command: "uvx", args: [spec, ...args] },
  { command: "pipx", args: ["run", spec, ...args] },
];

let child;
for (const signal of ["SIGINT", "SIGTERM", "SIGHUP"]) {
  process.on(signal, () => child?.kill(signal));
}

function run(index) {
  if (index >= candidates.length) {
    process.stderr.write(
      [
        "coding-tools-mcp: neither uvx nor pipx was found on PATH.",
        "Install one of them and retry:",
        "  uv:   curl -LsSf https://astral.sh/uv/install.sh | sh",
        "  pipx: python3 -m pip install --user pipx",
        "Or install the server directly from PyPI: pip install coding-tools-mcp",
        "",
      ].join("\n"),
    );
    process.exit(1);
  }
  const candidate = candidates[index];
  child = spawn(candidate.command, candidate.args, { stdio: "inherit" });
  child.once("error", (error) => {
    if (error.code === "ENOENT") {
      run(index + 1);
      return;
    }
    process.stderr.write(`coding-tools-mcp: failed to start ${candidate.command}: ${error.message}\n`);
    process.exit(1);
  });
  child.once("exit", (code, signal) => {
    if (signal) {
      // Mirror the child's fatal signal instead of remapping it to an exit code.
      process.removeAllListeners(signal);
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code ?? 0);
  });
}

run(0);
