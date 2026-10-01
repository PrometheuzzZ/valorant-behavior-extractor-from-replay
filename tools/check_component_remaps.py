#!/usr/bin/env python3
"""Check that the Blueprint-component remaps still match a build's replays.

`KNOWN_SUBOBJECT_CLASS_PATHS` in `crates/vrfkit/src/sink/paths.rs` maps bare
component instance names to the class groups a replay declares -- usually a
native `/Script/...` class, for four pairs a Blueprint `/Game/..._C` class.
A later build can break a pair without any test failing, and the export
baseline covers one replay; this works on any export, so run it on a replay
from a new build before trusting the output.

Each pair is judged on the kind of block it remaps. A RepLayout pair counts
RepLayout rows only (`CNC_MARKERS` rows are dropped: every RepLayout remap
leaves its component's RPC stream bare by design). A ClassNetCache pair
counts the leaf's ClassNetCache rows against the class's `_ClassNetCache`
group:

    RepLayout pair, no RepLayout row left bare           -> ok      (the remap is doing work)
    RepLayout pair, any RepLayout row left bare          -> broken  (it did not fire for them)
    ClassNetCache pair, no ClassNetCache row left bare   -> ok
    ClassNetCache pair, any ClassNetCache row left bare  -> broken
    neither present                                      -> absent  (not in this replay; says nothing)

Healthy is exactly zero bare rows, so nothing is tolerated. RepLayout rows
under a ClassNetCache-only leaf are not the pair's to route: they are counted
and printed on every run, never failed on.

Only `DamageHandlerComponent` -> `DamageableComponent` depends on the table;
the other three ClassNetCache pairs are also routed by instance name
(`resolve_cnc_for_instance_name`), so their verdict fires only when a build
renames the class out from under both routes.

A renamed component cannot produce `broken`: the replay stops declaring the
old leaf, so the pair reads `ok` (other leaves keep the target busy) or
`absent`. The rename appears under its NEW name as a bare group no pair
claims: `unmapped_bare_groups` lists those, worst first, reported rather than
failed on (a replay legitimately carries bare Blueprint components with no
remap). Read the list against the previous build's.

Exit 2 when `--export` holds no fields.parquet; exit 1 on a broken pair, a
table entry that did not parse, or an export in which not one pair appears
(a real match exercises many, so that is an empty or wrong export).

Usage:
    python tools/check_component_remaps.py --export out/probe
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path
from typing import NamedTuple

REPO = Path(__file__).resolve().parents[1]
PATHS_RS = REPO / "crates" / "vrfkit" / "src" / "sink" / "paths.rs"

#: One `(leaf, class path, GroupKind)` tuple of the remap table, on one line
#: or rustfmt-wrapped. Any absolute target path is read (`/Script/...` and
#: Blueprint `/Game/..._C`); `unparsed_entries` fails a pair it cannot read.
PAIR_RE = re.compile(
    r'\(\s*"([^"]+)",\s*"(/[^"]+)",\s*GroupKind::(\w+)\s*,?\s*\)', re.S
)

#: Field names that mark a ClassNetCache block rather than a RepLayout property.
CNC_MARKERS = ("_cnc_h", "__vrfkit_unresolved_class_net_cache_payload__")

#: A ClassNetCache pair routes its leaf's blocks to the target path plus this.
CNC_GROUP_SUFFIX = "_ClassNetCache"


class Verdict(NamedTuple):
    leaf: str
    native: str
    state: str
    detail: str


def table_source() -> str:
    """The body of the Rust remap table, as text."""
    src = PATHS_RS.read_text(encoding="utf-8")
    start = src.index("const KNOWN_SUBOBJECT_CLASS_PATHS")
    end = src.index("\n];", start)
    return src[start:end]


def remap_entries(table: str | None = None) -> list[tuple[str, str, str]]:
    """`(leaf, class path, GroupKind)` for every entry of the Rust table."""
    return PAIR_RE.findall(table_source() if table is None else table)


def unparsed_entries(table: str, entries) -> int:
    """Entries `PAIR_RE` did not read (each carries one `GroupKind::`): pairs
    this check would never look at."""
    return table.count("GroupKind::") - len(entries)


def class_net_cache_verdict(leaf, native, rows_by_group, cnc_bare) -> Verdict:
    """The leaf's ClassNetCache rows against `<native>_ClassNetCache`, strictly.

    Bare is tested first and alone decides `broken`: the class group also
    takes blocks that reach it without the table (78 rows beside 5,247 bare
    on a 13.01 export with the remap broken). The leaf's RepLayout rows, which
    the pair does not route, go in the detail, never the state.
    """
    routed = rows_by_group.get(native + CNC_GROUP_SUFFIX, 0)
    bare = cnc_bare.get(leaf, 0)
    rep_layout = rows_by_group.get(leaf, 0)
    note = (f"; {rep_layout} RepLayout rows under the leaf, which a ClassNetCache "
            f"pair does not remap" if rep_layout else "")
    if bare:
        return Verdict(
            leaf, native, "broken",
            f"{bare} ClassNetCache rows still bare ({routed} on the class's "
            f"{CNC_GROUP_SUFFIX} group); a working ClassNetCache remap leaves none"
            + note)
    if routed:
        return Verdict(leaf, native, "ok",
                       f"{routed} rows on the {CNC_GROUP_SUFFIX} group, 0 still bare"
                       + note)
    return Verdict(leaf, native, "absent",
                   "no ClassNetCache rows in this replay" + note)


def verdicts(entries, rows_by_group, cnc_bare=None) -> list[Verdict]:
    """Classify each `(leaf, target, kind)` entry against `{group_path: rows}`.

    A kind with no rule raises ValueError, so a `GroupKind` added in paths.rs
    cannot pass its pairs unjudged.
    """
    cnc_bare = cnc_bare or {}
    out = []
    for leaf, native, kind in entries:
        if kind == "ClassNetCache":
            out.append(class_net_cache_verdict(leaf, native, rows_by_group, cnc_bare))
            continue
        if kind != "RepLayout":
            raise ValueError(f"{leaf} has GroupKind {kind!r}, which no verdict rule covers")
        native_rows = rows_by_group.get(native, 0)
        bare_rows = rows_by_group.get(leaf, 0)
        if not native_rows and not bare_rows:
            out.append(Verdict(leaf, native, "absent", "not in this replay"))
        elif bare_rows:
            out.append(Verdict(
                leaf, native, "broken",
                f"{bare_rows} RepLayout rows still bare ({native_rows} on the "
                f"class group); a working RepLayout remap leaves none"))
        else:
            out.append(Verdict(leaf, native, "ok", f"{native_rows} rows, 0 still bare"))
    return out


def unmapped_bare_groups(rows_by_group, entries, min_rows: int = 1) -> list:
    """`(group, rows)` for bare groups no pair claims, worst first: where a
    renamed component appears. Native `/Script/...` paths are the targets,
    so they are excluded; so are groups at zero, which keeps the RepLayout-only
    remaps out (`row_counts` already dropped their ClassNetCache rows).
    """
    leaves = {entry[0] for entry in entries}
    return sorted(
        ((group, rows) for group, rows in rows_by_group.items()
         if not group.startswith("/") and group not in leaves and rows >= min_rows),
        key=lambda kv: (-kv[1], kv[0]),
    )


def row_counts(export_dir: Path) -> tuple[dict, dict]:
    """`({group_path: rows}, {bare group: ClassNetCache rows})`.

    A native (`/`-rooted) group counts every row; a bare group puts its
    RepLayout rows in the first map and its `CNC_MARKERS` rows in the second,
    so each row counts once. Grouped in Arrow: 26x faster than a Python loop
    over the 1.3M rows of a match export.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    table = pq.read_table(export_dir / "fields.parquet",
                          columns=["group_path", "field_name"])
    names = pc.fill_null(table.column("field_name").cast(pa.string()), "")
    is_cnc = pc.match_substring(names, CNC_MARKERS[0])
    for marker in CNC_MARKERS[1:]:
        is_cnc = pc.or_(is_cnc, pc.match_substring(names, marker))
    grouped = pa.table({
        "group": table.column("group_path").cast(pa.string()), "cnc": is_cnc,
    }).group_by("group").aggregate([([], "count_all"), ("cnc", "sum")])
    out, cnc_bare = {}, {}
    for group, rows, cnc_rows in zip(grouped["group"].to_pylist(),
                                     grouped["count_all"].to_pylist(),
                                     grouped["cnc_sum"].to_pylist()):
        if group.startswith("/"):
            out[group] = rows
        else:
            out[group] = rows - cnc_rows
            cnc_bare[group] = cnc_rows
    return out, cnc_bare


def class_net_cache_leaf_rep_layout_rows(rows_by_group, entries) -> int:
    """RepLayout rows under the leaves of ClassNetCache pairs: not a failure
    (those pairs do not remap RepLayout blocks), but printed on every run,
    zero included, so the rows stay visible."""
    return sum(rows_by_group.get(leaf, 0)
               for leaf, _, kind in entries if kind == "ClassNetCache")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export", type=Path, required=True,
                    help="directory written by `vrfkit export`")
    ap.add_argument("--verbose", action="store_true",
                    help="list every pair, not just the problems")
    args = ap.parse_args(argv)

    fields = args.export / "fields.parquet"
    if not fields.is_file():
        print(f"FAILED: no fields.parquet in {args.export}", file=sys.stderr)
        return 2

    table = table_source()
    entries = remap_entries(table)
    if not entries:
        print(f"FAILED: parsed no pairs out of {PATHS_RS.name}", file=sys.stderr)
        return 1
    missing = unparsed_entries(table, entries)
    if missing:
        print(f"FAILED: {missing} entr{'y' if missing == 1 else 'ies'} of the "
              f"remap table in {PATHS_RS.name} did not parse, so nothing here "
              f"would check them", file=sys.stderr)
        return 1

    rows, cnc_bare = row_counts(args.export)
    try:
        results = verdicts(entries, rows, cnc_bare)
    except ValueError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    tally = collections.Counter(v.state for v in results)

    for v in results:
        if v.state == "broken" or args.verbose:
            print(f"  {v.state:<7} {v.leaf} -> {v.native.split('.')[-1]}: {v.detail}")

    print(f"{len(entries)} remaps: {tally['ok']} ok, {tally['absent']} absent, "
          f"{tally['broken']} broken")
    # Unconditional, zero included: see class_net_cache_leaf_rep_layout_rows.
    print(f"{class_net_cache_leaf_rep_layout_rows(rows, entries)} RepLayout row(s) "
          f"under the leaves of the ClassNetCache pairs, which do not remap "
          f"RepLayout blocks (reported, not a failure)")

    # Where a rename shows up (no verdict can see one), printed pass or fail.
    suspects = unmapped_bare_groups(rows, entries)
    print(f"\n{len(suspects)} bare group(s) no remap claims"
          + (" (a renamed component appears here, not above):" if suspects else ""))
    for group, n in suspects[:10]:
        print(f"  {n:>8,}  {group}")
    if len(suspects) > 10:
        print(f"  ... and {len(suspects) - 10} more")

    if tally["broken"]:
        print("\nFAILED: a remap stopped matching -- its rows are still "
              "arriving under the bare leaf instead of reaching the native "
              "group. Check that the pair still spells the leaf the way this "
              "replay declares it; if the component was RENAMED the leaf would "
              "be gone from the export entirely and would show up in the "
              "unclaimed list above, not here. Re-derive the pair from the "
              "cooked asset (docs/DATA.md) rather than guessing a new name.",
              file=sys.stderr)
        return 1
    if not tally["ok"]:
        print(f"\nFAILED: nothing checked -- not one of the {len(entries)} remap "
              f"pairs appears in this export. A real match exercises many of "
              f"them, so this is an empty or wrong export rather than a clean "
              f"result.", file=sys.stderr)
        return 1
    print(f"\nOK: {tally['ok']} remap(s) are doing work; {tally['absent']} do "
          f"not appear in this replay, which says nothing about them. A "
          f"renamed component is not covered by this verdict -- read the "
          f"unclaimed bare groups above against the previous build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
