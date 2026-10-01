"""Guards for the corpus oracle sweep: a counter the oracle stopped printing
must fail the run, not print a WARNING beside exit 0."""
import collections
import contextlib
import sys
import unittest


from support import TempDirTestCase, run_cli
import validate_corpus as guard


class ProblemTests(unittest.TestCase):
    def test_a_clean_sweep_has_no_problems(self):
        self.assertEqual(guard.problems([], collections.Counter()), [])

    def test_a_replay_the_oracle_could_not_validate_is_a_problem(self):
        found = guard.problems([("a.vrf", "exit 101")], collections.Counter())
        self.assertTrue(found)
        self.assertIn("a.vrf", " ".join(found))

    def test_a_counter_the_oracle_stopped_printing_is_a_problem(self):
        found = guard.problems([], collections.Counter({"malformed": 3}))
        self.assertTrue(found)
        joined = " ".join(found)
        self.assertIn("malformed", joined)
        self.assertIn("3", joined)

    def test_every_absent_counter_is_named_not_just_the_first(self):
        found = guard.problems(
            [], collections.Counter({"malformed": 3, "skipped": 1}))
        self.assertEqual(len(found), 2, found)

    def test_failures_and_absent_counters_are_both_reported(self):
        found = guard.problems([("a.vrf", "timeout")],
                               collections.Counter({"malformed": 1}))
        self.assertEqual(len(found), 2, found)


class PatternTests(unittest.TestCase):
    """The regexes are shared with check_corpus_baseline.py so they cannot drift."""

    def test_the_malformed_pattern_matches_the_label_the_oracle_prints(self):
        m = guard.PATTERNS["malformed"].search("Malformed framing:  0")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "0")

    def test_missing_branch_is_a_controlled_parse_failure(self):
        text = """
Total content blocks: 10
Fields emitted: 20
RPCs emitted: 5
Malformed framing: 0
Skipped bits: 0
ORACLE PASS RATE: 100.000000%
"""
        parsed, error = guard.parse_oracle_output(text)
        self.assertIsNone(parsed)
        self.assertIn("Branch", error)


class ArgParsingTests(unittest.TestCase):
    def test_flags_are_opt_in_and_the_limit_still_parses_positionally(self):
        """Discovery recursion and redaction are explicit, opt-in flags, and
        `<exe> <corpus> [limit]` must keep working."""
        argv = ["validate_corpus.py", "vrfkit.exe", "corpus"]
        plain = guard.parse_args(argv)
        self.assertIsNone(plain.limit)
        self.assertEqual(guard.parse_args(argv + ["5"]).limit, 5)
        for flag in ("recursive", "redact_identifiers"):
            with self.subTest(flag=flag):
                self.assertFalse(getattr(plain, flag))
                given = guard.parse_args(argv + ["--" + flag.replace("_", "-")])
                self.assertTrue(getattr(given, flag))


#: Stand-in for `vrfkit.exe`, invoked as `_run_one` invokes the real one --
#: `[str(exe), "validate", str(path)]`. Run under `sys.executable`, the first
#: argv token becomes the script Python executes, so a file literally named
#: `validate` in the process's cwd stands in for the binary (other suites'
#: fakes point here). Its output depends on the replay's filename.
FAKE_VALIDATE_SCRIPT = '''\
import sys
from pathlib import Path

name = Path(sys.argv[1]).name

if "badexit" in name:
    print("oracle blew up", file=sys.stderr)
    raise SystemExit(3)

print("Branch: ++Ares-Core+release-13.01")
print("Total content blocks: 100")
if "missingmalformed" not in name:
    print("Malformed framing:  0")
print("Skipped bits:  " + {"a": "12", "b": "3671"}.get(name.partition("skips")[0], "0"))
print("Fields emitted: 50")
print("RPCs emitted: 10")
print("ORACLE PASS RATE: 100.000000%")
'''


class MainWiringTests(TempDirTestCase):
    """`ProblemTests` pins what `problems()` returns; these pin that `main()`
    reads it before choosing an exit code."""

    def setUp(self):
        self.root = self.tmp()
        (self.root / "validate").write_text(FAKE_VALIDATE_SCRIPT, encoding="utf-8")
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        self.enterContext(contextlib.chdir(self.root))

    def make_replay(self, name: str) -> None:
        (self.corpus / name).write_bytes(b"not a real replay")

    def run_main(self, limit: str | None = None):
        extra = () if limit is None else (limit,)
        code, out, _ = run_cli(guard.main, "validate_corpus.py", sys.executable, self.corpus, *extra, merged=True)
        return code, out

    def test_a_clean_sweep_exits_zero(self):
        self.make_replay("a.vrf")
        self.make_replay("b.vrf")
        code, output = self.run_main()
        self.assertEqual(code, 0, output)
        self.assertIn("OK:", output)
        self.assertRegex(output, r"(?m)^replays that skip bits: 0$")

    def test_each_replay_that_skips_bits_is_listed_most_first(self):
        for name in ("askips.vrf", "bskips.vrf", "c.vrf"):
            self.make_replay(name)
        code, output = self.run_main()
        self.assertEqual(code, 0, output)
        self.assertRegex(output, r"(?m)^replays that skip bits: 2\n +3,671 bits  malformed=0  "
                                 r"rate=100\.000000%  bskips\.vrf\n +12 bits  malformed=0  "
                                 r"rate=100\.000000%  askips\.vrf$")

    def test_an_oracle_that_could_not_validate_a_replay_fails_the_run(self):
        self.make_replay("badexit.vrf")
        code, output = self.run_main()
        self.assertNotEqual(code, 0, output)
        self.assertIn("FAILED", output)

    def test_a_counter_the_oracle_stopped_printing_fails_the_run(self):
        """`missingmalformed.vrf` exits 0 with every other counter present, so
        only `main()` acting on `problems()` can catch the absent counter."""
        self.make_replay("missingmalformed.vrf")
        code, output = self.run_main()
        self.assertNotEqual(code, 0, output)
        self.assertIn("FAILED", output)
        self.assertIn("malformed", output)

    def test_an_empty_corpus_is_a_controlled_failure_not_a_silent_pass(self):
        with self.assertRaises(SystemExit) as caught:
            guard.main(["validate_corpus.py", sys.executable, str(self.corpus)])
        self.assertIn("no .vrf under", str(caught.exception))
