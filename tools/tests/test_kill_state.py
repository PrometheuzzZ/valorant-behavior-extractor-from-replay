import copy
import math
import struct
import unittest

import support  # puts tools/ on sys.path
import kill_state


def raw(handle, value, bits=8):
    return {"handle": handle, "bit_count": bits, "raw_bits_hex": value}


def float_raw(handle, value):
    return raw(handle, struct.pack("<f", value).hex(), 32)


def members(*, finisher=False, damage=10.0, raw_overrides=None):
    raw_members = {
        "Victim": raw(3, "00"),
        "KillingEquippableClass": raw(4, "00"),
        "WeaponTheme": raw(5, "0100000000", 33),
        "DamageType": raw(9, "00"),
        "DamageTaken": float_raw(10, damage),
        "DamageRegion": raw(11, "02"),
        "GameTimeElapsed": float_raw(12, 20.0),
        "RoundTimestamp": float_raw(13, 5.0),
        "RoundNumber": raw(14, "03000000", 32),
        "bDidKillTriggerFinisher": raw(15, "01" if finisher else "00", 1),
    }
    if raw_overrides is not None:
        raw_members = raw_overrides
    result = {
        "victim_ref": 0,
        "killing_equippable_class_ref": 0,
        "weapon_theme": "",
        "damage_type_ref": 0,
        "damage_taken": damage,
        "damage_region": 2,
        "game_time_elapsed": 20.0,
        "round_timestamp": 5.0,
        "round_number": 3,
        "did_kill_trigger_finisher": finisher,
        "victim_ref_resolution": "null" if "Victim" in raw_members else "missing",
        "killing_equippable_class_ref_resolution": "null" if "KillingEquippableClass" in raw_members else "missing",
        "damage_type_ref_resolution": "null" if "DamageType" in raw_members else "missing",
        "assisting_players": None,
        "raw_members": raw_members,
    }
    # The member names above are written out; only which to null is shared.
    for raw_name, member_name in kill_state._RAW_TO_MEMBER.items():
        if raw_name not in raw_members:
            result[member_name] = None
    return result


def observation(*, ordinal, index=0, complete=True, table="fields", actor=7,
                obj=50, member_values=None, checkpoint_index=None):
    if member_values is None:
        member_values = members()
    checkpoint = table == "checkpoint_fields"
    return {
        "schema_version": 1,
        "kind": "killdata_serialized_update",
        "source_table": table,
        "checkpoint_index": checkpoint_index if checkpoint else None,
        "checkpoint_id": f"cp-{checkpoint_index}" if checkpoint else None,
        "physical_parent_row_ordinal": ordinal,
        "element_index": index,
        "time_ms": ordinal * 10,
        "packet_id": ordinal,
        "channel_index": 2,
        "actor_net_guid": actor,
        "object_net_guid": obj,
        "parent_raw_sha256": f"{ordinal:064x}",
        "members_complete": complete,
        "members": member_values,
    }


def document(*items):
    return {
        "schema_version": 1,
        "kind": "vrfkit_killdata_observation_export",
        "provenance": {
            "export_id": "replay-a.json",
            "input_sha256": {"replay-a.json": "a" * 64},
        },
        "observations": list(items),
    }


class KillStateTests(unittest.TestCase):
    def test_main_checkpoint_repeat_and_unmatched_checkpoint(self):
        base = observation(ordinal=10)
        repeated = observation(
            ordinal=2, table="checkpoint_fields", checkpoint_index=3,
            member_values=copy.deepcopy(base["members"]),
        )
        unknown = observation(
            ordinal=3, table="checkpoint_fields", checkpoint_index=4, obj=999,
        )
        result = kill_state.project_kill_state(document(unknown, repeated, base))
        self.assertEqual(result["counts"]["entities"], 1)
        self.assertEqual(result["counts"]["checkpoint_matches"], 1)
        self.assertEqual(result["checkpoint_snapshots"]["matches"][0]["matched_revision_indices"], [0])
        self.assertEqual(result["checkpoint_snapshots"]["unresolved"][0]["reason"], "no_main_entity")
        self.assertEqual(result["entities"][0]["latest"]["source"]["source_index"], 2)
        self.assertEqual(result["counts"]["main_partial"], 0)
        self.assertEqual(result["counts"]["main_revisions_unchanged"], 0)
        self.assertEqual(
            result["checkpoint_snapshots"]["unresolved_by_reason"],
            {"incomplete_checkpoint": 0, "no_main_entity": 1, "state_signature_unmatched": 0},
        )

    def test_finisher_partials_distinguish_changed_and_unchanged(self):
        finisher_raw = {"bDidKillTriggerFinisher": raw(15, "01", 1)}
        changed = observation(
            ordinal=11, complete=False,
            member_values=members(finisher=True, raw_overrides=finisher_raw),
        )
        unchanged = observation(
            ordinal=12, complete=False,
            member_values=members(finisher=True, raw_overrides=copy.deepcopy(finisher_raw)),
        )
        result = kill_state.project_kill_state(document(observation(ordinal=10), unchanged, changed))
        entity = result["entities"][0]
        self.assertEqual([revision["changed"] for revision in entity["revisions"]], [True, False])
        self.assertEqual(result["counts"]["main_revisions_changed"], 1)
        self.assertEqual(result["counts"]["main_revisions_unchanged"], 1)
        self.assertEqual(entity["latest"]["members"]["victim_ref"], 0)
        self.assertEqual(entity["revisions"][0]["present_raw_members"], ["bDidKillTriggerFinisher"])
        self.assertIn("Victim", entity["latest"]["raw_members"])

    def test_partial_before_base_is_rejected(self):
        partial = observation(
            ordinal=9, complete=False,
            member_values=members(finisher=True, raw_overrides={"bDidKillTriggerFinisher": raw(15, "01", 1)}),
        )
        with self.assertRaisesRegex(kill_state.KillStateError, "no prior exact-key base"):
            kill_state.project_kill_state(document(observation(ordinal=10), partial))

    def test_actor_owner_conflict_is_rejected(self):
        with self.assertRaisesRegex(kill_state.KillStateError, "actor ownership conflicts"):
            kill_state.project_kill_state(document(
                observation(ordinal=1, index=0, actor=7),
                observation(ordinal=2, index=1, actor=8),
            ))

    def test_duplicate_complete_key_index_reuse_is_rejected(self):
        with self.assertRaisesRegex(kill_state.KillStateError, "duplicates complete state key"):
            kill_state.project_kill_state(document(
                observation(ordinal=1, index=4),
                observation(ordinal=2, index=4),
            ))

    def test_schema_and_type_errors_are_rejected(self):
        cases = []
        missing_export = document()
        missing_export["provenance"].pop("export_id")
        cases.append((missing_export, "export_id"))
        wrong_schema = document()
        wrong_schema["schema_version"] = 2
        cases.append((wrong_schema, "schema_version"))
        boolean_schema = document()
        boolean_schema["schema_version"] = True
        cases.append((boolean_schema, "schema_version"))
        wrong_kind = document()
        wrong_kind["kind"] = "other"
        cases.append((wrong_kind, "kind"))
        bool_guid = document(observation(ordinal=1))
        bool_guid["observations"][0]["object_net_guid"] = True
        cases.append((bool_guid, "object_net_guid"))
        malformed_raw = document(observation(ordinal=1))
        malformed_raw["observations"][0]["members"]["raw_members"]["Victim"]["raw_bits_hex"] = "zz"
        cases.append((malformed_raw, "hexadecimal"))
        fake_complete = document(observation(
            ordinal=1, complete=True,
            member_values=members(raw_overrides={"bDidKillTriggerFinisher": raw(15, "00", 1)}),
        ))
        cases.append((fake_complete, "members_complete"))
        wrong_handle = document(observation(ordinal=1))
        wrong_handle["observations"][0]["members"]["raw_members"]["Victim"]["handle"] = 4
        cases.append((wrong_handle, "producer-compatible"))
        raw_typed_mismatch = document(observation(ordinal=1))
        raw_typed_mismatch["observations"][0]["members"]["damage_region"] = 3
        cases.append((raw_typed_mismatch, "differs from raw"))
        for bad_document, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(kill_state.KillStateError, message):
                    kill_state.project_kill_state(bad_document)

    def test_signed_zero_is_an_exact_typed_difference(self):
        base_members = members(damage=0.0)
        checkpoint_members = copy.deepcopy(base_members)
        checkpoint_members["damage_taken"] = -0.0
        checkpoint_members["raw_members"]["DamageTaken"] = float_raw(10, -0.0)
        result = kill_state.project_kill_state(document(
            observation(ordinal=1, member_values=base_members),
            observation(
                ordinal=2, table="checkpoint_fields", checkpoint_index=0,
                member_values=checkpoint_members,
            ),
        ))
        self.assertEqual(result["checkpoint_snapshots"]["unresolved"][0]["reason"], "state_signature_unmatched")
        self.assertEqual(math.copysign(1.0, result["checkpoint_snapshots"]["unresolved"][0]["snapshot"]["members"]["damage_taken"]), -1.0)

    def test_same_physical_coordinate_partial_tie_is_rejected(self):
        partial = observation(
            ordinal=10, complete=False,
            member_values=members(finisher=True, raw_overrides={"bDidKillTriggerFinisher": raw(15, "01", 1)}),
        )
        with self.assertRaisesRegex(kill_state.KillStateError, "repeats main physical"):
            kill_state.project_kill_state(document(observation(ordinal=10), partial))

    def test_duplicate_checkpoint_coordinate_is_rejected(self):
        first = observation(ordinal=2, table="checkpoint_fields", checkpoint_index=0)
        second = copy.deepcopy(first)
        second["parent_raw_sha256"] = "f" * 64
        with self.assertRaisesRegex(kill_state.KillStateError, "repeats checkpoint physical"):
            kill_state.project_kill_state(document(observation(ordinal=1), first, second))

    def test_reference_resolution_contract_is_rejected_when_stale(self):
        bad_complete = observation(ordinal=1)
        bad_complete["members"]["victim_ref_resolution"] = "missing"
        partial_members = members(
            finisher=True,
            raw_overrides={"bDidKillTriggerFinisher": raw(15, "01", 1)},
        )
        partial_members["victim_ref"] = 0
        partial_members["victim_ref_resolution"] = "null"
        bad_partial = observation(ordinal=2, complete=False, member_values=partial_members)
        for item in (bad_complete, bad_partial):
            with self.subTest(complete=item["members_complete"]):
                with self.assertRaisesRegex(kill_state.KillStateError, "victim_ref_resolution"):
                    kill_state.project_kill_state(document(item))
