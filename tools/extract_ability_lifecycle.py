#!/usr/bin/env python3
"""Derive evidence-first ability actor ownership and lifecycle candidates.

No cast-count claim. A candidate is admitted only when its class lives below
``/Game/Characters/<name>/`` with an explicit ability path segment. Owner and
Instigator are reported as replicated references, linked to a player only
when an unambiguous one equals a ``SpawnedCharacter`` pawn
(``player_identity.py``), which is still not proof of a cast.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from atomic_io import run_json_cli
from player_identity import FINAL_PROVENANCE, load_player_bodies


ACTOR_COLUMNS = ("time_ms", "packet_id", "channel_index", "actor_net_guid",
                 "event", "class_path")
FIELD_COLUMNS = ("time_ms", "packet_id", "channel_index", "actor_net_guid",
                 "group_path", "field_name", "value_i64")
UNRESOLVED_REASONS = (
    "channel_reused_without_close", "same_packet_channel_reopen_ambiguous",
    "component_reference_rows_ignored", "manifest_character_guid_conflict",
    "owner_missing", "owner_null_reference", "owner_conflicting_updates",
    "instigator_missing", "instigator_null_reference",
    "instigator_conflicting_updates", "owner_instigator_player_conflict",
    "no_unambiguous_manifest_player_reference",
)


def classify_candidate(class_path: str | None) -> dict | None:
    """Return structural classification evidence, never a name-keyword guess."""
    if not class_path:
        return None
    parts = class_path.split("/")
    if len(parts) < 5 or parts[:3] != ["", "Game", "Characters"]:
        return None
    character = parts[3]
    if character.startswith("_"):
        return None
    evidence = next((part for part in parts[4:-1]
                     if part.casefold() == "abilities"
                     or part.casefold().startswith("ability_")), None)
    if evidence is None:
        return None
    return {
        "classification": "character_ability_path_candidate",
        "classification_provenance": "class_path_segment",
        "classification_evidence": evidence,
        "character_path_segment": character,
    }


def _position(row: dict) -> tuple[int, int]:
    return int(row["time_ms"]), int(row["packet_id"])


def _reference(rows: list[dict], field: str) -> dict:
    evidence = [
        {"net_guid": int(row["value_i64"]), "time_ms": int(row["time_ms"]),
         "packet_id": int(row["packet_id"])}
        for row in rows if row.get("field_name") == field
        and row.get("value_i64") is not None
    ]
    values = sorted({row["net_guid"] for row in evidence if row["net_guid"] != 0})
    only_null = bool(evidence) and not values
    return {
        "net_guid": values[0] if len(values) == 1 else None,
        "status": "resolved" if len(values) == 1 else
                  ("null_reference" if only_null else
                   ("missing" if not values else "conflicting_updates")),
        "evidence": evidence,
    }


def build(export_dir: Path) -> dict:
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    bodies = load_player_bodies(export_dir, manifest)
    players = bodies.subjects
    conflicting_player_guids = bodies.conflicts
    actors = pq.read_table(export_dir / "actors.parquet",
                           columns=list(ACTOR_COLUMNS)).to_pylist()
    fields = pq.read_table(export_dir / "fields.parquet",
                           columns=list(FIELD_COLUMNS),
                           filters=[("field_name", "in", ["Owner", "Instigator"])]
                           ).to_pylist()
    fields_by_actor = defaultdict(list)
    for row in fields:
        fields_by_actor[int(row["actor_net_guid"])].append(row)

    open_instances: dict[tuple[int, int], dict] = {}
    open_by_channel: dict[int, tuple[int, int]] = {}
    last_close_by_channel: dict[int, tuple[tuple[int, int], int, int]] = {}
    records = []
    event_counts = Counter()

    def finish(open_row: dict, close_row: dict | None, superseded: bool = False,
               evidence_end: tuple[int, int] | None = None) -> None:
        start, end = _position(open_row), (_position(close_row) if close_row else None)
        bounded = [row for row in fields_by_actor[int(open_row["actor_net_guid"])]
                   if int(row["channel_index"]) == int(open_row["channel_index"])
                   and _position(row) >= start
                   and (end is None or _position(row) <= end)
                   and (evidence_end is None or _position(row) < evidence_end)]
        related = [row for row in bounded if row.get("group_path") == open_row["class_path"]]
        mismatched = [row for row in bounded if row.get("group_path") != open_row["class_path"]]
        owner = _reference(related, "Owner")
        instigator = _reference(related, "Instigator")
        player_refs = {ref["net_guid"] for ref in (owner, instigator)
                       if ref["status"] == "resolved" and ref["net_guid"] in players}
        unresolved = []
        if superseded:
            unresolved.append("channel_reused_without_close")
        if open_row.get("same_packet_reopen"):
            unresolved.append("same_packet_channel_reopen_ambiguous")
        if mismatched:
            unresolved.append("component_reference_rows_ignored")
        if any(ref["net_guid"] in conflicting_player_guids
               for ref in (owner, instigator) if ref["net_guid"] is not None):
            unresolved.append("manifest_character_guid_conflict")
        for name, ref in (("owner", owner), ("instigator", instigator)):
            if ref["status"] != "resolved":
                unresolved.append(f"{name}_{ref['status']}")
        if len(player_refs) > 1:
            unresolved.append("owner_instigator_player_conflict")
        link_blocked = open_row.get("same_packet_reopen", False) or any(
            ref["status"] == "conflicting_updates" for ref in (owner, instigator)
        )
        player_link = (next(iter(player_refs))
                       if len(player_refs) == 1 and not link_blocked else None)
        if player_link is None:
            unresolved.append("no_unambiguous_manifest_player_reference")
        dormant = [row for row in open_row["following_events"]
                   if row["event"] == "dormant" and (end is None or _position(row) <= end)]
        record = {
            **open_row["classification"],
            "actor_net_guid": int(open_row["actor_net_guid"]),
            "channel_index": int(open_row["channel_index"]),
            "class_path": open_row["class_path"],
            "opened_time_ms": int(open_row["time_ms"]),
            "opened_packet_id": int(open_row["packet_id"]),
            "closed_time_ms": int(close_row["time_ms"]) if close_row else None,
            "closed_packet_id": int(close_row["packet_id"]) if close_row else None,
            "lifecycle_status": "closed" if close_row else "right_censored",
            "last_seen_event_time_ms": max([int(open_row["time_ms"])] +
                [int(row["time_ms"]) for row in open_row["following_events"]]),
            "dormant_observations": [{"time_ms": int(row["time_ms"]),
                                      "packet_id": int(row["packet_id"])} for row in dormant],
            "replicated_owner": owner,
            "replicated_instigator": instigator,
            "linked_player_net_guid": player_link,
            "linked_player_subject": players.get(player_link),
            "player_reference_provenance": (
                None if not player_link else
                "manifest_character_net_guid_reference"
                if bodies.provenance[player_link] == FINAL_PROVENANCE else
                "spawned_character_history_reference"),
            "unresolved_reasons": sorted(set(unresolved)),
        }
        records.append(record)

    for row in sorted(actors, key=lambda r: (_position(r), int(r["channel_index"]))):
        event_counts[row["event"]] += 1
        key = int(row["actor_net_guid"]), int(row["channel_index"])
        if row["event"] == "open":
            classification = classify_candidate(row.get("class_path"))
            previous_key = open_by_channel.pop(int(row["channel_index"]), None)
            previous = open_instances.pop(previous_key, None) if previous_key else None
            same_packet = bool(previous and _position(previous) == _position(row))
            previous_close = last_close_by_channel.get(int(row["channel_index"]))
            same_packet = same_packet or bool(
                previous_close and previous_close[0] == _position(row)
            )
            if previous_close and previous_close[0] == _position(row):
                closed_record = records[previous_close[2]]
                closed_record["linked_player_net_guid"] = None
                closed_record["linked_player_subject"] = None
                closed_record["player_reference_provenance"] = None
                closed_record["unresolved_reasons"] = sorted(set(
                    closed_record["unresolved_reasons"] + [
                        "same_packet_channel_reopen_ambiguous",
                        "no_unambiguous_manifest_player_reference",
                    ]
                ))
            if previous is not None:
                previous["same_packet_reopen"] = same_packet
                finish(previous, None, True, _position(row))
            if classification:
                open_instances[key] = {**row, "classification": classification,
                                       "following_events": [],
                                       "same_packet_reopen": same_packet}
                open_by_channel[int(row["channel_index"])] = key
        elif key in open_instances:
            open_instances[key]["following_events"].append(row)
            if row["event"] == "close":
                finish(open_instances.pop(key), row)
                open_by_channel.pop(int(row["channel_index"]), None)
                last_close_by_channel[int(row["channel_index"])] = (
                    _position(row), int(row["actor_net_guid"]), len(records) - 1
                )
    for open_row in open_instances.values():
        finish(open_row, None)

    records.sort(key=lambda r: (r["opened_time_ms"], r["opened_packet_id"],
                                r["channel_index"], r["actor_net_guid"]))
    linked = sum(row["linked_player_net_guid"] is not None for row in records)
    closed = sum(row["lifecycle_status"] == "closed" for row in records)
    reason_counts = Counter(reason for row in records
                            for reason in row["unresolved_reasons"])
    return {
        "schema_version": 1,
        "semantics": "candidate actor lifecycles; not an ability cast ledger",
        "source": str(export_dir.resolve()),
        "records": records,
        "totals": {
            "candidate_instances": len(records), "linked_player_reference": linked,
            "linked_via_non_final_spawned_character": sum(
                row["player_reference_provenance"] == "spawned_character_history_reference"
                for row in records),
            "unresolved_player_reference": len(records) - linked,
            "conflicting_manifest_character_guids": len(conflicting_player_guids),
            "player_identity": bodies.counts,
            "unresolved_reasons": {reason: reason_counts[reason]
                                   for reason in UNRESOLVED_REASONS},
            "closed": closed, "right_censored": len(records) - closed,
            "went_dormant": sum(bool(row["dormant_observations"]) for row in records),
            "actor_events_seen": dict(sorted(event_counts.items())),
        },
    }


def summary(document, out):
    totals = document["totals"]
    yield "wrote {} ({} candidate(s), {} linked, {} unresolved, {} closed, {} right-censored)".format(
        out, totals["candidate_instances"], totals["linked_player_reference"],
        totals["unresolved_player_reference"], totals["closed"], totals["right_censored"])
    # Printed with its zero, so 0 reads as "none".
    yield "  linked via an earlier SpawnedCharacter pawn: {}; player identity: {}".format(
        totals["linked_via_non_final_spawned_character"],
        json.dumps(totals["player_identity"], sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    return run_json_cli(__doc__, build, summary, argv, sources=[Path(__file__)], indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
