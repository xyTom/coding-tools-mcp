from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import yaml

from scripts.finalize_release import inspect, main as finalize_main, notes, require_workflow_token_compatibility
from scripts.release_artifacts import archive_contents, download, main as artifacts_main, registry_state, verify_payload, npm_publish_tag
from scripts.release_plan import select_release


ROOT = Path(__file__).resolve().parents[1]


class ReleaseSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Release test")
        self.git("config", "user.email", "release@example.invalid")
        (self.root / "packages/npm-launcher").mkdir(parents=True)
        (self.root / "packages/npm-launcher/package.json").write_text('{"version":"0.1.0"}')
        self.initial = self.commit("0.5.0")

    def git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.root, text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self, version: str, label: str = "") -> str:
        (self.root / "pyproject.toml").write_text(f'[project]\nversion="{version}"\n')
        (self.root / "note").write_text(label)
        self.git("add", ".")
        self.git("commit", "--allow-empty", "-qm", label or version)
        return self.git("rev-parse", "HEAD")

    def test_non_version_push_is_noop(self) -> None:
        head = self.commit("0.5.0", "docs")
        self.assertEqual(select_release(self.root, head, self.initial), {"publish": "false"})

    def test_multi_commit_push_and_recovery_select_identical_boundary(self) -> None:
        source = self.commit("0.5.1")
        head = self.commit("0.5.1", "later docs")
        automatic = select_release(self.root, head, self.initial)
        recovery = select_release(self.root, head, requested="v0.5.1")
        self.assertEqual(automatic, recovery)
        self.assertEqual(automatic["source_sha"], source)
        self.assertNotEqual(source, head)

    def test_squash_merge_and_normal_merge_use_main_boundary(self) -> None:
        self.git("checkout", "-qb", "version-pr")
        branch_commit = self.commit("0.5.1")
        self.git("checkout", "-q", "-")
        self.git("merge", "--no-ff", "-qm", "Merge version PR", "version-pr")
        merge = self.git("rev-parse", "HEAD")
        self.assertNotEqual(branch_commit, merge)
        self.assertEqual(select_release(self.root, merge, self.initial)["source_sha"], merge)

    def test_recovery_after_next_release_uses_old_boundary(self) -> None:
        source = self.commit("0.5.1")
        head = self.commit("0.6.0")
        self.assertEqual(select_release(self.root, head, requested="0.5.1")["source_sha"], source)

    def test_multiple_bumps_in_batch_fail_without_silently_dropping_release(self) -> None:
        self.commit("0.5.1")
        head = self.commit("0.6.0")
        with self.assertRaisesRegex(ValueError, "multiple"):
            select_release(self.root, head, self.initial)

    def test_batched_bump_then_revert_is_not_a_non_version_push(self) -> None:
        self.commit("0.5.1")
        head = self.commit("0.5.0", "revert version")
        with self.assertRaisesRegex(ValueError, "multiple version changes"):
            select_release(self.root, head, self.initial)

    def test_reintroduced_version_is_ambiguous(self) -> None:
        self.commit("0.5.1")
        self.commit("0.5.0")
        head = self.commit("0.5.1")
        with self.assertRaisesRegex(ValueError, "ambiguity"):
            select_release(self.root, head, requested="0.5.1")

    def test_sealed_version_and_prerelease_are_rejected(self) -> None:
        for version in ("0.5.0", "0.2.0", "0.6.0rc1", "$(id)"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                select_release(self.root, self.initial, requested=version)

    def test_history_before_pyproject_is_supported(self) -> None:
        self.git("checkout", "--orphan", "old-root")
        self.git("rm", "-rf", ".")
        (self.root / "README.md").write_text("Old repository")
        self.git("add", ".")
        self.git("commit", "-qm", "Before Python packaging")
        (self.root / "packages/npm-launcher").mkdir(parents=True)
        (self.root / "packages/npm-launcher/package.json").write_text('{"version":"0.1.0"}')
        before = self.commit("0.5.0")
        source = self.commit("0.5.1")
        self.assertEqual(select_release(self.root, source, before)["source_sha"], source)

    def test_initial_or_force_push_is_rejected(self) -> None:
        head = self.commit("0.5.1")
        for before in ("", "0" * 40, "f" * 40):
            with self.assertRaises(ValueError):
                select_release(self.root, head, before)


def wheel(payload: bytes = b"same", date: tuple[int, ...] = (2026, 1, 1, 0, 0, 0)) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(zipfile.ZipInfo("coding_tools_mcp/__init__.py", date), payload)
        archive.writestr(zipfile.ZipInfo("coding_tools_mcp-0.5.1.dist-info/METADATA", date), "Name: coding-tools-mcp\nVersion: 0.5.1\n")
    return stream.getvalue()


def tar(payload: bytes = b"same", mtime: int = 1, path: str = "package/index.js", mode: int = 0o644) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo(path)
        info.size = len(payload)
        info.mtime = mtime
        info.mode = mode
        archive.addfile(info, io.BytesIO(payload))
        for name, content in {
            "package/package.json": b'{"name":"coding-tools-mcp","version":"0.1.0"}',
            "coding_tools_mcp-0.5.1/PKG-INFO": b"Name: coding-tools-mcp\nVersion: 0.5.1\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = mtime
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(stream.getvalue(), mtime=0)


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wheel = self.root / "coding_tools_mcp-0.5.1-py3-none-any.whl"
        self.sdist = self.root / "coding_tools_mcp-0.5.1.tar.gz"
        self.wheel.write_bytes(wheel())
        self.sdist.write_bytes(tar())

    def test_same_payload_different_timestamps_are_valid(self) -> None:
        verify_payload(self.wheel, wheel(date=(2025, 1, 1, 0, 0, 0)))
        verify_payload(self.sdist, tar(mtime=999))

    def test_content_mode_and_hash_conflicts_are_rejected(self) -> None:
        for data in (tar(b"different"), tar(mode=0o755)):
            with self.assertRaisesRegex(ValueError, "immutable package conflict"):
                verify_payload(self.sdist, data)
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            verify_payload(self.wheel, wheel(), "f" * 64)

    def test_unsafe_or_duplicate_archive_members_are_rejected(self) -> None:
        for name in ("../escape", "/absolute", "package/../../escape", "package\\escape"):
            with self.assertRaises(ValueError):
                archive_contents("test.tgz", tar(path=name))
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for _ in range(2):
                info = tarfile.TarInfo("same")
                archive.addfile(info, io.BytesIO())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            archive_contents("test.tgz", stream.getvalue())

    def pypi_metadata(self, paths: list[Path]) -> bytes:
        return json.dumps({"urls": [{
            "filename": path.name, "url": f"https://files.pythonhosted.org/{path.name}",
            "digests": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
        } for path in paths]}).encode()

    def test_pypi_absent_partial_and_complete(self) -> None:
        for paths, expected in (([], "absent"), ([self.wheel], "partial"), ([self.wheel, self.sdist], "complete")):
            with self.subTest(expected=expected):
                metadata = self.pypi_metadata(paths) if paths else None
                def fetch(url: str, **kwargs: object) -> bytes | None:
                    return metadata if url.endswith("/json") else (self.root / url.rsplit("/", 1)[1]).read_bytes()
                with patch("scripts.release_artifacts.download", side_effect=fetch):
                    state = registry_state("pypi", self.root, "0.5.1")
                self.assertEqual(state["state"], expected)
                self.assertEqual(len(state["present"]), len(paths))

    def test_pypi_conflicting_existing_file_blocks_recovery(self) -> None:
        metadata = self.pypi_metadata([self.wheel])
        self.wheel.write_bytes(wheel(b"changed after first publish"))
        with patch("scripts.release_artifacts.download", side_effect=[metadata, wheel()]):
            with self.assertRaisesRegex(ValueError, "conflict"):
                registry_state("pypi", self.root, "0.5.1")

    def test_partial_recovery_stages_only_missing_files(self) -> None:
        stage = self.root / "pending"
        report = self.root / "receipt.json"
        output = self.root / "github-output"
        argv = ["release_artifacts", "pypi", "--directory", str(self.root), "--version", "0.5.1",
                "--stage", str(stage), "--report", str(report)]
        with patch("sys.argv", argv), patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), patch(
            "scripts.release_artifacts.download", side_effect=[self.pypi_metadata([self.wheel]), self.wheel.read_bytes()],
        ):
            self.assertEqual(artifacts_main(), 0)
        self.assertEqual([path.name for path in stage.iterdir()], [self.sdist.name])
        self.assertEqual((stage / self.sdist.name).read_bytes(), self.sdist.read_bytes())
        self.assertEqual(json.loads(report.read_text())["state"], "partial")
        self.assertEqual(output.read_text(), "missing=1\nstate=partial\n")

    def test_conflict_never_stages_other_missing_files_or_writes_outputs(self) -> None:
        metadata = self.pypi_metadata([self.wheel])
        self.wheel.write_bytes(wheel(b"changed after first publish"))
        stage = self.root / "pending"
        output = self.root / "github-output"
        argv = ["release_artifacts", "pypi", "--directory", str(self.root), "--version", "0.5.1", "--stage", str(stage)]
        with patch("sys.argv", argv), patch.dict(os.environ, {"GITHUB_OUTPUT": str(output)}), patch(
            "scripts.release_artifacts.download", side_effect=[metadata, wheel()],
        ), self.assertRaisesRegex(ValueError, "conflict"):
            artifacts_main()
        self.assertFalse(stage.exists())
        self.assertFalse(output.exists())

    def test_registry_preflight_is_read_only_and_accepts_missing_files(self) -> None:
        argv = ["release_artifacts", "pypi", "--directory", str(self.root), "--version", "0.5.1"]
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        with patch("sys.argv", argv), patch.dict(os.environ, {"GITHUB_OUTPUT": ""}), patch(
            "scripts.release_artifacts.download", return_value=None,
        ):
            self.assertEqual(artifacts_main(), 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)

    def test_npm_existing_version_must_match_payload(self) -> None:
        path = self.root / "coding-tools-mcp-0.1.0.tgz"
        path.write_bytes(tar())
        metadata = json.dumps({"name": "coding-tools-mcp", "version": "0.1.0", "dist": {
            "tarball": "https://registry.npmjs.org/package.tgz", "shasum": hashlib.sha1(tar()).hexdigest(),
        }}).encode()
        with patch("scripts.release_artifacts.download", side_effect=[metadata, tar()]):
            self.assertEqual(registry_state("npm", self.root, "0.1.0")["state"], "complete")
        path.write_bytes(tar(b"changed launcher"))
        with patch("scripts.release_artifacts.download", side_effect=[metadata, tar()]):
            with self.assertRaisesRegex(ValueError, "conflict"):
                registry_state("npm", self.root, "0.1.0")

    def test_npm_recovery_preserves_newer_latest(self) -> None:
        data = json.dumps({"name": "coding-tools-mcp", "version": "0.2.0"}).encode()
        with patch("scripts.release_artifacts.download", return_value=data):
            self.assertEqual(npm_publish_tag("0.1.1"), "release-0.1.1")
            self.assertEqual(npm_publish_tag("0.3.0"), "latest")
        with patch("scripts.release_artifacts.download", return_value=None):
            self.assertEqual(npm_publish_tag("0.1.0"), "latest")
        with patch("scripts.release_artifacts.download", side_effect=urllib.error.URLError("timeout")):
            with self.assertRaises(urllib.error.URLError):
                npm_publish_tag("0.1.0")

    def test_only_404_is_absent(self) -> None:
        for code in (401, 403, 429, 500, 503):
            error = urllib.error.HTTPError("https://pypi.org", code, "error", {}, None)
            with patch("urllib.request.urlopen", side_effect=error), self.assertRaises(urllib.error.HTTPError):
                download("https://pypi.org", absent_ok=True)
        error = urllib.error.HTTPError("https://pypi.org", 404, "missing", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            self.assertIsNone(download("https://pypi.org", absent_ok=True))
            with self.assertRaises(urllib.error.HTTPError):
                download("https://pypi.org")


class FinalizationTests(unittest.TestCase):
    def test_finalization_reruns_only_create_missing_objects(self) -> None:
        ref = {"object": {"type": "commit", "sha": "a" * 40}}
        release = {"draft": False, "prerelease": False}
        for initial, expected_writes in (((None, None), 2), ((ref, None), 1), ((ref, release), 0)):
            with self.subTest(expected_writes=expected_writes), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "CHANGELOG.md"
                path.write_text("## 0.5.1 - 2026-10-06\n\nRelease notes\n")
                responses = [*initial, *([{}] * expected_writes), ref, release]
                argv = ["finalize", "--repository", "owner/repo", "--tag", "v0.5.1", "--source", "a" * 40,
                        "--changelog", str(path), "--finalize"]
                with patch("sys.argv", argv), patch("scripts.finalize_release.api", side_effect=responses) as api:
                    self.assertEqual(finalize_main(), 0)
                    writes = [call for call in api.call_args_list if len(call.args) > 2]
                    self.assertEqual(len(writes), expected_writes)
                    for call in writes:
                        self.assertNotIn("force", call.args[2])
                        if call.args[1] == "releases":
                            self.assertEqual(call.args[2]["make_latest"], "legacy")

    def test_github_token_stops_when_source_workflows_differ(self) -> None:
        responses = [
            {"default_branch": "main"}, {"object": {"sha": "b" * 40}},
            {"tree": [{"path": ".github", "sha": "c" * 40}]},
            {"tree": [{"path": "workflows", "sha": "d" * 40}]},
            {"tree": [{"path": ".github", "sha": "e" * 40}]},
            {"tree": [{"path": "workflows", "sha": "f" * 40}]},
        ]
        with patch("scripts.finalize_release.api", side_effect=responses) as api:
            with self.assertRaisesRegex(ValueError, "maintainer"):
                require_workflow_token_compatibility("owner/repo", "a" * 40)
            self.assertTrue(all(len(call.args) == 2 for call in api.call_args_list))

    def test_github_token_allows_same_source_and_default_commit(self) -> None:
        with patch("scripts.finalize_release.api", side_effect=[
            {"default_branch": "main"}, {"object": {"sha": "a" * 40}},
        ]):
            require_workflow_token_compatibility("owner/repo", "a" * 40)

    def test_tag_alone_is_partial(self) -> None:
        with patch("scripts.finalize_release.api", side_effect=[{"object": {"type": "commit", "sha": "a" * 40}}, None]):
            self.assertEqual(inspect("owner/repo", "v0.5.1", "a" * 40), {"tag_exists": True, "release_exists": False})

    def test_wrong_tag_is_never_moved(self) -> None:
        with patch("scripts.finalize_release.api", return_value={"object": {"type": "commit", "sha": "b" * 40}}):
            with self.assertRaisesRegex(ValueError, "never move"):
                inspect("owner/repo", "v0.5.1", "a" * 40)

    def test_annotated_tag_is_peeled(self) -> None:
        with patch("scripts.finalize_release.api", side_effect=[
            {"object": {"type": "tag", "sha": "b" * 40}},
            {"object": {"type": "commit", "sha": "a" * 40}},
            {"draft": False, "prerelease": False},
        ]):
            self.assertTrue(inspect("owner/repo", "v0.5.1", "a" * 40)["release_exists"])

    def test_sealed_release_is_rejected_without_api_call(self) -> None:
        with patch("scripts.finalize_release.api") as api:
            with self.assertRaises(ValueError):
                inspect("owner/repo", "v0.5.0", "a" * 40)
            api.assert_not_called()

    def test_draft_and_prerelease_need_manual_review(self) -> None:
        for release in ({"draft": True, "prerelease": False}, {"draft": False, "prerelease": True}):
            with patch("scripts.finalize_release.api", side_effect=[
                {"object": {"type": "commit", "sha": "a" * 40}}, release,
            ]), self.assertRaisesRegex(ValueError, "manual review"):
                inspect("owner/repo", "v0.5.1", "a" * 40)

    def test_notes_only_contain_selected_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CHANGELOG.md"
            path.write_text("# Changelog\n\n## 0.5.1 - 2026-10-06\n\nNew release\n\n## 0.5.0 - 2026-09-01\nOld release\n")
            self.assertIn("New release", notes(path, "0.5.1"))
            self.assertNotIn("Old release", notes(path, "0.5.1"))


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
        self.jobs = self.workflow["jobs"]

    def test_main_is_entrypoint_and_secrets_are_scoped(self) -> None:
        events = self.workflow.get("on", self.workflow.get(True))
        self.assertEqual(events["push"], {"branches": ["main"]})
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for name, job in self.jobs.items():
            if job.get("permissions", {}).get("id-token") == "write":
                self.assertIn(name, {"publish-pypi", "publish-npm"})
                self.assertIn("environment", job)

    def test_finalization_requires_both_verified_registries(self) -> None:
        self.assertEqual(set(self.jobs["github-release"]["needs"]), {"plan", "publish-pypi", "publish-npm"})
        self.assertEqual(set(self.jobs["build"]["needs"]), {"plan", "compliance", "real-workloads"})

    def test_both_registries_are_preflighted_before_either_publisher_can_start(self) -> None:
        steps = self.jobs["build"]["steps"]
        preflight = next(step for step in steps if step.get("id") == "registry-preflight")
        self.assertNotIn("if", preflight)
        self.assertNotIn("continue-on-error", preflight)
        self.assertIn('scripts.release_artifacts pypi --directory source/dist --version "$VERSION"', preflight["run"])
        self.assertIn('scripts.release_artifacts npm --directory source/packages/npm-launcher --version "$NPM_VERSION"', preflight["run"])
        self.assertEqual(preflight["env"], {"VERSION": "${{ needs.plan.outputs.version }}", "NPM_VERSION": "${{ needs.plan.outputs.npm_version }}"})
        uploads = [i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertTrue(uploads)
        self.assertLess(steps.index(preflight), min(uploads))
        for publisher in ("publish-pypi", "publish-npm"):
            self.assertIn("build", self.jobs[publisher]["needs"])
            self.assertNotIn("if", self.jobs[publisher])
            registry = next(step for step in self.jobs[publisher]["steps"] if step.get("id") == "registry")
            self.assertIn("scripts.release_artifacts", registry["run"])

    def test_external_benchmark_is_not_a_transitive_release_gate(self) -> None:
        for name, job in self.jobs.items():
            self.assertNotIn("swebench-lite", job.get("needs", []), name)
        self.assertFalse(self.jobs["swebench-lite"]["with"]["blocking"])

    def test_source_sha_is_passed_to_all_evidence_and_builds(self) -> None:
        for job in ("compliance", "real-workloads", "swebench-lite"):
            self.assertEqual(self.jobs[job]["with"]["source_ref"], "${{ needs.plan.outputs.source_sha }}")
        for job in ("build", "github-release"):
            checkouts = [step for step in self.jobs[job]["steps"] if step.get("with", {}).get("path") == "source"]
            self.assertEqual(checkouts[0]["with"]["ref"], "${{ needs.plan.outputs.source_sha }}")

    def test_recovery_uses_source_pinned_build_tools_and_safe_latest_selection(self) -> None:
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("-r source/scripts/release-build-requirements.txt", text)
        self.assertIn("source/scripts/release-npm-version.txt", text)
        self.assertEqual(self.jobs["publish-npm"]["concurrency"]["group"], "release-npm")
        self.assertIn('--tag "$PUBLISH_TAG"', text)
        self.assertIn("--finalize --github-token", text)

    def test_reusable_gate_artifacts_can_be_reuploaded_on_retry(self) -> None:
        for name in ("compliance.yml", "real-workloads.yml"):
            workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
            for job in workflow["jobs"].values():
                for step in job["steps"]:
                    if step.get("uses", "").startswith("actions/upload-artifact@"):
                        self.assertTrue(step["with"]["overwrite"], name)

    def test_failed_job_rerun_can_reuse_prior_artifacts(self) -> None:
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertNotIn("github.run_attempt", text)
        for job in ("publish-pypi", "publish-npm", "github-release"):
            self.assertFalse(self.jobs[job]["concurrency"]["cancel-in-progress"])


if __name__ == "__main__":
    unittest.main()
