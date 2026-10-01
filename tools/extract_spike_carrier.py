#!/usr/bin/env python3
"""Derive a spike (bomb) custody timeline from an export.

`BombEquippable_C.Owner` is re-replicated on every custody change, so a round
reads as a sequence of owner intervals (ground pickup, player, dropped
projectile, ...). This pairs consecutive `Owner` writes into intervals,
resolves each owner NetGUID to a class and joins players to their manifest
`subject`, from fields.parquet, actors.parquet and manifest.json alone. The
export must type `Owner`/`Instigator` (value_i64); older ones do not.

A player is every pawn a `SpawnedCharacter` value names (player_identity.py),
including one from before a reconnect; `carrier_identity_provenance` says which.

`Owner` is custody (in the backpack too); `AresInventory.NewCurrentEquippable`
sets `in_hand`. The pickup RPC `MulticastPlayBombPickedUpAudio` always agrees
with `Owner` and is not read.

An EquippableGroundPickup_C (on the floor) or EquippablePickupProjectile_C
(mid-air after a drop) owner means loose. Any other non-player owner, such as
Gekko's Wingman (Pawn_Aggrobot_SeekerNade_C, which really carries and plants
it), is asked for its own `Instigator`, with no allowlist, and reported as
`carrier_pawn_guid` with `via_proxy_class`; otherwise it stays `unknown`.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sys
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from atomic_io import atomic_write_file
from player_identity import EARLIER_PROVENANCE, load_player_bodies

BOMB_CLASS = "BombEquippable.BombEquippable_C"

#: Holder kinds that mean somebody is actually carrying the spike.
HELD_KINDS = ("player", "proxy")

#: Owner classes that mean nobody carries it, matched on the leaf name.
LOOSE_CLASSES = ("EquippableGroundPickup_C", "EquippablePickupProjectile_C")

#: The only field rows `build()` reads: the proxy `Instigator` walk, the
#: `Owner` custody log and the AresInventory in-hand writes.
FIELD_NAMES = ("Instigator", "Owner", "CurrentEquippable", "NewCurrentEquippable")
FIELD_COLUMNS = ("time_ms", "actor_net_guid", "group_path", "field_name", "value_i64")


def leaf(class_path: str | None) -> str:
    """Last path segment of a class path, or "" when the class is unknown."""
    return class_path.rsplit("/", 1)[-1] if class_path else ""


def classify_owner(owner: int, owner_class: str | None,
                   pawn_subject: dict, instigator: dict):
    """`(kind, carrier_pawn_guid, via_proxy_class)` for one `Owner` value, by
    the rules in the module docstring; anything else stays `unknown`."""
    if owner in pawn_subject:
        return "player", owner, ""
    name = leaf(owner_class)
    if any(k in name for k in LOOSE_CLASSES):
        return "loose", None, ""
    source = instigator.get(owner)
    if source in pawn_subject:
        return "proxy", source, name
    return "unknown", None, ""


def carrier_at(held, t_ms: int):
    """The custody interval covering `t_ms`, or None. A None `to_ms` (the
    bomb actor never closed) runs to the end of the replay."""
    covering = [r for r in held
                if r["from_ms"] <= t_ms and (r["to_ms"] is None or t_ms <= r["to_ms"])]
    return covering[-1] if covering else None


def unresolved(rows, events) -> list[str]:
    """What this extraction failed to resolve, one line each; any line makes
    the exit nonzero: no custody at all, or a plant with NO CARRIER."""
    problems = []
    if not rows:
        problems.append(
            "no custody intervals at all: no BombEquippable_C.Owner writes "
            "were found, so an empty table was written")
    held = [r for r in rows if r["holder_kind"] in HELD_KINDS]
    for group, t1 in zip(events.get("group", []), events.get("time1", [])):
        if group == "spikePlanted" and carrier_at(held, t1) is None:
            problems.append(
                f"plant at t={t1} resolves to NO CARRIER: no player or proxy "
                f"held the spike at that moment")
    return problems


def load(out_dir: Path):
    for name in ("fields.parquet", "actors.parquet", "manifest.json"):
        if not (out_dir / name).exists():
            raise SystemExit(f"no {name} in {out_dir} -- run `vrfkit export` first")

    # Filtered in Arrow (~1% of the rows are read); the first Instigator write
    # per actor depends on the physical order Table.filter keeps.
    fields = pq.read_table(out_dir / "fields.parquet", columns=list(FIELD_COLUMNS))
    fields = fields.filter(pc.is_in(fields.column("field_name"),
                                    value_set=pa.array(FIELD_NAMES)))
    f = {c: fields.column(c).to_pylist() for c in FIELD_COLUMNS}

    actors = pq.read_table(out_dir / "actors.parquet")
    a = {c: actors.column(c).to_pylist() for c in
         ("time_ms", "actor_net_guid", "event", "class_path")}

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))

    events = {}
    ev_path = out_dir / "events.parquet"
    if ev_path.exists():
        et = pq.read_table(ev_path)
        events = {c: et.column(c).to_pylist() for c in ("group", "time1", "metadata")}
    return f, a, manifest, events


def build(out_dir: Path):
    f, a, manifest, events = load(out_dir)

    # Dynamic actors (the spike, pawns) appear only in actors.parquet. A flat
    # map: a GUID recycled for another class would need time scoping.
    guid_class: dict[int, str] = {}
    for g, cp in zip(a["actor_net_guid"], a["class_path"]):
        if cp:
            guid_class.setdefault(g, cp)

    # Bomb actor lifetimes -- the close bounds the last custody interval.
    bomb_close: dict[int, int] = {}
    bombs: set[int] = set()
    for t, g, ev, cp in zip(a["time_ms"], a["actor_net_guid"],
                            a["event"], a["class_path"]):
        if cp and BOMB_CLASS in cp:
            bombs.add(g)
            if ev == "close":
                bomb_close.setdefault(g, t)

    bodies = load_player_bodies(out_dir, manifest)
    pawn_subject = bodies.subjects

    # Non-integer roundStarted metadata gives a null round_number.
    round_starts: list[tuple[int, int | None]] = []
    malformed_round_meta = 0
    for grp, t1, meta in zip(events.get("group", []), events.get("time1", []),
                             events.get("metadata", [])):
        if grp == "roundStarted":
            try:
                round_starts.append((t1, int(meta)))
            except (TypeError, ValueError):
                malformed_round_meta += 1
                round_starts.append((t1, None))
    round_starts.sort(key=lambda r: r[0])
    round_ts = [t for t, _ in round_starts]

    def round_of(ms: int):
        i = bisect.bisect_right(round_ts, ms) - 1
        return round_starts[i][1] if i >= 0 else None

    # instigator: an actor's first Instigator write (the proxy walk);
    # owner_log: every Owner write on a bomb channel, sorted below;
    # in_hand: AresInventory (pawn, bomb) pairs with their timestamps.
    instigator: dict[int, int] = {}
    owner_log: dict[int, list[tuple[int, int]]] = {}
    in_hand: dict[tuple[int, int], list[int]] = {}
    for t, actor, grp, name, value in zip(
            f["time_ms"], f["actor_net_guid"], f["group_path"], f["field_name"],
            f["value_i64"]):
        if name == "Instigator":
            if value:
                instigator.setdefault(actor, value)
        elif name == "Owner":
            if BOMB_CLASS in grp and value is not None:
                owner_log.setdefault(actor, []).append((t, value))
        elif grp.endswith("AresInventory") and value in bombs:
            in_hand.setdefault((actor, value), []).append(t)

    rows: list[dict] = []
    for bomb, log in sorted(owner_log.items()):
        log.sort()
        for n, (t, owner) in enumerate(log):
            end = log[n + 1][0] if n + 1 < len(log) else bomb_close.get(bomb)
            cls = guid_class.get(owner)
            kind, carrier, proxy = classify_owner(
                owner, cls, pawn_subject, instigator)
            held = in_hand.get((owner, bomb), [])
            rows.append({
                "round_number": round_of(t),
                "bomb_net_guid": bomb,
                "from_ms": t,
                "to_ms": end,
                "duration_ms": (end - t) if end is not None else None,
                "owner_net_guid": owner,
                # None, never "": "" would group as a real category.
                "owner_class": leaf(cls) or None,
                "holder_kind": kind,
                "carrier_pawn_guid": carrier,
                "carrier_subject": pawn_subject.get(carrier) or None,
                # The manifest's (last) SpawnedCharacter or an earlier one.
                "carrier_identity_provenance": bodies.provenance.get(carrier),
                "via_proxy_class": proxy or None,
                "in_hand": any(t <= h and (end is None or h <= end)
                               for h in held),
            })

    rows.sort(key=lambda r: (r["from_ms"], r["bomb_net_guid"]))
    return rows, events, malformed_round_meta, bodies.counts


SCHEMA = pa.schema([
    pa.field("round_number", pa.int32()),
    pa.field("bomb_net_guid", pa.int64()),
    pa.field("from_ms", pa.int64()),
    pa.field("to_ms", pa.int64()),
    pa.field("duration_ms", pa.int64()),
    pa.field("owner_net_guid", pa.int64()),
    pa.field("owner_class", pa.string()),
    pa.field("holder_kind", pa.string()),
    pa.field("carrier_pawn_guid", pa.int64()),
    pa.field("carrier_subject", pa.string()),
    pa.field("carrier_identity_provenance", pa.string()),
    pa.field("via_proxy_class", pa.string()),
    pa.field("in_hand", pa.bool_()),
])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", type=Path, required=True,
                    help="directory written by `vrfkit export`")
    ap.add_argument("--out", type=Path, required=True,
                    help="output spike_carrier.parquet path")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="also print the timeline, carried intervals only")
    args = ap.parse_args()

    rows, events, malformed_round_meta, identity = build(args.export)
    cols = {name: [r[name] for r in rows] for name in SCHEMA.names}
    table = pa.Table.from_pydict(cols, schema=SCHEMA)
    atomic_write_file(args.out, lambda out: pq.write_table(table, out, compression="zstd"))

    by_kind = Counter(r["holder_kind"] for r in rows)
    print(f"wrote {args.out} ({len(rows)} custody intervals)")
    for k, n in sorted(by_kind.items()):
        print(f"  {k:8s} {n}")
    print(f"  malformed roundStarted metadata: {malformed_round_meta} "
          f"(round_number is null for the interval(s) it touches)")
    earlier = sum(r["carrier_identity_provenance"] == EARLIER_PROVENANCE for r in rows)
    print(f"  carried by an earlier SpawnedCharacter pawn: {earlier} interval(s); "
          f"player identity: {json.dumps(identity, sort_keys=True)}")

    held = [r for r in rows if r["holder_kind"] in HELD_KINDS]
    print(f"  rounds {len({r['round_number'] for r in rows})}, "
          f"with a carrier {len({r['round_number'] for r in held})}, "
          f"bombs {len({r['bomb_net_guid'] for r in rows})}")

    # The carrier at plant time is the join's answer: `spikePlanted` names no planter.
    for grp, t1 in zip(events.get("group", []), events.get("time1", [])):
        if grp != "spikePlanted":
            continue
        who = carrier_at(held, t1)
        tag = "NO CARRIER" if who is None else (
            f"{(who['carrier_subject'] or '?')[:8]} pawn={who['carrier_pawn_guid']}"
            + (f" via {who['via_proxy_class']}" if who["via_proxy_class"] else ""))
        round_disp = who["round_number"] if who and who["round_number"] is not None else "?"
        print(f"  plant t={t1:>8}  round {round_disp}  {tag}")

    if args.show:
        print()
        for r in held:
            print("  r%-3s %8d-%-8s %-6s %-8s pawn=%-5s %s%s" % (
                r["round_number"], r["from_ms"], r["to_ms"], r["holder_kind"],
                (r["carrier_subject"] or "?")[:8], r["carrier_pawn_guid"],
                "in-hand " if r["in_hand"] else "",
                "" if r["duration_ms"] is None
                else f"({r['duration_ms'] / 1000:.1f}s)"))

    problems = unresolved(rows, events)
    if problems:
        print(f"\nFAILED: {len(problems)} thing(s) this export could not "
              f"resolve", file=sys.stderr)
        for line in problems:
            print(f"    {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
