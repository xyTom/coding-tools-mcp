from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
NEW_ENTRYPOINT = REPO_ROOT / "integrations" / "tunnels" / "tunnel.sh"
LEGACY_ENTRYPOINT = REPO_ROOT / "scripts" / "tunnel.sh"


class TunnelLauncherTests(unittest.TestCase):
    def run_help(self, entrypoint: Path, *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(entrypoint), "--help"],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_entrypoints_are_executable(self) -> None:
        self.assertTrue(os.access(NEW_ENTRYPOINT, os.X_OK))
        self.assertTrue(os.access(LEGACY_ENTRYPOINT, os.X_OK))

    def test_legacy_entrypoint_forwards_to_new_launcher_from_any_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cwd = Path(temp_dir)
            current = self.run_help(NEW_ENTRYPOINT, cwd=cwd)
            legacy = self.run_help(LEGACY_ENTRYPOINT, cwd=cwd)

        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(legacy.stdout, current.stdout)
        self.assertIn("cloudflared|ngrok|devtunnel", current.stdout)
        self.assertIn("integrations/tunnels/tunnel.sh", current.stdout)


if __name__ == "__main__":
    unittest.main()
