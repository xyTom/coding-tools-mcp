import assert from "node:assert/strict";
import { chmod, copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const launcher = path.join(packageRoot, "bin", "coding-tools-mcp.js");
const fixtureRoot = path.join(packageRoot, ".tmp");

async function createFixtureDirectory(testContext, prefix) {
  await mkdir(fixtureRoot, { recursive: true });
  const directory = await mkdtemp(path.join(fixtureRoot, prefix));
  testContext.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

async function writeExecutable(directory, name, { captureArgs = false, exitCode = 0 } = {}) {
  const windows = process.platform === "win32";
  if (windows) {
    const target = path.join(directory, `${name}.exe`);
    const preload = path.join(directory, `${name}-stub.cjs`);
    await copyFile(process.execPath, target);
    await writeFile(
      preload,
      [
        'const fs = require("node:fs");',
        'const path = require("node:path");',
        'const executable = path.basename(process.execPath).toLowerCase();',
        'if (executable === "uvx.exe" || executable === "pipx.exe") {',
        captureArgs
          ? '  fs.writeFileSync(process.env.RESULT_FILE, `${process.argv.slice(1).join("\\n")}\\n`);'
          : "",
        `  process.exit(${exitCode});`,
        "}",
        "",
      ].join("\n"),
      "utf8",
    );
    return { NODE_OPTIONS: `--require=${preload.replaceAll("\\", "/")}` };
  }

  const target = path.join(directory, name);
  const body = [
    "#!/bin/sh",
    captureArgs ? 'printf "%s\\n" "$@" > "$RESULT_FILE"' : "",
    `exit ${exitCode}`,
    "",
  ].join("\n");
  await writeFile(target, body, "utf8");
  await chmod(target, 0o755);
  return {};
}

function runLauncher(binDirectory, args = [], extraEnv = {}, ambientEnv = process.env) {
  const env = { ...ambientEnv };
  delete env.CODING_TOOLS_MCP_VERSION;
  return spawnSync(process.execPath, [launcher, ...args], {
    encoding: "utf8",
    env: {
      ...env,
      ...extraEnv,
      PATH: binDirectory,
    },
  });
}

async function readCapturedArgs(output) {
  const args = (await readFile(output, "utf8")).trim().split("\n");
  if (process.platform === "win32" && args.length > 0) {
    args[0] = path.basename(args[0]);
  }
  return args;
}

test("uvx receives the pinned Python package and forwarded arguments", async (t) => {
  const directory = await createFixtureDirectory(t, "uvx-");
  const output = path.join(directory, "args.txt");
  const runnerEnv = await writeExecutable(directory, "uvx", { captureArgs: true });

  const result = runLauncher(directory, ["--stdio", "--workspace", "/repo"], {
    ...runnerEnv,
    CODING_TOOLS_MCP_VERSION: "0.2.0",
    RESULT_FILE: output,
  });

  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await readCapturedArgs(output), [
    "coding-tools-mcp==0.2.0",
    "--stdio",
    "--workspace",
    "/repo",
  ]);
});

test("pipx is used when uvx is unavailable", async (t) => {
  const directory = await createFixtureDirectory(t, "pipx-");
  const output = path.join(directory, "args.txt");
  const runnerEnv = await writeExecutable(directory, "pipx", { captureArgs: true });

  const result = runLauncher(
    directory,
    ["--help"],
    { ...runnerEnv, RESULT_FILE: output },
    { ...process.env, CODING_TOOLS_MCP_VERSION: "9.9.9" },
  );

  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await readCapturedArgs(output), [
    "run",
    "coding-tools-mcp",
    "--help",
  ]);
});

test("the launcher explains how to install a supported runner", async (t) => {
  const directory = await createFixtureDirectory(t, "empty-");
  const result = runLauncher(directory);

  assert.equal(result.status, 1);
  assert.match(result.stderr, /neither uvx nor pipx was found/);
  assert.match(result.stderr, /pip install coding-tools-mcp/);
});

test("the child exit code is preserved", async (t) => {
  const directory = await createFixtureDirectory(t, "exit-");
  const runnerEnv = await writeExecutable(directory, "uvx", { exitCode: 7 });

  const result = runLauncher(directory, [], runnerEnv);

  assert.equal(result.status, 7);
});
