# Native execution helper, protocol 1 / version 0.1.0

This is a small **original implementation**, with no dependency on the Codex CLI
or agent runtime and no third-party Rust crates. It is part of the Python
`coding_tools_mcp.sandbox` backend, not an independently secure sandbox command.
Running it alone installs a syscall filter but **does not install filesystem or
network namespaces**. Never report an isolated command merely because this
binary can run.

## Build and install

The toolchain is fixed in `rust-toolchain.toml` (Rust 1.85.1). From this directory:

```sh
cargo test --locked
cargo build --release --locked
sudo install -d -m 0755 /opt/coding-tools-mcp/bin
sudo install -m 0755 target/release/coding-tools-sandbox-helper /opt/coding-tools-mcp/bin/
sha256sum /opt/coding-tools-mcp/bin/coding-tools-sandbox-helper
```

Use the platform's SHA-256 tool on macOS. Configure the absolute, canonical
installed path and the exact output hash in the server. The original install
directory must not overlap any command read or write root, including the workspace. Use a
dedicated directory, and deny that directory to commands. The backend verifies
all ancestors, ownership, write bits, a single-link regular executable inode,
and its SHA-256 through an opened descriptor. It never searches `PATH` for this
helper. The built binary is not implicitly bundled in the Python wheel.

The Linux backend requires `/usr/bin/bwrap` **0.12.0 or newer**, installed with
root-owned, non-group/world-writable ancestors. Older versions do not supply the
fd-pinned bind operations used here. Container images with differently owned
system directories may be rejected; an administrator must provide a properly
trusted install rather than relaxing checks automatically.

## Linux boundary

Python builds a fresh bubblewrap mount, user, PID, network, IPC and UTS namespace
with capabilities dropped, nested user namespaces disabled, a fresh proc/dev,
readonly root, private tmp/home, and only explicit descriptor-backed read/write
mounts. Bind descriptors are inode-checked by bubblewrap and closed during setup.
Denied roots must be disjoint from every read/write grant. Nested denies are
rejected before launch: path masks cannot deny alternate hardlink names or
protect mutable ancestor names across launches. Keep credentials and service
state outside granted trees. A granted tree authorizes its existing contents,
including any preexisting hardlinks to outside data; administrators must keep
such private aliases out of granted trees. The structured file broker has a
separate, narrower no-hardlinks policy. The helper is copied from its already
verified descriptor into an immutable internal mount. Neither source path swaps
nor an executable with the same name in the workspace select the helper.

The helper installs `no_new_privs` and a seccomp filter before acknowledging
startup. Supported syscall architectures are x86-64 and AArch64 only. Other
architectures and compat ABIs fail closed. The filter blocks namespace/mount
changes, ptrace, cross-process memory/fd acquisition, keyring/BPF/perf operations,
and io_uring. `clone3` returns ENOSYS for libc fallback; legacy clone may create
ordinary children but cannot create namespaces. Resource-exhaustion quotas are
not implemented; this is an access/lifetime boundary, not a resource budget.

* **Offline:** creating sockets or socketpairs is denied by the kernel,
  including IPv4, IPv6, UDP, DNS, loopback, Unix, netlink, and vsock.
* **Proxy:** a trusted, non-dumpable helper process listens on a private
  namespace-local loopback TCP port and relays bytes to one dedicated host Unix
  socket. The untrusted child may create only AF_INET/AF_INET6 sockets in that
  otherwise disconnected namespace. Unix sockets, socketpairs, descriptor
  acquisition and io_uring remain denied. No host listener or tunnel descriptor
  survives into the command. Proxy variables aid compatible clients but are not
  the boundary. The host `ControlledProxy` accepts exact allowlisted CONNECT
  host:port targets and validates/pins actual global-IP resolution. No UDP
  egress, TLS interception or credential injection is implemented.

The proxy helper exits when its direct command exits. Bubblewrap's PID-namespace
init and `--die-with-parent` own all descendants, including setsid/double-fork
children. Cancellation kills the outer bubblewrap process group; namespace
teardown kills children regardless of their own process groups. The host proxy
is closed on startup failure or command exit and revokes all active tunnels.

## Trusted startup protocol

A private inherited Unix socketpair carries startup control; stdout/stderr are
never protocol channels. The helper sends exactly:

```text
CTMCP_SANDBOX 1 0.1.0 linux-bwrap NONCE\n
```

Proxy mode uses `linux-bwrap-proxy`. The nonce is a random 64-hex-character
per-launch value. Only after receiving the exact frame does Python send
`GO NONCE\n`. The command cannot execute before that gate. Timeout, malformed
frames, version mismatch, failed restriction installation, failed proxy startup,
or EOF cause cleanup and a security error, never a plain-subprocess fallback.
The control fd closes before command exec. In proxy mode the command child
installs its filter and performs this handshake in `pre_exec`, using prepared
filter memory and raw descriptor I/O before the trusted parent starts threads.

## macOS and Windows: deliberately unavailable strict capability

A deny-default Seatbelt profile compiler exists in Python, but installing a
filesystem/network profile alone does not satisfy the promised descendant
lifetime boundary. Native macOS strict execution therefore rejects before
command startup. This is an implementation gap, not a claim that secure macOS
supervision is impossible. In particular:

* Process-group termination does not contain children that change groups or
  sessions. The current profile permits these transitions. XNU does have a
  [generic Unix-syscall sandbox callback](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/dev/arm/systemcalls.c),
  and [WebKit uses syscall-number filters](https://github.com/WebKit/WebKit/blob/1c1ab7c623f434b413e8a7c7016539630aa0b35b/Source/WebKit/WebProcess/com.apple.WebProcess.sb.in).
  The absence of a dedicated callback inside `setsid_internal` is therefore
  **not** proof that `setsid` cannot be filtered.
* Filtering only the `setsid` and `setpgid` syscall entries is insufficient:
  [XNU handles `posix_spawn` process-group/session attributes inside the kernel](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/kern_exec.c).
  Denying all `posix_spawn` calls would restrict ordinary toolchains as well.
  There is no implemented, validated attribute-aware spawn broker or equivalent
  lifetime supervisor here. Native tests separately characterize direct
  `setsid`, `setpgid`, `daemon`, and `posix_spawn` group transitions; their
  test-only syscall profile is not a supported backend.
* Darwin explicitly marks `NOTE_TRACK`, `NOTE_TRACKERR`, and `NOTE_CHILD`
  unsupported since macOS 10.5. FreeBSD kqueue's automatic descendant tracking
  cannot simply be ported. `NOTE_FORK` notifications or process-list polling
  leave a fork/reparent/registration race.
  [Apple source](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/sys/event.h)

A stronger implementation needs a separately validated OS-supported supervisor
or VM. Privileged Endpoint Security deployment would require separate
entitlements and an independently reviewed authority model. No such guarantee
is claimed by this release. Windows similarly rejects native strict mode;
compatibility shell execution is a separate, weaker mode.

## Verification

Return to the repository root before running the Python suites:

```sh
export CODING_TOOLS_SANDBOX_HELPER=/opt/coding-tools-mcp/bin/coding-tools-sandbox-helper
export CODING_TOOLS_SANDBOX_REQUIRE_NATIVE=1
python -m unittest -v tests.test_sandbox tests.test_sandbox_native
```

The required Linux gate fails if namespaces, seccomp, trusted installations, or
proxy Unix-socket support are unavailable. Optional local runs identify an
unavailable namespace backend with a skip rather than claim acceptance. The
helper-specific tests run actual seccomp and launch-gate behavior independently
and explicitly do **not** count as namespace acceptance. On macOS the suite additionally runs actual Seatbelt filesystem/network profile
checks, separately from strict capability rejection. On Windows it verifies
honest strict rejection. Neither is evidence of full strict lifetime support.

Full Linux tests cover outside-file read/write/delete, external credential
directories and rejection of unsafe nested denies, readonly source and symlink escape, no leaked source/control descriptors,
IPv4/IPv6 TCP/UDP and DNS-wire loopback fixtures proven reachable outside,
allowed/denied CONNECT targets, proxy live revocation, and setsid-grandchild
cleanup. The proxy fixture patches public-address classification solely to map
a synthetic public hostname to a real local test server; separate production
policy tests reject private/special-use addresses without that patch.
