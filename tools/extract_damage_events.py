#!/usr/bin/env python3
"""Derive damage_events.parquet from an export: one row per
`MulticastNotifyDamage_{Point,Base}` invocation (hit lines, the kill feed).

An invocation is a run of consecutive parameter rows sharing packet, channel,
actor, subobject and function; a parameter repeating inside the run starts the
next one, since the export carries no invocation id. The victim is the row's
actor -- the damaged component's owner -- never the `Character` parameter,
which is 0 on a killing blow against utility; `victim_subject` is set only for
a `SpawnedCharacter` pawn one player claims (player_identity.py). `NetTimestamp`
-3.4028e38 and `RespawnNumber` -1 are sentinels and become null (210 of 632
records on 13.01).

Killing blows on a player body (conflicting claims included) must pair one to
one, in time order per victim, with `events.characterDeath` (victim = word1);
anything unpaired fails the run.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from equippable_table import EQUIPPABLE_BY_PATH
from extract_rounds import parquet_cli
from player_identity import COUNT_KEYS as IDENTITY_KEYS, load_player_bodies
from wire_bits import iter_selected, text

FUNCTIONS = ("MulticastNotifyDamage_Point", "MulticastNotifyDamage_Base")
#: Parameter -> (column, value column); vectors are "(x,y,z)" strings.
SCALARS = {
    "DamagerPlayerState": ("damager_player_state", "value_i64"),
    "EquippableUsed": ("weapon_net_guid", "value_i64"),
    "DamageDealt": ("damage_dealt", "value_f64"), "DamageTaken": ("damage_taken", "value_f64"),
    "FalloffMultiplier": ("falloff", "value_f64"), "DamagedBone": ("bone", "value_str"),
    "RegionalDamage": ("regional_damage", "value_i64"), "bDamageKilledTarget": ("killed", "value_bool"),
    "bIsWallPenetration": ("wallbang", "value_bool"), "NetTimestamp": ("net_timestamp", "value_f64"),
    "RespawnNumber": ("respawn_number", "value_i64"),
}
_TYPES = {"value_i64": pa.int64(), "value_f64": pa.float64(), "value_str": pa.string(),
          "value_bool": pa.bool_()}
VECTORS = {"DamageOrigin": "origin", "DamageImpactLocation": "impact", "DamageDirection": "direction"}
SENTINELS = {"net_timestamp": -3.4028234663852886e38, "respawn_number": -1}
#: A killing blow lands 8-31 ms after its characterDeath (3,148 pairs, 22 exports).
KILL_FEED_SLACK_MS = 100

SCHEMA = pa.schema([
    ("time_ms", pa.int64()), ("packet_id", pa.int64()), ("function", pa.string()),
    ("victim_actor_net_guid", pa.int64()),
    *((name, pa.string()) for name in ("victim_class_path", "victim_subject", "damager_subject",
                                        "weapon_class_path", "weapon_name")),
    *((column, _TYPES[value]) for column, value in SCALARS.values()),
    *((f"{v}_{axis}", pa.float64()) for v in VECTORS.values() for axis in "xyz"),
])
COUNT_KEYS = (*FUNCTIONS, "repeated-parameter splits", "sentinels nulled",
              "untyped parameter rows", "unparsed vectors", "actor GUIDs with two classes",
              "weapon GUIDs without a class", "killing blows on a player body",
              "characterDeath events", "kill feed pairs", "killing blows without a death",
              "deaths without a killing blow", *(f"player_identity {k}" for k in IDENTITY_KEYS))


def actor_classes(export: Path, counts: Counter) -> dict:
    """actor GUID -> class path from actors.parquet; a GUID opened as two classes maps to None."""
    table = pq.read_table(export / "actors.parquet", columns=["actor_net_guid", "class_path"])
    classes = {}
    for guid, path in zip(table.column("actor_net_guid").to_pylist(),
                          pc.cast(table.column("class_path"), pa.string()).to_pylist()):
        if path and classes.setdefault(guid, path) not in (path, None):
            counts["actor GUIDs with two classes"] += 1
            classes[guid] = None
    return classes


def invocations(export: Path, counts: Counter):
    """Yield (first row, {parameter: row}) per invocation, in wire order."""
    columns = ["time_ms", "packet_id", "channel_index", "actor_net_guid", "object_net_guid",
               "field_name", "value_i64", "value_f64", "value_bool", "value_str"]
    mask = lambda batch: pc.starts_with(text(batch, "field_name"), "MulticastNotifyDamage_")  # noqa: E731
    key, current, first = None, None, None
    for _, row in iter_selected(export / "fields.parquet", columns, mask):
        function, _, parameter = row["field_name"].partition(".")
        if function not in FUNCTIONS:
            continue
        row_key = (row["packet_id"], row["channel_index"], row["actor_net_guid"],
                   row["object_net_guid"], function)
        if row_key != key or parameter in current:
            if row_key == key:
                counts["repeated-parameter splits"] += 1
            if current is not None:
                yield first, current
            key, current, first = row_key, {}, row
        current[parameter] = row
    if current is not None:
        yield first, current


def build(export: Path) -> tuple[list[dict], Counter, list[str]]:
    counts, problems = Counter(dict.fromkeys(COUNT_KEYS, 0)), []
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    identity = load_player_bodies(export, manifest)
    counts.update({f"player_identity {k}": v for k, v in identity.counts.items()})
    bodies = identity.subjects
    subject_of_state = {p.get("actor_net_guid"): p.get("subject") for p in manifest.get("players", [])}
    classes = actor_classes(export, counts)
    rows = []
    for first, parameters in invocations(export, counts):
        function = first["field_name"].split(".", 1)[0]
        counts[function] += 1
        victim = first["actor_net_guid"]
        out = dict.fromkeys(SCHEMA.names)
        out.update(time_ms=first["time_ms"], packet_id=first["packet_id"],
                   function=function.rsplit("_", 1)[1], victim_actor_net_guid=victim,
                   victim_class_path=classes.get(victim), victim_subject=bodies.get(victim))
        for parameter, (column, value_column) in SCALARS.items():
            row = parameters.get(parameter)
            if row is None:
                continue
            value = row[value_column]
            if value is None:
                counts["untyped parameter rows"] += 1
            elif SENTINELS.get(column) == value:
                counts["sentinels nulled"] += 1
                value = None
            out[column] = value
        for parameter, prefix in VECTORS.items():
            if parameter in parameters:
                try:
                    out[f"{prefix}_x"], out[f"{prefix}_y"], out[f"{prefix}_z"] = map(
                        float, parameters[parameter]["value_str"].strip("()").split(","))
                except (AttributeError, ValueError):
                    counts["unparsed vectors"] += 1
        for column in ("weapon_net_guid", "damager_player_state"):
            out[column] = out[column] or None  # GUID 0 is the null reference
        out["damager_subject"] = subject_of_state.get(out["damager_player_state"])
        if out["weapon_net_guid"]:
            out["weapon_class_path"] = classes.get(out["weapon_net_guid"])
            if out["weapon_class_path"] is None:
                counts["weapon GUIDs without a class"] += 1
            hit = EQUIPPABLE_BY_PATH.get(out["weapon_class_path"])
            out["weapon_name"] = hit[0] if hit else None
        rows.append(out)

    blows, deaths = defaultdict(list), defaultdict(list)
    for r in rows:
        if r["killed"] and (r["victim_subject"] or r["victim_actor_net_guid"] in identity.conflicts):
            blows[r["victim_actor_net_guid"]].append(r["time_ms"])
    for event in pq.read_table(export / "events.parquet", columns=["group", "time1", "word1"]).to_pylist():
        if event["group"] == "characterDeath":
            deaths[event["word1"]].append(event["time1"])
    for victim in blows.keys() | deaths.keys():
        blow_times, death_times = sorted(blows[victim]), sorted(deaths[victim])
        paired = sum(0 <= b - d <= KILL_FEED_SLACK_MS for b, d in zip(blow_times, death_times))
        counts["killing blows on a player body"] += len(blow_times)
        counts["characterDeath events"] += len(death_times)
        counts["kill feed pairs"] += paired
        counts["killing blows without a death"] += len(blow_times) - paired
        counts["deaths without a killing blow"] += len(death_times) - paired
    if counts["killing blows without a death"] or counts["deaths without a killing blow"]:
        problems.append("the kill feed does not pair one to one with characterDeath")
    return rows, counts, problems


def main(argv=None) -> int:
    return parquet_cli(__doc__, build, SCHEMA, "damage invocation(s)", argv)


if __name__ == "__main__":
    raise SystemExit(main())
