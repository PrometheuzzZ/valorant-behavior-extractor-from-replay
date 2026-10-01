"""Guards for the C# comparison report: a report, not a gate, but one that
must not read an empty C# side as total coverage, and must exit nonzero when
it measured nothing.
"""
import json
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase, run_cli
import compare_with_csharp as guard


PAIR_A = ("/Script/ShooterGame.Thing", "Health")
PAIR_B = ("/Script/ShooterGame.Thing", "Armor")


class CoverageProblemTests(unittest.TestCase):
    def test_an_overlapping_comparison_has_no_problems(self):
        self.assertEqual(guard.coverage_problems({PAIR_A}, {PAIR_A, PAIR_B}), [])

    def test_an_empty_csharp_side_is_not_full_coverage(self):
        problems = guard.coverage_problems(set(), {PAIR_A})
        self.assertTrue(problems)
        self.assertIn("C#", " ".join(problems))

    def test_an_empty_vrfkit_side_is_not_a_comparison_either(self):
        self.assertTrue(guard.coverage_problems({PAIR_A}, set()))

    def test_two_sides_sharing_nothing_is_total_disagreement(self):
        problems = guard.coverage_problems({PAIR_A}, {PAIR_B})
        self.assertTrue(problems)
        self.assertIn("no", " ".join(problems).lower())

    def test_missing_pairs_alone_are_not_reported_here(self):
        """C#-only pairs are the report's subject, listed under INVESTIGATE,
        not a gate: an acceptable miss cannot be decided without the corpus."""
        self.assertEqual(guard.coverage_problems({PAIR_A, PAIR_B}, {PAIR_A}), [])


class CoverageTextTests(TempDirTestCase):
    def test_unattributed_rows_are_counted_without_becoming_named_coverage(self):
        root = self.tmp()
        events = root / "events.ndjson"
        events.write_text("\n".join(json.dumps(row) for row in [
            {"type": "export_group_received", "export_group_path": PAIR_A[0],
             "payload": {"Health": 100}},
            {"type": "export_group_received", "export_group_path": None,
             "payload": {"Health": 100}},
        ]), encoding="utf-8")
        parquet = root / "fields.parquet"
        pq.write_table(pa.table({
            "group_path": [PAIR_A[0], PAIR_A[0], None, PAIR_A[0]],
            "field_name": ["Health", None, "Health",
                           guard.UNRESOLVED_CLASS_NET_CACHE_PAYLOAD_FIELD_NAME],
        }), parquet)
        report, problems = guard.compare_group_field_coverage(events, parquet)
        self.assertEqual(problems, [])
        self.assertIn("Distinct (group, field) pairs from vrfkit: 1", report)
        self.assertIn("vrfkit rows without a group/name: 2 (excluded)", report)
        self.assertIn("C# payload fields without a group/name: 1 (excluded)", report)

    def test_full_coverage_is_only_claimed_when_something_was_compared(self):
        lines = guard.coverage_lines({PAIR_A}, {PAIR_A, PAIR_B})
        self.assertIn("covers everything", " ".join(lines))

    def test_nothing_compared_never_claims_full_coverage(self):
        lines = guard.coverage_lines(set(), {PAIR_A})
        self.assertNotIn("covers everything", " ".join(lines))

    def test_a_real_miss_is_still_listed_for_investigation(self):
        lines = guard.coverage_lines({PAIR_A, PAIR_B}, {PAIR_A})
        joined = " ".join(lines)
        self.assertIn("INVESTIGATE", joined)
        self.assertIn("Armor", joined)


class RpcNameTests(TempDirTestCase):
    """vrfkit writes no RPC-name column or manifest key: its RPC names are the
    `Function.` prefixes of its ClassNetCache rows, the rows
    to_valplay_bundle.py builds rpc_received from."""

    CNC = "/Script/ShooterGame.Thing_ClassNetCache"

    def section_4(self) -> str:
        root = self.tmp()
        cs, vk = root / "cs", root / "vk"
        cs.mkdir()
        vk.mkdir()
        (cs / "manifest.json").write_text("{}", encoding="utf-8")
        (vk / "manifest.json").write_text("{}", encoding="utf-8")
        (cs / "events.ndjson").write_text("".join(json.dumps(row) + "\n" for row in [
            {"type": "export_group_received", "export_group_path": PAIR_A[0],
             "payload": {"Health": 100}},
            {"type": "rpc_received", "function_name": "MulticastShared"},
            {"type": "rpc_received", "function_name": "MulticastShared"},
            {"type": "rpc_received", "function_name": "ClientCsharpOnly"},
        ]), encoding="utf-8")
        rows = [
            (PAIR_A[0], "Health"),
            (self.CNC, "MulticastShared.Damage"),
            (self.CNC, "MulticastShared.Target"),
            (self.CNC, "ZeroParamOnly"),        # a zero-parameter RPC is its bare name
            (self.CNC, guard.UNRESOLVED_CLASS_NET_CACHE_PAYLOAD_FIELD_NAME),
            (self.CNC, None),
            (PAIR_A[0], "Rounds[3].Score"),     # an array leaf, not an RPC
        ]
        pq.write_table(pa.table({"group_path": [g for g, _ in rows],
                                 "field_name": [f for _, f in rows]}),
                       vk / "fields.parquet")
        _, report, _ = run_cli(guard.main, cs, vk, prog="compare_with_csharp.py")
        return report.split("## 4. RPC name comparison", 1)[1].split("## 5.", 1)[0]

    def test_vrfkit_rpc_names_come_from_class_net_cache_prefixes(self):
        section = self.section_4()
        self.assertIn("vrfkit RPC distinct names: 2", section)
        self.assertIn("C# only: 1", section)
        self.assertIn("vrfkit only: 1", section)
        self.assertIn("Both: 1", section)
        self.assertRegex(section, r"C# only, INVESTIGATE \(1\):\n\s+ClientCsharpOnly")
        self.assertRegex(section, r"vrfkit only, sample \(1\):\n\s+ZeroParamOnly")
        self.assertNotIn("Rounds[3]", section)
        self.assertNotIn("rpcs_by_name", section)


class MovementMultiplicityTests(TempDirTestCase):
    def compare(self, csharp_rows: list[dict], vrfkit_rows: list[dict]) -> str:
        root = self.tmp()
        csharp = root / "movement.ndjson"
        parquet = root / "movement.parquet"
        csharp.write_text(
            "".join(json.dumps(row) + "\n" for row in csharp_rows),
            encoding="utf-8",
        )
        table = pa.table(
            {
                "time_ms": pa.array([r["time_ms"] for r in vrfkit_rows], pa.uint32()),
                "character_net_guid": pa.array(
                    [r["character_net_guid"] for r in vrfkit_rows], pa.uint32()
                ),
                "pos_x": pa.array([r.get("pos_x", 0.0) for r in vrfkit_rows]),
                "pos_y": pa.array([r.get("pos_y", 0.0) for r in vrfkit_rows]),
                "pos_z": pa.array([r.get("pos_z", 0.0) for r in vrfkit_rows]),
                "yaw": pa.array([r.get("yaw", 0.0) for r in vrfkit_rows]),
                "pitch": pa.array([r.get("pitch", 0.0) for r in vrfkit_rows]),
                "vel_x": pa.array([r.get("vel_x", 0.0) for r in vrfkit_rows]),
                "vel_y": pa.array([r.get("vel_y", 0.0) for r in vrfkit_rows]),
                "vel_z": pa.array([r.get("vel_z", 0.0) for r in vrfkit_rows]),
            }
        )
        pq.write_table(table, parquet)
        return guard.compare_movement(csharp, parquet)

    @staticmethod
    def csharp_row(x: float) -> dict:
        return {
            "time_ms": 100,
            "shooter_character_net_guid": 42,
            "position": {"x": x, "y": 0, "z": 0},
            "velocity": {"x": 0, "y": 0, "z": 0},
            "yaw": 0,
            "pitch": 0,
        }

    def test_one_vrfkit_row_cannot_satisfy_two_duplicate_references(self):
        report = self.compare(
            [self.csharp_row(1), self.csharp_row(2)],
            [{"time_ms": 100, "character_net_guid": 42, "pos_x": 1}],
        )
        self.assertIn("Joined: 1 / 2", report)
        self.assertRegex(report, r"Missed .*: 1")

    def test_duplicate_rows_on_both_sides_are_all_compared(self):
        report = self.compare(
            [self.csharp_row(1), self.csharp_row(2)],
            [
                {"time_ms": 100, "character_net_guid": 42, "pos_x": 1},
                {"time_ms": 100, "character_net_guid": 42, "pos_x": 2},
            ],
        )
        self.assertIn("Joined: 2 / 2", report)

    def test_a_value_the_reference_lacks_is_not_compared_as_zero(self):
        row = self.csharp_row(1)
        del row["yaw"]
        report = self.compare([row], [{"time_ms": 100, "character_net_guid": 42,
                                       "pos_x": 1, "yaw": 170.0}])
        self.assertIn("Yaw: no data", report)
        self.assertIn("Pitch (1 rows)", report)
