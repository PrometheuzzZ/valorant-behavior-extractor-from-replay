"""`extract_rounds.py`: rows come from the phase RPCs, never from RoundResults."""
from __future__ import annotations


import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase, run_cli
import extract_rounds as rounds


def phase_rows(reset, *, buy_end=True, post=True):
    """One round's phase RPC rows, each named RPC in its phase's frame."""
    rows = [(reset, rounds.SET_PHASE, 2, None), (reset, "ClientResetRound", None, None),
            (reset + 10, rounds.SET_PHASE, 3, None), (reset + 10, "ClientRoundStart", None, None)]
    if buy_end:
        rows += [(reset + 100, rounds.SET_PHASE, 4, None), (reset + 100, "ClientBuyPhaseEnd", None, None)]
    if post:
        rows.append((reset + 500, rounds.SET_PHASE, 5, None))
    return rows


def result_rows(t, index, team, role, result):
    prefix = f"RoundResults[{index}]."
    return [(t, prefix + "RoundNumber", index, None), (t, prefix + "WinningTeam", None, team),
            (t, prefix + "WinningTeamRole", None, role), (t, prefix + "RoundResult", None, result)]


EVENTS = [("roundStarted", 1011, 0), ("spikePlanted", 2200, None),
          ("spikeExploded", 2490, None), ("roundStarted", 2010, 1)]


class ExtractRoundsTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()
        self.export = self.root / "export"
        self.export.mkdir()

    def write(self, fields, events):
        pq.write_table(pa.table({
            "time_ms": pa.array([r[0] for r in fields], pa.uint32()),
            "field_name": pa.array([r[1] for r in fields], pa.string()).dictionary_encode(),
            "value_i64": pa.array([r[2] for r in fields], pa.int64()),
            "value_str": pa.array([r[3] for r in fields], pa.string())}), self.export / "fields.parquet")
        pq.write_table(pa.table({
            "group": pa.array([e[0] for e in events], pa.string()),
            "time1": pa.array([e[1] for e in events], pa.uint32()),
            "word0": pa.array([e[2] for e in events], pa.uint32())}), self.export / "events.parquet")
        return self.export

    def two_rounds(self, extra=(), events=EVENTS, *, move_buy_end=0):
        fields = [*phase_rows(1000), *result_rows(1500, 0, "Blue", "defender", "elimination"),
                  *phase_rows(2000), *result_rows(2500, 1, "Red", "attacker", "detonate"), *extra]
        fields = [(t + move_buy_end if (name, value) == (rounds.SET_PHASE, 4) else t, name, value, text)
                  for t, name, value, text in sorted(fields, key=lambda r: r[0])]
        return self.write(fields, events)

    def run_main(self, out):
        return run_cli(rounds.main, "--export", self.export, "--out", out)

    def test_a_round_is_its_phases_joined_to_its_events_and_result(self):
        rows, counts, problems = rounds.build(self.two_rounds())
        self.assertEqual(problems, [])
        self.assertEqual([(r["round_number"], r["reset_ms"], r["start_ms"], r["buy_end_ms"],
                           r["post_round_ms"]) for r in rows],
                         [(0, 1000, 1010, 1100, 1500), (1, 2000, 2010, 2100, 2500)])
        self.assertEqual([(r["winning_team"], r["attacker_team"], r["result"]) for r in rows],
                         [("Blue", "Red", "elimination"), ("Red", "Red", "detonate")])
        self.assertEqual((rows[1]["plant_ms"], rows[1]["explode_ms"], rows[0]["plant_ms"]),
                         (2200, 2490, None))
        self.assertEqual(counts["max |roundStarted - start_ms| ms"], 1)

    def test_a_buy_phase_end_off_its_rpc_fails_and_writes_nothing(self):
        self.two_rounds(move_buy_end=7)
        out = self.root / "rounds.parquet"
        code, _, stderr = self.run_main(out)
        self.assertEqual(code, 1)
        self.assertIn("phase 4", stderr)
        self.assertFalse(out.exists())

    def test_an_overtime_switch_resets_twice_in_one_frame(self):
        rows, counts, problems = rounds.build(self.two_rounds([(2000, "ClientResetRound", None, None)]))
        self.assertEqual((len(rows), problems), (2, []))
        self.assertEqual(counts["named RPC repeated in its frame"], 1)

    def test_a_reset_that_never_starts_a_round_is_dropped_and_counted(self):
        extra = [(3000, rounds.SET_PHASE, 2, None), (3000, "ClientResetRound", None, None)]
        rows, counts, problems = rounds.build(self.two_rounds(extra))
        self.assertEqual((len(rows), problems), (2, []))
        self.assertEqual(counts["unstarted final round (dropped)"], 1)

    def test_surrender_padding_is_counted_never_a_row(self):
        padding = [row for i in range(2, 13) for row in result_rows(2600, i, "Red", "attacker", "surrendered")]
        rows, counts, problems = rounds.build(self.two_rounds(padding))
        self.assertEqual((len(rows), problems), (2, []))
        self.assertEqual(counts["RoundResults awarded, not played"], 11)

    def test_results_join_by_round_number_not_by_position(self):
        fields = [*phase_rows(1000), *phase_rows(2000),
                  *result_rows(2600, 6, "Red", "attacker", "defuse"),
                  *result_rows(2600, 5, "Blue", "attacker", "detonate")]
        rows, _, problems = rounds.build(self.write(fields, [("roundStarted", 1010, 5),
                                                             ("roundStarted", 2010, 6)]))
        self.assertEqual(problems, [])
        self.assertEqual([(r["round_number"], r["result"]) for r in rows], [(5, "detonate"), (6, "defuse")])

    def test_an_unplayed_result_that_is_not_a_surrender_is_a_problem(self):
        _, _, problems = rounds.build(self.two_rounds(result_rows(2600, 5, "Red", "attacker", "elimination")))
        self.assertEqual(problems, ["RoundResults round 5 (elimination) has no played round"])

    def test_a_repeated_or_unknown_phase_opens_no_round(self):
        rows, counts, problems = rounds.build(self.two_rounds([(1050, rounds.SET_PHASE, 3, None),
                                                               (1060, rounds.SET_PHASE, 7, None)]))
        self.assertEqual((len(rows), rows[0]["start_ms"], problems), (2, 1010, []))
        self.assertEqual((counts["phase repeated in a round (first kept)"], counts["phase other"]), (1, 1))

    def test_a_missing_phase_stays_null_and_a_side_switch_joins_the_round_before(self):
        def fields(rpc_ms):
            return [*phase_rows(1000), (1600, rounds.SET_PHASE, 6, None),
                    (rpc_ms, "Multicast Side Switch Event", None, None),
                    *phase_rows(2000, buy_end=False, post=False)]
        rows, _, problems = rounds.build(self.write(fields(1600), []))
        self.assertEqual(problems, [])
        self.assertEqual([(r["side_switch_ms"], r["buy_end_ms"], r["round_number"]) for r in rows],
                         [(1600, 1100, None), (None, None, None)])
        self.write(fields(1601), [])
        code, _, stderr = self.run_main(self.root / "rounds.parquet")
        self.assertEqual(code, 1)
        self.assertIn("phase 6", stderr)

    def test_a_second_plant_in_a_round_is_left_null_and_counted(self):
        events = [("spikePlanted", 2200, None), ("spikePlanted", 2300, None), ("roundStarted", 500, 9)]
        rows, counts, _ = rounds.build(self.two_rounds(events=events))
        self.assertIsNone(rows[1]["plant_ms"])
        self.assertEqual(counts["plant_ms repeated in a round (left null)"], 1)
        self.assertEqual(counts["round_number before the first round"], 1)

    def test_the_cli_writes_the_table_and_prints_every_count(self):
        self.two_rounds()
        out = self.root / "rounds.parquet"
        code, stdout, _ = self.run_main(out)
        self.assertEqual(code, 0)
        self.assertEqual(pq.read_table(out).schema, rounds.SCHEMA)
        for key in rounds.COUNT_KEYS:
            self.assertIn(f"  {key}: ", stdout)
        self.assertIn("  non-null: round_ordinal 2, round_number 2, reset_ms 2", stdout)
        self.assertEqual(self.run_main(self.export / "fields.parquet")[0], 1)
