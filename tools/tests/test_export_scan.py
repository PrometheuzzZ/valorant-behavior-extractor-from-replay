"""The export-leftover filter names exactly what `vrfkit export` creates."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from support import REPO, TempDirTestCase
import export_scan

PUBLISH_RS = REPO / "crates" / "vrfkit" / "src" / "driver" / "publish.rs"


def rust_str_constant(source: str, name: str) -> str:
    """The value of `const NAME: &str = "...";`, which must appear exactly once."""
    values = re.findall(rf'^const {name}: &str = "([^"\\]*)";$', source, re.M)
    if len(values) != 1:
        raise AssertionError(
            f"expected one `const {name}: &str = \"...\";` in {PUBLISH_RS}, found {len(values)}")
    return values[0]


class RustContractTests(unittest.TestCase):
    """publish.rs's names, read back from the Rust source: renamed there
    alone, every tool here would read leftovers as exports again, and no Rust
    test knows this filter exists."""

    def test_the_rust_generator_and_this_filter_name_the_same_directories(self):
        source = PUBLISH_RS.read_text(encoding="utf-8")
        infix = rust_str_constant(source, "GENERATED_INFIX")
        kinds = (rust_str_constant(source, "STAGING"), rust_str_constant(source, "PREVIOUS"))
        self.assertEqual(infix, export_scan.GENERATED_INFIX)
        self.assertEqual(kinds, export_scan.GENERATED_KINDS)
        # `generated_name`: "." + destination + this format string.
        self.assertIn('OsString::from(".")', source)
        self.assertIn('"{GENERATED_INFIX}{kind}-{}-{nonce}"', source)
        for kind in kinds:
            self.assertTrue(export_scan.is_generated_sibling(f".pub2{infix}{kind}-55396-0"), kind)


class GeneratedSiblingTests(TempDirTestCase):
    def test_every_name_rust_can_write_is_recognised(self):
        for name in (
            ".pub2.vrfkit-staging-55396-0",                     # the measured leftover
            ".export.vrfkit-previous-4294967295-18446744073709551615",
            ".my export.v2.vrfkit-staging-1-0",                 # dots and spaces
            "..hidden.vrfkit-staging-1-0",                      # --out .hidden
            ".line\nbreak.vrfkit-previous-1-0",                 # any byte a name may hold
            ".a.vrfkit-staging-1.vrfkit-staging-5-0",           # a generated-looking destination
        ):
            self.assertTrue(export_scan.is_generated_sibling(name), repr(name))

    def test_near_misses_stay_candidate_exports(self):
        for name in (
            "pub2", ".pub2", "pub2.vrfkit-staging-1-0", ".vrfkit-staging-1-0",
            ".pub2.vrfkit-staging-1", ".pub2.vrfkit-staging-1-", ".pub2.vrfkit-staging--1-0",
            ".pub2.vrfkit-staging-x-0", ".pub2.vrfkit-staging-1-0-2",
            ".pub2.vrfkit-staging-1-0.bak", ".pub2.vrfkit-draft-1-0",
            ".pub2.VRFKIT-staging-1-0",                         # Rust writes lowercase only
            ".pub2.vrfkit-staging-١-0",                    # a non-ASCII digit
        ):
            self.assertFalse(export_scan.is_generated_sibling(name), repr(name))

    def test_child_exports_splits_candidates_from_every_leftover(self):
        root = self.tmp()
        for name in ("b", "a"):
            (root / name).mkdir()
            (root / name / "fields.parquet").write_bytes(b"table")
        (root / "no-table").mkdir()
        (root / "fields.parquet").write_bytes(b"a file, not a child export")
        (root / ".b.vrfkit-staging-9-0").mkdir()            # killed before its first table
        backup = root / ".b.vrfkit-previous-9-1"
        backup.mkdir()
        (backup / "fields.parquet").write_bytes(b"table")
        candidates, skipped = export_scan.child_exports(root)
        self.assertEqual([path.name for path in candidates], ["a", "b"])
        self.assertEqual([path.name for path in skipped],
                         [".b.vrfkit-previous-9-1", ".b.vrfkit-staging-9-0"])

    def test_the_root_and_the_leaf_never_count_as_leftovers(self):
        root = Path("exports") / ".x.vrfkit-previous-1-0"
        self.assertIsNone(export_scan.generated_ancestor(root / "fields.parquet", root))
        self.assertIsNone(export_scan.generated_ancestor(root / ".y.vrfkit-staging-2-3", root))
        nested = root / "build" / ".y.vrfkit-staging-2-3" / "fields.parquet"
        self.assertEqual(export_scan.generated_ancestor(nested, root),
                         root / "build" / ".y.vrfkit-staging-2-3")

    def test_the_note_counts_and_names_what_was_skipped(self):
        self.assertEqual(export_scan.leftover_note([]), "")
        note = export_scan.leftover_note([Path("p") / ".a.vrfkit-staging-1-0"] * 2)
        self.assertIn("skipped 1 vrfkit export staging/backup directory,", note)
        self.assertIn(".a.vrfkit-staging-1-0", note)
