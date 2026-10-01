"""`extract_active_effects.py`: a dormant actor is not despawned, and its
open-ended instance is counted as `went_dormant`; the classifier's keyword
false positives stay excluded."""
from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase
import extract_active_effects as effects

CLASS = "/Game/Characters/Pandemic/S0/Ability_Q/GameObject_Pandemic_Q_Smoke.GameObject_X_C"


def _export(tmp: Path, rows: list[tuple[int, str, int]]) -> Path:
    """Write a minimal `actors.parquet` of `(guid, event, time_ms)` rows."""
    n = len(rows)
    table = pa.table({
        "actor_net_guid": pa.array([r[0] for r in rows], pa.int64()),
        "event": pa.array([r[1] for r in rows], pa.string()),
        "time_ms": pa.array([r[2] for r in rows], pa.int64()),
        "class_path": pa.array([CLASS] * n, pa.string()),
        "spawn_x": pa.array([1.0] * n, pa.float64()),
        "spawn_y": pa.array([2.0] * n, pa.float64()),
        "spawn_z": pa.array([3.0] * n, pa.float64()),
    })
    out = tmp / "export"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out / "actors.parquet")
    return out


class DormantCloseTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()

    def test_a_dormant_actor_does_not_end_its_effect_instance(self):
        """Dormancy is not destruction: the instance stays open-ended."""
        rows = effects.build_with_tally(_export(self.root, [(7, "open", 100), (7, "dormant", 500)]))[0]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["close_ms"])
        self.assertIsNone(rows[0]["duration_ms"])

    def test_a_dormant_instance_is_counted_rather_than_quietly_open_ended(self):
        """The table alone cannot tell it from a row the export cut off."""
        out = _export(self.root, [(7, "open", 100), (7, "dormant", 500)])
        self.assertEqual(effects.build_with_tally(out)[1]["went_dormant"], 1)

    def test_a_real_close_still_ends_the_instance(self):
        rows = effects.build_with_tally(_export(self.root, [(7, "open", 100), (7, "close", 500)]))[0]
        self.assertEqual((rows[0]["close_ms"], rows[0]["duration_ms"]), (500, 400))
        self.assertEqual(
            effects.build_with_tally(_export(self.root, [(7, "open", 100), (7, "close", 500)]))[1]
            ["went_dormant"], 0)

    def test_an_actor_that_wakes_after_dormancy_keeps_one_instance(self):
        """Never gone: two instances would be a false despawn/respawn pair."""
        out = _export(self.root, [(7, "open", 100), (7, "dormant", 300), (7, "close", 900)])
        rows, tally = effects.build_with_tally(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["open_ms"], rows[0]["close_ms"]), (100, 900))
        self.assertEqual(tally["went_dormant"], 1)


GIANTSLAYER = ("/Game/Characters/Deadeye/S0/Ability_X/Gun_Giantslayer/"
               "Gun_Deadeye_X_Giantslayer_Prototype_FIreRatePrototype."
               "Gun_Deadeye_X_Giantslayer_Prototype_FireRatePrototype_C")
BREACH_FLASH = ("/Game/Characters/Breach/S0/Ability_Q/Projectile_Breach_Q_ThroughWalls_Flash."
                "Projectile_Breach_Q_ThroughWalls_Flash_C")
PHOENIX_WALL = ("/Game/Characters/Phoenix/S0/Ability_Q/Production/"
                "Projectile_Phoenix_Q_FlameWall_ThroughWall."
                "Projectile_Phoenix_Q_FlameWall_ThroughWall_C")
ORBITAL_STRIKE = ("/Game/Characters/Sarge/S0/Ability_OrbitalStrike/"
                  "GameObject_Sarge_X_OrbitalStrike_Production."
                  "GameObject_Sarge_X_OrbitalStrike_Production_C")
ULT_ORB = "/Game/GameObjects/CollectibleOrbs/UltPointOrb.UltPointOrb_C"


class ClassifierTests(unittest.TestCase):
    """Substring keywords that match names they were not written for."""

    def test_chambers_ult_gun_is_not_an_effect(self):
        """'fire' inside 'FIreRate': an equippable, not a zone."""
        self.assertFalse(effects.is_effect_class(GIANTSLAYER))

    def test_breachs_flash_through_walls_is_not_a_wall(self):
        """'wall' inside 'ThroughWalls': a flash projectile, not an effect."""
        self.assertFalse(effects.is_effect_class(BREACH_FLASH))

    def test_classify_reads_the_name_the_filter_reads(self):
        """Not needed by the corpus: a 'ThroughWall' name with a real keyword
        is classified by that keyword, never as the wall the filter refused."""
        path = "/Game/Characters/X/S0/Ability_Q/Projectile_X_ThroughWall_Trap.Projectile_X_ThroughWall_Trap_C"
        self.assertTrue(effects.is_effect_class(path))
        self.assertEqual(effects.classify(path), "trap")

    def test_phoenixs_flame_wall_through_wall_is_still_a_wall(self):
        self.assertTrue(effects.is_effect_class(PHOENIX_WALL))
        self.assertEqual(effects.classify(PHOENIX_WALL), "wall")

    def test_brimstones_orbital_strike_is_a_damage_zone_not_an_orb(self):
        """'orb' inside 'OrbitalStrike', 4-9 s of area damage."""
        self.assertTrue(effects.is_effect_class(ORBITAL_STRIKE))
        self.assertEqual(effects.classify(ORBITAL_STRIKE), "damage_zone")

    def test_the_ult_orb_is_still_an_orb(self):
        self.assertTrue(effects.is_effect_class(ULT_ORB))
        self.assertEqual(effects.classify(ULT_ORB), "orb")


class ActorKindTests(TempDirTestCase):
    """The table keeps a projectile and the zone it places as two rows, by
    design; `actor_kind` lets a consumer count one of them."""

    def test_actor_kind_is_the_class_leaf_prefix(self):
        for path, kind in (
                ("/Game/Characters/Wraith/S0/Ability_4/Projectile_Wraith_4_Smoke."
                 "Projectile_Wraith_4_Smoke_C", "projectile"),
                ("/Game/Characters/Wraith/S0/Ability_4/Zone_Wraith_4_Smoke."
                 "Zone_Wraith_4_Smoke_C", "zone"),
                (CLASS, "game_object"),
                ("/Game/Characters/Phoenix/S0/Ability_4/Production/NewMolotov/"
                 "Patch_Phoenix_MolotovFire.Patch_Phoenix_MolotovFire_C", "patch"),
                ("/Game/Characters/Hunter/S0/Ability_E/Drone/Pawn_Hunter_E_Drone."
                 "Pawn_Hunter_E_Drone_C", "pawn"),
                (ULT_ORB, "other")):
            with self.subTest(path=path):
                self.assertEqual(effects.actor_kind(path), kind)

    def test_every_row_carries_its_actor_kind(self):
        temp = self.tmp()
        rows = effects.build_with_tally(_export(temp, [(7, "open", 100), (7, "close", 500)]))[0]
        self.assertEqual(rows[0]["actor_kind"], "game_object")
        self.assertIn("actor_kind", effects.SCHEMA.names)


SMOKE = "/Game/Characters/Wushu/S0/Ability_4/Projectile_Wushu_4_Smoke.Projectile_Wushu_4_Smoke_C"
WALL_MANAGER = "/Game/Characters/Phoenix/Wall_Manager.Wall_Manager_C_ClassNetCache"
MOVEMENT = ('{"linear_velocity":{"x":643,"y":-766,"z":-29},"location":{"x":-339,"y":954,"z":515},'
            '"rotation":{"pitch":1.4,"yaw":310.78125,"roll":0}}')


class TracksTests(TempDirTestCase):
    """`--tracks` flattens movement rows; an untyped one stays, counted."""

    def setUp(self):
        self.root = self.tmp()
        self.export = _export(self.root, [(7, "open", 100)])
        rows = [(SMOKE, "ReplicatedMovement", MOVEMENT), (SMOKE, "ReplicatedMovement", None),
                (WALL_MANAGER, "MulticastAddSmokeScreenPoint.Translation", "(1.5,-2,3)"),
                (SMOKE, "Other", "x")]
        pq.write_table(pa.table({
            "time_ms": [10, 20, 30, 40], "packet_id": [1, 2, 3, 4], "actor_net_guid": [5, 5, 6, 5],
            "group_path": [r[0] for r in rows], "field_name": [r[1] for r in rows],
            "value_str": pa.array([r[2] for r in rows], pa.string())}), self.export / "fields.parquet")

    def test_movement_and_wall_points_flatten_and_untyped_rows_stay(self):
        rows, untyped = effects.tracks(self.export)
        keys = ("source", "class_name", "x", "y", "z", "vx", "vy", "vz", "yaw")
        self.assertEqual([tuple(r[k] for k in keys) for r in rows], [
            ("ReplicatedMovement", "Projectile_Wushu_4_Smoke_C", -339, 954, 515, 643, -766, -29, 310.78125),
            ("ReplicatedMovement", "Projectile_Wushu_4_Smoke_C", *[None] * 7),
            ("MulticastAddSmokeScreenPoint", "Wall_Manager_C", 1.5, -2.0, 3.0, *[None] * 4)])
        self.assertEqual(untyped, {"Projectile_Wushu_4_Smoke_C": 1})

    def test_the_cli_writes_the_tracks_and_prints_the_untyped_count(self):
        out = self.root
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = effects.main(["--export", str(self.export), "--out", str(out / "effects.parquet"),
                                 "--tracks", str(out / "tracks.parquet")])
        self.assertEqual(code, 0)
        self.assertEqual(pq.read_table(out / "tracks.parquet").num_rows, 3)
        self.assertIn("untyped rows, kept with null coordinates: 1", stdout.getvalue())

    def test_an_output_naming_an_export_table_or_the_other_output_is_refused(self):
        inputs = {path: path.read_bytes() for path in self.export.iterdir()}
        for out, tracks in (("effects.parquet", self.export / "fields.parquet"),
                            (self.export / "actors.parquet", "tracks.parquet"), ("same", "same")):
            code = effects.main(["--export", str(self.export), "--out", str(self.root / out),
                                 "--tracks", str(self.root / tracks)])
            self.assertEqual(code, 1)
        self.assertEqual({path: path.read_bytes() for path in self.export.iterdir()}, inputs)
