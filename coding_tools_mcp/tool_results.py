from __future__ import annotations

import json
from typing import Any


MODEL_TEXT_SAFETY_LIMIT_BYTES = (2 * 1_048_576) + 65_536


def make_tool_result(
    tool_name: str,
    payload: dict[str, Any],
    *,
    is_error: bool,
    content: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build an MCP result without mirroring structured JSON into model text.

    Normal model-facing text is sized by each tool's own per-call limits. A
    generous final safety ceiling remains as defense in depth for count-bounded
    tools whose individual entries (for example a source line) can be huge.
    """

    result_content = list(content or [])
    text = render_tool_text(tool_name, payload, is_error=is_error)
    if text:
        result_content.append(
            {"type": "text", "text": _bounded_model_text(text, tool_name)}
        )
    return {"content": result_content, "structuredContent": payload, "isError": is_error}


def render_tool_text(tool_name: str, payload: dict[str, Any], *, is_error: bool) -> str:
    if is_error or payload.get("ok") is False:
        return _render_error(payload)
    renderer = _RENDERERS.get(tool_name)
    if renderer is not None:
        return renderer(payload)
    summary = payload.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    status = payload.get("status")
    return f"{tool_name}: {status or 'completed'}."


def _render_error(payload: dict[str, Any]) -> str:
    raw_error = payload.get("error")
    error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
    code = str(error.get("code") or "TOOL_ERROR")
    message = str(error.get("message") or "Tool call failed.")
    lines = [f"{code}: {message}"]
    raw_details = error.get("details")
    details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
    retry_hint = details.get("retry_hint")
    if isinstance(retry_hint, str) and retry_hint:
        lines.append(f"Retry: {retry_hint}")
    diagnostics = payload.get("diagnostics")
    if isinstance(diagnostics, list):
        for item in diagnostics:
            if not isinstance(item, dict):
                continue
            suggestion = item.get("suggested_fix") or item.get("suggested_next_command")
            if isinstance(suggestion, str) and suggestion:
                lines.append(f"Suggested action: {suggestion}")
    return "\n".join(lines)


def _render_server_info(payload: dict[str, Any]) -> str:
    return (
        f"{payload.get('server', 'coding-tools-mcp')} {payload.get('version', 'unknown')}\n"
        f"Workspace: {payload.get('workspace', '.')}"
    )


def _render_exec_environment(payload: dict[str, Any]) -> str:
    raw_landlock = payload.get("landlock")
    landlock: dict[str, Any] = raw_landlock if isinstance(raw_landlock, dict) else {}
    state = "available" if landlock.get("available") else "unavailable"
    warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
    suffix = "\n" + "\n".join(str(item) for item in warnings) if warnings else ""
    return f"Execution environment checked. Landlock: {state}.{suffix}"


def _render_cwd(payload: dict[str, Any]) -> str:
    return f"Default working directory: {payload.get('default_cwd', '.')}"


def _render_read_file(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, str):
        return ""
    if not payload.get("truncated"):
        return content
    shown = (
        f"Showing lines {payload.get('start_line', '?')}-{payload.get('end_line', '?')}"
        f" of {payload.get('total_lines', '?')}"
    )
    next_start = payload.get("next_start_line")
    next_call = _render_next_action(payload)
    if not next_call and next_start:
        next_call = _render_tool_call(
            "read_file",
            {"path": payload.get("path", ""), "start_line": next_start},
        )
    if next_call:
        hint = f"; continue with {next_call}"
    else:
        hint = "; content truncated; raise max_bytes or request a narrower range"
    return f"[{shown}{hint}]\n{content}"


def _render_list(payload: dict[str, Any]) -> str:
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else payload.get("files")
    if not isinstance(entries, list) or not entries:
        return "No entries found."
    lines: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("name")
        kind = item.get("type")
        lines.append(f"{path}{' [' + str(kind) + ']' if kind else ''}")
    if payload.get("truncated"):
        lines.append("… results truncated; narrow the path/patterns or raise the entry limit.")
    return "\n".join(lines)


def _render_search(payload: dict[str, Any]) -> str:
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        return "No matches found."
    lines: list[str] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        path = match.get("path", "")
        line = match.get("line", "")
        column = match.get("column", "")
        preview = match.get("preview", "")
        location = f"{path}:{line}" + (f":{column}" if column else "")
        lines.append(f"{location}: {preview}")
        before = match.get("before")
        after = match.get("after")
        if isinstance(before, list):
            lines.extend(f"  {value}" for value in before)
        if isinstance(after, list):
            lines.extend(f"  {value}" for value in after)
    if payload.get("truncated"):
        total = payload.get("total_matches")
        if isinstance(total, int):
            shown = sum(1 for match in matches if isinstance(match, dict))
            exact = payload.get("total_matches_exact", True)
            lines.append(
                f"… showing {shown} of {total}{'' if exact else '+'} matches;"
                " narrow the query/path or raise max_results."
            )
        else:
            lines.append("… results truncated; narrow the query/path or raise max_results.")
    return "\n".join(lines)


def _render_patch(payload: dict[str, Any]) -> str:
    prefix = "Patch validated" if payload.get("dry_run") else "Patch applied"
    files = payload.get("affected_files")
    count = len(files) if isinstance(files, list) else 0
    changes = f" (+{payload.get('additions', 0)} -{payload.get('removals', 0)})"
    summary = str(payload.get("summary") or "").strip()
    return f"{prefix} to {count} file{'s' if count != 1 else ''}{changes}." + (
        f"\n{summary}" if summary else ""
    )


def _render_exec(payload: dict[str, Any]) -> str:
    # Decision-critical fields lead every command result so the model never
    # has to infer success from output alone.
    header = [f"Status: {payload.get('status', 'unknown')}"]
    exit_code = payload.get("exit_code")
    if exit_code is not None:
        header.append(f"exit code {exit_code}")
    if payload.get("signal"):
        header.append(f"signal {payload['signal']}")
    if payload.get("timed_out"):
        header.append("timed out")
    elapsed_ms = payload.get("elapsed_ms")
    if isinstance(elapsed_ms, (int, float)):
        header.append(f"{int(elapsed_ms)} ms")
    sections: list[str] = [" | ".join(header)]
    stdout = payload.get("stdout")
    stderr = payload.get("stderr")
    preview = payload.get("preview")
    if isinstance(stdout, str) and stdout:
        sections.append(stdout)
    if isinstance(stderr, str) and stderr:
        sections.append(f"stderr:\n{stderr}")
    if len(sections) == 1 and isinstance(preview, str) and preview:
        sections.append(preview)
    summary = payload.get("summary")
    if len(sections) == 1 and isinstance(summary, str) and summary:
        sections.append(summary)
    session_id = payload.get("session_id")
    if payload.get("status") == "running" and session_id:
        sections.append(
            f'Session still running; poll with write_stdin(session_id="{session_id}", chars="", yield_time_ms=10000).'
        )
    if payload.get("truncated"):
        continuations = _render_exec_continuations(payload)
        if continuations:
            sections.extend(continuations)
        else:
            sections.append("Output truncated; use read_output with the returned output_ref to read more.")
    return "\n".join(sections)


def _render_read_output(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, str):
        return ""
    next_offset = payload.get("next_offset")
    if next_offset is None:
        return content
    next_call = _render_next_action(payload)
    if not next_call:
        ref = payload.get("stream_output_ref") or payload.get("output_ref") or ""
        next_call = _render_tool_call("read_output", {"output_ref": ref, "offset": next_offset})
    return f"{content}\n[more: {next_call}]"


def _render_kill(payload: dict[str, Any]) -> str:
    signal_sent = payload.get("signal_sent")
    suffix = f" (signal {signal_sent})" if isinstance(signal_sent, str) and signal_sent else ""
    return f"Session {payload.get('session_id', '')}: {payload.get('status', 'completed')}{suffix}."


def _render_git_status(payload: dict[str, Any]) -> str:
    if not payload.get("is_repo", True):
        return "Not a Git repository."
    branch = payload.get("branch") or "detached"
    entries = payload.get("entries") if isinstance(payload.get("entries"), list) else []
    lines = [f"## {branch}"]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        lines.append(
            f"{entry.get('index_status', ' ')}{entry.get('worktree_status', ' ')} {entry.get('path', '')}"
        )
    if not entries:
        lines.append("Working tree clean.")
    if payload.get("truncated"):
        lines.append("… status entries truncated; narrow path or raise max_entries.")
    return "\n".join(lines)


def _render_key(payload: dict[str, Any], key: str, empty: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value else empty


def _render_git_diff(payload: dict[str, Any]) -> str:
    text = _render_key(payload, "diff", "No diff.")
    if payload.get("truncated"):
        text += "\n… diff truncated; raise max_bytes or diff specific paths."
    return text


def _render_git_show(payload: dict[str, Any]) -> str:
    text = _render_key(payload, "content", "No output.")
    if payload.get("truncated"):
        text += "\n… output truncated; raise max_bytes or narrow paths."
    return text


def _render_git_log(payload: dict[str, Any]) -> str:
    raw_commits = payload.get("commits")
    commits: list[Any] = raw_commits if isinstance(raw_commits, list) else []
    if not commits:
        return "No commits found."
    text = "\n".join(
        f"{item.get('short_hash', '')} {item.get('subject', '')}"
        for item in commits
        if isinstance(item, dict)
    )
    if payload.get("truncated"):
        next_call = _render_next_action(payload)
        text += "\n… more commits available"
        text += f"; continue with {next_call}." if next_call else "; raise max_count or use skip."
    return text


def _render_git_blame(payload: dict[str, Any]) -> str:
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        return "No blame lines found."
    rendered: list[str] = []
    for item in lines:
        if not isinstance(item, dict):
            continue
        rendered.append(
            f"{item.get('line', '')} {item.get('commit', '')} {item.get('content', '')}"
        )
    text = "\n".join(rendered)
    if payload.get("truncated"):
        next_call = _render_next_action(payload)
        text += "\n… blame lines truncated"
        text += f"; continue with {next_call}." if next_call else "; raise max_lines or advance start_line."
    return text


def _render_exec_continuations(payload: dict[str, Any]) -> list[str]:
    raw_refs = payload.get("output_refs")
    refs: dict[str, Any] = raw_refs if isinstance(raw_refs, dict) else {}
    raw_streams = payload.get("truncated_output_streams")
    streams = (
        [stream for stream in raw_streams if stream in {"stdout", "stderr"}]
        if isinstance(raw_streams, list)
        else []
    )
    if not streams:
        for stream in ("stdout", "stderr"):
            omitted = payload.get(f"{stream}_omitted_bytes")
            if payload.get(f"{stream}_truncated") or (
                isinstance(omitted, int) and omitted > 0
            ):
                streams.append(stream)
    if not streams and payload.get("preview_truncated"):
        streams.extend(stream for stream in ("stdout", "stderr") if refs.get(stream))

    continuations: list[str] = []
    seen_refs: set[str] = set()
    for stream in streams:
        ref = refs.get(stream)
        if not isinstance(ref, str) or not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        call = _render_tool_call("read_output", {"output_ref": ref, "offset": 0})
        continuations.append(f"{stream} output truncated; continue with {call}.")
    if continuations:
        return continuations

    ref = payload.get("output_ref")
    if isinstance(ref, str) and ref:
        call = _render_tool_call("read_output", {"output_ref": ref, "offset": 0})
        return [f"Output truncated; continue with {call}."]
    return []


def _render_next_action(payload: dict[str, Any]) -> str:
    raw_action = payload.get("next_action")
    if not isinstance(raw_action, dict):
        return ""
    tool = raw_action.get("tool")
    arguments = raw_action.get("arguments")
    if not isinstance(tool, str) or not isinstance(arguments, dict):
        return ""
    return _render_tool_call(tool, arguments)


def _render_tool_call(tool: str, arguments: dict[str, Any]) -> str:
    rendered = ", ".join(
        f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
        for key, value in arguments.items()
    )
    return f"{tool}({rendered})"


def _bounded_model_text(value: str, tool_name: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MODEL_TEXT_SAFETY_LIMIT_BYTES:
        return value
    suffix = (
        f"\n… {tool_name} model text reached the "
        f"{MODEL_TEXT_SAFETY_LIMIT_BYTES}-byte safety ceiling; retry with narrower paths or limits."
    )
    preview_budget = max(
        0,
        MODEL_TEXT_SAFETY_LIMIT_BYTES - len(suffix.encode("utf-8")),
    )
    preview = encoded[:preview_budget].decode("utf-8", errors="ignore")
    return preview + suffix


def _render_image(payload: dict[str, Any]) -> str:
    dimensions = ""
    if payload.get("width") and payload.get("height"):
        dimensions = f", {payload['width']}×{payload['height']}"
    return f"Image: {payload.get('path', '')} ({payload.get('mime_type', 'unknown')}{dimensions})"


_RENDERERS = {
    "server_info": _render_server_info,
    "check_exec_environment": _render_exec_environment,
    "get_default_cwd": _render_cwd,
    "set_default_cwd": _render_cwd,
    "read_file": _render_read_file,
    "list_dir": _render_list,
    "list_files": _render_list,
    "search_text": _render_search,
    "apply_patch": _render_patch,
    "exec_command": _render_exec,
    "write_stdin": _render_exec,
    "kill_session": _render_kill,
    "read_output": _render_read_output,
    "git_status": _render_git_status,
    "git_diff": _render_git_diff,
    "git_log": _render_git_log,
    "git_show": _render_git_show,
    "git_blame": _render_git_blame,
    "request_permissions": lambda payload: f"Permission request: {payload.get('status', 'completed')}.",
    "view_image": _render_image,
}
