//! Small, dependency-free post-isolation launcher. No shell and no stdout protocol.
//! The caller verifies this binary's SHA-256 and binds a descriptor-backed copy
//! into a fresh bubblewrap mount/PID/network namespace. This helper installs
//! the remaining syscall boundary, then waits for an authenticated launch gate.
use std::env;
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::fd::FromRawFd;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
use std::process::Command;

const VERSION: &str = env!("CARGO_PKG_VERSION");
const PROTOCOL: &str = "1";

#[cfg(target_os = "linux")]
mod boundary {
    use std::io;
    use std::os::raw::{c_int, c_ulong};
    #[repr(C)]
    struct Filter {
        code: u16,
        jt: u8,
        jf: u8,
        k: u32,
    }
    #[repr(C)]
    struct Program {
        len: u16,
        filter: *const Filter,
    }
    extern "C" {
        fn prctl(option: c_int, ...) -> c_int;
    }
    fn ins(code: u16, jt: u8, jf: u8, k: u32) -> Filter {
        Filter { code, jt, jf, k }
    }
    // Linux UAPI syscall numbers are architecture-specific. Unknown ABIs fail
    // closed; x86's x32 ABI and compat i386 are deliberately unsupported.
    #[cfg(target_arch = "x86_64")]
    const ARCH: u32 = 0xc000003e;
    #[cfg(target_arch = "aarch64")]
    const ARCH: u32 = 0xc00000b7;
    #[cfg(target_arch = "x86_64")]
    const DENIED: &[u32] = &[
        53,  // socketpair (do not hand any Unix socket to untrusted code)
        101, // ptrace
        155, // pivot_root
        161, // chroot
        165, 166, // mount, umount2
        246, 248, 249, 250, // kexec_load, add_key, request_key, keyctl
        272, // unshare
        298, // perf_event_open
        303, 304, // name_to_handle_at, open_by_handle_at
        308, 310, 311, // setns, process_vm_readv, process_vm_writev
        313, 321, 323, // finit_module, bpf, userfaultfd
        425, 426, 427, // io_uring (can issue socket operations asynchronously)
        428, 429, 430, 431, 432, 433, // new mount API
        435, // clone3: ENOSYS below enables ordinary libc fork fallback
        438, // pidfd_getfd
        442, // mount_setattr
    ];
    #[cfg(target_arch = "aarch64")]
    const DENIED: &[u32] = &[
        199, 117, 41, 51, 40, 39, 104, 217, 218, 219, 97, 241, 264, 265, 268, 270, 271, 273, 280,
        282, 425, 426, 427, 428, 429, 430, 431, 432, 433, 435, 438, 442,
    ];
    pub struct Prepared {
        filters: Vec<Filter>,
    }
    impl Prepared {
        pub fn new(proxy: bool) -> io::Result<Self> {
            #[cfg(not(any(target_arch = "x86_64", target_arch = "aarch64")))]
            {
                return Err(io::Error::other("unsupported Linux syscall architecture"));
            }
            #[cfg(any(target_arch = "x86_64", target_arch = "aarch64"))]
            {
                const KILL: u32 = 0x80000000;
                const ERRNO: u32 = 0x00050000;
                const ALLOW: u32 = 0x7fff0000;
                let mut f = vec![
                    ins(0x20, 0, 0, 4), // arch
                    ins(0x15, 1, 0, ARCH),
                    ins(0x06, 0, 0, KILL),
                    ins(0x20, 0, 0, 0), // syscall number
                    ins(0x45, 0, 1, 0x40000000),
                    ins(0x06, 0, 0, KILL),
                ];
                for nr in DENIED {
                    f.push(ins(0x15, 0, 1, *nr));
                    f.push(ins(0x06, 0, 0, ERRNO | if *nr == 435 { 38 } else { 1 }));
                }
                #[cfg(target_arch = "x86_64")]
                let socket_nr = 41;
                #[cfg(target_arch = "aarch64")]
                let socket_nr = 198;
                if proxy {
                    // Only network-namespace-local INET sockets can reach the
                    // trusted relay. No AF_UNIX, NETLINK, PACKET, VSOCK, etc.
                    f.push(ins(0x15, 0, 4, socket_nr));
                    f.push(ins(0x20, 0, 0, 16));
                    f.push(ins(0x15, 6, 0, 2)); // AF_INET -> final ALLOW
                    f.push(ins(0x15, 5, 0, 10)); // AF_INET6 -> final ALLOW
                    f.push(ins(0x06, 0, 0, ERRNO | 1));
                } else {
                    f.push(ins(0x15, 0, 1, socket_nr));
                    f.push(ins(0x06, 0, 0, ERRNO | 1));
                }
                // clone is needed by tools, but cannot create any new namespace.
                #[cfg(target_arch = "x86_64")]
                let clone_nr = 56;
                #[cfg(target_arch = "aarch64")]
                let clone_nr = 220;
                f.push(ins(0x15, 0, 3, clone_nr));
                f.push(ins(0x20, 0, 0, 16)); // args[0], low word (little-endian ABIs)
                f.push(ins(0x45, 0, 1, 0x7e020080)); // CLONE_NEW* plus NEWCGROUP
                f.push(ins(0x06, 0, 0, ERRNO | 1));
                f.push(ins(0x06, 0, 0, ALLOW));
                Ok(Self { filters: f })
            }
        }
        pub fn install(&self) -> io::Result<()> {
            let program = Program {
                len: self.filters.len() as u16,
                filter: self.filters.as_ptr(),
            };
            // No privilege gain through setuid executables or later filters.
            unsafe {
                if prctl(38, 1 as c_ulong, 0 as c_ulong, 0 as c_ulong, 0 as c_ulong) != 0 {
                    return Err(io::Error::last_os_error());
                }
                if prctl(
                    22,
                    2 as c_ulong,
                    &program as *const Program,
                    0 as c_ulong,
                    0 as c_ulong,
                ) != 0
                {
                    return Err(io::Error::last_os_error());
                }
            }
            Ok(())
        }
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        fn verdict(proxy: bool, nr: u32, arg0: u32, arch: u32) -> u32 {
            let prepared = Prepared::new(proxy).unwrap();
            let mut pc = 0;
            let mut a = 0;
            loop {
                let instruction = &prepared.filters[pc];
                match instruction.code {
                    0x20 => {
                        a = match instruction.k {
                            0 => nr,
                            4 => arch,
                            16 => arg0,
                            _ => panic!("bad load"),
                        }
                    }
                    0x15 => {
                        pc += if a == instruction.k {
                            instruction.jt
                        } else {
                            instruction.jf
                        } as usize;
                    }
                    0x45 => {
                        pc += if a & instruction.k != 0 {
                            instruction.jt
                        } else {
                            instruction.jf
                        } as usize;
                    }
                    0x06 => return instruction.k,
                    _ => panic!("unsupported instruction"),
                }
                pc += 1;
            }
        }
        #[test]
        fn network_domains_are_explicitly_limited() {
            #[cfg(target_arch = "x86_64")]
            let socket_nr = 41;
            #[cfg(target_arch = "aarch64")]
            let socket_nr = 198;
            for family in 0..64 {
                assert_eq!(verdict(false, socket_nr, family, ARCH), 0x00050001);
                let expected = if family == 2 || family == 10 {
                    0x7fff0000
                } else {
                    0x00050001
                };
                assert_eq!(verdict(true, socket_nr, family, ARCH), expected);
            }
        }
        #[test]
        fn inherited_boundary_forbids_namespace_and_descriptor_escape() {
            #[cfg(target_arch = "x86_64")]
            let clone_nr = 56;
            #[cfg(target_arch = "aarch64")]
            let clone_nr = 220;
            for proxy in [false, true] {
                assert_eq!(verdict(proxy, clone_nr, 17, ARCH), 0x7fff0000);
                for flag in [
                    0x80, 0x20000, 0x2000000, 0x4000000, 0x8000000, 0x10000000, 0x20000000,
                    0x40000000,
                ] {
                    assert_eq!(verdict(proxy, clone_nr, flag | 17, ARCH), 0x00050001);
                }
                for nr in DENIED {
                    assert_eq!(
                        verdict(proxy, *nr, 0, ARCH),
                        0x00050000 | if *nr == 435 { 38 } else { 1 }
                    );
                }
                assert_eq!(verdict(proxy, 0, 0, 0), 0x80000000);
                assert_eq!(verdict(proxy, 0x40000000, 0, ARCH), 0x80000000);
            }
        }
    }
}

#[cfg(unix)]
fn launch() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = env::args_os().skip(1);
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--control-fd")) {
        return Err("expected --control-fd".into());
    }
    let fd: i32 = args
        .next()
        .ok_or("missing control fd")?
        .to_str()
        .ok_or("invalid fd")?
        .parse()?;
    if fd < 3 {
        return Err("control fd must not be standard input/output".into());
    }
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--nonce")) {
        return Err("missing nonce option".into());
    }
    let nonce = args
        .next()
        .ok_or("missing nonce")?
        .into_string()
        .map_err(|_| "invalid nonce")?;
    if nonce.len() != 64 || !nonce.bytes().all(|c| c.is_ascii_hexdigit()) {
        return Err("invalid nonce".into());
    }
    let mut delimiter = args.next().ok_or("missing command delimiter")?;
    let proxy_socket = if delimiter == "--proxy-socket" {
        let path = args.next().ok_or("missing proxy socket path")?;
        if !std::path::Path::new(&path).is_absolute() {
            return Err("proxy socket must be absolute".into());
        }
        delimiter = args.next().ok_or("missing command delimiter")?;
        Some(path)
    } else {
        None
    };
    if delimiter != "--" {
        return Err("missing command delimiter".into());
    }
    let executable = args.next().ok_or("missing executable")?;
    if !std::path::Path::new(&executable).is_absolute() {
        return Err("executable must be absolute".into());
    }
    let command_args: Vec<_> = args.collect();
    // File owns the descriptor. Its close before exec is part of the protocol.
    let mut control = unsafe { std::fs::File::from_raw_fd(fd) };

    #[cfg(not(target_os = "linux"))]
    return Err("strict native backend unsupported on this platform".into());
    #[cfg(target_os = "linux")]
    {
        if let Some(path) = proxy_socket {
            return proxy_launch(executable, command_args, fd, nonce, path, control);
        }
        boundary::Prepared::new(false)?.install()?;
        let ready = format!("CTMCP_SANDBOX {PROTOCOL} {VERSION} linux-bwrap {nonce}\n");
        control.write_all(ready.as_bytes())?;
        control.flush()?;
        // Bounded exact frame. The parent closes on timeout or any invalid
        // handshake, so command execution cannot precede acceptance.
        let expected = format!("GO {nonce}\n");
        let mut gate = vec![0; expected.len()];
        control.read_exact(&mut gate)?;
        if gate != expected.as_bytes() {
            return Err("invalid launch authorization".into());
        }
        drop(control);
        let error = Command::new(executable).args(command_args).exec();
        Err(error.into())
    }
}

#[cfg(target_os = "linux")]
fn proxy_launch(
    executable: std::ffi::OsString,
    args: Vec<std::ffi::OsString>,
    fd: i32,
    nonce: String,
    path: std::ffi::OsString,
    control: std::fs::File,
) -> Result<(), Box<dyn std::error::Error>> {
    use std::io;
    use std::net::{Shutdown, TcpListener};
    use std::os::unix::net::UnixStream;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;
    use std::time::Duration;
    extern "C" {
        fn prctl(option: i32, ...) -> i32;
        fn read(fd: i32, buf: *mut u8, count: usize) -> isize;
        fn write(fd: i32, buf: *const u8, count: usize) -> isize;
        fn close(fd: i32) -> i32;
    }
    // Trusted relay is not dumpable or ptrace-accessible to a same-UID child.
    if unsafe { prctl(4, 0_usize, 0_usize, 0_usize, 0_usize) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    // Verify the host proxy is live before opening the execution gate. This
    // descriptor is closed now and is never inherited by the command.
    drop(UnixStream::connect(&path)?);
    let listener = TcpListener::bind("127.0.0.1:0")?;
    listener.set_nonblocking(true)?;
    let port = listener.local_addr()?.port();
    let ready =
        format!("CTMCP_SANDBOX {PROTOCOL} {VERSION} linux-bwrap-proxy {nonce}\n").into_bytes();
    let expected = format!("GO {nonce}\n").into_bytes();
    let mut gate = vec![0_u8; expected.len()];
    let prepared = boundary::Prepared::new(true)?;
    let proxy_url = format!("http://127.0.0.1:{port}");
    let mut command = Command::new(executable);
    command.args(args);
    for name in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ] {
        command.env(name, &proxy_url);
    }
    command.env("NO_PROXY", "").env("no_proxy", "");
    // No threads exist until spawn returns. pre_exec performs only prepared
    // seccomp installation and raw descriptor I/O, and closes the control fd.
    unsafe {
        command.pre_exec(move || {
            prepared.install()?;
            let mut sent = 0;
            while sent < ready.len() {
                let count = write(fd, ready[sent..].as_ptr(), ready.len() - sent);
                if count <= 0 {
                    return Err(io::Error::last_os_error());
                }
                sent += count as usize;
            }
            let mut got = 0;
            while got < gate.len() {
                let count = read(fd, gate[got..].as_mut_ptr(), gate.len() - got);
                if count <= 0 {
                    return Err(io::Error::from_raw_os_error(1));
                }
                got += count as usize;
            }
            if gate != expected {
                return Err(io::Error::from_raw_os_error(1));
            }
            close(fd);
            Ok(())
        });
    }
    let mut child = command.spawn()?;
    drop(command);
    drop(control);
    let active = Arc::new(AtomicUsize::new(0));
    loop {
        if let Some(status) = child.try_wait()? {
            // Exiting this PID-namespace leader's child causes bwrap's reaper
            // to dispose of ALL remaining children, including relay threads.
            std::process::exit(status.code().unwrap_or(128));
        }
        match listener.accept() {
            Ok((tcp, _)) => {
                if active.fetch_add(1, Ordering::SeqCst) >= 32 {
                    active.fetch_sub(1, Ordering::SeqCst);
                    drop(tcp);
                    continue;
                }
                let path = path.clone();
                let active = Arc::clone(&active);
                std::thread::spawn(move || {
                    let _ = (|| -> io::Result<()> {
                        let mut unix = UnixStream::connect(path)?;
                        let mut tcp_reader = tcp.try_clone()?;
                        let mut unix_writer = unix.try_clone()?;
                        let upstream = std::thread::spawn(move || {
                            let _ = io::copy(&mut tcp_reader, &mut unix_writer);
                            let _ = unix_writer.shutdown(Shutdown::Write);
                        });
                        let mut tcp_writer = tcp;
                        let _ = io::copy(&mut unix, &mut tcp_writer);
                        let _ = tcp_writer.shutdown(Shutdown::Both);
                        let _ = unix.shutdown(Shutdown::Both);
                        let _ = upstream.join();
                        Ok(())
                    })();
                    active.fetch_sub(1, Ordering::SeqCst);
                });
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(20))
            }
            Err(error) => return Err(error.into()),
        }
    }
}

fn main() {
    if env::args().nth(1).as_deref() == Some("--version") {
        println!("coding-tools-sandbox-helper {VERSION} protocol {PROTOCOL}");
        return;
    }
    #[cfg(unix)]
    if let Err(error) = launch() {
        eprintln!("coding-tools-sandbox-helper: {error}");
        std::process::exit(125);
    }
    #[cfg(not(unix))]
    {
        eprintln!("strict native backend unsupported on this platform");
        std::process::exit(125);
    }
}
