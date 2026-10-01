"""Guards for the shared `.vrf` corpus discovery: top level by default, the
subdirectory files it leaves out always counted, case-insensitive suffixes."""
import unittest
from pathlib import Path


from support import TempDirTestCase
import corpus_scan


class DiscoverTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()
        for name in ("a.vrf", "b.vrf", "old/c.vrf", "old/d.vrf", "old/e.vrf", "notes.txt"):
            (self.root / name).parent.mkdir(exist_ok=True)
            (self.root / name).write_bytes(b"")

    def test_top_level_only_by_default_with_the_rest_counted(self):
        scan = corpus_scan.discover(self.root, recursive=False)
        self.assertEqual([p.name for p in scan.files], ["a.vrf", "b.vrf"])
        self.assertEqual((scan.excluded, scan.recursive), (3, False))

    def test_recursive_scans_everything_and_excludes_nothing(self):
        scan = corpus_scan.discover(self.root, recursive=True)
        self.assertEqual(len(scan.files), 5)
        self.assertEqual((scan.excluded, scan.recursive), (0, True))

    def test_a_flat_corpus_excludes_nothing_either_way(self):
        """No subdirectory at all: both modes must agree, and say so."""
        root = self.tmp()
        (root / "a.vrf").write_bytes(b"")
        (root / "b.vrf").write_bytes(b"")
        flat = corpus_scan.discover(root, recursive=False)
        deep = corpus_scan.discover(root, recursive=True)
        self.assertEqual(flat.excluded, 0)
        self.assertEqual([p.name for p in flat.files], [p.name for p in deep.files])

    def test_uppercase_extension_is_a_replay_on_case_sensitive_filesystems(self):
        """POSIX glob("*.vrf") does not match MATCH.VRF."""
        root = self.tmp()
        lower = root / "lower.vrf"
        upper = root / "UPPER.VRF"
        lower.write_bytes(b"")
        upper.write_bytes(b"")

        class CaseSensitiveRoot:
            """Expose POSIX-style glob results even when this test runs on Windows."""

            def glob(self, pattern):
                if pattern == "*.vrf":
                    return iter([lower])
                if pattern == "*":
                    return iter([lower, upper])
                raise AssertionError(pattern)

            rglob = glob

        scan = corpus_scan.discover(CaseSensitiveRoot(), recursive=True)

        self.assertEqual({p.name for p in scan.files}, {"UPPER.VRF", "lower.vrf"})


class ScopeLineTests(unittest.TestCase):
    """The whole line, so count, root, mode and the excluded count -- zero
    included -- are each pinned."""

    def test_the_line_states_count_root_mode_and_excluded(self):
        root = Path("/private/player/corpus")
        top = corpus_scan.CorpusScan(files=[Path("a.vrf"), Path("b.vrf")],
                                     scanned_root=root, recursive=False, excluded=27)
        self.assertEqual(corpus_scan.scope_line(top),
                         f"corpus scope: 2 .vrf file(s) under {root} (top-level only); "
                         f"27 more in subdirectories excluded (pass --recursive to include)")
        deep = corpus_scan.CorpusScan(files=[], scanned_root=root, recursive=True, excluded=0)
        self.assertEqual(corpus_scan.scope_line(deep, redact_identifiers=True),
                         "corpus scope: 0 .vrf file(s) under <private corpus> (recursive); "
                         "0 more in subdirectories excluded")


class ReplayLabelTests(unittest.TestCase):
    def test_redacted_label_does_not_print_the_filename(self):
        label = corpus_scan.replay_label(
            Path("account-or-session-identifier.vrf"), 7, True)
        self.assertEqual(label, "replay-0007")
        self.assertNotIn("identifier", label)

    def test_unredacted_label_preserves_existing_output(self):
        self.assertEqual(
            corpus_scan.replay_label(Path("match.vrf"), 1, False),
            "match.vrf")


class DiagnosticTests(unittest.TestCase):
    def test_redacted_diagnostic_drops_a_subprocess_tail(self):
        detail = "exit 1: failed to open /private/player/match.vrf"
        shown = corpus_scan.diagnostic(detail, True)
        self.assertEqual(shown, "exit 1")
        self.assertNotIn("private", shown)

    def test_unredacted_diagnostic_preserves_existing_output(self):
        detail = "exit 1: useful diagnostic"
        self.assertEqual(corpus_scan.diagnostic(detail, False), detail)
