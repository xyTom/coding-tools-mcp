#!/usr/bin/env python3
"""Read-only registry verification and staging of missing immutable packages.

Only a real HTTP 404 means absent. Existing files must contain exactly the same
files, modes and bytes; container timestamps/compression may differ on rebuild.
No publication happens here, and conflicting versions are never overwritten.
"""
from __future__ import annotations

import argparse
from email.parser import BytesParser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile


MAX_ARCHIVE = 64 * 1024 * 1024


def download(url: str, *, absent_ok: bool = False) -> bytes | None:
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("registry artifacts must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "coding-tools-mcp-release/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = response.read(MAX_ARCHIVE + 1)
    except urllib.error.HTTPError as error:
        if absent_ok and error.code == 404:
            return None
        raise
    if len(result) > MAX_ARCHIVE:
        raise ValueError("registry response exceeds size limit")
    return result


def archive_contents(name: str, data: bytes) -> dict[str, tuple[str, bool]]:
    """Canonical payload without unsafe extraction or timestamp comparisons."""
    result: dict[str, tuple[str, bool]] = {}
    total = 0

    def add(path: str, payload: bytes, executable: bool) -> None:
        nonlocal total
        value = PurePosixPath(path)
        if value.is_absolute() or ".." in value.parts or "\\" in path:
            raise ValueError(f"unsafe archive member: {path}")
        if path in result:
            raise ValueError(f"duplicate archive member: {path}")
        total += len(payload)
        if total > MAX_ARCHIVE:
            raise ValueError("expanded archive exceeds size limit")
        result[path] = (hashlib.sha256(payload).hexdigest(), executable)

    if name.endswith(".whl"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                if member.file_size > MAX_ARCHIVE:
                    raise ValueError("expanded archive member exceeds size limit")
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("archive symlinks are not supported")
                add(member.filename, archive.read(member), bool(mode & 0o111))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar_archive:
            for tar_member in tar_archive:
                if tar_member.isdir():
                    continue
                if not tar_member.isfile() or tar_member.size > MAX_ARCHIVE:
                    raise ValueError("archive links/special files or oversized members are not supported")
                stream = tar_archive.extractfile(tar_member)
                assert stream is not None
                add(tar_member.name, stream.read(MAX_ARCHIVE + 1), bool(tar_member.mode & 0o111))
    if not result:
        raise ValueError("empty package archive")
    return result


def verify_payload(path: Path, remote: bytes, digest: str = "", algorithm: str = "sha256") -> None:
    if digest and hashlib.new(algorithm, remote).hexdigest() != digest:
        raise ValueError(f"registry digest mismatch for {path.name}")
    if archive_contents(path.name, path.read_bytes()) != archive_contents(path.name, remote):
        raise ValueError(f"immutable package conflict: {path.name}; bump the version, never delete/reuse it")


def validate_identity(path: Path, kind: str, version: str) -> None:
    archive_contents(path.name, path.read_bytes())
    if kind == "npm":
        with tarfile.open(path) as archive:
            stream = archive.extractfile("package/package.json")
            if stream is None:
                raise ValueError("npm archive has no package.json")
            metadata = json.load(stream)
            name, actual = metadata.get("name"), metadata.get("version")
    else:
        if path.name.endswith(".whl"):
            if not re.fullmatch(rf"coding_tools_mcp-{re.escape(version)}-[^-]+-[^-]+-[^-]+\.whl", path.name):
                raise ValueError(f"unexpected wheel filename: {path.name}")
            with zipfile.ZipFile(path) as wheel_archive:
                data = wheel_archive.read(f"coding_tools_mcp-{version}.dist-info/METADATA")
        else:
            if path.name != f"coding_tools_mcp-{version}.tar.gz":
                raise ValueError(f"unexpected sdist filename: {path.name}")
            with tarfile.open(path) as source_archive:
                stream = source_archive.extractfile(f"coding_tools_mcp-{version}/PKG-INFO")
                if stream is None:
                    raise ValueError("sdist has no PKG-INFO")
                data = stream.read()
        parsed = BytesParser().parsebytes(data)
        name, actual = parsed.get("Name"), parsed.get("Version")
    if name != "coding-tools-mcp" or actual != version:
        raise ValueError(f"unexpected package identity in {path.name}: {name} {actual}")


def registry_state(kind: str, directory: Path, version: str) -> dict[str, Any]:
    from scripts.release_plan import stable_version

    stable_version(version)
    paths = sorted(directory.glob("*.whl")) + sorted(directory.glob("*.tar.gz")) if kind == "pypi" else sorted(directory.glob("*.tgz"))
    expected = 2 if kind == "pypi" else 1
    if len(paths) != expected or (kind == "pypi" and not (len(list(directory.glob("*.whl"))) == len(list(directory.glob("*.tar.gz"))) == 1)):
        raise ValueError(f"expected {expected} {kind} artifacts, found {[p.name for p in paths]}")
    for path in paths:
        validate_identity(path, kind, version)
    present: list[str] = []
    missing: list[str] = []
    if kind == "pypi":
        data = download(f"https://pypi.org/pypi/coding-tools-mcp/{version}/json", absent_ok=True)
        published = {} if data is None else {item["filename"]: item for item in json.loads(data)["urls"]}
        extra = set(published) - {path.name for path in paths}
        if extra:
            raise ValueError(f"unexpected distributions for version {version}: {sorted(extra)}")
        for path in paths:
            if f"-{version}" not in path.name:
                raise ValueError(f"artifact version does not match {version}: {path.name}")
            item = published.get(path.name)
            if item is None:
                missing.append(path.name)
                continue
            if item.get("yanked"):
                raise ValueError(f"refusing yanked distribution: {path.name}")
            remote = download(item["url"])
            assert remote is not None
            verify_payload(path, remote, item["digests"]["sha256"])
            present.append(path.name)
    else:
        path = paths[0]
        data = download(f"https://registry.npmjs.org/coding-tools-mcp/{version}", absent_ok=True)
        if data is None:
            missing.append(path.name)
        else:
            metadata = json.loads(data)
            if metadata.get("name") != "coding-tools-mcp" or metadata.get("version") != version:
                raise ValueError("npm returned unexpected package identity")
            if metadata.get("deprecated"):
                raise ValueError("refusing deprecated npm version")
            remote = download(metadata["dist"]["tarball"])
            assert remote is not None
            verify_payload(path, remote, metadata["dist"]["shasum"], "sha1")
            present.append(path.name)
    state = "complete" if not missing else "partial" if present else "absent"
    return {
        "registry": kind, "version": version, "state": state, "present": present, "missing": missing,
        "artifacts": [{"filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in paths],
    }


def npm_publish_tag(version: str) -> str:
    """Called under the repository-wide npm publication lock, before publish."""
    from scripts.release_plan import stable_version

    requested = stable_version(version)
    data = download("https://registry.npmjs.org/coding-tools-mcp/latest", absent_ok=True)
    if data is None:
        return "latest"
    latest = json.loads(data)
    if latest.get("name") != "coding-tools-mcp":
        raise ValueError("npm returned unexpected latest package identity")
    # Unexpected prerelease/custom latest values require an operator decision.
    return "latest" if requested >= stable_version(latest["version"]) else f"release-{version}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", choices=["pypi", "npm"])
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--stage", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--select-publish-tag", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    deadline = time.monotonic() + args.wait_seconds
    while True:
        result = registry_state(args.registry, args.directory, args.version)
        if not args.require_complete or result["state"] == "complete" or time.monotonic() >= deadline:
            break
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    if args.select_publish_tag:
        if args.registry != "npm":
            raise ValueError("publish tags apply only to npm")
        result["publish_tag"] = npm_publish_tag(args.version) if result["missing"] else ""
    print(json.dumps(result, sort_keys=True))
    if args.report:
        args.report.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.require_complete and result["state"] != "complete":
        raise SystemExit(f"{args.registry} release is {result['state']}; finalization is blocked")
    if args.stage:
        args.stage.mkdir(parents=True, exist_ok=False)
        for name in result["missing"]:
            shutil.copyfile(args.directory / name, args.stage / name)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            stream.write(f"missing={len(result['missing'])}\nstate={result['state']}\n")
            if "publish_tag" in result:
                stream.write(f"publish_tag={result['publish_tag']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
