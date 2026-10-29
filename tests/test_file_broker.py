from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_tools_mcp.errors import ToolFailure
from coding_tools_mcp.file_broker import FileBroker, broker_supported
from coding_tools_mcp.patching import AtomicPatchCommitter, FileBaseline, StagedFile


@unittest.skipUnless(broker_supported(), "POSIX handle-relative broker required")
class FileBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("outside secret\n")
        self.broker = FileBroker(self.root)
        self.addCleanup(self.broker.close)

    def assert_denied(self, code: str, callback, *args) -> ToolFailure:
        with self.assertRaises(ToolFailure) as failure:
            callback(*args)
        self.assertEqual(failure.exception.code, code)
        return failure.exception

    def staged(self, path: str, content: str | None, *, action: str = "") -> StagedFile:
        baseline = self.broker.capture_baseline(path)
        return StagedFile(path, self.root / path, content, baseline, baseline.mode, action=action)

    def test_read_stat_list_walk_and_patch_round_trip(self) -> None:
        (self.root / "a.txt").write_bytes(b"one\r\ntwo\n")
        self.assertEqual(self.broker.read_bytes("a.txt"), b"one\r\ntwo\n")
        self.assertEqual(self.broker.read_text("a.txt"), "one\r\ntwo\n")
        self.assertEqual(self.broker.stat("a.txt").st_size, 9)
        self.assertEqual([entry.display for entry in self.broker.list_dir()], ["a.txt"])
        committer = AtomicPatchCommitter(broker=self.broker)
        committer.commit([self.staged("nested/b.txt", "new\n"), self.staged("a.txt", "changed\n")])
        self.assertEqual(set(self.broker.walk_files()), {"a.txt", "nested/b.txt"})
        self.assertEqual(self.broker.read_text("nested/b.txt"), "new\n")
        self.assertTrue(self.broker.is_dir("nested"))
        self.assertTrue(self.broker.is_file("a.txt"))
        self.assertFalse(self.broker.exists("missing"))
        committer.commit([self.staged("a.txt", None)])
        self.assertFalse(self.broker.exists("a.txt"))

    def test_absolute_parent_nul_windows_and_unc_paths_rejected(self) -> None:
        for value in ("/etc/passwd", r"C:\file", "C:file", r"\\server\share", r"dir\file"):
            self.assert_denied("ABSOLUTE_PATH_DENIED", self.broker.read_bytes, value)
        for value in ("../secret.txt", "dir/../../secret.txt", "dir/../a.txt"):
            self.assert_denied("PATH_OUTSIDE_WORKSPACE", self.broker.read_bytes, value)
        for value in ("", "a\x00b"):
            self.assert_denied("INVALID_ARGUMENT", self.broker.read_bytes, value)
        self.assert_denied("PATH_OUTSIDE_WORKSPACE", self.broker.read_bytes, self.outside / "secret.txt")

    def test_leaf_and_parent_symlinks_rejected(self) -> None:
        (self.root / "link").symlink_to(self.outside, target_is_directory=True)
        (self.root / "secret.txt").symlink_to(self.outside / "secret.txt")
        for path in ("link/secret.txt", "secret.txt"):
            self.assert_denied("SYMLINK_ESCAPE", self.broker.read_bytes, path)
            self.assert_denied("SYMLINK_ESCAPE", self.broker.capture_baseline, path)
            self.assert_denied("SYMLINK_ESCAPE", self.broker.stat, path)
        self.assertEqual(self.broker.list_dir(), [])
        self.assertEqual(list(self.broker.walk_files()), [])

    def test_in_workspace_symlink_also_denied_in_strict_mode(self) -> None:
        (self.root / "plain").write_text("content")
        (self.root / "alias").symlink_to("plain")
        self.assert_denied("SYMLINK_ESCAPE", self.broker.read_bytes, "alias")

    def test_hardlink_to_outside_and_special_files_denied(self) -> None:
        os.link(self.outside / "secret.txt", self.root / "hard")
        self.assert_denied("HARDLINK_DENIED", self.broker.read_bytes, "hard")
        self.assert_denied("HARDLINK_DENIED", self.broker.capture_baseline, "hard")
        os.mkfifo(self.root / "fifo")
        self.assert_denied("UNSUPPORTED_FILE_TYPE", self.broker.read_bytes, "fifo")
        self.assertEqual(list(self.broker.walk_files()), [])

    def test_denied_roots_cover_read_stat_walk_and_write(self) -> None:
        private = self.root / "service"
        private.mkdir()
        (private / "token").write_text("credential")
        metadata = self.root / "auth.json"
        metadata.write_text("credential")
        helper = self.root / "helper"
        helper.mkdir()
        (helper / "binary").write_text("trusted")
        with FileBroker(self.root, denied_roots=(private, metadata, helper)) as broker:
            for path in ("service/token", "auth.json", "helper/binary"):
                self.assert_denied("PATH_DENIED", broker.read_bytes, path)
                self.assert_denied("PATH_DENIED", broker.stat, path)
                self.assert_denied("PATH_DENIED", broker.capture_baseline, path)
                change = StagedFile(path, self.root / path, "bad", FileBaseline(None, None, None), None)
                self.assert_denied("PATH_DENIED", broker.commit, [change])
            self.assertEqual(broker.list_dir(), [])
            self.assertEqual(list(broker.walk_files()), [])

    def test_denied_directory_cannot_be_renamed_into_allowed_name(self) -> None:
        private = self.root / "service"
        private.mkdir()
        (private / "token").write_text("credential")
        with FileBroker(self.root, denied_roots=(private,)) as broker:
            private.rename(self.root / "renamed")
            self.assert_denied("PATH_DENIED", broker.read_bytes, "renamed/token")
            self.assertEqual(list(broker.walk_files()), [])

    def test_write_roots_are_enforced_and_verify_is_read_only(self) -> None:
        (self.root / "source").write_text("source")
        (self.root / "build").mkdir()
        with FileBroker(self.root, writable_roots=(self.root / "build",)) as broker:
            change = self.staged("source", "bad")
            self.assert_denied("WRITE_DENIED", broker.commit, [change])
            broker.commit([self.staged("source", "source", action="verify")])
            broker.commit([self.staged("build/output", "good")])
        self.assertEqual((self.root / "source").read_text(), "source")
        self.assertEqual((self.root / "build/output").read_text(), "good")

    def test_capture_baseline_uses_one_opened_file_for_mode_and_bytes(self) -> None:
        target = self.root / "a.txt"
        target.write_text("first")
        target.chmod(0o640)
        original = os.fstat
        swapped = False

        def swap_after_fd_open(fd):
            nonlocal swapped
            value = original(fd)
            if stat.S_ISREG(value.st_mode) and not swapped:
                swapped = True
                target.rename(self.root / "old")
                target.write_text("replacement")
                target.chmod(0o600)
            return value

        with mock.patch("coding_tools_mcp.file_broker.os.fstat", side_effect=swap_after_fd_open):
            baseline = FileBaseline.capture(target, broker=self.broker)
        self.assertEqual(baseline.data, b"first")
        self.assertEqual(baseline.mode, 0o640)

    def test_parent_swap_before_component_open_cannot_read_outside(self) -> None:
        parent = self.root / "dir"
        parent.mkdir()
        (parent / "secret.txt").write_text("inside")
        original = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "dir" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                parent.rename(self.root / "original")
                parent.symlink_to(self.outside, target_is_directory=True)
            return original(path, flags, *args, **kwargs)

        with mock.patch("coding_tools_mcp.file_broker.os.open", side_effect=swap_before_open):
            self.assert_denied("SYMLINK_ESCAPE", self.broker.read_bytes, "dir/secret.txt")
        self.assertTrue(swapped)

    def test_parent_swap_after_open_stays_on_pinned_directory(self) -> None:
        parent = self.root / "dir"
        parent.mkdir()
        (parent / "secret.txt").write_text("inside")
        original = os.open
        swapped = False

        def swap_after_open(path, flags, *args, **kwargs):
            nonlocal swapped
            fd = original(path, flags, *args, **kwargs)
            if path == "dir" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                parent.rename(self.root / "original")
                parent.symlink_to(self.outside, target_is_directory=True)
            return fd

        with mock.patch("coding_tools_mcp.file_broker.os.open", side_effect=swap_after_open):
            self.assertEqual(self.broker.read_text("dir/secret.txt"), "inside")
        self.assertTrue(swapped)

    def test_leaf_swap_at_actual_open_is_not_check_then_open(self) -> None:
        target = self.root / "secret.txt"
        target.write_text("inside")
        original = os.open
        swapped = False

        def swap_before_leaf(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "secret.txt" and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                target.unlink()
                target.symlink_to(self.outside / "secret.txt")
            return original(path, flags, *args, **kwargs)

        with mock.patch("coding_tools_mcp.file_broker.os.open", side_effect=swap_before_leaf):
            self.assert_denied("SYMLINK_ESCAPE", self.broker.read_bytes, "secret.txt")
        self.assertTrue(swapped)

    def test_listing_directory_swap_at_listdir_uses_fd(self) -> None:
        parent = self.root / "dir"
        parent.mkdir()
        (parent / "inside.txt").write_text("inside")
        original = os.listdir
        swapped = False

        def swap_at_list(fd):
            nonlocal swapped
            self.assertIsInstance(fd, int)
            if not swapped:
                swapped = True
                parent.rename(self.root / "original")
                parent.symlink_to(self.outside, target_is_directory=True)
            return original(fd)

        with mock.patch("coding_tools_mcp.file_broker.os.listdir", side_effect=swap_at_list):
            entries = self.broker.list_dir("dir")
        self.assertEqual([entry.display for entry in entries], ["dir/inside.txt"])

    def test_walk_directory_swap_between_listing_and_open_is_denied(self) -> None:
        parent = self.root / "dir"
        parent.mkdir()
        (parent / "inside.txt").write_text("inside")
        original = self.broker._entries_at
        swapped = False

        def swap_after_listing(fd, display):
            nonlocal swapped
            entries = original(fd, display)
            if display == "." and not swapped:
                swapped = True
                parent.rename(self.root / "original")
                parent.symlink_to(self.outside, target_is_directory=True)
            return entries

        with mock.patch.object(self.broker, "_entries_at", side_effect=swap_after_listing):
            self.assertEqual(list(self.broker.walk_files()), [])

    def test_walk_broad_tree_keeps_open_descriptors_bounded(self) -> None:
        for index in range(100):
            directory = self.root / f"directory-{index}"
            directory.mkdir()
            (directory / "file.txt").write_text("content")
        opened: set[int] = set()
        peak = 0
        original_open, original_dup, original_close = os.open, os.dup, os.close

        def track(fd):
            nonlocal peak
            opened.add(fd)
            peak = max(peak, len(opened))
            return fd

        def tracked_open(*args, **kwargs):
            return track(original_open(*args, **kwargs))

        def tracked_dup(*args, **kwargs):
            return track(original_dup(*args, **kwargs))

        def tracked_close(fd):
            opened.discard(fd)
            original_close(fd)

        with mock.patch("coding_tools_mcp.file_broker.os.open", side_effect=tracked_open), \
                mock.patch("coding_tools_mcp.file_broker.os.dup", side_effect=tracked_dup), \
                mock.patch("coding_tools_mcp.file_broker.os.close", side_effect=tracked_close):
            self.assertEqual(len(list(self.broker.walk_files())), 100)
        self.assertLessEqual(peak, 2)
        self.assertEqual(opened, set())

    def test_walk_prunes_excluded_directories_and_relative_depth_before_opening(self) -> None:
        for relative in ("nested/root.txt", "nested/child/allowed.txt", "nested/child/deep/skipped.txt",
                         "nested/excluded/skipped.txt", "nested/child/excluded/skipped.txt"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("content")
        with mock.patch.object(self.broker, "_open_directory", wraps=self.broker._open_directory) as opened:
            paths = set(self.broker.walk_files("nested", excluded_directories={"excluded"}, max_depth=1))
        self.assertEqual(paths, {"nested/root.txt", "nested/child/allowed.txt"})
        self.assertEqual([call.args[0] for call in opened.call_args_list], ["nested", "nested/child"])
        self.assertEqual(list(self.broker.walk_files("nested", max_depth=0)), ["nested/root.txt"])

    def test_patch_parent_swap_before_final_rename_never_writes_outside(self) -> None:
        parent = self.root / "dir"
        parent.mkdir()
        target = parent / "secret.txt"
        target.write_text("inside")
        change = self.staged("dir/secret.txt", "changed")
        original = os.rename
        swapped = False

        def swap_at_rename(src, dst, *args, **kwargs):
            nonlocal swapped
            if src == "secret.txt" and not swapped:
                swapped = True
                original(parent, self.root / "original")
                parent.symlink_to(self.outside, target_is_directory=True)
            self.assertIsInstance(kwargs.get("src_dir_fd"), int)
            self.assertEqual(kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd"))
            return original(src, dst, *args, **kwargs)

        with mock.patch("coding_tools_mcp.file_broker.os.rename", side_effect=swap_at_rename):
            self.broker.commit([change])
        self.assertEqual((self.outside / "secret.txt").read_text(), "outside secret\n")
        self.assertEqual((self.root / "original/secret.txt").read_text(), "changed")

    def test_patch_symlink_after_baseline_is_rejected_without_side_effect(self) -> None:
        target = self.root / "secret.txt"
        target.write_text("inside")
        change = self.staged("secret.txt", "changed")
        target.unlink()
        target.symlink_to(self.outside / "secret.txt")
        self.assert_denied("SYMLINK_ESCAPE", self.broker.commit, [change])
        self.assertEqual((self.outside / "secret.txt").read_text(), "outside secret\n")
        self.assertEqual(list(self.root.glob(".coding-tools-*")), [])

    def test_patch_conflict_keeps_all_originals_and_cleans_new_directories(self) -> None:
        target = self.root / "a.txt"
        target.write_text("inside")
        change = self.staged("a.txt", "changed")
        created = self.staged("new/nested/b.txt", "created")
        target.write_text("concurrent")
        self.assert_denied("PATCH_CONFLICT", self.broker.commit, [created, change])
        self.assertEqual(target.read_text(), "concurrent")
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(list(self.root.glob(".coding-tools-*")), [])

    def test_patch_install_failure_rolls_back_all_files(self) -> None:
        (self.root / "a").write_text("first")
        (self.root / "b").write_text("second")
        changes = [self.staged("a", "new first"), self.staged("b", "new second")]
        original = os.rename

        def fail_second_install(src, dst, *args, **kwargs):
            if src.startswith(".coding-tools-patch-") and dst == "b":
                raise OSError("injected install failure")
            return original(src, dst, *args, **kwargs)

        with mock.patch("coding_tools_mcp.file_broker.os.rename", side_effect=fail_second_install):
            with self.assertRaisesRegex(OSError, "injected"):
                self.broker.commit(changes)
        self.assertEqual((self.root / "a").read_text(), "first")
        self.assertEqual((self.root / "b").read_text(), "second")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["a", "b"])

    def test_ensure_directory_uses_handles_and_rejects_swapped_parent(self) -> None:
        self.broker.ensure_directory("build/nested")
        self.assertTrue((self.root / "build/nested").is_dir())
        self.assertFalse(self.broker.exists("missing/child"))
        self.assertTrue(stat.S_ISDIR(self.broker.stat(".").st_mode))
        parent = self.root / "build"
        parent.rename(self.root / "original")
        parent.symlink_to(self.outside, target_is_directory=True)
        self.assert_denied("SYMLINK_ESCAPE", self.broker.ensure_directory, "build/escape")
        self.assertFalse((self.outside / "escape").exists())

    def test_closed_broker_never_falls_back(self) -> None:
        self.broker.close()
        self.assert_denied("SANDBOX_UNAVAILABLE", self.broker.read_bytes, "secret.txt")


class FileBrokerCapabilitiesTests(unittest.TestCase):
    def test_windows_and_unknown_platforms_fail_closed_before_any_io(self) -> None:
        for platform in ("win32", "freebsd14", "unknown"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                with mock.patch("coding_tools_mcp.file_broker.os.open") as opened:
                    with self.assertRaises(ToolFailure) as failure:
                        FileBroker(Path("workspace"))
                    self.assertEqual(failure.exception.code, "SANDBOX_UNAVAILABLE")
                    opened.assert_not_called()

    def test_missing_nofollow_or_directory_fd_capability_fails_closed(self) -> None:
        with mock.patch("coding_tools_mcp.file_broker.os.supports_dir_fd", set()):
            self.assertFalse(broker_supported())
            with self.assertRaises(ToolFailure) as failure:
                FileBroker(Path("workspace"))
            self.assertEqual(failure.exception.code, "SANDBOX_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
