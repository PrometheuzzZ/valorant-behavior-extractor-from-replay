"""Guards for the corpus baseline pinner: a run with a failed replay or an
unprinted counter must not be pinned (the same rule check_metrics_baseline.py
applies), and a baseline naming no corpus is missing input.
"""
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from support import TempDirTestCase, run_cli
import check_corpus_baseline as guard


def measurement(per_file, totals=None):
    return {
        "branches": {"1302": len(per_file)},
        "totals": totals or {"blocks": 10, "fields": 20, "rpcs": 5,
                             "malformed": 0, "skipped": 0},
        "per_file": per_file,
    }


CLEAN_ENTRY = {"branch": "1302", "rate": "100.000000", "blocks": 10,
               "fields": 20, "rpcs": 5, "malformed": 0, "skipped": 0}


class UnpinnableTests(unittest.TestCase):
    def test_a_clean_run_can_be_pinned(self):
        self.assertEqual(guard.unpinnable(measurement({"a.vrf": CLEAN_ENTRY})), [])

    def test_a_run_with_a_failed_replay_cannot_be_pinned(self):
        """Pinning it stores zeros that the same failure will match later."""
        current = measurement({"a.vrf": CLEAN_ENTRY,
                               "b.vrf": {"error": "exit 1"}})
        reasons = guard.unpinnable(current)
        self.assertTrue(reasons)
        self.assertIn("b.vrf", " ".join(reasons))

    def test_a_counter_the_oracle_did_not_print_cannot_be_pinned(self):
        """`measure` records it as None, and a pinned None would match the
        same counter going missing again."""
        entry = dict(CLEAN_ENTRY, malformed=None)
        reasons = guard.unpinnable(measurement({"a.vrf": entry}))
        self.assertTrue(reasons)
        self.assertIn("malformed", " ".join(reasons))

    def test_a_run_with_no_replays_at_all_cannot_be_pinned(self):
        self.assertTrue(guard.unpinnable(measurement({})))


class DiffTests(unittest.TestCase):
    def test_identical_measurements_do_not_drift(self):
        m = measurement({"a.vrf": CLEAN_ENTRY})
        self.assertEqual(guard.diff(m, m), [])

    def test_a_replay_leaving_the_corpus_is_drift(self):
        before = measurement({"a.vrf": CLEAN_ENTRY, "b.vrf": CLEAN_ENTRY})
        after = measurement({"a.vrf": CLEAN_ENTRY})
        self.assertTrue(any("missing replay: b.vrf" in d for d in guard.diff(before, after)))


class CorpusMeasurementTests(TempDirTestCase):
    SUMMARY = """
Branch: ++Ares-Core+release-13.02
Total content blocks: 10
Fields emitted: 20
RPCs emitted: 5
Malformed framing: 0
Skipped bits: 0
ORACLE PASS RATE: 100.000000%
"""

    def run_measure(self, script: str, relative_files: list[str]):
        root = self.tmp()
        corpus = root / "corpus"
        corpus.mkdir()
        for relative in relative_files:
            path = corpus / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"replay")
        (root / "validate").write_text(script, encoding="utf-8")
        with contextlib.chdir(root):
            return guard.measure(Path(sys.executable), corpus)

    def test_duplicate_basenames_are_keyed_by_relative_path(self):
        script = (
            "from pathlib import Path\nimport sys\n"
            f"summary = {self.SUMMARY!r}\n"
            "if 'bad' in Path(sys.argv[1]).parts:\n"
            "    print('deliberate failure', file=sys.stderr)\n"
            "    raise SystemExit(7)\n"
            "print(summary)\n"
        )
        result = self.run_measure(script, ["good/same.vrf", "bad/same.vrf"])

        self.assertEqual(
            set(result["per_file"]), {"good/same.vrf", "bad/same.vrf"}
        )
        self.assertNotIn("error", result["per_file"]["good/same.vrf"])
        self.assertIn("error", result["per_file"]["bad/same.vrf"])
        self.assertTrue(any("bad/same.vrf" in r for r in guard.unpinnable(result)))

    def test_missing_branch_is_a_controlled_unpinnable_failure(self):
        script = f"print({self.SUMMARY.replace('Branch: ++Ares-Core+release-13.02', '')!r})\n"
        result = self.run_measure(script, ["nested/replay.vrf"])

        entry = result["per_file"]["nested/replay.vrf"]
        self.assertIn("error", entry)
        self.assertIn("Branch", entry["error"])
        self.assertTrue(guard.unpinnable(result))

    def test_uppercase_vrf_extension_is_measured(self):
        result = self.run_measure(f"print({self.SUMMARY!r})\n", ["MATCH.VRF"])

        self.assertEqual(set(result["per_file"]), {"MATCH.VRF"})
        self.assertNotIn("error", result["per_file"]["MATCH.VRF"])


class UpdateCorpusNameTests(TempDirTestCase):
    """--update must not pin a resolved corpus path (an absolute --corpus, or
    VRFKIT_CORPUS_DIR joined to a relative one) into a new baseline: it would
    put one machine's directory into a committed file. check_export_baseline.py
    refuses the same for --replay."""

    def run_update(self, root: Path, corpus: str, corpus_dir: str | None):
        with mock.patch.dict(os.environ), mock.patch.object(
                guard, "measure", return_value=measurement({"a.vrf": CLEAN_ENTRY})) as measured:
            os.environ.pop("VRFKIT_CORPUS_DIR", None)
            os.environ.pop("VRFKIT_REQUIRE_CORPUS", None)
            if corpus_dir is not None:
                os.environ["VRFKIT_CORPUS_DIR"] = corpus_dir
            code, output, _ = run_cli(guard.main, "--baseline", root / "baseline.json", "--exe", sys.executable,
                                      "--corpus", corpus, "--update", prog="check_corpus_baseline.py", merged=True)
        return code, output, measured

    def test_an_absolute_corpus_is_refused_before_the_oracle_runs(self):
        root = self.tmp()
        (root / "build_9999").mkdir()
        code, output, measured = self.run_update(root, str(root / "build_9999"), None)
        self.assertEqual(code, 2, output)
        self.assertIn("VRFKIT_CORPUS_DIR", output)
        self.assertIn("--corpus build_9999", output)
        measured.assert_not_called()
        self.assertFalse((root / "baseline.json").exists())

    def test_a_relative_corpus_is_pinned_as_given_not_as_resolved(self):
        root = self.tmp()
        (root / "build_9999").mkdir()
        code, output, measured = self.run_update(root, "build_9999", str(root))
        self.assertEqual(code, 0, output)
        measured.assert_called_once()
        self.assertEqual(measured.call_args.args[1], root / "build_9999")
        stored = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["corpus"], "build_9999")


class NoCorpusNamedTests(unittest.TestCase):
    """A baseline that names no corpus is missing input, not the working
    directory: `Path("")` is `Path(".")`, which exists, and `--update` would
    pin that walk as `"corpus": "."`."""

    def run_main(self, root: Path, *extra: str) -> tuple[int, str]:
        with contextlib.chdir(root):
            code, output, _ = run_cli(guard.main, "--baseline", root / "baseline.json", "--exe", sys.executable,
                                      *extra, prog="check_corpus_baseline.py", merged=True)
        return code, output

    def test_no_corpus_named_is_missing_input_not_the_working_directory(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.dict(os.environ, clear=False) as environ:
            environ.pop("VRFKIT_REQUIRE_CORPUS", None)
            environ.pop("VRFKIT_CORPUS_DIR", None)
            root = Path(temp)
            # A replay the walk would find, and an oracle that validates it,
            # so a guard that walks the working directory gets far enough to
            # pin it.
            (root / "sub").mkdir()
            (root / "sub" / "planted.vrf").write_bytes(b"replay")
            (root / "validate").write_text(
                f"print({CorpusMeasurementTests.SUMMARY!r})\n", encoding="utf-8")
            baseline = root / "baseline.json"
            no_key = {"branches": {}, "totals": {}, "per_file": {}}
            for stored, extra, code, marker in (
                (None, ("--update",), 0, "SKIP:"),
                (None, ("--update", "--require-input"), 2, "REQUIRED INPUT MISSING"),
                (no_key, (), 0, "SKIP:"),
                (dict(no_key, corpus=""), ("--require-input",), 2, "REQUIRED INPUT MISSING"),
            ):
                with self.subTest(stored=stored, extra=extra):
                    if stored is None:
                        baseline.unlink(missing_ok=True)
                    else:
                        baseline.write_text(json.dumps(stored), encoding="utf-8")
                    before = baseline.read_bytes() if baseline.exists() else None
                    got, output = self.run_main(root, *extra)
                    self.assertEqual(got, code, output)
                    self.assertIn(marker, output)
                    self.assertIn("no corpus named", output)
                    self.assertEqual(baseline.read_bytes() if baseline.exists() else None,
                                     before, "the baseline must not be written")
