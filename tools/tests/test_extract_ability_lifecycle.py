import json

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase
import extract_ability_lifecycle as lifecycle


class AbilityLifecycleTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()
        (self.root / "manifest.json").write_text(json.dumps({"players": [
            {"character_net_guid": 50, "subject": "player-50"}]}), encoding="utf-8")

    def write(self, actors, fields):
        actor_schema = pa.schema([
            ("time_ms", pa.uint32()), ("packet_id", pa.uint32()),
            ("channel_index", pa.uint32()), ("actor_net_guid", pa.uint32()),
            ("event", pa.string()), ("class_path", pa.string())])
        # `object_net_guid` is null on every row here, as on an actor-level
        # property of a real export; the player-body join reads it.
        field_schema = pa.schema([
            ("time_ms", pa.uint32()), ("packet_id", pa.uint32()),
            ("channel_index", pa.uint32()), ("actor_net_guid", pa.uint32()),
            ("object_net_guid", pa.uint32()),
            ("group_path", pa.string()), ("field_name", pa.string()),
            ("value_i64", pa.int64())])
        pq.write_table(pa.Table.from_pylist(actors, schema=actor_schema), self.root / "actors.parquet")
        pq.write_table(pa.Table.from_pylist(fields, schema=field_schema), self.root / "fields.parquet")
        return lifecycle.build(self.root)

    @staticmethod
    def actor(time, packet, channel, guid, event, path=None):
        return {"time_ms": time, "packet_id": packet, "channel_index": channel,
                "actor_net_guid": guid, "event": event, "class_path": path}

    @staticmethod
    def field(time, packet, channel, guid, name, value,
              group="/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"):
        return {"time_ms": time, "packet_id": packet, "channel_index": channel,
                "actor_net_guid": guid, "group_path": group,
                "field_name": name, "value_i64": value}

    def test_links_only_replicated_manifest_player_reference(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path),
                          self.actor(30, 3, 7, 100, "close")],
                         [self.field(20, 2, 7, 100, "Instigator", 50)])
        row = doc["records"][0]
        self.assertEqual(row["linked_player_subject"], "player-50")
        self.assertEqual(row["replicated_instigator"]["evidence"][0]["packet_id"], 2)

    def test_links_an_earlier_pawn_of_a_reconnected_player(self):
        """The manifest keeps only the last SpawnedCharacter; the pawn the
        player had before reconnecting is still that player's body."""
        (self.root / "manifest.json").write_text(json.dumps({"players": [
            {"actor_net_guid": 256, "character_net_guid": 50, "subject": "player-50"}]}),
            encoding="utf-8")
        state = "/Game/GameModes/Bomb/BombPlayerState.BombPlayerState_C"
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path),
                          self.actor(30, 3, 7, 100, "close")],
                         [self.field(1, 1, 5, 256, "SpawnedCharacter", 40, state),
                          self.field(2, 2, 5, 256, "SpawnedCharacter", 0, state),
                          self.field(3, 3, 5, 256, "SpawnedCharacter", 50, state),
                          self.field(20, 2, 7, 100, "Instigator", 40)])
        row = doc["records"][0]
        self.assertEqual(row["linked_player_net_guid"], 40)
        self.assertEqual(row["linked_player_subject"], "player-50")
        self.assertEqual(row["player_reference_provenance"],
                         "spawned_character_history_reference")
        self.assertEqual(doc["totals"]["linked_via_non_final_spawned_character"], 1)
        self.assertEqual(doc["totals"]["player_identity"]["non_final_spawned_character_pawns"], 1)

    def test_dormant_is_last_seen_evidence_and_not_close(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path),
                          self.actor(40, 4, 7, 100, "dormant")], [])
        row = doc["records"][0]
        self.assertEqual(row["lifecycle_status"], "right_censored")
        self.assertIsNone(row["closed_time_ms"])
        self.assertEqual(row["last_seen_event_time_ms"], 40)
        self.assertEqual(doc["totals"]["went_dormant"], 1)

    def test_channel_reuse_cannot_attach_old_actors_owner(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path),
                          self.actor(20, 2, 7, 100, "close"),
                          self.actor(30, 3, 7, 200, "open", path)],
                         [self.field(15, 1, 7, 100, "Owner", 50)])
        self.assertIsNone(doc["records"][1]["linked_player_net_guid"])

    def test_reopened_same_guid_does_not_look_into_new_instance(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path),
                          self.actor(30, 3, 7, 100, "open", path)],
                         [self.field(31, 4, 7, 100, "Owner", 50)])
        first, second = doc["records"]
        self.assertIsNone(first["linked_player_net_guid"])
        self.assertEqual(second["linked_player_net_guid"], 50)

    def test_late_owner_update_after_close_remains_unresolved(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path),
                          self.actor(20, 2, 7, 100, "close")],
                         [self.field(21, 3, 7, 100, "Owner", 50)])
        row = doc["records"][0]
        self.assertIsNone(row["linked_player_net_guid"])
        self.assertIn("owner_missing", row["unresolved_reasons"])

    def test_vague_keyword_class_is_not_a_candidate(self):
        doc = self.write([self.actor(10, 1, 7, 100, "open",
                          "/Game/Props/SmokeWallTrap.SmokeWallTrap_C")], [])
        self.assertEqual(doc["records"], [])

    def test_conflicting_replicated_updates_are_preserved_unresolved(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path)],
                         [self.field(11, 2, 7, 100, "Owner", 50),
                          self.field(12, 3, 7, 100, "Owner", 60)])
        owner = doc["records"][0]["replicated_owner"]
        self.assertEqual(owner["status"], "conflicting_updates")
        self.assertIsNone(owner["net_guid"])
        self.assertEqual(len(owner["evidence"]), 2)

    def test_component_owner_is_ignored_despite_same_outer_actor(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path)],
                         [self.field(11, 2, 7, 100, "Owner", 50,
                          "/Game/Characters/Components/AbilityComponent.AbilityComponent_C")])
        row = doc["records"][0]
        self.assertIsNone(row["linked_player_net_guid"])
        self.assertIn("component_reference_rows_ignored", row["unresolved_reasons"])

    def test_conflicting_manifest_subjects_fail_closed(self):
        (self.root / "manifest.json").write_text(json.dumps({"players": [
            {"character_net_guid": 50, "subject": "one"},
            {"character_net_guid": 50, "subject": "two"}]}), encoding="utf-8")
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path)],
                         [self.field(11, 2, 7, 100, "Owner", 50)])
        row = doc["records"][0]
        self.assertIsNone(row["linked_player_net_guid"])
        self.assertIn("manifest_character_guid_conflict", row["unresolved_reasons"])
        self.assertEqual(doc["totals"]["conflicting_manifest_character_guids"], 1)

    def test_zero_is_preserved_as_explicit_null_reference(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path)],
                         [self.field(11, 2, 7, 100, "Owner", 0)])
        owner = doc["records"][0]["replicated_owner"]
        self.assertEqual(owner["status"], "null_reference")
        self.assertEqual(owner["evidence"][0]["net_guid"], 0)

    def test_same_packet_channel_reopen_is_visible_ambiguity(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path),
                          self.actor(10, 1, 7, 200, "open", path)],
                         [self.field(10, 1, 7, 200, "Owner", 50)])
        reasons = [reason for row in doc["records"]
                   for reason in row["unresolved_reasons"]]
        self.assertIn("channel_reused_without_close", reasons)
        self.assertIn("same_packet_channel_reopen_ambiguous", reasons)
        self.assertIsNone(doc["records"][1]["linked_player_net_guid"])

    def test_same_packet_close_then_reopen_blocks_new_identity_link(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path),
                          self.actor(20, 2, 7, 100, "close"),
                          self.actor(20, 2, 7, 100, "open", path)],
                         [self.field(20, 2, 7, 100, "Owner", 50)])
        reopened = doc["records"][1]
        self.assertIn("same_packet_channel_reopen_ambiguous",
                      reopened["unresolved_reasons"])
        self.assertIsNone(reopened["linked_player_net_guid"])
        self.assertIsNone(doc["records"][0]["linked_player_net_guid"])

    def test_conflict_in_one_role_blocks_stable_other_role_link(self):
        path = "/Game/Characters/Sarge/S0/Ability_Q/Zone.Zone_C"
        doc = self.write([self.actor(10, 1, 7, 100, "open", path)], [
            self.field(11, 2, 7, 100, "Owner", 50),
            self.field(12, 3, 7, 100, "Owner", 60),
            self.field(13, 4, 7, 100, "Instigator", 50),
        ])
        row = doc["records"][0]
        self.assertEqual(row["replicated_instigator"]["net_guid"], 50)
        self.assertEqual(row["replicated_owner"]["status"], "conflicting_updates")
        self.assertIsNone(row["linked_player_net_guid"])
