"""Guards for the per-build entry survival report.

Each test builds the declarations for one class -- declared, field-missing,
not observed, moved -- and asserts the category as well as the exit code: a
vanished group exits 0 too, so an exit code alone would pass with the move
detection deleted.
"""
import contextlib
import io
import json
import re
import unittest
from pathlib import Path

from support import TempDirTestCase, run_cli
import check_entry_survival as guard

REFS = ["Owner", "Instigator", "AttachParent", "Controller"]
BOOL = guard.normalize_type("FieldType::Bool")
GUID = guard.normalize_type("FieldType::ObjectNetGuid")
SKIP = guard.normalize_type("FieldType::Skip")

OLD = "/Game/Characters/Spy/S0/Ability_E/GameObject_Spy_E_Wire.GameObject_Spy_E_Wire_C"
NEW = "/Game/Characters/Spy/S0/Ability_4/GameObject_Spy_4_Wire.GameObject_Spy_4_Wire_C"
STATE = "/Script/ShooterGame.SomeStateComponent"


def table(group, name, ftype=BOOL):
    return guard.Entry("table", group, name, ftype=ftype)


def catalog(*entries, aliases=()):
    return guard.Catalog(list(entries), list(aliases), REFS)


def replay(build, decl):
    """`decl`: {group: [(name, checksum, handle), ...]}."""
    groups = frozenset(decl)
    fields = frozenset((g, n, c, h) for g, fs in decl.items() for (n, c, h) in fs)
    return guard.Replay("synthetic", f"++Ares-Core+release-{build}", groups, fields)


def many(build, count, decl):
    return [replay(build, decl) for _ in range(count)]


def judge(replays, cat):
    overlay = guard.Overlay(cat)
    tally = guard.tally(replays, cat, overlay)
    return tally, guard.judge(tally, cat, overlay)


def run(replays, cat, expected=()):
    return run_cli(lambda _: guard.run(replays, cat, list(expected), guard.LoadStats()))


def categories(judgement):
    return sorted((guard.short(f.build), f.entry.key, f.category, f.evidenced)
                  for f in judgement.findings)


class ClassificationTests(unittest.TestCase):
    """One scenario per class, 20 replays a build: well past `MIN_CONTEXT`."""

    def test_declared_in_both_builds_survives(self):
        cat = catalog(table(STATE, "bArmed"))
        reps = (many("13.01", 20, {STATE: [("bArmed", 11, 3)]})
                + many("13.02", 20, {STATE: [("bArmed", 11, 3)]}))
        tally, judgement = judge(reps, cat)
        self.assertEqual(judgement.findings, [])
        self.assertEqual(judgement.per_build["++Ares-Core+release-13.02"]["survived"], 1)
        self.assertEqual(run(reps, cat)[0], 0)

    def test_field_missing_with_evidence_fails(self):
        cat = catalog(table(STATE, "bArmed"))
        reps = (many("13.01", 20, {STATE: [("bArmed", 11, 3), ("Other", 12, 4)]})
                + many("13.02", 20, {STATE: [("Other", 12, 4), ("bArmedV2", 13, 5)]}))
        _, judgement = judge(reps, cat)
        self.assertEqual(categories(judgement),
                         [("13.02", f"table|{STATE}|bArmed", "field-missing", True)])
        self.assertEqual(judgement.findings[0].new_fields, ["bArmedV2"])
        code, out, err = run(reps, cat)
        self.assertEqual(code, 1)
        self.assertIn("FAILED", err)
        self.assertIn("20/20 group replays -> 0/20", out)

    def test_a_group_nobody_declared_is_not_a_finding(self):
        """The class was not used: no successor, so it is `vanished`, which
        never fails -- even with evidence that it is not sampling."""
        cat = catalog(table(OLD, "Deployed"))
        reps = (many("13.01", 20, {OLD: [("Deployed", 21, 17)], STATE: [("X", 1, 1)]})
                + many("13.02", 20, {STATE: [("X", 1, 1)]}))
        _, judgement = judge(reps, cat)
        self.assertEqual(categories(judgement),
                         [("13.02", f"table|{OLD}|Deployed", "vanished", True)])
        code, _, err = run(reps, cat)
        self.assertEqual(code, 0, err)

    def test_a_move_to_a_one_token_different_class_is_caught(self):
        """Cypher's tripwire: `_E_` became `_4_` in the path and in the class
        name, and the old `table.rs` key stopped matching."""
        cat = catalog(table(OLD, "Deployed"))
        reps = (many("13.01", 20, {OLD: [("Deployed", 21, 17)]})
                + many("13.02", 20, {NEW: [("Deployed", 21, 17)]}))
        _, judgement = judge(reps, cat)
        self.assertEqual(categories(judgement),
                         [("13.02", f"table|{OLD}|Deployed", "moved", True)])
        f = judgement.findings[0]
        self.assertEqual(f.successors[0].group, NEW)
        self.assertIn("one short token", f.successors[0].rule)
        self.assertEqual(f.coverage, "lost")
        code, out, err = run(reps, cat)
        self.assertEqual(code, 1)
        self.assertIn(NEW, out)
        self.assertIn("FAILED", err)

    def test_a_rare_shared_field_nominates_a_renamed_successor(self):
        renamed = "/Game/Characters/Spy/S0/Ability_4/Snare_Spy.Snare_Spy_C"
        cat = catalog(table(OLD, "Deployed"))
        reps = (many("13.01", 20, {OLD: [("Deployed", 21, 17)]})
                + many("13.02", 20, {renamed: [("Deployed", 21, 17)]}))
        _, judgement = judge(reps, cat)
        self.assertEqual(categories(judgement),
                         [("13.02", f"table|{OLD}|Deployed", "moved", True)])
        self.assertIn("rare", judgement.findings[0].successors[0].rule)

    def test_a_generic_field_does_not_nominate_a_successor(self):
        """`Owner` sits on hundreds of groups; a new class declaring it is not
        a successor of every class that stopped being used."""
        others = {f"/Game/Other{i}.Other{i}_C": [("Owner", 99, 11)] for i in range(4)}
        unrelated = "/Game/Characters/Nova/Ability_Nova_Q_Beam.Ability_Nova_Q_Beam_C"
        cat = catalog(table(OLD, "Owner", GUID))
        reps = (many("13.01", 20, {OLD: [("Owner", 99, 11)], **others})
                + many("13.02", 20, {unrelated: [("Owner", 99, 11)], **others}))
        _, judgement = judge(reps, cat)
        self.assertEqual(categories(judgement),
                         [("13.02", f"table|{OLD}|Owner", "vanished", True)])

    def test_a_class_differing_in_a_whole_word_is_not_a_successor(self):
        """13.01 introduced `Ability_Gumshoe_E_Camera` beside the vanished
        `Ability_Gumshoe_E_TripWire`: one token apart, but a different word,
        and a different ability. Only a short token (a slot, an index) may
        differ."""
        old = "/Game/Characters/Spy/S0/Ability_E/Ability_Spy_E_Wire.Ability_Spy_E_Wire_C"
        other = "/Game/Characters/Spy/S0/Ability_E/Ability_Spy_E_Camera.Ability_Spy_E_Camera_C"
        others = {f"/Game/Other{i}.Other{i}_C": [("Owner", 99, 11)] for i in range(4)}
        cat = catalog(table(old, "Owner", GUID))
        reps = (many("13.01", 20, {old: [("Owner", 99, 11)], **others})
                + many("13.02", 20, {other: [("Owner", 99, 11)], **others}))
        _, judgement = judge(reps, cat)
        self.assertEqual(categories(judgement),
                         [("13.02", f"table|{old}|Owner", "vanished", True)])

    def test_a_move_the_successor_still_types_is_covered(self):
        """`Owner` resolves by name on any group: nothing turned raw."""
        cat = catalog(table(OLD, "Owner", GUID))
        reps = (many("13.01", 20, {OLD: [("Owner", 99, 11)]})
                + many("13.02", 20, {NEW: [("Owner", 99, 11)]}))
        _, judgement = judge(reps, cat)
        f = judgement.findings[0]
        self.assertEqual((f.category, f.coverage), ("moved", "covered"))
        self.assertIn("engine", f.coverage_detail)
        self.assertEqual(run(reps, cat)[0], 0)

    def test_an_untyped_entry_moving_to_an_untyped_successor_is_covered(self):
        cat = catalog(table(OLD, "CosmeticRandomSeed", SKIP))
        reps = (many("13.01", 20, {OLD: [("CosmeticRandomSeed", 5, 9)]})
                + many("13.02", 20, {NEW: [("CosmeticRandomSeed", 5, 9)]}))
        _, judgement = judge(reps, cat)
        self.assertEqual(judgement.findings[0].coverage, "covered")

    def test_a_successor_that_retypes_the_field_is_lost(self):
        """The checksum learned elsewhere says Float; the entry said Bool."""
        learned = guard.Entry("checksum", checksum=21,
                              ftype=guard.normalize_type("FieldType::Float"))
        cat = catalog(table(OLD, "Deployed"), learned)
        reps = (many("13.01", 20, {OLD: [("Deployed", 21, 17)]})
                + many("13.02", 20, {NEW: [("Deployed", 21, 17)]}))
        _, judgement = judge(reps, cat)
        moved = [f for f in judgement.findings if f.entry.kind == "table"]
        self.assertEqual(moved[0].coverage, "lost")
        self.assertIn("Float", moved[0].coverage_detail)


class SamplingTests(unittest.TestCase):
    def test_every_replay_to_none_of_three_does_not_fail(self):
        """30 of 30 -> 0 of 3 is p = 1/5456, under ALPHA on its own; only
        `MIN_CONTEXT` keeps a three-replay legacy build from failing."""
        cat = catalog(table(STATE, "bArmed"))
        reps = (many("12.08", 30, {STATE: [("bArmed", 11, 3)]})
                + many("12.09", 3, {STATE: [("Other", 12, 4)]}))
        _, judgement = judge(reps, cat)
        self.assertEqual(categories(judgement),
                         [("12.09", f"table|{STATE}|bArmed", "field-missing", False)])
        self.assertLess(judgement.findings[0].p, guard.ALPHA)
        self.assertEqual(run(reps, cat)[0], 0)

    def test_a_move_seen_in_three_replays_is_weak(self):
        cat = catalog(table(OLD, "Deployed"))
        reps = (many("12.08", 30, {OLD: [("Deployed", 21, 17)]})
                + many("12.09", 3, {NEW: [("Deployed", 21, 17)]}))
        _, judgement = judge(reps, cat)
        self.assertEqual(categories(judgement),
                         [("12.09", f"table|{OLD}|Deployed", "moved", False)])
        self.assertEqual(run(reps, cat)[0], 0)

    def test_a_rare_field_absent_from_a_large_build_is_weak(self):
        """3 of 120 -> 0 of 100 is p = 0.16: the field was just not replicated."""
        cat = catalog(table(STATE, "bArmed"))
        reps = (many("13.01", 3, {STATE: [("bArmed", 11, 3)]})
                + many("13.01", 117, {STATE: [("Other", 12, 4)]})
                + many("13.02", 100, {STATE: [("Other", 12, 4)]}))
        _, judgement = judge(reps, cat)
        self.assertFalse(judgement.findings[0].evidenced)
        self.assertGreater(judgement.findings[0].p, guard.ALPHA)

    def test_the_reference_window_grows_back_to_thirty_replays(self):
        builds = [f"++Ares-Core+release-12.0{i}" for i in range(6)]
        replays = dict(zip(builds, [3, 10, 20, 1, 1, 200]))
        # 1 + 1 + 20 = 22, then 12.01 brings it to 32: 12.00 stays out.
        self.assertEqual(guard.reference_window(builds, replays, 5), builds[4:0:-1])
        self.assertEqual(guard.reference_window(builds, replays, 1), [builds[0]])

    def test_p_absent_is_the_hypergeometric_miss_probability(self):
        self.assertEqual(guard.p_absent(6, 3, 3), 1 / 20)
        self.assertEqual(guard.p_absent(10, 0, 5), 1.0)
        self.assertEqual(guard.p_absent(10, 6, 5), 0.0)


class ExpectedListTests(TempDirTestCase):
    ITEM = {"entry": f"table|{STATE}|bArmed", "build": "13.02", "finding": "field-missing",
            "reason": "renamed to bArmedV2 in 13.02", "evidence": "20/20 -> 0/20"}

    def scenario(self):
        cat = catalog(table(STATE, "bArmed"))
        reps = (many("13.01", 20, {STATE: [("bArmed", 11, 3)]})
                + many("13.02", 20, {STATE: [("bArmedV2", 13, 5)]}))
        return reps, cat

    def test_a_listed_finding_does_not_fail(self):
        code, out, err = run(*self.scenario(), expected=[dict(self.ITEM)])
        self.assertEqual(code, 0, err)
        self.assertIn("expected", out)
        self.assertIn("renamed to bArmedV2", out)

    def test_a_listed_item_matching_nothing_is_stale(self):
        cat = catalog(table(STATE, "bArmed"))
        reps = (many("13.01", 20, {STATE: [("bArmed", 11, 3)]})
                + many("13.02", 20, {STATE: [("bArmed", 11, 3)]}))
        code, _, err = run(reps, cat, expected=[dict(self.ITEM)])
        self.assertEqual(code, 1)
        self.assertIn("STALE", err)

    def test_an_item_without_a_reason_is_refused(self):
        tmp = self.tmp()
        path = tmp / "expected.json"
        item = dict(self.ITEM, reason=" ")
        path.write_text(json.dumps({"expected": [item]}), encoding="utf-8")
        with self.assertRaises(ValueError):
            guard.load_expected(path)

    def test_every_committed_item_names_a_real_entry(self):
        """An item whose entry the tables no longer hold can never match, so
        it would read STALE on every corpus rather than say why."""
        keys = {e.key for e in guard.load_catalog(guard.Sources.from_repo()).entries}
        for item in guard.load_expected(guard.EXPECTED_JSON):
            with self.subTest(entry=item["entry"]):
                self.assertIn(item["entry"], keys)
                self.assertRegex(item["build"], r"\A\d+\.\d\d\Z")


class ReportTests(unittest.TestCase):
    def test_zeros_are_printed(self):
        """A clean run still prints every counter: a line that appears only
        when non-zero cannot tell "nothing wrong" from "never ran"."""
        cat = catalog(table(STATE, "bArmed"))
        reps = (many("13.01", 20, {STATE: [("bArmed", 11, 3)]})
                + many("13.02", 20, {STATE: [("bArmed", 11, 3)]}))
        code, out, _ = run(reps, cat)
        self.assertEqual(code, 0)
        transitions = out.split("against the reference window", 1)[1]
        line = next(x for x in transitions.splitlines() if x.startswith("13.02"))
        self.assertRegex(line, r"\b0/0\s")
        self.assertRegex(line, r"\b0/0/0\b")
        self.assertIn("structural findings (field-missing with evidence): 0", out)
        self.assertIn("moves with evidence: 0 group(s)", out)
        self.assertIn("never declared in any build of the input: 0 table", out)
        self.assertIn("0 field row(s) joining no group", out)
        self.assertIn("field(s) (0 without a name or checksum)", out)
        self.assertIn("OK:", out)

    def test_pass_and_fail_do_not_look_alike(self):
        cat = catalog(table(STATE, "bArmed"))
        good = (many("13.01", 20, {STATE: [("bArmed", 11, 3)]})
                + many("13.02", 20, {STATE: [("bArmed", 11, 3)]}))
        bad = (many("13.01", 20, {STATE: [("bArmed", 11, 3)]})
               + many("13.02", 20, {STATE: [("Other", 12, 4)]}))
        _, good_out, good_err = run(good, cat)
        _, bad_out, bad_err = run(bad, cat)
        self.assertNotIn("FAILED", good_out + good_err)
        self.assertNotIn("OK:", bad_out + bad_err)

    def test_one_build_judges_nothing_and_says_so(self):
        cat = catalog(table(STATE, "bArmed"))
        code, _, err = run(many("13.01", 20, {STATE: [("bArmed", 11, 3)]}), cat)
        self.assertEqual(code, 2)
        self.assertIn("nothing was judged", err)

    def test_a_build_without_a_version_cannot_be_ordered(self):
        cat = catalog(table(STATE, "bArmed"))
        reps = [guard.Replay("x", "++Ares-Core+dev", frozenset(), frozenset())]
        with self.assertRaises(guard.InputError):
            judge(reps, cat)


class ResolutionTests(unittest.TestCase):
    """The mirror of `resolve_entry` in overlay.rs, step by step."""

    def setUp(self):
        self.cat = catalog(
            table(STATE, "bIsQueued"), table(STATE, "Location",
                                             guard.normalize_type("FieldType::VectorDouble")),
            guard.Entry("handle", STATE, "Location", handle=26),
            table("/Game/Bomb.Bomb_C", "Wins", guard.normalize_type("FieldType::Int32")),
            guard.Entry("scoped", "/Game/Swift.Swift_C", "B", 7, ftype=BOOL),
            aliases=[("/Game/Swift.Swift_C", "/Game/Bomb.Bomb_C")])
        self.overlay = guard.Overlay(self.cat)

    def test_the_b_prefixed_spelling_reaches_the_entry(self):
        r = self.overlay.resolve(STATE, "IsQueued", 1, 2)
        self.assertEqual(r.entry.name, "bIsQueued")

    def test_a_bare_decimal_at_a_mapped_handle_reaches_the_entry(self):
        r = self.overlay.resolve(STATE, "248", 1, 26)
        self.assertEqual(r.entry.name, "Location")

    def test_a_real_name_at_a_mapped_handle_is_refused(self):
        self.assertIsNone(self.overlay.resolve(STATE, "Somewhere", 1, 26))
        self.assertEqual(self.overlay.handle_state(STATE, "Somewhere", 26)[1], "conflict")
        self.assertEqual(self.overlay.handle_state(STATE, "IsQueued", 26)[1], "other")
        self.assertEqual(self.overlay.handle_state(STATE, "248", 26)[1], "hit")

    def test_an_alias_reaches_the_canonical_group_and_scoped_keys_stay_literal(self):
        self.assertEqual(self.overlay.resolve("/Game/Swift.Swift_C", "Wins", 3, 1).step, "alias")
        self.assertEqual(self.overlay.resolve("/Game/Swift.Swift_C", "B", 7, 1).step, "scoped")
        self.assertIsNone(self.overlay.resolve("/Game/Bomb.Bomb_C", "B", 7, 1))

    def test_alias_sources_count_toward_the_canonical_entry(self):
        reps = (many("13.01", 20, {"/Game/Swift.Swift_C": [("Wins", 3, 1)]})
                + many("13.02", 20, {"/Game/Swift.Swift_C": [("Wins", 3, 1)]}))
        tally, _ = judge(reps, self.cat)
        wins = next(e for e in self.cat.entries if e.name == "Wins")
        self.assertEqual(tally.hits[wins]["++Ares-Core+release-13.02"], 20)
        self.assertEqual(tally.contexts[wins]["++Ares-Core+release-13.02"], 20)


class ParseTests(unittest.TestCase):
    def test_the_real_tables_parse_completely(self):
        cat = guard.load_catalog(guard.Sources.from_repo())
        kinds = cat.by_kind()
        table_src = guard.TABLE_RS.read_text(encoding="utf-8")
        self.assertEqual(len(kinds["table"]), int(re.search(
            r"\[OverlayEntry; (\d+)\]", table_src).group(1)))
        self.assertEqual(len(kinds["handle"]), int(re.search(
            r"\[OverlayHandleEntry; (\d+)\]", table_src).group(1)))
        self.assertEqual(len(kinds["scoped"]), int(re.search(
            r"FieldType\); (\d+)\]", guard.SCOPED_RS.read_text(encoding="utf-8")).group(1)))
        self.assertEqual(len(kinds["checksum"]), int(re.search(
            r"FieldType\); (\d+)\]", guard.CHECKSUM_RS.read_text(encoding="utf-8")).group(1)))
        self.assertEqual(len(kinds["route"]), int(re.search(
            r"const ALL: \[Self; (\d+)\]", guard.ROUTES_RS.read_text(encoding="utf-8")).group(1)))
        self.assertGreater(len(kinds["alias"]), 0)
        self.assertGreater(len(kinds["remap"]), 0)

    def test_a_length_the_literals_do_not_fill_fails(self):
        src = guard.TABLE_RS.read_text(encoding="utf-8")
        n = int(re.search(r"\[OverlayEntry; (\d+)\]", src).group(1))
        with self.assertRaises(guard.EntryParseError):
            guard.parse_overlay_table(src.replace(f"[OverlayEntry; {n}]",
                                                  f"[OverlayEntry; {n - 1}]"))

    def test_a_literal_the_pattern_cannot_read_fails(self):
        src = ("pub static OVERLAY_TABLE: [OverlayEntry; 2] = [\n"
               '    OverlayEntry {\n        group_path: "/G",\n        field_name: "A",\n'
               "        field_type: FieldType::Bool,\n    },\n"
               '    OverlayEntry {\n        group_path: "/G",\n        field_name: B,\n'
               "        field_type: FieldType::Bool,\n    },\n];\n")
        with self.assertRaises(guard.EntryParseError):
            guard.parse_overlay_table(src)

    def test_a_continued_alias_path_is_joined(self):
        src = ('const GROUP_ALIASES: &[(&str, &str)] = &[\n    (\n        "/Game/A\\\n'
               '/B.B_C",\n        "/Game/C.C_C",\n    ),\n];\n')
        self.assertEqual(guard.parse_group_aliases(src), [("/Game/A/B.B_C", "/Game/C.C_C")])

    def test_routes_must_add_up_to_the_route_list(self):
        routes = "pub(super) const ALL: [Self; 2] = [\n    Self::One,\n    Self::Two,\n];\n"
        blobs = ('fn measured_array_route(\n) {\n    let route = match (a, b, c) {\n'
                 '        ("/G", "P", Some(1_2)) => MeasuredArrayRoute::One,\n'
                 "        _ => return None,\n    };\n}\n")
        rpc = ('let x = self.admits(MeasuredArrayRoute::Two)\n'
               '    && param_group_path_ref == Some("/G:F")\n'
               '    && param_name == Some("N")\n    && param_checksum == Some(3);\n')
        parsed = guard.parse_routes(blobs, rpc, routes)
        self.assertEqual([(e.group, e.name, e.checksum) for e in parsed],
                         [("/G", "P", 12), ("/G:F", "N", 3)])
        with self.assertRaises(guard.EntryParseError):
            guard.parse_routes(blobs, "", routes)


def write_export(directory: Path, build: str, main: dict, checkpoint=None):
    """A minimal export: `manifest.json`, and optionally the two checkpoint
    declaration tables. `checkpoint`: (groups rows, fields rows)."""
    directory.mkdir(parents=True)
    manifest = {"replay_build": f"++Ares-Core+release-{build}",
                "net_field_export_groups": [
                    {"path": g, "path_name_index": i,
                     "fields": [{"handle": h, "name": n, "compatible_checksum": c}
                                for n, c, h in fs]}
                    for i, (g, fs) in enumerate(main.items())]}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if checkpoint is not None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        groups, fields = checkpoint
        pq.write_table(pa.table({
            "checkpoint_index": pa.array([r[0] for r in groups], pa.uint32()),
            "ordinal": pa.array([r[1] for r in groups], pa.uint32()),
            "path_name_index": pa.array([r[2] for r in groups], pa.uint32()),
            "group_path": pa.array([r[3] for r in groups], pa.string()),
        }), directory / "checkpoint_export_groups.parquet")
        pq.write_table(pa.table({
            "checkpoint_index": pa.array([r[0] for r in fields], pa.uint32()),
            "group_ordinal": pa.array([r[1] for r in fields], pa.uint32()),
            "path_name_index": pa.array([r[2] for r in fields], pa.uint32()),
            "handle": pa.array([r[3] for r in fields], pa.uint32()),
            "compatible_checksum": pa.array([r[4] for r in fields], pa.uint32()),
            "rendered_name": pa.array([r[5] for r in fields], pa.string()),
        }), directory / "checkpoint_export_fields.parquet")


class LoadTests(TempDirTestCase):
    def test_a_checkpoint_only_declaration_counts_and_joins_on_the_ordinal(self):
        new = "/Game/New.New_C"
        tmp = self.tmp()
        d = tmp / "e"
        write_export(d, "13.01", {STATE: [("Other", 12, 4)]}, checkpoint=(
            [(0, 0, 5, STATE), (0, 1, 6, OLD), (0, 2, None, new),
             (1, 0, 5, STATE), (1, 0, 5, OLD)],        # (1, 0) twice
            [(0, 1, 6, 17, 21, "Deployed"),     # joins OLD
             (0, 7, 6, 17, 21, "Ghost"),        # no group ordinal 7
             (0, 1, 9, 17, 21, "Mismatch"),     # path index disagrees
             (0, 2, None, 17, 21, "NoIndex"),   # no path index on either side
             (0, 0, None, 17, 21, "HalfIndex")]))
        stats = guard.LoadStats()
        r = guard.load_export(d, stats)
        self.assertIn(OLD, r.groups)
        self.assertEqual({f for f in r.fields if f[0] != STATE},
                         {(OLD, "Deployed", 21, 17), (new, "NoIndex", 21, 17)})
        self.assertEqual((stats.orphan_checkpoint_fields, stats.path_index_mismatches,
                          stats.repeated_checkpoint_groups), (1, 2, 1))

    INCOMPLETE = [{"handle": 3, "name": "bArmed", "compatible_checksum": 11},
                  {"handle": 4, "compatible_checksum": 12},
                  {"handle": 5, "name": None, "compatible_checksum": 13},
                  {"handle": 6, "name": "bSafe"},
                  {"handle": 7, "name": "bLoud", "compatible_checksum": "14"}]

    def write_incomplete(self, directory: Path, build: str):
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text(json.dumps({
            "replay_build": f"++Ares-Core+release-{build}",
            "net_field_export_groups": [{"path": STATE, "fields": self.INCOMPLETE}]}),
            encoding="utf-8")

    def test_a_field_without_a_name_or_checksum_is_counted_not_declared(self):
        """Kept under None with no tally, a nameless field at a mapped handle
        would resolve through the handle alone: a guess."""
        tmp = self.tmp()
        d = tmp / "e"
        self.write_incomplete(d, "13.01")
        stats = guard.LoadStats()
        r = guard.load_export(d, stats)
        self.assertEqual(r.fields, frozenset({(STATE, "bArmed", 11, 3)}))
        self.assertEqual((stats.main_fields, stats.fields_without_identity), (5, 4))

    def test_a_field_without_a_name_or_checksum_fails_the_run(self):
        tmp = self.tmp()
        root = tmp / "exports"
        write_export(root / "a", "13.01", {STATE: [("bArmed", 11, 3)]})
        self.write_incomplete(root / "b", "13.02")
        empty = tmp / "empty.json"
        empty.write_text(json.dumps({"expected": []}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code = guard.main(["--root", str(root), "--expected", str(empty)])
        self.assertEqual(code, 1, out.getvalue() + err.getvalue())
        self.assertIn("with 6 field(s) (4 without a name or checksum)", out.getvalue())
        self.assertIn("without a name or checksum -- the input is inconsistent", err.getvalue())

    def test_a_checkpoint_field_joining_no_or_no_one_group_fails_the_run(self):
        cat = catalog(table(STATE, "bArmed"))
        reps = (many("13.01", 20, {STATE: [("bArmed", 11, 3)]})
                + many("13.02", 20, {STATE: [("bArmed", 11, 3)]}))
        for counter in ("orphan_checkpoint_fields", "path_index_mismatches",
                        "repeated_checkpoint_groups"):
            with self.subTest(counter=counter):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = guard.run(reps, cat, [], guard.LoadStats(**{counter: 1}))
                self.assertEqual(code, 1)
                self.assertIn("join no group", err.getvalue())

    def test_discovery_skips_and_lists_interrupted_exports(self):
        root = self.tmp()
        write_export(root / "a", "13.01", {STATE: []})
        write_export(root / ".a.vrfkit-staging-12-3", "13.01", {STATE: []})
        (root / "not-an-export").mkdir()
        stats = guard.LoadStats()
        found = guard.discover([root], [], stats)
        self.assertEqual([p.name for p in found], ["a"])
        self.assertEqual([p.name for p in stats.skipped], [".a.vrfkit-staging-12-3"])

    def test_main_reads_the_real_tables_and_the_committed_expected_list(self):
        """End to end: every committed item names a finding this input
        reproduces through the real tables, so each reads `expected`; with an
        empty list the run fails."""
        tmp = self.tmp()
        root = tmp / "exports"
        write_committed_findings(root)
        empty = tmp / "empty.json"
        empty.write_text(json.dumps({"expected": []}), encoding="utf-8")
        report = tmp / "report.json"
        with contextlib.redirect_stdout(io.StringIO()) as out:
            listed = guard.main(["--root", str(root), "--json", str(report)])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            unlisted = guard.main(["--root", str(root), "--expected", str(empty)])
        data = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(listed, 0, out.getvalue())
        self.assertEqual(unlisted, 1)
        self.assertIn("FAILED: 2 entr(y/ies)", err.getvalue())
        self.assertEqual(data["stale_expected"], [])
        economy = [f for f in data["findings"] if f["entry"].endswith("|TeamEconomy")]
        self.assertEqual([(f["category"], f["evidenced"], f["expected"]) for f in economy],
                         [("field-missing", True, True)])
        # 13.02 judges the old group again (12 + 12 replays do not fill the
        # reference window), as vanished and weak; only 13.01 is the move.
        stopped = [f for f in data["findings"]
                   if f["entry"] == f"table|{CAGE_OLD}|HasStopped" and f["build"] == "13.01"]
        self.assertEqual([(f["category"], f["coverage"], f["evidenced"], f["expected"])
                          for f in stopped], [("moved", "lost", True, True)])
        self.assertEqual(stopped[0]["successors"][0]["group"], CAGE_NEW)
        self.assertEqual(stopped[0]["coverage_detail"], "the successor does not declare HasStopped")
        self.assertRegex(out.getvalue(), r"expected\s+lost\s+table\s+HasStopped: the successor "
                                         r"does not declare HasStopped")
        self.assertEqual(sum(f["fails"] for f in data["findings"]), 0)

    def test_failures_name_the_expected_list_that_was_read(self):
        """FAILED lines name the list --expected read, not the default, or they
        send the reader to the wrong file."""
        stale = {"entry": f"table|{STATE}|bArmed", "build": "13.02",
                 "finding": "field-missing", "reason": "r", "evidence": "e"}
        tmp = self.tmp()
        root = tmp / "exports"
        write_committed_findings(root)
        path = tmp / "other_expected.json"
        path.write_text(json.dumps({"expected": [stale]}), encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = guard.main(["--root", str(root), "--expected", str(path)])
        self.assertEqual(code, 1)
        failed = [line for line in err.getvalue().splitlines() if line.startswith("FAILED:")]
        self.assertEqual(len(failed), 2, err.getvalue())
        self.assertIn("are not in other_expected.json", failed[0])
        self.assertIn("item(s) of other_expected.json match no failing finding", failed[1])
        self.assertNotIn(guard.EXPECTED_JSON.name, err.getvalue())

    def test_each_committed_item_is_needed(self):
        """Drop either committed item and its finding fails the run by name --
        the list honours HasStopped only while the item is there."""
        items = json.loads(guard.EXPECTED_JSON.read_text(encoding="utf-8"))["expected"]
        # Not a size pin: `write_committed_findings` must reproduce every
        # committed item, so an item added to the list fails here naming what
        # to add there, rather than reading STALE in the end-to-end test.
        self.assertEqual(sorted(i["entry"].rsplit("|", 1)[1] for i in items),
                         ["HasStopped", "TeamEconomy"])
        tmp = self.tmp()
        root = tmp / "exports"
        write_committed_findings(root)
        for dropped in items:
            name = dropped["entry"].rsplit("|", 1)[1]
            with self.subTest(dropped=name):
                path = tmp / "partial.json"
                path.write_text(json.dumps({"expected": [i for i in items if i is not dropped]}),
                                encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()) as out, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    code = guard.main(["--root", str(root), "--expected", str(path)])
                self.assertEqual(code, 1)
                self.assertIn("FAILED: 1 entr(y/ies)", err.getvalue())
                self.assertNotIn("STALE", err.getvalue())
                self.assertRegex(out.getvalue(), rf"FAIL\s.*\b{name}\b")


#: Cypher's cage-trap projectile before and after the 13.01 move, with the
#: fields, handles and checksums declaration corpus r3 has for them: the old
#: group adds HasStopped, the successor never declares it.
CAGE_OLD = ("/Game/Characters/Gumshoe/S0/Ability_4/Projectile_Gumshoe_4_CageTrap."
            "Projectile_Gumshoe_4_CageTrap_C")
CAGE_NEW = ("/Game/Characters/Gumshoe/S0/Ability_Q/Projectile_Gumshoe_Q_CageTrap."
            "Projectile_Gumshoe_Q_CageTrap_C")
CAGE_FIELDS = [("216", 4109980037, 3), ("ReplicatedMovement", 2749104612, 10),
               ("Owner", 1022089157, 11), ("215", 1710918439, 12), ("Instigator", 220456479, 13)]
HAS_STOPPED = ("HasStopped", 3667634484, 15)


def write_committed_findings(root: Path):
    """Exports that reproduce both committed findings through the real
    tables, 12 replays a build: the cage projectile declared with HasStopped
    in 12.08 and moved without it in 13.01; `TeamEconomy` declared in 13.01
    and replaced by `TeamStates` in 13.02."""
    bomb = "/Game/GameModes/Bomb/BombGameState.BombGameState_C"
    for i in range(12):
        write_export(root / f"c{i}", "12.08", {CAGE_OLD: CAGE_FIELDS + [HAS_STOPPED]})
        write_export(root / f"a{i}", "13.01", {bomb: [("TeamEconomy", 338800366, 52)],
                                               CAGE_NEW: CAGE_FIELDS})
        write_export(root / f"b{i}", "13.02", {bomb: [("TeamStates", 929598027, 52)]})
