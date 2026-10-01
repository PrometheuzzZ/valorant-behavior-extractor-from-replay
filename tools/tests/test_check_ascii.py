import shutil
import os
import subprocess
import sys
from support import REPO, TempDirTestCase


SCRIPT = REPO / "tools" / "check_ascii.py"
NESTED_WORKING_DIRECTORY = REPO / "crates" / "vrfkit"


class CheckAsciiTests(TempDirTestCase):
    def run_checker(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=NESTED_WORKING_DIRECTORY,
            capture_output=True,
            text=True,
            encoding="utf-8", errors="strict",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            check=False,
        )

    def test_default_check_uses_full_repository_from_nested_directory(self):
        """Run from a nested crate, the checker must still scan the whole repo.
        The count is derived from `git ls-files`, not a literal: a test edited
        whenever the codebase grows is edited without being read."""
        tracked = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "*.rs"],
            capture_output=True, text=True, encoding="utf-8", errors="strict", check=True,
        ).stdout.split()
        self.assertGreater(len(tracked), 0, "no tracked Rust files found")

        result = self.run_checker("--check")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            f"OK: {len(tracked)} tracked Rust file(s), ASCII only\n",
        )
        self.assertEqual(result.stderr, "")

    def test_explicit_temporary_fixture_is_detected_from_nested_directory(self):
        directory = self.tmp()
        fixture = directory / "planted.rs"
        fixture.write_bytes(b"// planted: \xc3\xa9\n")

        result = self.run_checker("--check", "--path", str(fixture))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            f"{fixture.as_posix()}:1:13: non-ASCII byte 0xC3\n"
            f"{fixture.as_posix()}:1:14: non-ASCII byte 0xA9\n"
            "FAILED: 1 line(s), 2 byte(s)\n",
        )

    def test_an_empty_tracked_list_is_a_broken_measurement_not_a_clean_sweep(self):
        """`git ls-files` succeeding with no output scanned nothing: a sweep
        that covers nothing must not read like one that found nothing wrong."""
        repository = self.tmp()
        (repository / "tools").mkdir()
        copied_script = repository / "tools" / "check_ascii.py"
        shutil.copyfile(SCRIPT, copied_script)
        (repository / "notes.md").write_bytes(b"no Rust here\n")
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "add", "--", "notes.md"],
                       cwd=repository, check=True)

        result = subprocess.run(
            [sys.executable, str(copied_script), "--check"],
            cwd=repository, capture_output=True, text=True, check=False,
            encoding="utf-8", errors="strict", env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )

        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("OK:", result.stdout)
        self.assertIn("no tracked Rust files", result.stderr)

    def test_default_check_detects_tracked_fixture_outside_nested_directory(self):
        repository = self.tmp()
        nested_directory = repository / "nested"
        nested_directory.mkdir()
        (repository / "tools").mkdir()
        copied_script = repository / "tools" / "check_ascii.py"
        shutil.copyfile(SCRIPT, copied_script)
        (nested_directory / "local.rs").write_bytes(b"// ASCII\n")
        (repository / "planted.rs").write_bytes(b"// planted: \xc3\xa9\n")
        subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "add", "--", "nested/local.rs", "planted.rs"],
                       cwd=repository, check=True)

        result = subprocess.run(
            [sys.executable, str(copied_script), "--check"],
            cwd=nested_directory,
            capture_output=True,
            text=True,
            encoding="utf-8", errors="strict",
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            result.stderr,
            "planted.rs:1:13: non-ASCII byte 0xC3\n"
            "planted.rs:1:14: non-ASCII byte 0xA9\n"
            "FAILED: 1 line(s), 2 byte(s)\n",
        )
