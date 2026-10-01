import json

import pyarrow as pa
import pyarrow.parquet as pq

from support import TempDirTestCase
import extract_player_effects as effects


class PlayerEffectTests(TempDirTestCase):
    def setUp(self):
        self.root = self.tmp()

    def export(self, fields, players=None):
        if players is None:
            players = [{"character_net_guid": 20, "subject": "target",
                        "possessed_character": 412}]
        (self.root / "manifest.json").write_text(json.dumps({"players": players}),
                                                 encoding="utf-8")
        schema = pa.schema([(name, pa.string() if name in ("group_path", "field_name", "value_str")
                             else pa.float64() if name == "value_f64"
                             else pa.bool_() if name == "value_bool" else pa.int64())
                            for name in effects.COLUMNS])
        pq.write_table(pa.Table.from_pylist(fields, schema=schema), self.root / "fields.parquet")
        pq.write_table(pa.table({"net_guid": [99],
                                "path": ["FXC_Wraith_Q_NearsightMissile_Nearsight_C"]}),
                       self.root / "net_guids.parquet")
        before = (self.root / "fields.parquet").read_bytes()
        result = effects.build(self.root)
        self.assertEqual(before, (self.root / "fields.parquet").read_bytes())
        return result

    @staticmethod
    def row(actor, name, value=None, packet=1, rpc=False):
        return {"time_ms": packet * 100, "packet_id": packet,
                "channel_index": 4, "actor_net_guid": actor, "object_net_guid": actor + 1,
                "group_path": effects.EFFECT_GROUP if rpc else effects.BLIND_GROUP,
                "field_name": name, "value_i64": value}

    def blind(self, actor, effect_id=1, packet=1):
        return [self.row(actor, "ActiveBlinds[0].EffectID", effect_id, packet),
                self.row(actor, "ActiveBlinds[0].CausingActor", 100, packet),
                self.row(actor, "ActiveBlinds", packet=packet)]

    def test_possessed_devices_keep_evidence_without_player_hits(self):
        for device in (412, 798, 1170, 1534, 1884):
            with self.subTest(device=device):
                doc = self.export(self.blind(device) + self.blind(20, 2, 2)
                                  + self.blind(device, 3, 3))
                self.assertEqual(doc["totals"]["player_blind_update"], 1)
                self.assertEqual(doc["totals"]["unconfirmed_target_observations"], 2)
                self.assertEqual([r["values"]["CausingActor"] for r in doc["records"]], [100] * 3)
                self.assertEqual(doc["records"][1]["target_subject"], "target")

    def test_device_only_flash_is_preserved_with_zero_player_observations(self):
        doc = self.export(self.blind(412))
        self.assertEqual(len(doc["records"]), 1)
        self.assertEqual(doc["totals"]["player_blind_update"], 0)
        self.assertEqual(doc["records"][0]["values"]["CausingActor"], 100)

    def test_nearsight_start_and_stop_use_body_identity(self):
        fields = []
        for actor in (412, 20):
            for rpc in ("MulticastPlayContinuousEffect", "MulticastStopContinuousEffect"):
                fields.append(self.row(actor, rpc + ".EffectID", 1, rpc=True))
                if rpc.startswith("MulticastPlay"):
                    fields.append(self.row(actor, rpc + ".EffectContainer", 99, rpc=True))
        doc = self.export(fields)
        self.assertEqual(doc["totals"]["player_continuous_start"], 1)
        self.assertEqual(doc["totals"]["player_continuous_stop"], 1)
        self.assertEqual(len(doc["records"]), 4)
        self.assertEqual(doc["records"][0]["effect_container_path"],
                         "FXC_Wraith_Q_NearsightMissile_Nearsight_C")

    def test_same_packet_invocations_and_array_items_do_not_merge(self):
        fields = [self.row(20, "MulticastPlayContinuousEffect.EffectID", n, rpc=True)
                  for n in (1, 2)]
        fields += [self.row(20, f"ActiveBlinds[{n}].EffectID", n + 3) for n in (0, 1)]
        doc = self.export(fields)
        self.assertEqual([r["values"]["EffectID"] for r in doc["records"]], [1, 2, 3, 4])
        self.assertEqual(doc["totals"]["same_packet_parameter_restarts"], 1)

    def test_missing_and_conflicting_identities_are_not_players(self):
        for players in ([], [{"character_net_guid": 20, "subject": "a"},
                             {"character_net_guid": 20, "subject": "b"}]):
            doc = self.export(self.blind(20), players)
            self.assertEqual(doc["totals"]["player_blind_update"], 0)
            self.assertIsNone(doc["records"][0]["target_subject"])

    @staticmethod
    def spawned(state, value, time_ms, name="SpawnedCharacter",
                group="/Game/GameModes/Bomb/BombPlayerState.BombPlayerState_C"):
        return {"time_ms": time_ms, "packet_id": time_ms, "channel_index": 33,
                "actor_net_guid": state, "object_net_guid": None,
                "group_path": group, "field_name": name, "value_i64": value}

    def reconnect(self, extra=()):
        """PlayerState 256 is given 1510, loses it on a disconnect, and
        reconnects as 45530 -- the manifest keeps 45530."""
        players = [{"actor_net_guid": 256, "subject": "reconnected",
                    "character_net_guid": 45530}]
        fields = [self.spawned(256, 1510, 66), self.spawned(256, 0, 1851838),
                  self.spawned(256, 45530, 1948245), *extra]
        for actor in (1510, 45530, 777):
            fields += [self.row(actor, "MulticastPlayContinuousEffect.EffectID", 1, rpc=True),
                       self.row(actor, "MulticastPlayContinuousEffect.EffectContainer", 99,
                                rpc=True)]
        return self.export(fields, players)

    def test_an_earlier_pawn_of_a_reconnected_player_is_still_a_player_body(self):
        doc = self.reconnect()
        by_actor = {r["actor_net_guid"]: r for r in doc["records"]}
        self.assertEqual(by_actor[1510]["target_identity"], "player_body")
        self.assertEqual(by_actor[1510]["target_subject"], "reconnected")
        self.assertIn("SpawnedCharacter history", by_actor[1510]["identity_provenance"])
        self.assertEqual(by_actor[45530]["identity_provenance"],
                         "manifest.players.character_net_guid (SpawnedCharacter)")
        self.assertEqual(doc["totals"]["player_continuous_start"], 2)
        self.assertEqual(doc["totals"]["player_body_via_non_final_spawned_character"], 1)
        self.assertEqual(
            doc["totals"]["player_identity"]["non_final_spawned_character_pawns"], 1)

    def test_a_pawn_carrying_the_players_state_but_never_spawned_is_not_a_body(self):
        """Astra's Rift_TargetingForm_PC_C: its own PlayerState names the player
        and PossessedCharacter points at it, but SpawnedCharacter never does."""
        form = "/Game/Characters/Rift/Rift_TargetingForm_PC.Rift_TargetingForm_PC_C"
        doc = self.reconnect([self.spawned(777, 256, 90, "PlayerState", form),
                              self.spawned(256, 777, 90, "PossessedCharacter")])
        by_actor = {r["actor_net_guid"]: r for r in doc["records"]}
        self.assertEqual(by_actor[777]["target_identity"], "unconfirmed_actor")
        self.assertIsNone(by_actor[777]["target_subject"])
        self.assertEqual(doc["totals"]["unconfirmed_target_observations"], 1)

    def test_every_identity_count_is_reported_with_its_zero(self):
        totals = self.export(self.blind(20))["totals"]
        self.assertEqual(totals["player_body_via_non_final_spawned_character"], 0)
        self.assertEqual(totals["player_identity"]["pawns_claimed_by_multiple_player_states"], 0)

    def test_untyped_member_stays_missing_and_wrong_group_is_ignored(self):
        fields = [self.row(20, "ActiveBlinds[0].InitialDuration")]
        wrong = self.row(20, "ActiveBlinds[0].EffectID", 1)
        wrong["group_path"] = "/unrelated"
        doc = self.export(fields + [wrong])
        self.assertIsNone(doc["records"][0]["values"]["InitialDuration"])
        self.assertNotIn("EffectID", doc["records"][0]["values"])
        self.assertEqual(doc["totals"]["untyped_members"], 1)
