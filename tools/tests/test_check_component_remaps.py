"""Guards for the component-remap check: the strict verdict for each pair
kind, the rename signal, and a run that checked nothing. Fixture row counts
are measured shapes from real exports."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import TOOLS, TempDirTestCase, run_cli
import check_component_remaps as guard

SCRIPT = TOOLS / "check_component_remaps.py"

NATIVE = "/Script/ShooterGame.EquippableStateMachineComponent"
ENTRIES = [("ZoomStateMachine", NATIVE, "RepLayout")]
CNC_PAYLOAD = "__vrfkit_unresolved_class_net_cache_payload__"


def write_fields(directory, rows) -> Path:
    """A fields.parquet of `(group_path, field_name)` rows, both columns
    dictionary-encoded as the exporter writes them."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    table = pa.table({
        "group_path": pa.array([g for g, _ in rows], pa.string()).dictionary_encode(),
        "field_name": pa.array([f for _, f in rows], pa.string()).dictionary_encode(),
    })
    pq.write_table(table, Path(directory) / "fields.parquet")
    return Path(directory)


def states(rows, entries=ENTRIES):
    return [v.state for v in guard.verdicts(entries, rows)]


class VerdictTests(unittest.TestCase):
    def test_a_dead_remap_with_the_leaf_still_present_fails(self):
        """The rename signature: nothing on the target, everything still bare."""
        v = guard.verdicts(ENTRIES, {"ZoomStateMachine": 8112})
        self.assertEqual([x.state for x in v], ["broken"])
        self.assertIn("8112", v[0].detail)

    def test_neither_present_is_absent_not_broken(self):
        """A replay without that component is not evidence of anything."""
        self.assertEqual(states({"/Script/Other.Thing": 5}), ["absent"])

    def test_a_pair_whose_kind_has_no_rule_is_refused(self):
        """A `GroupKind` paths.rs adds must not fall through to a lenient rule."""
        with self.assertRaises(ValueError):
            states({NATIVE: 1}, [("ZoomStateMachine", NATIVE, "Future")])


class StrictRepLayoutTests(unittest.TestCase):
    """A RepLayout pair is broken by any RepLayout row left bare: healthy is
    exactly zero."""

    def test_nothing_bare_is_ok(self):
        self.assertEqual(states({NATIVE: 60101}), ["ok"])

    def test_one_bare_row_is_broken(self):
        self.assertEqual(states({NATIVE: 60101, "ZoomStateMachine": 1}), ["broken"])


class ClassNetCachePairTests(TempDirTestCase):
    """A ClassNetCache pair is judged on the rows it routes, strictly."""

    ENTRY = [("DamageHandlerComponent", "/Script/ShooterGame.DamageableComponent",
              "ClassNetCache")]
    ROUTED = "/Script/ShooterGame.DamageableComponent_ClassNetCache"

    def verdict(self, rows, cnc_bare=None):
        return guard.verdicts(self.ENTRY, rows, cnc_bare)[0]

    def test_routed_rows_and_nothing_bare_is_ok(self):
        v = self.verdict({self.ROUTED: 89843})
        self.assertEqual(v.state, "ok")
        self.assertIn("89843 rows on the _ClassNetCache group", v.detail)

    def test_a_stray_rep_layout_row_under_the_leaf_is_reported_not_broken(self):
        """A healthy 13.05 export: one 1-bit RepLayout row under the leaf, 0 on
        the RepLayout group, 89,843 routed."""
        v = self.verdict({"DamageHandlerComponent": 1, self.ROUTED: 89843})
        self.assertEqual(v.state, "ok")
        self.assertIn("1 RepLayout rows under the leaf", v.detail)

    def test_a_remap_that_did_not_fire_is_broken(self):
        """0 routed, the one RepLayout row unchanged, 4,563 whole payloads bare
        under the leaf."""
        v = self.verdict({"DamageHandlerComponent": 1},
                         {"DamageHandlerComponent": 4563})
        self.assertEqual(v.state, "broken")
        self.assertIn("4563 ClassNetCache rows still bare", v.detail)

    def test_one_bare_payload_beside_a_busy_target_is_broken(self):
        """Strict, as for a RepLayout pair: bare is checked first, so no share
        of a busy target may hide one."""
        v = self.verdict({self.ROUTED: 89843}, {"DamageHandlerComponent": 1})
        self.assertEqual(v.state, "broken")

    def test_the_rep_layout_target_group_says_nothing(self):
        """`EffectManager`'s RepLayout group carries rows on every export, but
        not rows this pair routes."""
        v = self.verdict({"/Script/ShooterGame.DamageableComponent": 5000})
        self.assertEqual(v.state, "absent")

    def test_neither_routed_nor_bare_is_absent(self):
        v = self.verdict({"DamageHandlerComponent": 2})
        self.assertEqual(v.state, "absent")
        self.assertIn("2 RepLayout rows under the leaf", v.detail)

    def test_the_two_bare_counts_split_every_row_of_a_bare_group(self):
        """A native group counts every row, ClassNetCache ones included."""
        directory = self.tmp()
        rep_layout, cnc = guard.row_counts(write_fields(
            directory, [("DamageHandlerComponent", CNC_PAYLOAD)] * 4563
            + [("DamageHandlerComponent", None)]
            + [("AbilitiesAndBuffsComponent", "_cnc_h1")] * 4683
            + [("AbilitiesAndBuffsComponent", "Status")] * 3
            + [(self.ROUTED, "_cnc_h2")] * 2))
        self.assertEqual(rep_layout, {"DamageHandlerComponent": 1,
                                      "AbilitiesAndBuffsComponent": 3, self.ROUTED: 2})
        self.assertEqual(cnc, {"DamageHandlerComponent": 4563,
                               "AbilitiesAndBuffsComponent": 4683})

    def test_the_rep_layout_tally_counts_only_class_net_cache_leaves(self):
        rows = {"DamageHandlerComponent": 2, "EffectManager": 3,
                "ZoomStateMachine": 70}
        entries = self.ENTRY + ENTRIES + [("EffectManager", "/X", "ClassNetCache")]
        self.assertEqual(guard.class_net_cache_leaf_rep_layout_rows(rows, entries), 5)


class RenameSignalTests(unittest.TestCase):
    """A rename never reads `broken`: the old leaf stops being declared, so the
    pair reads `ok` (another leaf keeps the target busy) or `absent`. The
    component arrives as a bare group under its NEW name, which no pair
    claims; that is the signal."""

    RENAMED = {NATIVE: 52059, "ZoomStateMachineV2": 8112}

    def test_a_renamed_leaf_is_not_broken(self):
        """States the gap the suspects list exists to cover."""
        self.assertEqual(states(self.RENAMED), ["ok"])

    def test_the_renamed_leaf_surfaces_as_an_unclaimed_bare_group(self):
        self.assertEqual(guard.unmapped_bare_groups(self.RENAMED, ENTRIES),
                         [("ZoomStateMachineV2", 8112)])

    def test_a_mapped_leaf_is_never_a_suspect(self):
        """A leaf the table already claims is not an unexplained group."""
        self.assertEqual(guard.unmapped_bare_groups({"ZoomStateMachine": 70}, ENTRIES), [])

    def test_a_native_group_is_never_a_suspect(self):
        """Only bare Blueprint leaves can be renamed out from under the table."""
        self.assertEqual(guard.unmapped_bare_groups({NATIVE: 52059}, ENTRIES), [])

    def test_a_class_net_cache_only_group_is_not_a_suspect(self):
        """`row_counts` already zeroes a RepLayout-only leaf such as
        `AbilitiesAndBuffsComponent`; its 0 must not read as a rename."""
        self.assertEqual(
            guard.unmapped_bare_groups({"AbilitiesAndBuffsComponent": 0}, ENTRIES), [])

    def test_suspects_come_back_worst_first(self):
        suspects = guard.unmapped_bare_groups(
            {"Small": 3, "Large": 900, "Middle": 40}, ENTRIES)
        self.assertEqual([g for g, _ in suspects], ["Large", "Middle", "Small"])


class MainTests(TempDirTestCase):
    """The exit code and the vacuity live in `main`, so they are exercised
    there, in process."""

    def run_main(self, rows):
        """(exit code, stdout, stderr) of `main` over an export of `rows`."""
        with tempfile.TemporaryDirectory() as directory:
            write_fields(directory, rows)
            return run_cli(guard.main, "--export", directory)

    def test_a_missing_export_fails_with_exit_2(self):
        """`--export` is required, so no fields.parquet is a wrong path, not a
        skip. A child process: the exit code a caller chains on is the point."""
        directory = self.tmp()
        result = subprocess.run(
            [sys.executable, "-W", "error", str(SCRIPT), "--export",
             str(directory / "missing")],
            capture_output=True, text=True, check=False,
            encoding="utf-8", errors="strict", env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FAILED: no fields.parquet", result.stderr)

    def test_an_export_with_no_remap_at_all_does_not_report_OK(self):
        """Not the same as having no export: this one ran and learned nothing."""
        code, out, err = self.run_main([("/Script/Other.Thing", "X")] * 5)
        self.assertEqual(code, 1, out + err)
        self.assertIn("FAILED: nothing checked", err)
        self.assertNotIn("OK:", out)

    def test_a_healthy_export_still_passes_and_says_so(self):
        code, out, err = self.run_main([(NATIVE, "CurrentState")] * 100)
        self.assertEqual(code, 0, out + err)
        self.assertIn("OK:", out)
        # Printed with its zero: a line that appears only when nonzero could
        # not tell "none" from "never counted".
        self.assertIn("0 RepLayout row(s) under the leaves of the ClassNetCache "
                      "pairs", out)

    def test_main_reads_a_stray_rep_layout_row_under_a_class_net_cache_leaf_as_ok(self):
        routed = "/Script/ShooterGame.DamageableComponent_ClassNetCache"
        code, out, err = self.run_main([(routed, "MulticastNotifyHeal.HealTaken")] * 100
                                       + [("DamageHandlerComponent", None)])
        self.assertEqual(code, 0, out + err)
        self.assertIn("1 RepLayout row(s) under the leaves of the ClassNetCache "
                      "pairs", out)

    def test_main_fails_a_class_net_cache_remap_that_did_not_fire(self):
        """`main` must hand the leaf's ClassNetCache rows to `verdicts`: whole
        payloads bare and nothing routed."""
        code, out, err = self.run_main([(NATIVE, "CurrentState")] * 100
                                       + [("DamageHandlerComponent", CNC_PAYLOAD)] * 50
                                       + [("DamageHandlerComponent", None)])
        self.assertEqual(code, 1, out + err)
        self.assertIn("broken  DamageHandlerComponent", out)
        self.assertIn("50 ClassNetCache rows still bare", out)

    def test_main_judges_a_rep_layout_pair_strictly(self):
        """`main` must hand the table's kinds to `verdicts`: one bare RepLayout
        row against 100 on the target is `broken`."""
        code, out, err = self.run_main([(NATIVE, "CurrentState")] * 100
                                       + [("ZoomStateMachine", None)])
        self.assertEqual(code, 1, out + err)
        self.assertIn("broken  ZoomStateMachine", out)

    def test_main_fails_a_pair_whose_kind_has_no_rule(self):
        """With the table's kinds replaced: paths.rs has only the two kinds
        today, so the real table cannot reach this."""
        future = [(leaf, target, "Future") for leaf, target, _ in guard.remap_entries()]
        with mock.patch.object(guard, "remap_entries", return_value=future):
            code, out, err = self.run_main([(NATIVE, "CurrentState")] * 100)
        self.assertEqual(code, 1, out + err)
        self.assertIn("GroupKind 'Future'", err)
        self.assertNotIn("OK:", out)


class PairParsingTests(unittest.TestCase):
    def test_the_real_table_parses_completely(self):
        """Reads sink/paths.rs, so a reformat that breaks it shows up here."""
        table = guard.table_source()
        entries = guard.remap_entries(table)
        self.assertGreater(len(entries), 15)
        self.assertEqual(guard.unparsed_entries(table, entries), 0)
        self.assertIn(("ZoomStateMachine", NATIVE, "RepLayout"), entries)
        self.assertIn(("InventoryComponent", "/Script/ShooterGame.AresInventory",
                       "RepLayout"), entries)
        self.assertIn(("EffectManager", "/Script/ShooterGame.EffectManagerComponent",
                       "ClassNetCache"), entries)
        self.assertEqual({kind for *_, kind in entries}, {"RepLayout", "ClassNetCache"})

    def test_every_target_is_an_absolute_object_path(self):
        """Native `/Script/` classes and, for four pairs, Blueprint classes."""
        for leaf, target, _ in guard.remap_entries():
            self.assertTrue(target.startswith(("/Script/", "/Game/")), f"{leaf} -> {target}")
            self.assertIn(".", target, f"{leaf} -> {target}")

    def test_a_blueprint_class_target_is_read(self):
        """A `/Game/..._C` target, which a `/Script/`-only pattern would skip."""
        table = """const KNOWN_SUBOBJECT_CLASS_PATHS: &[(&str, &str, GroupKind)] = &[
    (
        "ZoomStateMachine",
        "/Script/ShooterGame.EquippableStateMachineComponent",
        GroupKind::RepLayout,
    ),
    (
        "Comp_Ability_CooldownComponent1",
        "/Game/Characters/Components/Comp_Ability_CooldownComponent.Comp_Ability_CooldownComponent_C",
        GroupKind::RepLayout,
    ),"""
        entries = guard.remap_entries(table)
        self.assertEqual([leaf for leaf, *_ in entries],
                         ["ZoomStateMachine", "Comp_Ability_CooldownComponent1"])
        self.assertEqual(guard.unparsed_entries(table, entries), 0)

    def test_an_entry_the_pattern_cannot_read_is_counted(self):
        """A pair nothing checks must not pass as a table with one fewer pair."""
        table = """    (
        "ZoomStateMachine",
        "/Script/ShooterGame.EquippableStateMachineComponent",
        GroupKind::RepLayout,
    ),
    (
        "RelativeTarget",
        "Script/NoLeadingSlash.Thing",
        GroupKind::RepLayout,
    ),"""
        entries = guard.remap_entries(table)
        self.assertEqual(len(entries), 1)
        self.assertEqual(guard.unparsed_entries(table, entries), 1)
