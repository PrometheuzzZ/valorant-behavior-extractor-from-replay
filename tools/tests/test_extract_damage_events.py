"""`extract_damage_events.py`: one row per invocation, the victim from the row's
actor, sentinels as null, and a kill feed that pairs with characterDeath."""
from __future__ import annotations

import json

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase, run_cli
import extract_damage_events as damage

POINT = "MulticastNotifyDamage_Point"
STATE_GROUP = "/Game/GameModes/Bomb/BombPlayerState.BombPlayerState_C"
VANDAL = "/Game/Equippables/Guns/Rifles/AK/AssaultRifle_AK.AssaultRifle_AK_C"
PAWN, WALL, GUN, STATE = 40, 50, 60, 7


def invocation(t, actor, *, killed=False, sentinel=False, character=None):
    """One _Point invocation's parameter rows as (time, actor, name, i64, f64, bool, str)."""
    return [(t, actor, f"{POINT}.{name}", i, f, b, s) for name, i, f, b, s in (
        ("DamageDealt", None, 30.0, None, None), ("bDamageKilledTarget", None, None, killed, None),
        ("DamageOrigin", None, None, None, "(1,2,3)"), ("EquippableUsed", GUN, None, None, None),
        ("LifeChangeEvents[0].DeltaLife", None, -30.0, None, None),
        ("DamagerPlayerState", STATE, None, None, None),
        ("Character", actor if character is None else character, None, None, None),
        ("NetTimestamp", None, -3.4028234663852886e38 if sentinel else 4.5, None, None),
        ("RespawnNumber", -1 if sentinel else 0, None, None, None))]


class ExtractDamageEventsTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()
        self.export = self.root / "export"
        self.export.mkdir()

    def write(self, rows, deaths, *, shared_pawn=False):
        """`shared_pawn`: a second PlayerState names the same pawn, a conflict."""
        states = [STATE, STATE + 1] if shared_pawn else [STATE]
        rows = [(0, 1, state, "SpawnedCharacter", PAWN, None, None, None, STATE_GROUP) for state in states] + [
            (t, 9, actor, name, i, f, b, s, "/Script/ShooterGame.DamageableComponent_ClassNetCache")
            for t, actor, name, i, f, b, s in rows]
        names = ("time_ms", "packet_id", "actor_net_guid", "field_name", "value_i64", "value_f64",
                 "value_bool", "value_str", "group_path")
        table = {name: [r[i] for r in rows] for i, name in enumerate(names)}
        table["object_net_guid"] = [None] * len(rows)
        table["channel_index"] = [3] * len(rows)
        pq.write_table(pa.table(table), self.export / "fields.parquet")
        pq.write_table(pa.table({"actor_net_guid": [PAWN, WALL, GUN],
                                 "class_path": ["/Game/Characters/Wushu/Wushu_PC.Wushu_PC_C",
                                                "/Game/X/Wall.Wall_C", VANDAL]}),
                       self.export / "actors.parquet")
        pq.write_table(pa.table({"group": ["characterDeath"] * len(deaths),
                                 "time1": pa.array([d[0] for d in deaths], pa.uint32()),
                                 "word1": pa.array([d[1] for d in deaths], pa.uint32())}),
                       self.export / "events.parquet")
        (self.export / "manifest.json").write_text(json.dumps({"players": [
            {"actor_net_guid": state, "subject": f"subject-{state}", "character_net_guid": PAWN}
            for state in states]}), encoding="utf-8")
        return self.export

    def run_main(self):
        out = self.root / "damage.parquet"
        return *run_cli(damage.main, "--export", self.export, "--out", out)[:2], out

    def test_two_invocations_in_one_bunch_split_on_the_repeated_parameter(self):
        rows, counts, problems = damage.build(self.write(invocation(100, PAWN) + invocation(100, PAWN), []))
        self.assertEqual((len(rows), counts["repeated-parameter splits"], problems), (2, 1, []))

    def test_the_victim_is_the_rows_actor_and_names_resolve(self):
        rows, _, _ = damage.build(self.write(invocation(100, WALL, character=0), []))
        row = rows[0]
        self.assertEqual((row["victim_actor_net_guid"], row["victim_class_path"], row["victim_subject"]),
                         (WALL, "/Game/X/Wall.Wall_C", None))
        self.assertEqual((row["damager_subject"], row["weapon_name"], row["origin_z"]),
                         (f"subject-{STATE}", "Vandal", 3.0))
        self.assertEqual(damage.build(self.write(invocation(100, PAWN), []))[0][0]["victim_subject"],
                         f"subject-{STATE}")

    def test_sentinels_become_null_and_are_counted(self):
        rows, counts, _ = damage.build(self.write(invocation(100, WALL, sentinel=True), []))
        self.assertEqual((rows[0]["net_timestamp"], rows[0]["respawn_number"]), (None, None))
        self.assertEqual(counts["sentinels nulled"], 2)
        rows, _, _ = damage.build(self.write(invocation(100, WALL), []))
        self.assertEqual((rows[0]["net_timestamp"], rows[0]["respawn_number"]), (4.5, 0))

    def test_the_kill_feed_pairs_body_kills_with_characterdeath(self):
        kills = invocation(108, PAWN, killed=True) + invocation(200, WALL, killed=True)
        rows, counts, problems = damage.build(self.write(kills, [(100, PAWN)]))
        self.assertEqual((counts["kill feed pairs"], problems), (1, []))
        self.assertEqual(self.run_main()[0], 0)
        _, counts, problems = damage.build(self.write(kills, []))
        self.assertEqual(counts["killing blows without a death"], 1)
        self.assertEqual(problems, ["the kill feed does not pair one to one with characterDeath"])
        self.assertEqual(self.run_main()[0], 1)
        _, counts, _ = damage.build(self.write(kills, [(100 - damage.KILL_FEED_SLACK_MS, PAWN)]))
        self.assertEqual(counts["deaths without a killing blow"], 1)
        _, counts, _ = damage.build(self.write(kills, [(118, PAWN)]))  # the death after the blow
        self.assertEqual(counts["killing blows without a death"], 1)

    def test_a_pawn_two_players_claim_is_still_a_body_in_the_kill_feed(self):
        rows, counts, problems = damage.build(
            self.write(invocation(108, PAWN, killed=True), [(100, PAWN)], shared_pawn=True))
        self.assertIsNone(rows[0]["victim_subject"])
        self.assertEqual((counts["kill feed pairs"], problems), (1, []))
        stdout = self.run_main()[1]
        self.assertIn("  player_identity pawns_claimed_by_multiple_player_states: 1", stdout)
        self.assertIn("  player_identity conflicting_subject_pawns: 1", stdout)

    def test_odd_values_are_counted_and_guid_0_is_null(self):
        def patch(rows, values):
            return [(t, a, n, *values.get(n.partition(".")[2], v)) for t, a, n, *v in rows]
        rows = patch(invocation(100, WALL), {"DamageDealt": (None,) * 4, "DamageOrigin": (None,) * 3 + ("bad",),
                                             "EquippableUsed": (GUN + 1,) + (None,) * 3})
        rows += patch(invocation(200, WALL), {"EquippableUsed": (0,) + (None,) * 3,
                                              "DamagerPlayerState": (0,) + (None,) * 3})
        self.write(rows, [])
        pq.write_table(pa.table({"actor_net_guid": [WALL, WALL],
                                 "class_path": ["/Game/X/Wall.Wall_C", "/Game/X/Door.Door_C"]}),
                       self.export / "actors.parquet")
        rows, counts, _ = damage.build(self.export)
        odd = ("untyped parameter rows", "unparsed vectors", "actor GUIDs with two classes",
               "weapon GUIDs without a class")
        self.assertEqual([counts[key] for key in odd], [1, 1, 1, 1])
        self.assertIsNone(rows[0]["victim_class_path"])
        self.assertEqual((rows[1]["weapon_net_guid"], rows[1]["damager_player_state"]), (None, None))

    def test_the_cli_writes_the_table_and_prints_every_count(self):
        self.write(invocation(100, WALL), [])
        code, stdout, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(pq.read_table(out).schema, damage.SCHEMA)
        for key in damage.COUNT_KEYS:
            self.assertIn(f"  {key}: ", stdout)
