#!/usr/bin/env python3
"""Extract blind updates and continuous-effect observations by target identity.

The player-body distinction: only a SpawnedCharacter value proves a player
target (player_identity.py). Every
observation is kept, blinds on non-player actors included. These are
replicated updates and RPC observations, not hit counts, cast attribution or
effect intervals; checkpoint snapshots are excluded.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pyarrow.parquet as pq

from atomic_io import run_json_cli
from player_identity import FINAL_PROVENANCE, load_player_bodies
from wire_bits import load_net_guids


BLIND_GROUP = "/Script/ShooterGame.BlindManagerComponent"
EFFECT_GROUP = "/Script/ShooterGame.EffectManagerComponent_ClassNetCache"
RPC_KINDS = {
    "MulticastPlayContinuousEffect": "continuous_start",
    "MulticastStopContinuousEffect": "continuous_stop",
}
COLUMNS = ["time_ms", "packet_id", "channel_index", "actor_net_guid",
           "object_net_guid", "group_path", "field_name", "value_i64",
           "value_f64", "value_bool", "value_str"]


def build(export_dir: Path) -> dict:
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    bodies = load_player_bodies(export_dir, manifest)
    players = bodies.subjects
    conflicts = bodies.conflicts
    paths = load_net_guids(export_dir, "path")
    fields = pq.read_table(export_dir / "fields.parquet", columns=COLUMNS,
                           filters=[("group_path", "in", [BLIND_GROUP, EFFECT_GROUP])])
    records = []
    pending = None
    tally = Counter()

    def finish() -> None:
        nonlocal pending
        if pending is None:
            return
        record = pending
        pending = None
        actor = record["actor_net_guid"]
        record["target_identity"] = ("player_body" if actor in players else
                                     "conflicting_manifest_identity" if actor in conflicts
                                     else "unconfirmed_actor")
        record["target_subject"] = players.get(actor)
        record["identity_provenance"] = bodies.provenance.get(actor)
        container = record["values"].get("EffectContainer")
        record["effect_container_path"] = paths.get(container)
        record["observation_index"] = len(records)
        records.append(record)
        tally[record["kind"]] += 1
        if actor in players:
            tally["player_" + record["kind"]] += 1
            if bodies.provenance[actor] != FINAL_PROVENANCE:
                tally["player_body_via_non_final_spawned_character"] += 1
        else:
            tally["unconfirmed_target_observations"] += 1

    # Keep wire order. Repeated parameter names delimit consecutive invocations
    # in one packet; a parent ActiveBlinds row ends its already-emitted leaves.
    for batch in fields.to_batches(max_chunksize=65536):
        for row in batch.to_pylist():
            name = row["field_name"] or ""
            context = {key: row[key] for key in COLUMNS[:6]}
            if row["group_path"] == BLIND_GROUP:
                if not name.startswith("ActiveBlinds[") or "]." not in name:
                    finish()
                    if name == "ActiveBlinds":
                        tally["blind_parent_rows"] += 1
                    continue
                prefix, member = name.split("].", 1)
                kind, source = "blind_update", prefix + "]"
            else:
                source, separator, member = name.partition(".")
                kind = RPC_KINDS.get(source)
                if kind is None or not separator:
                    finish()
                    tally["other_effect_rows"] += 1
                    continue
            key = (context, kind, source)
            if pending is not None:
                old_key = ({k: pending[k] for k in context}, pending["kind"], pending["source"])
                if old_key != key or member in pending["values"]:
                    if old_key == key:
                        tally["same_packet_parameter_restarts"] += 1
                    finish()
            if pending is None:
                pending = {**context, "kind": kind, "source": source,
                           "values": {}, "untyped_members": []}
            typed = [row[k] for k in COLUMNS[7:] if row[k] is not None]
            if len(typed) > 1:
                raise ValueError(f"multiple typed values for {name}")
            pending["values"][member] = typed[0] if typed else None
            if not typed:
                pending["untyped_members"].append(member)
                tally["untyped_members"] += 1
    finish()
    counters = ["blind_update", "continuous_start", "continuous_stop",
                "player_blind_update", "player_continuous_start", "player_continuous_stop",
                "player_body_via_non_final_spawned_character",
                "unconfirmed_target_observations", "blind_parent_rows", "other_effect_rows",
                "same_packet_parameter_restarts", "untyped_members"]
    return {
        "schema_version": 1,
        "source": str(export_dir.resolve()),
        "semantics": "main-stream observations, not unique hits or inferred intervals",
        "records": records,
        "totals": {**{key: tally[key] for key in counters},
                   "conflicting_manifest_character_guids": len(conflicts),
                   "player_identity": bodies.counts},
    }


def main(argv: list[str] | None = None) -> int:
    return run_json_cli(__doc__, build, lambda d, out: [json.dumps(d["totals"], sort_keys=True)],
                        argv, sources=[Path(__file__)], indent=2, allow_nan=False)


if __name__ == "__main__":
    raise SystemExit(main())
