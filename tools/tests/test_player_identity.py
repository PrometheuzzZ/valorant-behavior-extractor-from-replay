"""`player_identity` admits every `SpawnedCharacter` pawn, and only those: not
only the manifest's last one, and never a pawn merely carrying the player's
`PlayerState` (Astra's targeting form).
"""
import json
import re
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from support import TOOLS, TempDirTestCase
import player_identity as identity

BOMB, SWIFT = identity.PLAYER_STATE_GROUPS
FIELD_SCHEMA = pa.schema([
    ("time_ms", pa.uint32()), ("packet_id", pa.uint32()),
    ("actor_net_guid", pa.uint32()), ("object_net_guid", pa.uint32()),
    ("group_path", pa.string()), ("field_name", pa.string()),
    ("value_i64", pa.int64())])


def spawned(state, value, time_ms, packet_id=None, group=BOMB, obj=None,
            name="SpawnedCharacter"):
    return {"time_ms": time_ms, "packet_id": time_ms if packet_id is None else packet_id,
            "actor_net_guid": state, "object_net_guid": obj, "group_path": group,
            "field_name": name, "value_i64": value}


#: 39c2bb2c (13.05), PlayerState 256: the pawn, the disconnect 0, the new pawn.
RECONNECT = [spawned(256, 1510, 66), spawned(256, 0, 1851838), spawned(256, 45530, 1948245)]
MANIFEST = {"players": [{"actor_net_guid": 256, "subject": "reconnected",
                         "character_net_guid": 45530},
                        {"actor_net_guid": 248, "subject": "steady",
                         "character_net_guid": 990}]}


class PlayerBodiesTests(unittest.TestCase):
    def bodies(self, rows, manifest=MANIFEST):
        return identity.player_bodies(manifest, rows + [spawned(248, 990, 66)])

    def test_an_earlier_pawn_of_a_reconnected_player_is_that_players_body(self):
        bodies = self.bodies(RECONNECT)
        self.assertEqual(bodies.subjects[1510], "reconnected")
        self.assertEqual(bodies.subjects[45530], "reconnected")
        self.assertEqual(bodies.provenance[1510], identity.EARLIER_PROVENANCE)
        self.assertEqual(bodies.provenance[45530], identity.FINAL_PROVENANCE)
        self.assertEqual(bodies.counts["non_final_spawned_character_pawns"], 1)
        self.assertEqual(bodies.counts["player_body_pawns"], 3)

    def test_the_disconnect_zero_is_kept_in_order_and_is_not_a_pawn(self):
        bodies = self.bodies(list(reversed(RECONNECT)))
        self.assertEqual([value for _, _, value in bodies.history[256]], [1510, 0, 45530])
        self.assertNotIn(0, bodies.subjects)

    def test_history_follows_packet_order_not_time(self):
        """A non-finite frame exports time 0; packet order is the wire's."""
        bodies = self.bodies([spawned(256, 1510, 100, 1), spawned(256, 45530, 0, 2)])
        self.assertEqual([value for _, _, value in bodies.history[256]], [1510, 45530])
        self.assertEqual(bodies.counts["manifest_history_disagreements"], 0)

    def test_a_swiftplay_player_state_names_bodies_like_the_bomb_class(self):
        rows = [dict(row, group_path=SWIFT) for row in RECONNECT]
        self.assertEqual(self.bodies(rows).subjects[1510], "reconnected")

    def test_other_groups_nested_rows_and_untyped_rows_are_counted_not_admitted(self):
        rows = [spawned(256, 1510, 66, group="/Game/Other.Other_C"),
                spawned(256, 1511, 67, obj=12),
                spawned(256, None, 68), spawned(256, 45530, 1948245)]
        bodies = self.bodies(rows)
        self.assertNotIn(1510, bodies.subjects)
        self.assertNotIn(1511, bodies.subjects)
        self.assertEqual(bodies.counts["spawned_character_rows"], 5)
        self.assertEqual(bodies.counts["spawned_character_rows_other_groups"], 1)
        self.assertEqual(bodies.counts["spawned_character_rows_nested"], 1)
        self.assertEqual(bodies.counts["untyped_spawned_character_rows"], 1)

    def test_a_pawn_named_by_two_player_states_is_a_conflict_not_a_body(self):
        bodies = self.bodies(RECONNECT + [spawned(248, 1510, 70)])
        self.assertNotIn(1510, bodies.subjects)
        self.assertIn(1510, bodies.conflicts)
        self.assertEqual(bodies.counts["pawns_claimed_by_multiple_player_states"], 1)
        self.assertEqual(bodies.counts["conflicting_subject_pawns"], 1)

    def test_two_player_states_naming_one_pawn_block_it_even_with_one_subject(self):
        """One pawn cannot be spawned by two PlayerStates; the same subject on
        both does not make that evidence consistent. 0 such pawns in the audit
        corpus, so this has no positive case to protect -- it fails closed."""
        manifest = {"players": MANIFEST["players"] + [
            {"actor_net_guid": 258, "subject": "reconnected", "character_net_guid": 60}]}
        bodies = self.bodies(RECONNECT + [spawned(258, 1510, 70), spawned(258, 60, 80)],
                             manifest)
        self.assertNotIn(1510, bodies.subjects)
        self.assertIn(1510, bodies.conflicts)
        self.assertEqual(bodies.counts["pawns_claimed_by_multiple_player_states"], 1)
        self.assertEqual(bodies.counts["conflicting_subject_pawns"], 0)

    def test_manifest_conflicts_without_player_state_ids_still_block(self):
        manifest = {"players": [{"character_net_guid": 20, "subject": "a"},
                                {"character_net_guid": 20, "subject": "b"}]}
        bodies = identity.player_bodies(manifest, [])
        self.assertEqual(bodies.subjects, {})
        self.assertEqual(bodies.conflicts, {20})

    def test_a_player_without_a_character_contributes_no_pawn_at_all(self):
        """A None key would make an owner with no Instigator (`.get()` -> None)
        test as a carrier; the 11-player exports carry such a player."""
        manifest = {"players": MANIFEST["players"] + [
            {"actor_net_guid": 300, "subject": "coach", "character_net_guid": None}]}
        bodies = self.bodies(RECONNECT, manifest)
        self.assertNotIn(None, bodies.subjects)
        self.assertNotIn("coach", bodies.subjects.values())

    def test_a_manifest_value_that_is_not_the_last_history_value_is_counted(self):
        manifest = {"players": [{"actor_net_guid": 256, "subject": "reconnected",
                                 "character_net_guid": 1510},
                                MANIFEST["players"][1]]}
        self.assertEqual(self.bodies(RECONNECT, manifest)
                         .counts["manifest_history_disagreements"], 1)
        self.assertEqual(self.bodies(RECONNECT).counts["manifest_history_disagreements"], 0)

    def test_a_manifest_character_with_no_history_is_counted_and_still_admitted(self):
        bodies = identity.player_bodies(MANIFEST, [])
        self.assertEqual(bodies.counts["manifest_characters_without_history"], 2)
        self.assertEqual(bodies.subjects, {45530: "reconnected", 990: "steady"})

    def test_a_player_state_missing_from_the_manifest_labels_nothing(self):
        bodies = self.bodies(RECONNECT + [spawned(999, 7777, 66)])
        self.assertNotIn(7777, bodies.subjects)
        self.assertEqual(bodies.counts["spawned_character_player_states_not_in_manifest"], 1)

    def test_every_count_is_present_even_when_zero(self):
        self.assertEqual(set(identity.player_bodies({}, []).counts), set(identity.COUNT_KEYS))

    def test_a_value_that_is_not_a_u32_netguid_fails_loudly(self):
        with self.assertRaisesRegex(ValueError, "u32"):
            self.bodies([spawned(256, -1, 66)])


class ManifestBodyListTests(unittest.TestCase):
    """The manifest's `character_net_guids` must equal the fields' history in last-write order."""

    def disagreements(self, player, rows=RECONNECT):
        manifest = {"players": [dict(player, actor_net_guid=256, subject="s")]}
        return identity.player_bodies(manifest, rows).counts["manifest_history_disagreements"]

    def test_the_list_must_equal_the_history_in_last_write_order(self):
        self.assertEqual(self.disagreements(
            {"character_net_guid": 45530, "character_net_guids": [1510, 45530]}), 0)
        for wrong in ([45530], [45530, 1510], [1510, 45530, 7], []):
            with self.subTest(wrong=wrong):
                self.assertEqual(self.disagreements(
                    {"character_net_guid": 45530, "character_net_guids": wrong}), 1)

    def test_a_repeat_moves_to_the_end(self):
        rows = [spawned(256, 7, 1), spawned(256, 8, 2), spawned(256, 7, 3)]
        self.assertEqual(self.disagreements(
            {"character_net_guid": 7, "character_net_guids": [8, 7]}, rows), 0)
        self.assertEqual(self.disagreements(
            {"character_net_guid": 7, "character_net_guids": [7, 8]}, rows), 1)

    def test_an_older_manifest_without_the_list_compares_the_last_body_only(self):
        self.assertEqual(self.disagreements({"character_net_guid": 45530}), 0)
        self.assertEqual(self.disagreements({"character_net_guid": 1510}), 1)


class LoadTests(TempDirTestCase):
    def test_only_spawned_character_proves_a_body(self):
        """A pawn that carries the player's PlayerState, or is possessed by the
        player, is not a body: the Rift_TargetingForm case, 1,236 pawns in the
        audit corpus."""
        root = self.tmp()
        (root / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
        rows = RECONNECT + [
            spawned(256, 777, 90, name="PossessedCharacter"),
            spawned(777, 256, 91, group="/Game/Characters/Rift/Rift_TargetingForm_PC.Rift_TargetingForm_PC_C",
                    name="PlayerState")]
        pq.write_table(pa.Table.from_pylist(rows, schema=FIELD_SCHEMA),
                       root / "fields.parquet")
        bodies = identity.load_player_bodies(root)
        # 990 has no history row here; the manifest alone still admits it.
        self.assertEqual(bodies.subjects,
                         {1510: "reconnected", 45530: "reconnected", 990: "steady"})
        self.assertEqual(bodies.counts["spawned_character_rows"], 3)
        self.assertEqual(bodies.counts["manifest_characters_without_history"], 1)


class AliasTableTests(unittest.TestCase):
    def test_every_player_state_alias_in_the_overlay_is_read_here(self):
        source = (TOOLS.parent / "crates" / "vrf-decode" / "src" / "overlay.rs").read_text(
            encoding="utf-8")
        block = source[source.index("const GROUP_ALIASES"):]
        block = block[:block.index("];")]
        # A Rust string continuation drops the newline and the next line's indent.
        block = re.sub(r"\\\n\s*", "", block)
        pairs = re.findall(r'\(\s*"([^"]+)",\s*"([^"]+)",?\s*\)', block)
        self.assertTrue(pairs, "GROUP_ALIASES not found in overlay.rs")
        aliases = {alias for alias, target in pairs if target == BOMB}
        self.assertEqual(aliases, {SWIFT})
        self.assertEqual(set(identity.PLAYER_STATE_GROUPS), aliases | {BOMB})
