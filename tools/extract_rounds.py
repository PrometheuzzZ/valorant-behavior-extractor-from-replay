#!/usr/bin/env python3
"""Derive rounds.parquet from an export: one row per played round.

Rows come from the game state's `MulticastSetPhase.NewPhase` RPC: 2 reset,
3 round start, 4 buy phase end, 5 an unnamed post-round phase (no RPC names
it), 6 side switch (on the round before the switch). Each phase but 5 is
checked against the named RPC sent in the same frame; any difference fails
the run. `buy_end_ms` is the epoch of `AbilityCastsThisRound[].CastTime`. A
phase the replay lacks stays null.

`RoundResults` is joined by `RoundNumber` but never makes a row: a surrender
pads it with awarded rounds nobody played (12 on each public fixture).
"""

from __future__ import annotations

import argparse
import bisect
import sys
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from atomic_io import atomic_write_file, refuse_input_path
from wire_bits import iter_selected, text

SET_PHASE = "MulticastSetPhase.NewPhase"
PHASE_COLUMNS = {2: "reset_ms", 3: "start_ms", 4: "buy_end_ms", 5: "post_round_ms",
                 6: "side_switch_ms"}
#: Sent in the same frame as their phase: 0 ms apart on all 25 A/B exports.
PHASE_RPCS = {2: "ClientResetRound", 3: "ClientRoundStart", 4: "ClientBuyPhaseEnd",
              6: "Multicast Side Switch Event"}
SPIKE_COLUMNS = {"spikePlanted": "plant_ms", "spikeDefused": "defuse_ms",
                 "spikeExploded": "explode_ms"}
RESULT_COLUMNS = {"WinningTeam": "winning_team", "WinningTeamRole": "winning_role",
                  "RoundResult": "result"}
#: The result of a round a surrender awarded rather than played.
AWARDED = "surrendered"
OTHER_TEAM = {"Red": "Blue", "Blue": "Red"}
PLANTED_AT_SITE = "PlantedAtSite"
_NAMES = pa.array([SET_PHASE, PLANTED_AT_SITE, *PHASE_RPCS.values()])

SCHEMA = pa.schema([
    ("round_ordinal", pa.int32()), ("round_number", pa.int32()),
    *((column, pa.int64()) for column in PHASE_COLUMNS.values()),
    *((column, pa.string()) for column in (*RESULT_COLUMNS.values(), "attacker_team")),
    # plant_site is TimedBomb's EnumByte, null when not replicated (4 of 9 plants on 13.01).
    ("plant_ms", pa.int64()), ("plant_site", pa.int32()),
    ("defuse_ms", pa.int64()), ("explode_ms", pa.int64()),
])

#: Values filed into a round by time: roundStarted word0, spike events, the site.
PLACED = ("round_number", "plant_site", *SPIKE_COLUMNS.values())
COUNT_KEYS = (*(f"phase {p}" for p in PHASE_COLUMNS), "phase other",
              "phase repeated in a round (first kept)", *PHASE_RPCS.values(),
              "named RPC repeated in its frame", "unstarted final round (dropped)",
              "RoundResults entries", "RoundResults joined to a played round",
              "RoundResults awarded, not played",
              *(f"{what} before the first round" for what in PLACED),
              *(f"{what} repeated in a round (left null)" for what in PLACED),
              "max |roundStarted - start_ms| ms")


def _mask(batch):
    names = text(batch, "field_name")
    return pc.or_(pc.is_in(names, value_set=_NAMES), pc.starts_with(names, "RoundResults["))


def build(export: Path) -> tuple[list[dict], Counter, list[str]]:
    """`(rows, counts, problems)`; any problem means the rows are not trusted."""
    counts, problems = Counter(dict.fromkeys(COUNT_KEYS, 0)), []
    rounds, sites, results = [], [], {}
    rpc_times = {rpc: [] for rpc in PHASE_RPCS.values()}
    columns = ["time_ms", "field_name", "value_i64", "value_str"]
    for _, row in iter_selected(export / "fields.parquet", columns, _mask):
        name, t, phase = row["field_name"], row["time_ms"], row["value_i64"]
        if name == SET_PHASE:
            column = PHASE_COLUMNS.get(phase)
            counts[f"phase {phase}" if column else "phase other"] += 1
            if column is None:
                continue
            if phase == 2 or not rounds:
                rounds.append(dict.fromkeys(SCHEMA.names))
            if rounds[-1][column] is None:
                rounds[-1][column] = t
            else:  # a second phase 3 inside one round on 11.08 and 12.03
                counts["phase repeated in a round (first kept)"] += 1
        elif name == PLANTED_AT_SITE:
            sites.append((t, row["value_i64"]))
        elif name in rpc_times:
            rpc_times[name].append(t)
        else:
            index, _, member = name[len("RoundResults["):].partition("].")
            if member:  # the last write of each member wins
                value = row["value_i64" if member == "RoundNumber" else "value_str"]
                results.setdefault(int(index), {})[member] = value

    for phase, rpc in PHASE_RPCS.items():
        # An overtime side switch sends ClientResetRound twice in one frame.
        sent = [t for i, t in enumerate(rpc_times[rpc]) if i == 0 or rpc_times[rpc][i - 1] != t]
        counts[rpc] = len(sent)
        counts["named RPC repeated in its frame"] += len(rpc_times[rpc]) - len(sent)
        kept = [r[PHASE_COLUMNS[phase]] for r in rounds if r[PHASE_COLUMNS[phase]] is not None]
        if kept != sent:
            problems.append(f"phase {phase} at {kept[:3]}... ({len(kept)}) disagrees with "
                            f"{rpc} at {sent[:3]}... ({len(sent)})")

    opens = [min(r[c] for c in PHASE_COLUMNS.values() if r[c] is not None) for r in rounds]
    placed: dict[tuple[int, str], list] = {}
    events = pq.read_table(export / "events.parquet", columns=["group", "time1", "word0"]).to_pylist()
    for what, t, value in [
            *(("round_number", e["time1"], e["word0"]) for e in events if e["group"] == "roundStarted"),
            *((SPIKE_COLUMNS[e["group"]], e["time1"], e["time1"]) for e in events
              if e["group"] in SPIKE_COLUMNS),
            *(("plant_site", t, site) for t, site in sites)]:
        index = bisect.bisect_right(opens, t) - 1
        if index < 0:
            counts[f"{what} before the first round"] += 1
        else:
            placed.setdefault((index, what), []).append((t, value))
    for (index, what), values in placed.items():
        if len(values) > 1:  # never pick one
            counts[f"{what} repeated in a round (left null)"] += 1
            continue
        t, rounds[index][what] = values[0]
        start = rounds[index]["start_ms"]
        if what == "round_number" and start is not None:
            key = "max |roundStarted - start_ms| ms"
            counts[key] = max(counts[key], abs(t - start))

    counts["RoundResults entries"] = len(results)
    by_number = {entry.get("RoundNumber", ("index", i)): entry for i, entry in results.items()}
    if len(by_number) != len(results):
        problems.append("RoundResults repeats a RoundNumber")
    if rounds and rounds[-1]["start_ms"] is None:  # the last side switch resets into no round
        rounds.pop()
        counts["unstarted final round (dropped)"] += 1
    for ordinal, r in enumerate(rounds):
        r["round_ordinal"] = ordinal
        entry = by_number.pop(r["round_number"], None) if r["round_number"] is not None else None
        if entry is None:
            continue
        counts["RoundResults joined to a played round"] += 1
        for member, column in RESULT_COLUMNS.items():
            r[column] = entry.get(member)
        team, role = entry.get("WinningTeam"), entry.get("WinningTeamRole")
        r["attacker_team"] = {"attacker": team, "defender": OTHER_TEAM.get(team)}.get(role)
    for number, entry in by_number.items():
        if entry.get("RoundResult") == AWARDED:
            counts["RoundResults awarded, not played"] += 1
        else:
            problems.append(f"RoundResults round {number} ({entry.get('RoundResult')}) "
                            "has no played round")
    return rounds, counts, problems


def parquet_cli(description, build, schema, noun, argv=None) -> int:
    """The --export/--out command of a Parquet view: print `build`'s counts and
    each column's non-null count, then write its rows atomically -- unless it
    reports a problem or --out names an export table: FAILED, exit 1, no file."""
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--export", type=Path, required=True, help="directory written by `vrfkit export`")
    ap.add_argument("--out", type=Path, required=True, help="output .parquet path")
    args = ap.parse_args(argv)
    try:
        refuse_input_path(args.out, [*args.export.glob("*.parquet"), args.export / "manifest.json"])
        rows, counts, problems = build(args.export)
    except (OSError, ValueError, KeyError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"{len(rows)} {noun}")
    for key, value in counts.items():
        print(f"  {key}: {value}")
    print("  non-null: " + ", ".join(
        f"{name} {sum(r[name] is not None for r in rows)}" for name in schema.names))
    for problem in problems:
        print(f"FAILED: {problem}", file=sys.stderr)
    if problems:
        return 1
    table = pa.Table.from_pylist(rows, schema=schema)
    atomic_write_file(args.out, lambda out: pq.write_table(table, out, compression="zstd"))
    print(f"wrote {args.out}")
    return 0


def main(argv=None) -> int:
    return parquet_cli(__doc__, build, SCHEMA, "played round(s)", argv)


if __name__ == "__main__":
    raise SystemExit(main())
