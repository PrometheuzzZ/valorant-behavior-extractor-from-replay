"""Guards for the checksum-table generator.

A different set of replays teaches a different subset of the same
content-addressed table, so byte equality with a fresh render can never hold.
`--check` catches disagreement instead -- a checksum both the file and the
manifests know, mapped two ways -- which is portable: it compares only the
overlap.
"""
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import TOOLS, TempDirTestCase
import extract_checksum_types as gen

#: The one multi-field braced type, in the one-line spelling `render` writes.
BYTE_WHOLE = ("FieldType::RepMovement { rotation: RotatorQuantization::ByteComponents, "
              "location: VectorQuantization::RoundWholeNumber }")


def read_with(attribute: str, text: str, reader):
    """`reader()` with the module's `attribute` path pointing at `text`."""
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "table.rs"
        path.write_text(text, encoding="utf-8")
        with mock.patch.object(gen, attribute, path):
            return reader()


class ReconcileTests(unittest.TestCase):
    def test_a_wider_basis_is_not_staleness(self):
        """Learning more than the file holds is coverage, not drift."""
        verdict = gen.reconcile(
            committed={1: "FieldType::Int32"},
            learned={1: "FieldType::Int32", 2: "FieldType::Float"},
        )
        self.assertEqual(verdict.disagreed, {})
        self.assertEqual(verdict.new, {2: "FieldType::Float"})
        self.assertTrue(verdict.ok)

    def test_a_narrower_basis_is_not_staleness_either(self):
        """One replay cannot teach what seventy-one did. That is not an error."""
        verdict = gen.reconcile(
            committed={1: "FieldType::Int32", 2: "FieldType::Float"},
            learned={1: "FieldType::Int32"},
        )
        self.assertEqual(verdict.disagreed, {})
        self.assertEqual(verdict.unseen, {2: "FieldType::Float"})
        self.assertTrue(verdict.ok)

    def test_a_type_that_changed_is_caught(self):
        """The one thing that is real drift."""
        verdict = gen.reconcile(
            committed={1: "FieldType::Int32"},
            learned={1: "FieldType::Float"},
        )
        self.assertEqual(
            verdict.disagreed, {1: ("FieldType::Int32", "FieldType::Float")})
        self.assertFalse(verdict.ok)

    def test_an_empty_basis_settles_nothing(self):
        verdict = gen.reconcile(committed={1: "FieldType::Int32"}, learned={})
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.unseen, {1: "FieldType::Int32"})


class MergeTests(unittest.TestCase):
    """Widening must not drop what a narrower basis cannot re-derive."""

    def test_merge_keeps_what_the_basis_did_not_see(self):
        merged = gen.merge({1: "FieldType::Int32"}, {2: "FieldType::Float"})
        self.assertEqual(merged, {1: "FieldType::Int32", 2: "FieldType::Float"})

    def test_merge_refuses_to_overwrite_a_disagreement(self):
        with self.assertRaises(ValueError):
            gen.merge({1: "FieldType::Int32"}, {1: "FieldType::Float"})


class RetypeTests(unittest.TestCase):
    """`--retype`, the one deliberate way to change a committed type: a
    retyped donor (the `EffectID`s, UInt64 -> Int64) teaches a type `merge`
    refuses, and the table must never be fixed by hand."""

    def test_a_named_disagreement_takes_the_learned_type(self):
        merged = gen.merge(
            {1: "FieldType::UInt64", 2: "FieldType::Float"},
            {1: "FieldType::Int64"},
            retype={1},
        )
        self.assertEqual(merged, {1: "FieldType::Int64", 2: "FieldType::Float"})

    def test_an_unnamed_disagreement_is_still_refused(self):
        with self.assertRaises(ValueError):
            gen.merge(
                {1: "FieldType::UInt64", 2: "FieldType::Int32"},
                {1: "FieldType::Int64", 2: "FieldType::UInt32"},
                retype={1},
            )

    def test_a_retype_the_basis_does_not_teach_cannot_delete_the_entry(self):
        with self.assertRaises(ValueError):
            gen.merge({1: "FieldType::UInt64"}, {}, retype={1})

    def test_only_a_real_disagreement_may_be_retyped(self):
        verdict = gen.reconcile(
            committed={1: "FieldType::UInt64", 2: "FieldType::Int32", 3: "FieldType::Float"},
            learned={1: "FieldType::Int64", 2: "FieldType::Int32"},
            conflicts={3: (["FieldType::Double", "FieldType::Float"], ["X"])},
        )
        self.assertEqual(gen.retype_problems(verdict, {1}), [])
        # agrees already / not learned at all / donors conflict: all refused
        for checksum in (2, 4, 3):
            self.assertEqual(len(gen.retype_problems(verdict, {checksum})), 1, checksum)

    def test_retype_is_refused_in_check_mode(self):
        export = TOOLS / "fixtures" / "checksum_export"
        result = subprocess.run(
            [sys.executable, str(Path(gen.__file__)), "--export", str(export),
             "--check", "--retype", "24357661"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("--retype", result.stderr)


class ConflictTests(unittest.TestCase):
    """`learn()`'s `conflicts` reach the verdict too: dropping a conflict is
    right for NEW evidence, but a committed type its donors now rule out, or
    no longer settle, must fail `--check` and leave the table on write."""

    def test_a_committed_type_the_evidence_rules_out_is_caught(self):
        verdict = gen.reconcile(
            committed={1: "FieldType::Float"},
            learned={},
            conflicts={1: (["FieldType::Int32", "FieldType::UInt32"], ["Foo"])},
        )
        self.assertIn(1, verdict.contradicted)
        self.assertFalse(verdict.ok)

    def test_a_committed_type_among_conflicting_donors_fails_closed(self):
        """Once donors disagree, no candidate remains safe to publish."""
        verdict = gen.reconcile(
            committed={1: "FieldType::Int32"},
            learned={},
            conflicts={1: (["FieldType::Int32", "FieldType::UInt32"], ["Foo"])},
        )
        self.assertEqual(verdict.contradicted, {})
        self.assertIn(1, verdict.ambiguous)
        self.assertFalse(verdict.ok)

    def test_a_conflict_the_file_never_committed_is_not_a_problem(self):
        """The safety property stays: an unwritten checksum stays unwritten."""
        verdict = gen.reconcile(
            committed={},
            learned={},
            conflicts={1: (["FieldType::Int32", "FieldType::UInt32"], ["Foo"])},
        )
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.ambiguous, {})

    def test_merge_drops_an_existing_mapping_when_widened_donors_conflict(self):
        """Write mode heals the file by omitting the unsafe checksum."""
        merged = gen.merge(
            {1: "FieldType::Int32", 2: "FieldType::Float"},
            {},
            conflicts={1: (["FieldType::Int32", "FieldType::UInt32"], ["Foo"])},
        )
        self.assertEqual(merged, {2: "FieldType::Float"})


class ParseTests(unittest.TestCase):
    def test_the_committed_table_parses(self):
        committed = gen.load_committed()
        self.assertGreater(len(committed), 300, "expected a populated table")
        for checksum, ftype in committed.items():
            self.assertIsInstance(checksum, int)
            self.assertTrue(ftype.startswith("FieldType::"), ftype)

    def test_the_overlay_table_is_read_in_the_spelling_render_writes(self):
        """A braced type broken over lines with a trailing comma reads as the
        one-line spelling `render` writes, and an escaped key is unescaped."""
        table = (
            "pub static OVERLAY_TABLE: [OverlayEntry; 2] = [\n"
            "    OverlayEntry {\n"
            '        group_path: "/Game/A.A_C",\n'
            '        field_name: "ReplicatedMovement",\n'
            "        field_type: FieldType::RepMovement {\n"
            "            rotation: RotatorQuantization::ByteComponents,\n"
            "            location: VectorQuantization::RoundWholeNumber,\n"
            "        },\n"
            "    },\n"
            '    OverlayEntry { group_path: "/Game/B.B_C", field_name: "Origin", '
            "field_type: FieldType::VectorNetQuantize { scale: 100 } },\n"
            "];\n"
        )
        self.assertEqual(read_with("TABLE_RS", table, gen.load_overlay_table), {
            ("/Game/A.A_C", "ReplicatedMovement"): BYTE_WHOLE,
            ("/Game/B.B_C", "Origin"): "FieldType::VectorNetQuantize { scale: 100 }",
        })
        escaped = table.replace('"/Game/B.B_C"', '"/Game/B\\"B_C"')
        self.assertIn(("/Game/B\"B_C", "Origin"),
                      read_with("TABLE_RS", escaped, gen.load_overlay_table))
        with self.assertRaises(SystemExit):
            read_with("TABLE_RS", table.replace("; 2]", "; 3]"), gen.load_overlay_table)

    def test_every_committed_row_is_read_or_reading_fails(self):
        """A row broken over lines is still read, or --check would skip it and
        the next write drop it; a read short of the declared length fails."""
        head = gen.render({}).split("pub static")[0]
        rows = (
            "    (5, FieldType::Float),\n"
            "    (\n"
            "        6,\n"
            "        FieldType::RepMovement {\n"
            "            rotation: RotatorQuantization::ByteComponents,\n"
            "            location: VectorQuantization::RoundWholeNumber,\n"
            "        },\n"
            "    ),\n"
            "];\n"
        )
        table = f"{head}pub static CHECKSUM_TYPES: [(u32, FieldType); 2] = [\n{rows}"
        self.assertEqual(read_with("OUT_RS", table, gen.load_committed),
                         {5: "FieldType::Float", 6: BYTE_WHOLE})
        with self.assertRaises(SystemExit):
            read_with("OUT_RS", table.replace("; 2]", "; 3]"), gen.load_committed)


class RenderTests(unittest.TestCase):
    def test_a_quantized_type_brings_its_import(self):
        """RepMovement names RotatorQuantization and VectorQuantization, so it
        needs their import -- only then: an unused `use` fails clippy -D
        warnings."""
        self.assertIn(
            "use crate::types::{RotatorQuantization, VectorQuantization};",
            gen.render({1: "FieldType::Int32", 2: BYTE_WHOLE}))
        self.assertNotIn("crate::types", gen.render({1: "FieldType::Int32"}))


class LearnTests(TempDirTestCase):
    def test_only_raw_and_skip_teach_nothing(self):
        """Raw and Skip carry no decode, so they are not donors; a type whose
        name merely contains one of them still is."""
        temp = self.tmp()
        manifest = temp / "manifest.json"
        manifest.write_text(json.dumps({"net_field_export_groups": [
            {"path": "/g", "fields": [
                {"name": name, "compatible_checksum": checksum}
                for checksum, name in enumerate(("raw", "skip", "raw_like"), 1)
            ]},
        ]}), encoding="utf-8")
        resolved, conflicts = gen.learn([manifest], {
            ("/g", "raw"): "FieldType::Raw",
            ("/g", "skip"): "FieldType::Skip",
            ("/g", "raw_like"): "FieldType::RawBits",
        })
        self.assertEqual((resolved, conflicts), ({3: "FieldType::RawBits"}, {}))


class CliGuardTests(TempDirTestCase):
    def test_missing_manifest_is_a_failure_not_a_successful_skip(self):
        temp = self.tmp()
        export = temp / "missing-export"
        result = subprocess.run(
            [sys.executable, str(Path(gen.__file__)), "--export", str(export),
             "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("SKIP:", result.stdout + result.stderr)

    def test_committed_ci_fixture_exercises_the_generator_guard(self):
        export = TOOLS / "fixtures" / "checksum_export"
        result = subprocess.run(
            [sys.executable, str(Path(gen.__file__)), "--export", str(export),
             "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK:", result.stdout)
        self.assertNotIn("SKIP:", result.stdout + result.stderr)
        # returncode==0 and "OK:" are also what an EMPTY overlap prints --
        # `verdict.ok` is vacuously True when nothing was compared. Confirm
        # the fixture actually shares checksums with the committed table, or
        # this gate is a guard over nothing.
        table = gen.load_overlay_table()
        resolved, conflicts = gen.learn([export / "manifest.json"], table)
        overlap = gen.load_committed().keys() & resolved.keys()
        self.assertGreater(
            len(overlap), 0,
            "fixture and committed table share no checksums -- the CI gate "
            "is comparing nothing")
