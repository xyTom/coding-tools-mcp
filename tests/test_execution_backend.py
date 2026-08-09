from __future__ import annotations

import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from coding_tools_mcp.execution import LocalExecutionBackend


class LocalExecutionBackendTests(unittest.TestCase):
    def test_spawn_delegates_only_for_workspace_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "child"
            child.mkdir()
            process = Mock()
            spawn = Mock(return_value=(process, None))
            terminate = Mock()
            backend = LocalExecutionBackend(
                root,
                spawn_process=spawn,
                terminate_process=terminate,
            )

            returned, pty = backend.spawn(
                "echo ok",
                cwd=str(child),
                shell=True,
                env={"A": "1"},
                tty=False,
                popen_kwargs={},
            )
            self.assertIs(returned, process)
            self.assertIsNone(pty)
            self.assertEqual(spawn.call_count, 1)
            self.assertEqual(spawn.call_args.kwargs["cwd"], str(child.resolve()))

    def test_spawn_rejects_cwd_outside_workspace_before_delegate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp)
            spawn = Mock()
            backend = LocalExecutionBackend(
                root,
                spawn_process=spawn,
                terminate_process=Mock(),
            )
            with self.assertRaisesRegex(ValueError, "inside the workspace"):
                backend.spawn(
                    "echo no",
                    cwd=other,
                    shell=True,
                    env={},
                    tty=False,
                    popen_kwargs={},
                )
            spawn.assert_not_called()

    def test_terminate_delegates_signal_and_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            terminate = Mock()
            backend = LocalExecutionBackend(
                Path(tmp),
                spawn_process=Mock(),
                terminate_process=terminate,
            )
            process = Mock()
            backend.terminate(process, signal.SIGTERM, force=True)
            terminate.assert_called_once_with(process, signal.SIGTERM, force=True)


if __name__ == "__main__":
    unittest.main()
