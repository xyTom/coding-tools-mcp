from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "port_sentinel.py"
SPEC = importlib.util.spec_from_file_location("port_sentinel", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
sentinel = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sentinel
SPEC.loader.exec_module(sentinel)


class PortSentinelTests(unittest.TestCase):
    def test_classify_healthy_warning_critical(self) -> None:
        thresholds = sentinel.Thresholds(4000, 10000)
        self.assertEqual(sentinel.classify(3999, thresholds), "healthy")
        self.assertEqual(sentinel.classify(4000, thresholds), "warning")
        self.assertEqual(sentinel.classify(9999, thresholds), "warning")
        self.assertEqual(sentinel.classify(10000, thresholds), "critical")


if __name__ == "__main__":
    unittest.main()
