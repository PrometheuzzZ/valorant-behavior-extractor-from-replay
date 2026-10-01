"""Player bodies from the whole `SpawnedCharacter` history, not its last value.

Only `SpawnedCharacter` proves a body: `PossessedCharacter`, `Owner`,
`Instigator` and a pawn's own `PlayerState` can name a device (Astra's
`Rift_TargetingForm_PC_C` carries the player's PlayerState).

The manifest's `character_net_guid` is the last non-zero value, so it drops a
pre-reconnect pawn (docs/DATA.md, "Player identity": 1510 -> 0 -> 45530); its
`character_net_guids` lists every pawn, and this history must reproduce it
(`manifest_history_disagreements`). The rule is as static:
a pawn named by exactly one PlayerState is that player's body for the whole
export. Not time-scoped: 347 effect rows on 24 pawns share the naming write's
`time_ms` but precede it by packet id (the spawn tick).
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

#: The PlayerState classes that replicate `SpawnedCharacter`: Bomb, and the
#: Swiftplay alias `GROUP_ALIASES` in crates/vrf-decode/src/overlay.rs maps to
#: it (test_player_identity.py fails on an alias this tuple lacks).
PLAYER_STATE_GROUPS = (
    "/Game/GameModes/Bomb/BombPlayerState.BombPlayerState_C",
    "/Game/GameModes/_Development/Swiftplay_EndOfRoundCredits/"
    "Swiftplay_EoRCredits_PlayerState.Swiftplay_EoRCredits_PlayerState_C",
)

#: Provenance of the manifest's own `character_net_guid`, byte-identical to
#: what records labelled before this module carry.
FINAL_PROVENANCE = "manifest.players.character_net_guid (SpawnedCharacter)"
#: Provenance of a body the manifest dropped: an earlier non-zero value.
EARLIER_PROVENANCE = "fields.SpawnedCharacter history (earlier pawn of a manifest player)"

COUNT_KEYS = (
    "spawned_character_rows",
    "spawned_character_rows_other_groups",
    "spawned_character_rows_nested",
    "untyped_spawned_character_rows",
    "player_body_pawns",
    "non_final_spawned_character_pawns",
    "pawns_claimed_by_multiple_player_states",
    "conflicting_subject_pawns",
    "spawned_character_player_states_not_in_manifest",
    "manifest_history_disagreements",
    "manifest_characters_without_history",
)

_COLUMNS = ["time_ms", "packet_id", "actor_net_guid", "object_net_guid",
            "group_path", "field_name", "value_i64"]


@dataclass(frozen=True)
class PlayerBodies:
    """Every pawn a `SpawnedCharacter` proves, joined to one subject.

    `subjects` holds only unambiguous bodies; a pawn claimed by two
    PlayerStates or two subjects is in `conflicts` instead, never guessed.
    """

    subjects: dict
    provenance: dict
    conflicts: frozenset
    history: dict
    counts: dict


def spawned_character_rows(fields_path: Path) -> list[dict]:
    """Every `SpawnedCharacter` row of a main `fields.parquet`, any group."""
    table = pq.read_table(fields_path, columns=_COLUMNS,
                          filters=[("field_name", "==", "SpawnedCharacter")])
    return table.to_pylist()


def player_bodies(manifest: dict, rows: list[dict]) -> PlayerBodies:
    """Join `SpawnedCharacter` rows and manifest players into bodies."""
    counts = dict.fromkeys(COUNT_KEYS, 0)
    history = defaultdict(list)
    for ordinal, row in enumerate(rows):
        counts["spawned_character_rows"] += 1
        if row["group_path"] not in PLAYER_STATE_GROUPS:
            counts["spawned_character_rows_other_groups"] += 1
            continue
        if row["object_net_guid"] is not None:
            counts["spawned_character_rows_nested"] += 1
            continue
        value = row["value_i64"]
        if value is None:
            counts["untyped_spawned_character_rows"] += 1
            continue
        if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
            raise ValueError(f"SpawnedCharacter is not a u32 NetGUID: {value!r}")
        history[int(row["actor_net_guid"])].append(
            (int(row["packet_id"]), ordinal, int(row["time_ms"]), value))
    # Packet order, not time: a non-finite frame exports time_ms 0.
    history = {state: tuple((t, p, v) for p, _, t, v in sorted(writes))
               for state, writes in history.items()}

    subject_of = {}
    final_of = {}
    bodies_of = {}
    states = defaultdict(set)
    subjects = defaultdict(set)
    finals = set()
    for player in manifest.get("players", []):
        state = player.get("actor_net_guid")
        character = player.get("character_net_guid")
        if state is not None:
            subject_of[int(state)] = player.get("subject")
            final_of[int(state)] = int(character) if character else None
            bodies_of[int(state)] = player.get("character_net_guids")
        if character:
            finals.add(int(character))
            subjects[int(character)].add(player.get("subject"))
            if state is not None:
                states[int(character)].add(int(state))

    for state, writes in history.items():
        named = [value for _, _, value in writes if value]
        if not named:
            continue
        if state not in subject_of:
            # The manifest lists only PlayerStates whose Subject arrived; a
            # pawn of any other has no subject to join, so it stays unlabelled.
            counts["spawned_character_player_states_not_in_manifest"] += 1
        elif final_of[state] != named[-1] or bodies_of[state] not in (
                None, list(dict.fromkeys(reversed(named)))[::-1]):
            # The manifest's list is ordered by each pawn's last write.
            counts["manifest_history_disagreements"] += 1
        for pawn in set(named):
            states[pawn].add(state)
            if state in subject_of:
                subjects[pawn].add(subject_of[state])
    for state, character in final_of.items():
        if character and not any(value for _, _, value in history.get(state, ())):
            counts["manifest_characters_without_history"] += 1

    bodies = {}
    conflicts = set()
    for pawn in states.keys() | subjects.keys():
        if len(states[pawn]) > 1:
            counts["pawns_claimed_by_multiple_player_states"] += 1
        if len(subjects[pawn]) > 1:
            counts["conflicting_subject_pawns"] += 1
        if len(states[pawn]) > 1 or len(subjects[pawn]) > 1:
            conflicts.add(pawn)
        elif subjects[pawn]:
            bodies[pawn] = next(iter(subjects[pawn]))
    provenance = {pawn: FINAL_PROVENANCE if pawn in finals else EARLIER_PROVENANCE
                  for pawn in bodies}
    counts["player_body_pawns"] = len(bodies)
    counts["non_final_spawned_character_pawns"] = sum(
        value == EARLIER_PROVENANCE for value in provenance.values())
    return PlayerBodies(bodies, provenance, frozenset(conflicts), history, counts)


def load_player_bodies(export_dir: Path, manifest: dict | None = None) -> PlayerBodies:
    """`player_bodies` for one export directory."""
    if manifest is None:
        manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    return player_bodies(manifest, spawned_character_rows(export_dir / "fields.parquet"))
