# Coding Tools MCP Boundary Findings

This document records boundary issues found while dogfooding this workspace with
real coding tasks across Java/Spring Boot, C, C++, Node/npm, TypeScript, Go,
Rust/Cargo, Python, FFmpeg downloads, interactive sessions, images, and git.

## Confirmed capabilities

- Java/Maven and Spring Boot can be created, tested, packaged, started, and
  verified with local HTTP requests.
- C and C++ builds work with gcc/g++, Make, CMake, and CTest.
- Node/npm and TypeScript package install, compile, and run flows work.
- Go modules and Rust/Cargo can download dependencies and execute programs.
- Direct HTTPS downloads work, including a static FFmpeg tarball.
- Long-running interactive sessions work with `write_stdin`.
- `view_image` can inspect generated PNG files.
- Security policy correctly blocks workspace escapes, secret-looking environment
  variables, and destructive commands.

## Issues found and fixes planned

### 1. Git helper tools can falsely report `is_repo: false`

The workspace is a valid git repository and native `git` works under
`exec_command`, but `git_status`, `git_log`, and `git_diff` can report a
non-repository fallback. Reproducing native git without the configured global git
config produces Git's `dubious ownership` error for `/workspace`.

Root cause: git helper methods call `subprocess.run` without the command
environment used by `exec_command`, so they can miss `GIT_CONFIG_GLOBAL` and the
configured `safe.directory=/workspace` entry.

Fix: route git helper subprocesses through a shared git environment based on
`_command_env({})`, pass it to git subprocesses, and surface rev-parse stderr as
warnings instead of silently returning `is_repo: false`.

### 2. Python package installation is blocked by Landlock read roots

`python3 -m venv` failed in `ensurepip`, and `pip install --target` failed when
pip's vendored distro code attempted to read `/etc/debian_version` for its
User-Agent.

Status: partially investigated. A narrow file-root Landlock change was attempted
for low-sensitivity OS metadata commonly read by language package managers, but
current Landlock path traversal behavior still denied pip's distro metadata read.
This remains a follow-up item because broadening system read roots requires a
deliberate security review.

### 3. Common argument aliases are rejected by strict schemas

The schemas intentionally use `additionalProperties: false`, which is good for
contract clarity but brittle for common coding-agent parameter names.

Examples hit during dogfooding:

- `exec_command` accepts `workdir`, not `cwd`.
- `read_file` accepts `start_line`/`end_line`, not `max_lines`.
- `git_status` accepts `path`/`include_untracked`/`max_entries`, not `short`.

Fix: support safe aliases while keeping canonical fields. Reject conflicting
`workdir`/`cwd` values.

### 4. Heredoc XML can be misclassified as an escaping path

Shell tokenization of a heredoc containing XML such as `<modelVersion>` can
produce tokens like `/modelVersion`, which the path scanner treats as an absolute
path escape.

Fix: when a heredoc token is encountered in command path scanning, inspect the
redirection target and command arguments seen so far, then stop scanning the rest
of the command as path arguments because those tokens are stdin payload.

## Remaining known limitations

- `apt`/system package managers need `/etc/apt` and `/var/cache/apt`; they remain
  outside the current sandbox model and are not fixed here.
- Docker/Podman and several language ecosystems are not installed in the current
  image.
- The system lacks an `xz` executable; Python's `lzma` can still extract `.xz`
  archives as a fallback.
