"""Structured project validation recipes executed through an injected policy boundary."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import ValidationResult


ValidationExecutor = Callable[[dict[str, Any]], Mapping[str, Any]]
MAX_VALIDATION_DIAGNOSTIC_CHARS = 4000
MAX_NODE_SCAN_FILES = 2000
SAFE_NODE_SCRIPT_RE = re.compile(r"^[A-Za-z0-9_./\\:@%+=, -]+$")


@dataclass(frozen=True)
class ValidationPlan:
    recipe: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    available: bool = True
    reason: str | None = None

    @property
    def command(self) -> str:
        if os.name == "nt":
            return subprocess.list2cmdline(list(self.argv))
        return shlex.join(self.argv)


class StructuredValidationBackend:
    """Detect and run bounded validation recipes without installing dependencies.

    ``executor`` must already be scoped to the same Workspace. In production
    LocalWorkspaceHost supplies ``Runtime.exec_command`` so existing permission,
    environment and execution policy remains authoritative.
    """

    def __init__(
        self,
        root: str | Path,
        executor: ValidationExecutor,
        *,
        which: Callable[[str], str | None] = shutil.which,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        resolved = Path(root).expanduser().resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError("validation root must be a directory")
        self.root = resolved
        self.executor = executor
        self.which = which
        self.close_callback = close_callback
        self._closed = False

    def status(self) -> dict[str, Any]:
        recipes = []
        for recipe in (
            "python:syntax",
            "python:test",
            "python:lint",
            "node:syntax",
            "node:test",
            "node:lint",
            "rust:check",
            "rust:test",
            "go:test",
        ):
            plan = self._plan(recipe)
            recipes.append(
                {
                    "recipe": recipe,
                    "available": plan.available,
                    "command": plan.command if plan.available else None,
                    "reason": plan.reason,
                }
            )
        return {
            "ok": True,
            "backend": "structured",
            "status": "ready" if any(item["available"] for item in recipes) else "unavailable",
            "recipes": recipes,
        }

    def run(self, recipe: str) -> ValidationResult:
        if self._closed:
            return ValidationResult("unavailable", recipe, reason="Validation backend is closed.")
        plan = self._plan(recipe)
        if not plan.available:
            return ValidationResult(
                "unavailable",
                recipe,
                command=None,
                reason=plan.reason or "Validation recipe is unavailable.",
            )
        started = time.monotonic()
        try:
            payload = dict(
                self.executor(
                    {
                        "cmd": plan.command,
                        "workdir": ".",
                        "env": dict(plan.env),
                        "timeout_ms": 30_000,
                        "yield_time_ms": 30_000,
                        "max_output_bytes": 64 * 1024,
                        "verbosity": "summary",
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001 - executor owns policy/error taxonomy
            return ValidationResult(
                "unavailable",
                recipe,
                command=plan.command,
                duration_ms=int((time.monotonic() - started) * 1000),
                reason=str(exc)[:300],
            )
        duration_ms = _int_value(payload.get("elapsed_ms"))
        if duration_ms is None:
            duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = _int_value(payload.get("exit_code"))
        process_status = str(payload.get("status") or "")
        if exit_code == 0 and process_status in {"", "exited", "completed"}:
            status = "passed"
        elif process_status in {"running", "starting"}:
            status = "unavailable"
        else:
            status = "failed"
        diagnostics = _diagnostics(payload) if status != "passed" else ()
        reason = "Validation command did not finish within the bounded execution window." if status == "unavailable" else None
        return ValidationResult(
            status,
            recipe,
            command=plan.command,
            exit_code=exit_code,
            diagnostics=diagnostics,
            duration_ms=duration_ms,
            reason=reason,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.close_callback is not None:
            self.close_callback()

    def _plan(self, recipe: str) -> ValidationPlan:
        if not isinstance(recipe, str) or not recipe:
            raise ValueError("recipe must be a non-empty string")
        planners = {
            "python:syntax": self._python_syntax,
            "python:test": self._python_test,
            "python:lint": self._python_lint,
            "node:syntax": self._node_syntax,
            "node:test": self._node_test,
            "node:lint": self._node_lint,
            "rust:check": lambda: self._cargo("rust:check", "check"),
            "rust:test": lambda: self._cargo("rust:test", "test"),
            "go:test": self._go_test,
        }
        planner = planners.get(recipe)
        if planner is None:
            return ValidationPlan(recipe, (), {}, False, "Unknown validation recipe.")
        return planner()

    def _python_syntax(self) -> ValidationPlan:
        if not self._has_suffix(".py"):
            return ValidationPlan("python:syntax", (), {}, False, "No Python files were detected.")
        return ValidationPlan("python:syntax", (sys.executable, "-m", "compileall", "-q", "."), {})

    def _python_test(self) -> ValidationPlan:
        if not self._has_suffix(".py"):
            return ValidationPlan("python:test", (), {}, False, "No Python files were detected.")
        if self.which("pytest") and any(
            (self.root / name).exists()
            for name in ("pytest.ini", "conftest.py", "pyproject.toml")
        ):
            return ValidationPlan("python:test", ("pytest", "-q"), {})
        if (self.root / "tests").is_dir():
            return ValidationPlan("python:test", (sys.executable, "-m", "unittest", "discover"), {})
        return ValidationPlan("python:test", (), {}, False, "No supported Python test layout was detected.")

    def _python_lint(self) -> ValidationPlan:
        if not self._has_suffix(".py"):
            return ValidationPlan("python:lint", (), {}, False, "No Python files were detected.")
        if not self.which("ruff"):
            return ValidationPlan("python:lint", (), {}, False, "ruff is not installed.")
        return ValidationPlan("python:lint", ("ruff", "check", "."), {})

    def _node_syntax(self) -> ValidationPlan:
        if not self.which("node"):
            return ValidationPlan("node:syntax", (), {}, False, "node is not installed.")
        entry = self._node_entry_file()
        if entry is None:
            return ValidationPlan("node:syntax", (), {}, False, "No JavaScript entry file was detected.")
        return ValidationPlan("node:syntax", ("node", "--check", entry), {})

    def _node_test(self) -> ValidationPlan:
        if not self.which("node"):
            return ValidationPlan("node:test", (), {}, False, "node is not installed.")
        script = self._package_script("test")
        if script is None:
            return ValidationPlan("node:test", (), {}, False, "No package test script was detected.")
        argv = _safe_node_script_argv(script)
        if not argv or argv[:2] != ("node", "--test"):
            return ValidationPlan(
                "node:test",
                (),
                {},
                False,
                "Only an explicit node --test script is allowed without package lifecycle execution.",
            )
        return ValidationPlan("node:test", argv, {})

    def _node_lint(self) -> ValidationPlan:
        if not self.which("eslint"):
            return ValidationPlan("node:lint", (), {}, False, "eslint is not installed.")
        if not (self.root / "package.json").is_file():
            return ValidationPlan("node:lint", (), {}, False, "package.json was not detected.")
        return ValidationPlan("node:lint", ("eslint", "."), {})

    def _cargo(self, recipe: str, action: str) -> ValidationPlan:
        if not (self.root / "Cargo.toml").is_file():
            return ValidationPlan(recipe, (), {}, False, "Cargo.toml was not detected.")
        if not self.which("cargo"):
            return ValidationPlan(recipe, (), {}, False, "cargo is not installed.")
        return ValidationPlan(
            recipe,
            ("cargo", action, "--offline"),
            {"CARGO_NET_OFFLINE": "true"},
        )

    def _go_test(self) -> ValidationPlan:
        if not (self.root / "go.mod").is_file():
            return ValidationPlan("go:test", (), {}, False, "go.mod was not detected.")
        if not self.which("go"):
            return ValidationPlan("go:test", (), {}, False, "go is not installed.")
        return ValidationPlan(
            "go:test",
            ("go", "test", "./..."),
            {"GOPROXY": "off", "GOSUMDB": "off"},
        )

    def _package_script(self, name: str) -> str | None:
        path = self.root / "package.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        value = scripts.get(name) if isinstance(scripts, dict) else None
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _node_entry_file(self) -> str | None:
        package = self.root / "package.json"
        candidates: list[str] = []
        if package.is_file():
            try:
                payload = json.loads(package.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                for field in ("main", "module"):
                    value = payload.get(field)
                    if isinstance(value, str):
                        candidates.append(value)
        candidates.extend(("index.js", "index.mjs", "index.cjs"))
        for candidate in candidates:
            path = self.root / candidate
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(self.root)
            except (OSError, RuntimeError, ValueError):
                continue
            if resolved.is_file() and resolved.suffix.lower() in {".js", ".mjs", ".cjs"}:
                return resolved.relative_to(self.root).as_posix()
        scanned = 0
        for path in self.root.rglob("*.js"):
            scanned += 1
            if scanned > MAX_NODE_SCAN_FILES:
                break
            if "node_modules" in path.parts:
                continue
            try:
                return path.relative_to(self.root).as_posix()
            except ValueError:
                continue
        return None

    def _has_suffix(self, suffix: str) -> bool:
        scanned = 0
        for path in self.root.rglob(f"*{suffix}"):
            scanned += 1
            if scanned > MAX_NODE_SCAN_FILES:
                break
            if any(part in {".git", "node_modules", ".venv", "venv", "target"} for part in path.parts):
                continue
            return True
        return False


def _safe_node_script_argv(script: str) -> tuple[str, ...] | None:
    if len(script) > 500 or SAFE_NODE_SCRIPT_RE.fullmatch(script) is None:
        return None
    try:
        parts = tuple(shlex.split(script, posix=True))
    except ValueError:
        return None
    return parts or None


def _int_value(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _diagnostics(payload: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    text = ""
    for key in ("stderr", "stdout", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            text = value
            break
    if not text:
        return ()
    return (
        {
            "message": text[-MAX_VALIDATION_DIAGNOSTIC_CHARS:],
        },
    )


__all__ = ["StructuredValidationBackend", "ValidationPlan"]
