import os
import subprocess
import sys
import unittest
from support import TOOLS


SCRIPTS = {
    # These two use argparse and accept --help.
    "extract_match_observations.py": 0,
    "validate_metrics_corpus.py": 0,
}


class DirectScriptImportTests(unittest.TestCase):
    def test_help_is_warning_strict_for_every_direct_cli(self):
        for script, expected_returncode in SCRIPTS.items():
            with self.subTest(script=script):
                env = os.environ.copy()
                env["PYTHONWARNINGS"] = "error"
                result = subprocess.run(
                    [sys.executable, "-W", "error", str(TOOLS / script), "--help"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    timeout=30,
                )
                self.assertEqual(result.returncode, expected_returncode, result.stderr)
                self.assertNotIn("ImportWarning", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertIn("usage:", (result.stdout + result.stderr).lower())
