#!/usr/bin/env python3
"""Validate wheel payload, then import and launch it outside the checkout."""
from __future__ import annotations

import argparse
import configparser
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import venv
import zipfile


def verify(source: Path) -> None:
    source = source.resolve()
    project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    if os.environ.get("EXPECTED_VERSION", version) != version:
        raise ValueError("release version and source metadata differ")
    wheels = list((source / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise ValueError("expected exactly one wheel")
    wheel = wheels[0]
    scripts = project["project"]["scripts"]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        if "coding_tools_mcp/__init__.py" not in names:
            raise ValueError("wheel is missing core runtime")
        entrypoints = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entrypoints) != 1:
            raise ValueError("wheel is missing entrypoint metadata")
        config = configparser.ConfigParser()
        config.read_string(archive.read(entrypoints[0]).decode())
        if dict(config["console_scripts"]) != scripts:
            raise ValueError("wheel entrypoints differ from pyproject.toml")
        package_data = project.get("tool", {}).get("setuptools", {}).get("package-data", {})
        roots = project.get("tool", {}).get("setuptools", {}).get("packages", {}).get("find", {}).get("where", ["."])
        for package, patterns in package_data.items():
            for pattern in patterns:
                for root in roots:
                    directory = source / root / package.replace(".", "/")
                    for path in directory.glob(pattern):
                        member = f"{package.replace('.', '/')}/{path.relative_to(directory).as_posix()}"
                        if member not in names or archive.read(member) != path.read_bytes():
                            raise ValueError(f"wheel package data differs: {member}")
    with tempfile.TemporaryDirectory(prefix="release-wheel-") as temporary:
        root = Path(temporary)
        venv.EnvBuilder(with_pip=True).create(root / "venv")
        python = root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        environment = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        environment["CODING_TOOLS_MCP_TELEMETRY"] = "off"
        subprocess.run([str(python), "-m", "pip", "install", str(wheel)], cwd=root, env=environment, check=True)
        subprocess.run([
            str(python), "-I", "-c",
            "import coding_tools_mcp, importlib.metadata, pathlib; "
            f"assert coding_tools_mcp.__version__ == {version!r}; "
            f"assert importlib.metadata.version('coding-tools-mcp') == {version!r}; "
            f"assert not pathlib.Path(coding_tools_mcp.__file__).is_relative_to({str(source)!r})",
        ], cwd=root, env=environment, check=True)
        command = python.parent / ("coding-tools-mcp.exe" if os.name == "nt" else "coding-tools-mcp")
        subprocess.run([str(command), "--help"], cwd=root, env=environment, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("."))
    args = parser.parse_args()
    verify(args.source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
