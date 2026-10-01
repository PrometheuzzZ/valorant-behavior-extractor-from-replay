import os
import subprocess
import sys
from pathlib import Path
from support import REPO, TempDirTestCase


SCRIPT = REPO / "crates" / "vrf-export" / "tests" / "python_interop.py"


class ExactFixtureSelectionTests(TempDirTestCase):
    @staticmethod
    def run_script(temp: Path, configured: Path | None = None, *args: str):
        env = os.environ.copy()
        env.update({"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp)})
        env.pop("VRFKIT_INTEROP_DIR", None)
        if configured is not None:
            env["VRFKIT_INTEROP_DIR"] = str(configured)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=30,
        )

    def test_a_stale_temp_fixture_is_never_selected_implicitly(self):
        temp = self.tmp()
        stale = temp / "vrf_export_tests_stale" / "interop"
        stale.mkdir(parents=True)
        (stale / "fields_interop.parquet").write_bytes(b"stale")
        (stale / "movement_interop.parquet").write_bytes(b"stale")

        result = self.run_script(temp)

        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("explicit interop directory required", output.lower())
        self.assertNotIn(str(stale), output)

    def test_environment_names_the_rust_root_and_selects_its_interop_child(self):
        # VRFKIT_INTEROP_DIR is the root the Rust write_interop_files test is
        # given; its files are in `<root>/interop`, not in the root itself.
        temp = self.tmp()
        root = temp / "selected"
        (root / "interop").mkdir(parents=True)
        expected = (root / "interop").resolve()

        result = self.run_script(temp, root)

        output = (result.stdout + result.stderr).lower()
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn(f"interop dir: {expected}".lower(), output)
        self.assertIn("interop parquet files not found", output)

    def test_an_argument_is_the_exact_fixture_directory(self):
        # CI and CONTRIBUTING pass the `interop` child itself; nothing is
        # appended to it, and it wins over the variable.
        temp = self.tmp()
        exact = temp / "passed" / "interop"
        exact.mkdir(parents=True)
        expected = exact.resolve()

        result = self.run_script(temp, temp / "ignored", str(exact))

        output = (result.stdout + result.stderr).lower()
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn(f"interop dir: {expected}".lower(), output)
        self.assertNotIn("ignored", output)
        self.assertIn("interop parquet files not found", output)
