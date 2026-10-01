"""Project the schema-1 KillData document ``extract_kill_observations.py`` emits
into a self-contained, deterministic component-local state for later joins.
Deliberately no file or command-line interface."""

from __future__ import annotations

import copy
import math
import struct
from collections import Counter
from typing import Any

from extract_kill_observations import decode_direct
from wire_bits import InputError, exact_ref, parse_array

SCHEMA_VERSION = 1
DOCUMENT_KIND = "vrfkit_killdata_observation_export"
OBSERVATION_KIND = "killdata_serialized_update"

_RAW_TO_MEMBER = {
    "Victim": "victim_ref",
    "KillingEquippableClass": "killing_equippable_class_ref",
    "WeaponTheme": "weapon_theme",
    "DamageType": "damage_type_ref",
    "DamageTaken": "damage_taken",
    "DamageRegion": "damage_region",
    "GameTimeElapsed": "game_time_elapsed",
    "RoundTimestamp": "round_timestamp",
    "RoundNumber": "round_number",
    "bDidKillTriggerFinisher": "did_kill_trigger_finisher",
    "AssistingPlayers": "assisting_players",
}
_EXPECTED_HANDLES = {
    "Victim": 3,
    "KillingEquippableClass": 4,
    "WeaponTheme": 5,
    "AssistingPlayers": 6,
    "DamageType": 9,
    "DamageTaken": 10,
    "DamageRegion": 11,
    "GameTimeElapsed": 12,
    "RoundTimestamp": 13,
    "RoundNumber": 14,
    "bDidKillTriggerFinisher": 15,
}
_REQUIRED_RAW = frozenset(_EXPECTED_HANDLES) - {"AssistingPlayers"}
_REFERENCE_RESOLUTION = {
    "Victim": "victim_ref_resolution",
    "KillingEquippableClass": "killing_equippable_class_ref_resolution",
    "DamageType": "damage_type_ref_resolution",
}
_MEMBER_FIELDS = frozenset(_RAW_TO_MEMBER.values()) | frozenset(_REFERENCE_RESOLUTION.values())
_COUNT_KEYS = (
    "input_observations",
    "entities",
    "main_complete",
    "main_partial",
    "main_revisions",
    "main_revisions_changed",
    "main_revisions_unchanged",
    "checkpoint_observations",
    "checkpoint_matches",
    "checkpoint_unresolved",
)
_UNRESOLVED_REASONS = (
    "incomplete_checkpoint",
    "no_main_entity",
    "state_signature_unmatched",
)


class KillStateError(ValueError):
    """The observation document cannot be projected without ambiguity."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise KillStateError(message)


def _is_int(value: Any) -> bool:
    return type(value) is int


def _require_int(value: Any, path: str, *, minimum: int | None = None) -> int:
    _require(_is_int(value), f"{path} must be an integer")
    if minimum is not None:
        _require(value >= minimum, f"{path} must be at least {minimum}")
    return value


def _validate_typed(value: Any, path: str) -> None:
    if value is None or type(value) in (bool, int, str):
        return
    if type(value) is float:
        _require(math.isfinite(value), f"{path} must be a finite float")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_typed(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            _require(type(key) is str, f"{path} keys must be strings")
            _validate_typed(item, f"{path}.{key}")
        return
    raise KillStateError(f"{path} has unsupported value type {type(value).__name__}")


def _typed_token(value: Any) -> Any:
    """Return a hashable, type-exact token, retaining the sign bit of zero."""
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) is int:
        return ("int", value)
    if type(value) is float:
        return ("f64", struct.pack(">d", value).hex())
    if type(value) is str:
        return ("str", value)
    if type(value) is list:
        return ("list", tuple(_typed_token(item) for item in value))
    if type(value) is dict:
        return (
            "dict",
            tuple((key, _typed_token(value[key])) for key in sorted(value)),
        )
    raise AssertionError("values are validated before tokenization")


def _validate_raw_members(raw_members: Any, path: str) -> dict[str, Any]:
    _require(type(raw_members) is dict, f"{path} must be an object")
    for raw_name, raw in raw_members.items():
        _require(type(raw_name) is str and raw_name, f"{path} keys must be names")
        _require(raw_name in _RAW_TO_MEMBER, f"{path}.{raw_name} is unknown")
        _require(type(raw) is dict, f"{path}.{raw_name} must be an object")
        bit_count = _require_int(raw.get("bit_count"), f"{path}.{raw_name}.bit_count", minimum=0)
        handle = _require_int(raw.get("handle"), f"{path}.{raw_name}.handle", minimum=0)
        _require(handle == _EXPECTED_HANDLES[raw_name], f"{path}.{raw_name}.handle is not producer-compatible")
        raw_hex = raw.get("raw_bits_hex")
        _require(type(raw_hex) is str, f"{path}.{raw_name}.raw_bits_hex must be a string")
        try:
            decoded = bytes.fromhex(raw_hex)
        except ValueError as exc:
            raise KillStateError(f"{path}.{raw_name}.raw_bits_hex is not hexadecimal") from exc
        _require(len(decoded) == (bit_count + 7) // 8, f"{path}.{raw_name} byte length disagrees with bit_count")
        if bit_count % 8 and decoded:
            _require(decoded[-1] >> (bit_count % 8) == 0, f"{path}.{raw_name} has non-zero padding bits")
    return raw_members


def _validate_member_consistency(members: dict[str, Any], raw_members: dict[str, Any], path: str) -> None:
    for member_name in _MEMBER_FIELDS:
        _require(member_name in members, f"{path}.{member_name} is required by the producer schema")
    for raw_name, resolution_name in _REFERENCE_RESOLUTION.items():
        member_name = _RAW_TO_MEMBER[raw_name]
        value = members[member_name]
        resolution = members[resolution_name]
        if raw_name not in raw_members:
            _require(value is None and resolution == "missing", f"{path}.{resolution_name} must mark an absent reference missing")
        elif value == 0:
            _require(resolution == "null", f"{path}.{resolution_name} disagrees with null reference")
        else:
            allowed = ("resolved_actor", "unresolved_actor") if raw_name == "Victim" else ("resolved_net_guid", "unresolved_net_guid")
            _require(resolution in allowed, f"{path}.{resolution_name} is unsupported for a present reference")
    if "AssistingPlayers" in raw_members:
        _require(members.get("assisting_players_raw") is None, f"{path}.assisting_players_raw must retain the producer container marker")
    else:
        _require("assisting_players_raw" not in members, f"{path}.assisting_players_raw cannot exist without its raw container")

    for raw_name, record in raw_members.items():
        member_name = _RAW_TO_MEMBER[raw_name]
        if raw_name == "AssistingPlayers":
            _require(type(members[member_name]) is list, f"{path}.{member_name} must be an array when present")
            container_raw = bytes.fromhex(record["raw_bits_hex"])
            leaves = [(i, w, p) for i, _, w, p in parse_array(container_raw, record["bit_count"], {7})[2]]
            _require(len(members[member_name]) == len(leaves), f"{path}.{member_name} length differs from raw")
            for index, assistant in enumerate(members[member_name]):
                _require(type(assistant) is dict, f"{path}.{member_name}[{index}] must be an object")
                element_index = _require_int(assistant.get("element_index"), f"{path}.{member_name}[{index}].element_index", minimum=0)
                ref = _require_int(assistant.get("ref"), f"{path}.{member_name}[{index}].ref", minimum=0)
                resolution = assistant.get("actor_resolution")
                _require(resolution in ("null", "resolved_actor", "unresolved_actor"), f"{path}.{member_name}[{index}].actor_resolution is unsupported")
                _require((ref == 0) == (resolution == "null"), f"{path}.{member_name}[{index}].actor_resolution disagrees with reference")
                assistant_hex = assistant.get("raw_bits_hex")
                _require(type(assistant_hex) is str, f"{path}.{member_name}[{index}].raw_bits_hex must be a string")
                assistant_width = _require_int(assistant.get("bit_count"), f"{path}.{member_name}[{index}].bit_count", minimum=1)
                leaf_index, leaf_width, leaf_raw = leaves[index]
                _require((element_index, assistant_width, assistant_hex) == (leaf_index, leaf_width, leaf_raw.hex()), f"{path}.{member_name}[{index}] differs from nested raw")
                _require(ref == exact_ref(leaf_raw, leaf_width), f"{path}.{member_name}[{index}].ref differs from nested raw")
            continue
        raw = bytes.fromhex(record["raw_bits_hex"])
        expected = decode_direct(_EXPECTED_HANDLES[raw_name], raw, record["bit_count"])
        actual = members[member_name]
        _require(_typed_token(actual) == _typed_token(expected), f"{path}.{member_name} differs from raw")


def _validate_observation(observation: Any, index: int) -> dict[str, Any]:
    path = f"observations[{index}]"
    _require(type(observation) is dict, f"{path} must be an object")
    _require(type(observation.get("schema_version")) is int and observation["schema_version"] == SCHEMA_VERSION, f"{path}.schema_version must be integer 1")
    _require(observation.get("kind") == OBSERVATION_KIND, f"{path}.kind is unsupported")
    source_table = observation.get("source_table")
    _require(source_table in ("fields", "checkpoint_fields"), f"{path}.source_table is unsupported")

    for name in (
        "physical_parent_row_ordinal",
        "element_index",
        "actor_net_guid",
        "object_net_guid",
    ):
        _require_int(observation.get(name), f"{path}.{name}", minimum=0)
    for name in ("time_ms", "packet_id", "channel_index"):
        if observation.get(name) is not None:
            _require_int(observation[name], f"{path}.{name}", minimum=0)

    if source_table == "fields":
        _require(observation.get("checkpoint_index") is None, f"{path}.checkpoint_index must be null for fields")
        _require(observation.get("checkpoint_id") is None, f"{path}.checkpoint_id must be null for fields")
    else:
        _require_int(observation.get("checkpoint_index"), f"{path}.checkpoint_index", minimum=0)
        _require(type(observation.get("checkpoint_id")) is str and observation["checkpoint_id"], f"{path}.checkpoint_id must be non-empty")

    digest = observation.get("parent_raw_sha256")
    _require(type(digest) is str and len(digest) == 64, f"{path}.parent_raw_sha256 must be a SHA-256 hex digest")
    try:
        bytes.fromhex(digest)
    except ValueError as exc:
        raise KillStateError(f"{path}.parent_raw_sha256 is not hexadecimal") from exc

    _require(type(observation.get("members_complete")) is bool, f"{path}.members_complete must be boolean")
    members = observation.get("members")
    _require(type(members) is dict, f"{path}.members must be an object")
    _validate_typed(members, f"{path}.members")
    raw_members = _validate_raw_members(members.get("raw_members"), f"{path}.members.raw_members")
    for raw_name in raw_members:
        member_name = _RAW_TO_MEMBER[raw_name]
        _require(member_name in members, f"{path}.members lacks {member_name} for {raw_name}")
    computed_complete = set(raw_members) >= _REQUIRED_RAW
    _require(observation["members_complete"] == computed_complete, f"{path}.members_complete disagrees with required raw handles")
    try:
        _validate_member_consistency(members, raw_members, f"{path}.members")
    except InputError as exc:
        raise KillStateError(f"{path}.members: {exc}") from exc
    if not computed_complete:
        _require(set(raw_members) == {"bDidKillTriggerFinisher"}, f"{path} has unsupported partial member set")
    return observation


def _source_receipt(observation: dict[str, Any], source_index: int) -> dict[str, Any]:
    return {
        "source_index": source_index,
        "source_table": observation["source_table"],
        "checkpoint_index": observation.get("checkpoint_index"),
        "checkpoint_id": observation.get("checkpoint_id"),
        "physical_parent_row_ordinal": observation["physical_parent_row_ordinal"],
        "element_index": observation["element_index"],
        "time_ms": observation.get("time_ms"),
        "packet_id": observation.get("packet_id"),
        "channel_index": observation.get("channel_index"),
        "actor_net_guid": observation["actor_net_guid"],
        "object_net_guid": observation["object_net_guid"],
        "parent_raw_sha256": observation["parent_raw_sha256"],
    }


def _state(members: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    copied = copy.deepcopy(members)
    raw_members = copied.pop("raw_members")
    return {"members": copied, "raw_members": raw_members, "source": copy.deepcopy(source)}


def _state_token(state: dict[str, Any]) -> Any:
    return (_typed_token(state["members"]), _typed_token(state["raw_members"]))


def project_kill_state(document: Any) -> dict[str, Any]:
    """Validate and project one schema-1 kill-observation export; raises
    :class:`KillStateError` when identity or update order is ambiguous.
    Checkpoint observations remain snapshots and never create or mutate entities."""
    _require(type(document) is dict, "document must be an object")
    _require(type(document.get("schema_version")) is int and document["schema_version"] == SCHEMA_VERSION, "schema_version must be integer 1")
    _require(document.get("kind") == DOCUMENT_KIND, "kind is unsupported")
    provenance = document.get("provenance")
    _require(type(provenance) is dict, "provenance must be an object")
    export_id = provenance.get("export_id")
    _require(type(export_id) is str and export_id, "provenance.export_id must be explicit and non-empty")
    observations = document.get("observations")
    _require(type(observations) is list, "observations must be an array")

    indexed = [(_validate_observation(item, index), index) for index, item in enumerate(observations)]
    main = [(item, index) for item, index in indexed if item["source_table"] == "fields"]
    checkpoints = [(item, index) for item, index in indexed if item["source_table"] == "checkpoint_fields"]
    main.sort(key=lambda pair: (pair[0]["physical_parent_row_ordinal"], pair[0]["element_index"], pair[1]))
    checkpoints.sort(key=lambda pair: (pair[0]["checkpoint_index"], pair[0]["physical_parent_row_ordinal"], pair[0]["element_index"], pair[1]))

    entities: dict[tuple[str, int, int], dict[str, Any]] = {}
    entity_order: list[tuple[str, int, int]] = []
    owners: dict[tuple[str, int], int] = {}
    main_coordinates: set[tuple[int, int]] = set()
    counts = Counter({key: 0 for key in _COUNT_KEYS})
    counts["input_observations"] = len(indexed)

    def check_owner(item: dict[str, Any], index: int) -> None:
        component = (export_id, item["object_net_guid"])
        actor = item["actor_net_guid"]
        if component in owners and owners[component] != actor:
            raise KillStateError(
                f"observations[{index}] actor ownership conflicts for object_net_guid {component[1]}"
            )
        owners[component] = actor

    for item, index in main:
        coordinate = (item["physical_parent_row_ordinal"], item["element_index"])
        if coordinate in main_coordinates:
            raise KillStateError(f"observations[{index}] repeats main physical parent/element coordinate {coordinate!r}")
        main_coordinates.add(coordinate)
        check_owner(item, index)
        key = (export_id, item["object_net_guid"], item["element_index"])
        source = _source_receipt(item, index)
        raw_members = item["members"]["raw_members"]
        if item["members_complete"]:
            if key in entities:
                raise KillStateError(f"observations[{index}] duplicates complete state key {key!r}")
            base = _state(item["members"], source)
            entities[key] = {
                "key": {"export_id": export_id, "object_net_guid": key[1], "element_index": key[2]},
                "actor_net_guid": item["actor_net_guid"],
                "base": base,
                "latest": copy.deepcopy(base),
                "revisions": [],
            }
            entity_order.append(key)
            counts["main_complete"] += 1
            continue

        counts["main_partial"] += 1
        _require(raw_members, f"observations[{index}] partial update has no present raw members")
        if key not in entities:
            raise KillStateError(f"observations[{index}] partial update has no prior exact-key base")
        entity = entities[key]
        latest = entity["latest"]
        next_state = copy.deepcopy(latest)
        next_state["source"] = copy.deepcopy(source)
        changed_fields: list[str] = []
        for raw_name, raw_value in raw_members.items():
            member_name = _RAW_TO_MEMBER[raw_name]
            new_value = item["members"][member_name]
            if _typed_token(latest["members"].get(member_name)) != _typed_token(new_value) or _typed_token(latest["raw_members"].get(raw_name)) != _typed_token(raw_value):
                changed_fields.append(member_name)
            next_state["members"][member_name] = copy.deepcopy(new_value)
            next_state["raw_members"][raw_name] = copy.deepcopy(raw_value)
        revision = copy.deepcopy(next_state)
        revision.update(
            {
                "revision_index": len(entity["revisions"]) + 1,
                "changed": bool(changed_fields),
                "changed_fields": changed_fields,
                "present_raw_members": sorted(raw_members),
            }
        )
        entity["revisions"].append(revision)
        entity["latest"] = next_state
        counts["main_revisions"] += 1
        counts["main_revisions_changed" if changed_fields else "main_revisions_unchanged"] += 1

    snapshot_matches: list[dict[str, Any]] = []
    snapshot_unresolved: list[dict[str, Any]] = []
    unresolved_reasons = Counter({reason: 0 for reason in _UNRESOLVED_REASONS})
    checkpoint_coordinates: set[tuple[int, int, int]] = set()
    for item, index in checkpoints:
        coordinate = (item["checkpoint_index"], item["physical_parent_row_ordinal"], item["element_index"])
        if coordinate in checkpoint_coordinates:
            raise KillStateError(f"observations[{index}] repeats checkpoint physical parent/element coordinate {coordinate!r}")
        checkpoint_coordinates.add(coordinate)
        check_owner(item, index)
        counts["checkpoint_observations"] += 1
        key = (export_id, item["object_net_guid"], item["element_index"])
        source = _source_receipt(item, index)
        snapshot = _state(item["members"], source)
        common = {
            "key": {"export_id": export_id, "object_net_guid": key[1], "element_index": key[2]},
            "snapshot": snapshot,
        }
        reason: str | None = None
        if not item["members_complete"]:
            reason = "incomplete_checkpoint"
        elif key not in entities:
            reason = "no_main_entity"
        else:
            entity = entities[key]
            versions = [(0, entity["base"])] + [
                (revision["revision_index"], revision) for revision in entity["revisions"]
            ]
            token = _state_token(snapshot)
            matched = [revision_index for revision_index, state in versions if _state_token(state) == token]
            if matched:
                snapshot_matches.append({**common, "matched_revision_indices": matched})
                counts["checkpoint_matches"] += 1
                continue
            reason = "state_signature_unmatched"
        snapshot_unresolved.append({**common, "reason": reason})
        unresolved_reasons[reason] += 1
        counts["checkpoint_unresolved"] += 1

    counts["entities"] = len(entities)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "vrfkit_killdata_state_projection",
        "provenance": copy.deepcopy(provenance),
        "counts": dict(sorted(counts.items())),
        "entities": [entities[key] for key in entity_order],
        "checkpoint_snapshots": {
            "matches": snapshot_matches,
            "unresolved": snapshot_unresolved,
            "unresolved_by_reason": dict(sorted(unresolved_reasons.items())),
        },
    }
