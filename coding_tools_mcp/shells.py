"""Windows command-shell compatibility, not an operating-system sandbox.

Selectively adapted from #37/#69: Frederick Daell Lied Diaz (73ffb99) and
cf-pages (784404c, b8d8bb3). Keep process output/lifetime handling in processes.
Shell discovery deliberately never executes an executable from workspace PATH.
"""
from __future__ import annotations

import ctypes
import ntpath
import os
import re
import stat
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .errors import ToolFailure

PWSH_PATH_ENV = "CODING_TOOLS_MCP_PWSH_PATH"
WINDOWS_SHELL_ENV = "CODING_TOOLS_MCP_WINDOWS_SHELL"
CMD_FALLBACK_ENV = "CODING_TOOLS_MCP_ALLOW_CMD_FALLBACK"
WINDOWS_CORE_ENV_NAMES = frozenset({"PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR"})
CMD_FALLBACK_WARNING = (
    "PowerShell 7 is unavailable; commands use the cmd.exe compatibility fallback. "
    "Use cmd.exe syntax or install an administrator-managed PowerShell 7."
)


@dataclass(frozen=True)
class WindowsCommandShell:
    kind: Literal["pwsh", "cmd"]
    executable: str
    fallback: bool = False
    fallback_reason: str | None = None
    warning: str | None = None


def environment_value(env: Mapping[str, str], name: str) -> str | None:
    """Windows names are case-insensitive; last update wins deterministically."""
    result = None
    for key, value in env.items():
        if key.upper() == name.upper():
            result = value
    return result


def merge_environment(*sources: Mapping[str, str], windows: bool = False) -> dict[str, str]:
    result: dict[str, str] = {}
    spellings: dict[str, str] = {}
    for source in sources:
        for key, value in source.items():
            canonical = key.upper() if windows else key
            previous = spellings.get(canonical)
            if previous is not None:
                result.pop(previous)
            spellings[canonical] = key
            result[key] = value
    return result


def is_windows_loader_env_name(name: str) -> bool:
    upper = name.upper()
    return upper in {
        "PSMODULEPATH", "PSMODULEANALYSISCACHEPATH", "__COMPAT_LAYER",
        "DOTNET_STARTUP_HOOKS", "DOTNET_ADDITIONAL_DEPS", "DOTNET_SHARED_STORE",
    } or upper.startswith(("COR_", "CORECLR_", "COMPLUS_", "DOTNET_ROOT"))


def sanitized_environment(
    *sources: Mapping[str, str],
    windows: bool = False,
    is_filtered: Callable[[str, str], bool],
) -> dict[str, str]:
    """Apply credential/loader filtering after *all* configured and request merges."""
    return {key: value for key, value in merge_environment(*sources, windows=windows).items()
            if not is_filtered(key, value) and not (windows and is_windows_loader_env_name(key))}


def windows_pathext(env: Mapping[str, str]) -> tuple[str, ...]:
    raw = environment_value(env, "PATHEXT")
    if raw is None:
        raw = ".COM;.EXE;.BAT;.CMD"
    return tuple(dict.fromkeys(
        extension.strip().upper() for extension in raw.split(";")
        if re.fullmatch(r"\.[A-Za-z0-9]+", extension.strip())
    ))


def _failure(code: str, message: str, executable: str = "", **details: Any) -> ToolFailure:
    return ToolFailure(code, message, category="runtime",
                       details={"executable": executable, **details})


def _native_installation_paths() -> tuple[tuple[str, ...], str]:
    """Get installation directories from Windows, never request/host environment."""
    if os.name != "nt":
        raise _failure("SHELL_NOT_FOUND", "Windows shell discovery requires native Windows.")
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel32.GetSystemDirectoryW.restype = wintypes.UINT
    shell32.SHGetFolderPathW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                                       wintypes.DWORD, wintypes.LPWSTR]
    shell32.SHGetFolderPathW.restype = ctypes.c_long
    buf = ctypes.create_unicode_buffer(32768)
    size = kernel32.GetSystemDirectoryW(buf, len(buf))
    if not size or size >= len(buf):
        raise _failure("SHELL_UNTRUSTED", "The Windows system directory could not be verified.")
    system_directory = buf.value
    roots: list[str] = []
    # CSIDL_PROGRAM_FILES / CSIDL_PROGRAM_FILESX86. A 32-bit server only sees
    # its available installation roots; an explicit pin cannot bypass trust.
    for folder in (0x0026, 0x002A):
        if shell32.SHGetFolderPathW(None, folder, None, 0, buf) == 0 and buf.value:
            if ntpath.normcase(buf.value) not in {ntpath.normcase(p) for p in roots}:
                roots.append(buf.value)
    return tuple(roots), system_directory


def _is_within(path: str, parent: str) -> bool:
    try:
        return ntpath.commonpath((ntpath.normcase(path), ntpath.normcase(parent))) == ntpath.normcase(parent)
    except ValueError:
        return False


def _path_chain(path: str) -> list[str]:
    chain = [path]
    while (parent := ntpath.dirname(chain[-1])) != chain[-1]:
        chain.append(parent)
    return chain


def _protected_windows_acl(path: str, *, executable_directory: bool) -> bool:
    """Conservatively reject writable non-administrator owners/allow ACEs.

    Unlike os.access on Windows, this inspects the DACL. Unknown/object/callback
    ACEs fail closed. SYSTEM, Administrators and TrustedInstaller are trusted
    installation administrators, not adversaries native compatibility isolates.
    """
    from ctypes import wintypes

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD),
                    ("AclBytesFree", wintypes.DWORD)]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE),
                    ("AceSize", wintypes.WORD)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    pointer = ctypes.c_void_p
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD,
                                           ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer),
                                           pointer, ctypes.POINTER(pointer)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetAclInformation.argtypes = [pointer, pointer, wintypes.DWORD, ctypes.c_int]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [pointer]
    kernel32.LocalFree.restype = pointer
    trusted = {
        "S-1-5-18", "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }

    def sid_string(sid: Any) -> str:
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError("Cannot inspect installation owner/ACL SID")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)

    owner, dacl, descriptor = pointer(), pointer(), pointer()
    # SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
    result = advapi.GetNamedSecurityInfoW(path, 1, 5, ctypes.byref(owner), None,
                                        ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if result:
        return False
    try:
        if not owner or sid_string(owner) not in trusted or not dacl:
            return False
        information = ACL_SIZE_INFORMATION()
        if not advapi.GetAclInformation(dacl, ctypes.byref(information), ctypes.sizeof(information), 2):
            return False
        # Generic ALL/WRITE, DELETE, WRITE_DAC/OWNER, FILE_DELETE_CHILD.
        # On the executable and its directory, also forbid modification/new
        # sibling DLLs. Ancestors may permit creating unrelated directories.
        mutation = 0x500D0040
        if executable_directory or not os.path.isdir(path):
            mutation |= 0x00000116  # WRITE_DATA/ADD_FILE, APPEND, WRITE_EA, WRITE_ATTRIBUTES.
        for index in range(information.AceCount):
            ace = pointer()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                return False
            header = ctypes.cast(ace, ctypes.POINTER(ACE_HEADER)).contents
            if header.AceFlags & 0x08:  # INHERIT_ONLY_ACE does not apply here.
                continue
            if header.AceType == 1:  # ACCESS_DENIED_ACE can only restrict.
                continue
            if header.AceType != 0 or header.AceSize < 12:
                return False
            address = int(ace.value or 0)
            mask = ctypes.c_uint32.from_address(address + 4).value
            if mask & mutation and sid_string(pointer(address + 8)) not in trusted:
                return False
        return True
    finally:
        kernel32.LocalFree(descriptor)


def validate_windows_executable(
    executable: str,
    *,
    workspace: str,
    kind: Literal["pwsh", "cmd"],
    installation_paths: tuple[tuple[str, ...], str] | None = None,
) -> str:
    """Validate trust *before* probing. PATH/cwd never establish shell trust."""
    drive, _ = ntpath.splitdrive(executable)
    if (not re.fullmatch(r"[A-Za-z]:", drive) or not ntpath.isabs(executable)
            or ntpath.basename(executable).lower() != f"{kind}.exe"
            or any(char in executable for char in ('\x00', '"', '%'))):
        raise _failure("SHELL_UNTRUSTED", "The shell must have an absolute local executable path.", executable)
    candidate = ntpath.normpath(executable)
    roots, system_directory = installation_paths or _native_installation_paths()
    permitted = any(_is_within(candidate, root) for root in roots) if kind == "pwsh" else (
        ntpath.normcase(candidate) == ntpath.normcase(ntpath.join(system_directory, "cmd.exe"))
    )
    if not permitted:
        raise _failure("SHELL_UNTRUSTED", "The shell is not in an administrator-managed installation location.", candidate)
    workspace_real = ntpath.normpath(os.path.realpath(workspace))
    if _is_within(candidate, ntpath.normpath(workspace)) or _is_within(candidate, workspace_real):
        raise _failure("SHELL_UNTRUSTED", "A workspace executable cannot be used as the server shell.", candidate)
    try:
        chain = _path_chain(candidate)
        for index, component in enumerate(chain):
            info = os.lstat(component)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise _failure("SHELL_UNTRUSTED", "Shell installation paths cannot contain reparse points or junctions.", candidate)
            if index == 0 and not stat.S_ISREG(info.st_mode):
                raise _failure("SHELL_NOT_FOUND", "The configured shell is not a regular executable file.", candidate)
        canonical = ntpath.normpath(os.path.realpath(candidate))
        if ntpath.normcase(canonical) != ntpath.normcase(candidate):
            raise _failure("SHELL_UNTRUSTED", "The shell installation resolves to a different path.", candidate)
        for index, component in enumerate(chain):
            if not _protected_windows_acl(component, executable_directory=index == 1):
                raise _failure("SHELL_UNTRUSTED", "The shell or an ancestor can be replaced by a non-administrator.", candidate)
    except OSError as exc:
        raise _failure("SHELL_NOT_FOUND", "The shell installation could not be inspected.", candidate,
                       reason=str(exc)) from exc
    return candidate


def _pwsh_probe_environment(system_directory: str) -> dict[str, str]:
    root = ntpath.dirname(system_directory)
    # No server credentials, user profile, PATH entries or loader/module vars.
    return {"SYSTEMROOT": root, "WINDIR": root, "PATH": system_directory,
            "COMSPEC": ntpath.join(system_directory, "cmd.exe"), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}


def pwsh_major_version(executable: str, *, system_directory: str) -> int:
    """Only call after validate_windows_executable; never cache a version by path."""
    try:
        completed = subprocess.run(
            build_pwsh_argv(executable, "$PSVersionTable.PSVersion.Major"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=False, timeout=5, check=False,
            env=_pwsh_probe_environment(system_directory), cwd=ntpath.dirname(executable),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _failure("SHELL_VERSION_UNSUPPORTED", "PowerShell version could not be verified.", executable,
                       reason=str(exc)) from exc
    stdout = completed.stdout.strip()
    if completed.returncode or not re.fullmatch(rb"[0-9]+", stdout):
        raise _failure("SHELL_VERSION_UNSUPPORTED", "PowerShell version could not be verified.", executable,
                       exit_code=completed.returncode)
    return int(stdout)


def _configured_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = environment_value(env, name)
    if value is None:
        return default
    if value.lower().strip() in {"true", "1", "yes", "on"}:
        return True
    if value.lower().strip() in {"false", "0", "no", "off"}:
        return False
    raise _failure("SHELL_CONFIGURATION_ERROR", f"{name} must be true or false.")


def resolve_windows_command_shell(
    workspace: str,
    *,
    allow_cmd_fallback: bool | None = None,
) -> WindowsCommandShell:
    env = os.environ  # Only trusted server configuration, never per-command env.
    configured = environment_value(env, PWSH_PATH_ENV)
    mode = (environment_value(env, WINDOWS_SHELL_ENV) or "auto").strip().lower()
    if mode not in {"auto", "pwsh", "cmd"}:
        raise _failure("SHELL_CONFIGURATION_ERROR", f"{WINDOWS_SHELL_ENV} must be auto, pwsh or cmd.")
    if configured is not None and (not configured.strip() or mode == "cmd"):
        raise _failure("SHELL_CONFIGURATION_ERROR", f"{PWSH_PATH_ENV} must be nonempty and cannot be combined with cmd mode.")
    allowed = _configured_bool(env, CMD_FALLBACK_ENV, True) if allow_cmd_fallback is None else allow_cmd_fallback
    installation_paths = _native_installation_paths()
    roots, system_directory = installation_paths
    reason: str | None = None
    if mode != "cmd":
        candidates = [configured] if configured is not None else [ntpath.join(root, "PowerShell", "7", "pwsh.exe") for root in roots]
        reason = "SHELL_NOT_FOUND"
        for path in candidates:
            try:
                candidate = validate_windows_executable(path, workspace=workspace, kind="pwsh", installation_paths=installation_paths)
                major = pwsh_major_version(candidate, system_directory=system_directory)
                if major < 7:
                    raise _failure("SHELL_VERSION_UNSUPPORTED", "PowerShell 7 or newer is required; Windows PowerShell 5.1 is unsupported.", candidate, major_version=major)
                return WindowsCommandShell("pwsh", candidate)
            except ToolFailure as exc:
                if configured is not None:
                    raise
                reason = exc.code
        if mode == "pwsh" or not allowed:
            raise _failure(reason, "A trusted PowerShell 7 installation is required; cmd fallback is disabled.")
    candidate = validate_windows_executable(ntpath.join(system_directory, "cmd.exe"), workspace=workspace,
                                            kind="cmd", installation_paths=installation_paths)
    return WindowsCommandShell("cmd", candidate, fallback=mode != "cmd",
                               fallback_reason=reason if mode != "cmd" else "configured_cmd",
                               warning=CMD_FALLBACK_WARNING if mode != "cmd" else None)


_selected_lock = threading.Lock()
_selected_shells: dict[tuple[str, bool | None, tuple[str | None, ...]], WindowsCommandShell] = {}


def selected_windows_command_shell(
    workspace: str,
    *,
    refresh: bool = False,
    allow_cmd_fallback: bool | None = None,
) -> WindowsCommandShell:
    config = tuple(environment_value(os.environ, name) for name in (PWSH_PATH_ENV, WINDOWS_SHELL_ENV, CMD_FALLBACK_ENV))
    key = (ntpath.normcase(ntpath.normpath(workspace)), allow_cmd_fallback, config)
    with _selected_lock:
        if refresh or key not in _selected_shells:
            selected = resolve_windows_command_shell(workspace, allow_cmd_fallback=allow_cmd_fallback)
            # Bound server-wide state when independent workspaces are served.
            if len(_selected_shells) >= 32:
                _selected_shells.clear()
            _selected_shells[key] = selected
        else:
            selected = _selected_shells[key]
            # A cached version/selection never bypasses the filesystem checks.
            validate_windows_executable(selected.executable, workspace=workspace, kind=selected.kind)
        return selected


def build_pwsh_argv(executable: str, command: str) -> list[str]:
    return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]


def build_cmd_command_line(executable: str, command: str) -> str:
    """cmd /S /C takes raw payload quoting, not CRT list2cmdline escaping."""
    return f'"{executable}" /D /V:OFF /S /C "{command}"'


def shell_command(command: str, selected: WindowsCommandShell) -> list[str] | str:
    return build_pwsh_argv(selected.executable, command) if selected.kind == "pwsh" else build_cmd_command_line(selected.executable, command)


def windows_command_shell_payload(shell: WindowsCommandShell) -> dict[str, Any]:
    result: dict[str, Any] = {"available": True, "kind": shell.kind, "executable": shell.executable,
                              "fallback": shell.fallback}
    if shell.fallback_reason is not None:
        result["fallback_reason"] = shell.fallback_reason
    if shell.warning is not None:
        result["warning"] = shell.warning
    return result


def powershell_scannable(command: str) -> str:
    """Mask single-quoted literals using PowerShell quote semantics.

    This is a diagnostic preflight, never a security boundary or full parser.
    In particular single quotes *inside double quotes* are ordinary text.
    """
    result = list(command)
    i = 0
    double = False
    while i < len(command):
        if command[i] == "`":
            i += 2
            continue
        if command[i] == '"':
            double = not double
            i += 1
            continue
        if not double and command[i:i + 2] == "@'" and command[i + 2:i + 3] in {"\n", "\r"}:
            end = re.search(r"(?m)^'@", command[i + 2:])
            stop = len(command) if end is None else i + 2 + end.end()
            result[i:stop] = " " * (stop - i)
            i = stop
            continue
        if not double and command[i] == "'":
            start = i
            i += 1
            while i < len(command):
                if command[i] == "'":
                    if command[i:i + 2] == "''":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            result[start:i] = " " * (i - start)
            continue
        i += 1
    return "".join(result)


_POWERSHELL_DYNAMIC = re.compile(
    r"(?P<expansion>\$)|(?P<escape>`)|(?P<expression>[({])"
    r"|(?P<splatting>(?:^|[\s;&|(){},=])@)|(?P<static_member>::)"
    r"|(?P<call_operator>(?:^|[;|(){}\r\n]|&&|\|\|)\s*(?:&(?!&)|\.)\s)"
    r"|(?P<dynamic_eval>\b(?:Invoke-Expression|iex|Invoke-Command|icm|New-Object|Add-Type|Set-Alias|New-Alias|sal|nal)\b)", re.I,
)
_CMD_DYNAMIC = re.compile(r"(?P<expansion>%[^%\r\n]+%|![^!\r\n]+!)|(?P<escape>\^)"
                          r"|(?P<dynamic_eval>(?:^|[&|()\r\n])\s*(?:call|for)\b)", re.I)


def powershell_dynamic_construct(command: str) -> str | None:
    match = _POWERSHELL_DYNAMIC.search(powershell_scannable(command))
    return match.lastgroup if match else None


def cmd_dynamic_construct(command: str) -> str | None:
    match = _CMD_DYNAMIC.search(command)
    return match.lastgroup if match else None
