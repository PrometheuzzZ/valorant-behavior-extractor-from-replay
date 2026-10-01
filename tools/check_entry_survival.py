#!/usr/bin/env python3
"""Report, per game build, whether each name-keyed overlay entry is still declared.

Almost every typed value vrfkit emits is keyed on something the replay
declares. A patch that moves, renames or stops replicating it breaks nothing
loudly: the key stops matching, the rows arrive untyped and `Decode errors: 0`
holds (Cypher's tripwire moved from `.../Gumshoe/S0/Ability_E/` to
`.../Ability_4/` between 13.00 and 13.01). Only declarations are read: the
manifest's `net_field_export_groups` and, when present, the checkpoint
`checkpoint_export_groups` / `checkpoint_export_fields` tables joined on
(`checkpoint_index`, `group_ordinal`). Nothing is decoded; no game install is
read.

Entries, parsed from the Rust sources on every run (a count that disagrees
with the array length its source declares fails):

  table     `OVERLAY_TABLE` (table.rs): (group, field name) -> type
  handle    `OVERLAY_HANDLE_TABLE` (table.rs): (group, handle) -> field name
  scoped    `SCOPED_TYPES` (scoped_types.rs): (name, group, checksum) -> type
  checksum  `CHECKSUM_TYPES` (checksum_table.rs): checksum -> type
  route     the measured array routes: the arms of `measured_array_route`
            (sink/blobs.rs) and the `NetworkedProjectilePath` gate
            (sink/rpc.rs), checked against `MeasuredArrayRoute::ALL`
  remap     the class groups `KNOWN_SUBOBJECT_CLASS_PATHS` (sink/paths.rs)
            routes components to, read through `check_component_remaps`
  alias     the source groups of `GROUP_ALIASES` (overlay.rs)

Not covered: the handle-keyed member tables of the array routes, the
`MulticastRespondToValidMapClick` gate and the life-change schemas in
sink/rpc.rs, the `struct_blob_kind` names, and the effect-blob parameter names
(vrf-decode/src/effect.rs).

`table` entries match in `resolve_entry`'s order (overlay_mirror), so `Role` /
`RemoteRole`, declared as `215` / `216`, count only through a handle entry; a
real name at a mapped handle the table does not know is a conflict, as in
overlay.rs. Per (entry, build) an entry is declared, field-missing (its group
is; for a checksum, a group carrying it anywhere in the input) or not observed.

Each build is compared with its REFERENCE WINDOW: the builds before it, newest
first, until they hold `REFERENCE_REPLAYS` replays (13.00 alone is one replay
without the old tripwire groups). An entry declared there and not in the build
is field-missing (its group still is), moved (the group is gone and a
SUCCESSOR appears: a new group of the same kind whose class name is the same,
differs in one `_` token of at most `SHORT_TOKEN` characters, or declares one
of the old group's (name, checksum) pairs that at most `RARE_PAIR_GROUPS`
reference groups declare), or vanished. A move is `covered` when the
successor's field resolves to the entry's own type, `lost` otherwise. `drift`
(a table or handle entry declared under a checksum its single-checksum
reference never carried) and `conflicts` are reported, never failed.

An absence is EVIDENCE when p <= `ALPHA` -- the chance that the build's `m`
replays all miss the `k` of `c` reference replays declaring it, C(c+m-k, m) /
C(c+m, m) -- and the build has `MIN_CONTEXT` such replays; otherwise it is
weak and never fails. On the 1,018-replay corpus (24 builds) the largest
evidenced p was 1.9e-4 and the smallest weak one 0.0165.

Exit 1 on an evidenced field-missing entry or lost move not listed with its
reason in `tools/fixtures/entry_survival_expected.json`, on a listed item that
matches nothing (STALE), a checkpoint field joining no one group, a main-stream
field without a name or checksum, and a Rust table that does not parse. Exit 2
when there is nothing to judge: no export, no declarations, a build with no
version, or fewer than two builds. A new build is judged against the builds
before it, so export it into the same --root as they are.

Usage:
    python tools/check_entry_survival.py --root <exports-root> [--root ...]
    python tools/check_entry_survival.py --root <root> --show 'Gumshoe'
    python tools/check_entry_survival.py --root <root> --json report.json
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import overlay_mirror as mirror
from check_component_remaps import remap_entries, table_source, unparsed_entries
from export_scan import child_exports

REPO = Path(__file__).resolve().parents[1]
TABLE_RS = REPO / "crates" / "vrf-decode" / "src" / "table.rs"
SCOPED_RS = REPO / "crates" / "vrf-decode" / "src" / "scoped_types.rs"
CHECKSUM_RS = REPO / "crates" / "vrf-decode" / "src" / "checksum_table.rs"
OVERLAY_RS = REPO / "crates" / "vrf-decode" / "src" / "overlay.rs"
BLOBS_RS = REPO / "crates" / "vrfkit" / "src" / "sink" / "blobs.rs"
RPC_RS = REPO / "crates" / "vrfkit" / "src" / "sink" / "rpc.rs"
ROUTES_RS = REPO / "crates" / "vrfkit" / "src" / "sink" / "measured_routes.rs"
EXPECTED_JSON = REPO / "tools" / "fixtures" / "entry_survival_expected.json"

#: Largest sampling probability that is still evidence (the module docstring
#: gives the corpus numbers either side).
ALPHA = 1e-3
#: Fewest replays (declaring the group, or at all for a move) a build needs
#: before an absence in it can fail. Every legacy build holds at most three.
MIN_CONTEXT = 10
#: Replays the reference window gathers before it stops growing backwards.
REFERENCE_REPLAYS = 30
#: Longest token that may differ between an old and a successor class name.
SHORT_TOKEN = 2
#: Most groups a (name, checksum) pair may be declared by, across the
#: reference, and still nominate a successor.
RARE_PAIR_GROUPS = 3

KINDS = ("table", "handle", "scoped", "checksum", "route", "remap", "alias")
#: Kinds keyed on a group alone: they can be declared or not observed, never
#: field-missing.
GROUP_ONLY = ("remap", "alias")

BUILD_RE = re.compile(r"release-(\d+)\.(\d+)\Z")
STR, unescape, is_fname_index = mirror.STR, mirror.unescape, mirror.is_fname_index
#: A Rust table did not parse completely: fail rather than check fewer.
EntryParseError = mirror.ParseError


class InputError(ValueError):
    """An export that cannot be judged, or no export at all."""


def normalize_type(text: str) -> str:
    """One spelling for a `FieldType` expression, whatever rustfmt did to it."""
    return re.sub(r"\s+", "", text).replace(",}", "}")


OBJECT_NET_GUID = normalize_type("FieldType::ObjectNetGuid")


@dataclass(frozen=True)
class Entry:
    kind: str
    group: str | None = None
    name: str | None = None
    checksum: int | None = None
    handle: int | None = None
    ftype: str | None = None

    @property
    def key(self) -> str:
        if self.kind == "table":
            return f"table|{self.group}|{self.name}"
        if self.kind == "handle":
            return f"handle|{self.group}|{self.handle}"
        if self.kind in ("scoped", "route"):
            return f"{self.kind}|{self.group}|{self.name}|{self.checksum}"
        if self.kind == "checksum":
            return f"checksum|{self.checksum}"
        return f"{self.kind}|{self.group}"

    @property
    def label(self) -> str:
        """The entry as a report line names it."""
        if self.kind == "checksum":
            return f"checksum {self.checksum}"
        if self.kind in GROUP_ONLY:
            return f"{self.kind} {self.group}"
        tail = f"@{self.handle}" if self.kind == "handle" else ""
        extra = f" ({self.checksum})" if self.kind in ("scoped", "route") else ""
        return f"{self.kind} {self.group} . {self.name}{tail}{extra}"


def parse_overlay_table(src: str) -> list[Entry]:
    return [Entry("table", g, n, ftype=normalize_type(t))
            for g, n, t in mirror.overlay_entries(src)]


parse_group_aliases = mirror.group_aliases


def parse_routes(blobs: str, rpc: str, routes: str) -> list[Entry]:
    """The (group, parent, checksum) of every measured array route.

    `MeasuredArrayRoute::ALL` is the list both halves must add up to: seven
    arms of `measured_array_route` in blobs.rs, and the RPC-parameter route
    that rpc.rs gates on its own. A route added anywhere else fails here.
    """
    m = re.search(r"const ALL: \[Self; (\d+)\] = \[([^\]]*)\];", routes)
    if m is None:
        raise EntryParseError("MeasuredArrayRoute::ALL: not found")
    variants = re.findall(r"Self::(\w+)", m.group(2))
    mirror.check_count("MeasuredArrayRoute::ALL", int(m.group(1)), len(variants),
                       len(set(variants)))

    start = blobs.find("fn measured_array_route(")
    end = blobs.find("\n}\n", start)
    if start < 0 or end < 0:
        raise EntryParseError("measured_array_route: not found")
    body = blobs[start:end]
    arms = re.findall(
        r"\(\s*" + STR + r",\s*" + STR + r",\s*Some\(([\d_]+)\),?\s*\)\s*=>\s*\{?\s*"
        r"MeasuredArrayRoute::(\w+)", body, re.S)
    literal = body.count("MeasuredArrayRoute::")
    mirror.check_count("measured_array_route", literal, literal, len(arms))
    found = {v: Entry("route", unescape(g), unescape(n), int(c.replace("_", "")))
             for g, n, c, v in arms}

    for gate in re.finditer(r"self\.admits\(MeasuredArrayRoute::(\w+)\)", rpc):
        statement = rpc[gate.start():rpc.find(";", gate.start())]
        group = re.findall(r"param_group_path_ref\s*==\s*Some\(\s*" + STR, statement, re.S)
        name = re.findall(r"param_name\s*==\s*Some\(\s*" + STR, statement, re.S)
        checksum = re.findall(r"param_checksum\s*==\s*Some\(([\d_]+)\)", statement)
        if not (len(group) == len(name) == len(checksum) == 1):
            raise EntryParseError(
                f"rpc.rs gate for {gate.group(1)}: expected one group, name and checksum")
        if gate.group(1) in found:
            raise EntryParseError(f"route {gate.group(1)} is gated twice")
        found[gate.group(1)] = Entry("route", unescape(group[0]), unescape(name[0]),
                                     int(checksum[0].replace("_", "")))
    if sorted(found) != sorted(variants):
        raise EntryParseError(
            f"routes parsed {sorted(found)} but MeasuredArrayRoute::ALL lists "
            f"{sorted(variants)}")
    return [found[v] for v in variants]


def parse_remap_targets(src: str) -> list[Entry]:
    pairs = remap_entries(src)
    missing = unparsed_entries(src, pairs)
    if missing or not pairs:
        raise EntryParseError(
            f"KNOWN_SUBOBJECT_CLASS_PATHS: {missing} entr(y/ies) did not parse "
            f"({len(pairs)} did)")
    targets = sorted({path + ("_ClassNetCache" if kind == "ClassNetCache" else "")
                      for _, path, kind in pairs})
    return [Entry("remap", t) for t in targets]


@dataclass
class Sources:
    table: str
    scoped: str
    checksum: str
    overlay: str
    blobs: str
    rpc: str
    routes: str
    paths: str

    @classmethod
    def from_repo(cls) -> "Sources":
        read = lambda p: p.read_text(encoding="utf-8")  # noqa: E731
        return cls(read(TABLE_RS), read(SCOPED_RS), read(CHECKSUM_RS), read(OVERLAY_RS),
                   read(BLOBS_RS), read(RPC_RS), read(ROUTES_RS), table_source())


@dataclass
class Catalog:
    """Every entry, plus what the resolution order needs besides them."""
    entries: list[Entry]
    aliases: list[tuple[str, str]]
    engine_refs: list[str]

    def by_kind(self) -> dict[str, list[Entry]]:
        out = {k: [] for k in KINDS}
        for e in self.entries:
            out[e.kind].append(e)
        return out


def load_catalog(sources: Sources) -> Catalog:
    table = parse_overlay_table(sources.table)
    types = {(e.group, e.name): e.ftype for e in table}
    aliases = parse_group_aliases(sources.overlay)
    entries = (table
               + [Entry("handle", g, n, handle=h, ftype=types.get((g, n)))
                  for g, h, n in mirror.handle_entries(sources.table)]
               + [Entry("scoped", g, n, c, ftype=normalize_type(t))
                  for n, g, c, t in mirror.scoped_entries(sources.scoped)]
               + [Entry("checksum", checksum=c, ftype=normalize_type(t))
                  for c, t in mirror.checksum_entries(sources.checksum)]
               + parse_routes(sources.blobs, sources.rpc, sources.routes)
               + parse_remap_targets(sources.paths)
               + [Entry("alias", src) for src, _ in aliases])
    seen = collections.Counter(e.key for e in entries)
    dupes = [k for k, n in seen.items() if n > 1]
    if dupes:
        raise EntryParseError(f"{len(dupes)} duplicate entry key(s), e.g. {dupes[0]}")
    return Catalog(entries, aliases, mirror.engine_object_refs(sources.overlay))


@dataclass(frozen=True)
class Resolution:
    step: str               # table | alias | scoped | engine | checksum
    entry: Entry | None     # the table / scoped / checksum entry that typed it
    ftype: str


#: overlay_mirror's resolution steps, as the kinds this report counts.
STEP_KINDS = {"name": "table", "b-prefix": "table", "handle": "table", "scoped": "scoped",
              "checksum table": "checksum"}


class Overlay:
    """`resolve_entry` in overlay.rs (overlay_mirror.Resolver), over the parsed entries."""

    def __init__(self, catalog: Catalog):
        kinds = catalog.by_kind()
        self.table = {(e.group, e.name): e for e in kinds["table"]}
        self.handles = {(e.group, e.handle): e for e in kinds["handle"]}
        self.aliases = dict(catalog.aliases)
        self.scoped = {(e.name, e.group, e.checksum): e for e in kinds["scoped"]}
        self.checksums = {e.checksum: e for e in kinds["checksum"]}
        self.routes = {(e.group, e.name, e.checksum): e for e in kinds["route"]}
        self.remaps = {e.group: e for e in kinds["remap"]}
        self.alias_entries = {e.group: e for e in kinds["alias"]}
        self.engine_refs = frozenset(catalog.engine_refs)
        self.mirror = mirror.Resolver(
            self.table, {key: e.name for key, e in self.handles.items()}, self.scoped,
            self.checksums, self.aliases, self.engine_refs)

    def resolve(self, group, name, checksum, handle) -> Resolution | None:
        hit, step = self.mirror.resolve(group, name, handle, checksum)
        if step is None:
            return None
        if step == "engine reference":
            return Resolution("engine", None, OBJECT_NET_GUID)
        return Resolution("alias" if step.startswith("alias ") else STEP_KINDS[step], hit,
                          hit.ftype)

    def handle_state(self, group, name, handle) -> tuple[Entry | None, str]:
        """`(handle entry, state)` for a declaration at an explicit handle.

        `hit`: the replay names that handle with a bare decimal (the case the
        fallback exists for), with the descriptor's own name, or with a
        spelling the direct lookups resolve to that same property
        (`DeathMontageEffectOverrideIsQueued` for the descriptor's
        `bDeathMontageEffectOverrideIsQueued`) -- the entry is declared.
        `conflict`: a different real name the direct lookups miss, which
        `resolve_in_group` refuses. `other`: a different real name the table
        knows as another property -- the handle holds something else in this
        build, which is not a declaration of this entry either.
        """
        for g in (group, self.aliases.get(group)):
            if g is None:
                continue
            via = self.handles.get((g, handle))
            if via is None:
                continue
            if name is None or is_fname_index(name) or name == via.name:
                return via, "hit"
            direct = self.mirror.in_group(g, name, None)[0]
            if direct is None:
                return via, "conflict"
            return via, "hit" if direct.name == via.name else "other"
        return None, ""


@dataclass
class LoadStats:
    exports: int = 0
    main_groups: int = 0
    main_fields: int = 0
    fields_without_identity: int = 0
    exports_with_checkpoints: int = 0
    checkpoint_groups: int = 0
    checkpoint_fields: int = 0
    orphan_checkpoint_fields: int = 0
    path_index_mismatches: int = 0
    repeated_checkpoint_groups: int = 0
    skipped: list = field(default_factory=list)


@dataclass
class Replay:
    source: str
    build: str
    groups: frozenset
    fields: frozenset   # (group, name, checksum, handle)


def discover(roots: list[Path], exports: list[Path], stats: LoadStats) -> list[Path]:
    """Children of each root holding `manifest.json`, then the named exports.
    Staging and backup siblings are skipped and listed (export_scan.py); a
    directory named explicitly is always read."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            raise InputError(f"--root {root} is not a directory")
        children, skipped = child_exports(root, "manifest.json")
        found += children
        stats.skipped += skipped
    for export in exports:
        if not (export / "manifest.json").is_file():
            raise InputError(f"--export {export} holds no manifest.json")
        found.append(export)
    return found


def _intern(value):
    return sys.intern(value) if isinstance(value, str) else value


def load_export(directory: Path, stats: LoadStats) -> Replay:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    build = manifest.get("replay_build")
    if not isinstance(build, str) or not build:
        raise InputError(f"{directory}: manifest has no replay_build")
    declared = manifest.get("net_field_export_groups")
    if not isinstance(declared, list):
        raise InputError(f"{directory}: manifest has no net_field_export_groups")
    groups: set = set()
    fields: set = set()
    for group in declared:
        path = _intern(group.get("path"))
        if not path:
            raise InputError(f"{directory}: a declared group has no path")
        groups.add(path)
        stats.main_groups += 1
        for f in group.get("fields") or []:
            stats.main_fields += 1
            name, checksum = f.get("name"), f.get("compatible_checksum")
            if not isinstance(name, str) or type(checksum) is not int:
                # Counted and failed like an orphaned checkpoint field. Kept
                # under None, a nameless field at a mapped handle resolved
                # through the handle alone.
                stats.fields_without_identity += 1
                continue
            fields.add((path, _intern(name), checksum, f.get("handle")))

    group_file = directory / "checkpoint_export_groups.parquet"
    field_file = directory / "checkpoint_export_fields.parquet"
    if group_file.is_file() != field_file.is_file():
        raise InputError(f"{directory}: only one of the two checkpoint declaration tables")
    if group_file.is_file():
        import pyarrow as pa
        import pyarrow.compute as pc
        import pyarrow.parquet as pq

        stats.exports_with_checkpoints += 1
        keys = ["checkpoint_index", "group_ordinal"]
        gt = pq.read_table(group_file, columns=[
            "checkpoint_index", "ordinal", "path_name_index", "group_path"]).rename_columns(
            keys + ["group_path_index", "group_path"])
        groups.update(map(_intern, pc.unique(gt["group_path"]).to_pylist()))
        stats.checkpoint_groups += gt.num_rows
        stats.repeated_checkpoint_groups += gt.num_rows - gt.group_by(keys).aggregate([]).num_rows
        ft = pq.read_table(field_file, columns=keys + [
            "path_name_index", "handle", "compatible_checksum", "rendered_name"])
        joined = ft.join(gt.append_column("declared", pa.repeat(True, gt.num_rows)), keys)
        declared = pc.is_valid(joined["declared"])
        mine, theirs = joined["path_name_index"], joined["group_path_index"]
        same = pc.or_(pc.fill_null(pc.equal(mine, theirs), False),
                      pc.and_(pc.is_null(mine), pc.is_null(theirs)))
        stats.orphan_checkpoint_fields += joined.num_rows - pc.count(joined["declared"]).as_py()
        stats.path_index_mismatches += joined.filter(pc.and_(declared, pc.invert(same))).num_rows
        unique = joined.filter(pc.and_(declared, same)).group_by(
            ["group_path", "rendered_name", "compatible_checksum", "handle"]).aggregate([])
        fields.update(zip(map(_intern, unique["group_path"].to_pylist()),
                          map(_intern, unique["rendered_name"].to_pylist()),
                          unique["compatible_checksum"].to_pylist(), unique["handle"].to_pylist()))
        stats.checkpoint_fields += ft.num_rows
    stats.exports += 1
    return Replay(str(directory), build, frozenset(groups), frozenset(fields))


def build_version(build: str) -> tuple[int, int]:
    m = BUILD_RE.search(build)
    if m is None:
        raise InputError(f"build {build!r} carries no release-MM.mm version to order it by")
    return int(m.group(1)), int(m.group(2))


def short(build: str) -> str:
    major, minor = build_version(build)
    return f"{major}.{minor:02d}"


@dataclass
class Tally:
    """Everything per build the judgement needs, with no replay retained."""
    builds: list[str]
    replays: dict            # build -> replays
    hits: dict               # Entry -> Counter(build -> replays declaring it)
    contexts: dict           # Entry -> Counter(build -> replays declaring its group)
    identities: dict         # Entry -> build -> {(wire name, checksum)}
    conflicts: dict          # handle Entry -> Counter(build -> replays with a conflict)
    group_replays: dict      # build -> Counter(group -> replays), literal groups
    declarations: dict       # build -> Counter((group, name, checksum, handle) -> replays)
    _by_group: dict = field(default_factory=dict)

    def by_group(self, build: str) -> dict:
        """`{group: {(name, checksum, handle)}}` declared in `build`."""
        if build not in self._by_group:
            index = collections.defaultdict(set)
            for (g, n, c, h) in self.declarations[build]:
                index[g].add((n, c, h))
            self._by_group[build] = index
        return self._by_group[build]

    def pairs(self, builds, group: str) -> set:
        """`(name, checksum)` pairs `group` declares in any of `builds`."""
        return {(n, c) for b in builds for (n, c, _) in self.by_group(b).get(group, ())}


def tally(replays: list[Replay], catalog: Catalog, overlay: Overlay) -> Tally:
    kinds = catalog.by_kind()
    by_build = collections.defaultdict(list)
    for r in replays:
        by_build[r.build].append(r)
    versions = {b: build_version(b) for b in by_build}
    if len(set(versions.values())) != len(versions):
        raise InputError("two build strings carry the same version; cannot order them")
    builds = sorted(by_build, key=versions.get)

    hits = collections.defaultdict(collections.Counter)
    contexts = collections.defaultdict(collections.Counter)
    identities = collections.defaultdict(lambda: collections.defaultdict(set))
    conflicts = collections.defaultdict(collections.Counter)
    group_replays = {b: collections.Counter() for b in builds}
    declarations = {b: collections.Counter() for b in builds}
    replay_count = {b: len(by_build[b]) for b in builds}

    # The groups that carry each learned checksum anywhere in the input: a
    # checksum entry's "group" for the field-missing test.
    carriers = collections.defaultdict(set)
    for r in replays:
        for group, _, checksum, _ in r.fields:
            if checksum in overlay.checksums:
                carriers[checksum].add(group)
    group_keyed = kinds["table"] + kinds["handle"]
    literal_keyed = kinds["scoped"] + kinds["route"]

    for build in builds:
        for r in by_build[build]:
            group_replays[build].update(r.groups)
            declarations[build].update(r.fields)
            effective = set(r.groups)
            effective.update(overlay.aliases[g] for g in r.groups if g in overlay.aliases)
            hit: set = set()
            conflicted: set = set()

            def note(entry, name, checksum):
                hit.add(entry)
                identities[entry][build].add((name, checksum))

            for group, name, checksum, handle in r.fields:
                resolved = overlay.resolve(group, name, checksum, handle)
                if resolved is not None and resolved.step in ("table", "alias"):
                    note(resolved.entry, name, checksum)
                if handle is not None:
                    via, state = overlay.handle_state(group, name, handle)
                    if state == "hit":
                        note(via, name, checksum)
                    elif state == "conflict":
                        conflicted.add(via)
                if checksum is not None:
                    scoped = overlay.scoped.get((name, group, checksum))
                    if scoped is not None:
                        note(scoped, name, checksum)
                    learned = overlay.checksums.get(checksum)
                    if learned is not None:
                        note(learned, name, checksum)
                    route = overlay.routes.get((group, name, checksum))
                    if route is not None:
                        note(route, name, checksum)
            for group in r.groups:
                for table in (overlay.remaps, overlay.alias_entries):
                    if group in table:
                        hit.add(table[group])

            for entry in hit:
                hits[entry][build] += 1
            for entry in conflicted:
                conflicts[entry][build] += 1
            for entry in group_keyed:
                if entry.group in effective:
                    contexts[entry][build] += 1
            for entry in literal_keyed:
                if entry.group in r.groups:
                    contexts[entry][build] += 1
            for entry in kinds["checksum"]:
                if any(g in r.groups for g in carriers.get(entry.checksum, ())):
                    contexts[entry][build] += 1
            for entry in kinds["remap"] + kinds["alias"]:
                contexts[entry][build] += 1
    return Tally(builds, replay_count, hits, contexts, identities, conflicts,
                 group_replays, declarations)


def p_absent(total: int, hits: int, drawn: int) -> float:
    """Chance that `drawn` of `total` replays, `hits` of which declare the
    entry, all miss it: C(total - hits, drawn) / C(total, drawn)."""
    if hits <= 0 or drawn <= 0:
        return 1.0
    if drawn > total - hits:
        return 0.0
    return math.comb(total - hits, drawn) / math.comb(total, drawn)


def reference_window(builds: list[str], replays: dict, index: int) -> list[str]:
    """The builds before `builds[index]`, newest first, until they hold at least
    `REFERENCE_REPLAYS` replays (or run out)."""
    window, total = [], 0
    for j in range(index - 1, -1, -1):
        window.append(builds[j])
        total += replays[builds[j]]
        if total >= REFERENCE_REPLAYS:
            break
    return window


def group_kind(group: str) -> tuple[str, str]:
    if ":" in group:
        return ("rpc", group.split(":", 1)[1])
    if group.endswith("_ClassNetCache"):
        return ("cnc", "")
    return ("property", "")


def class_leaf(group: str) -> str:
    """The class name a group path ends in, without its RPC or CNC suffix."""
    base = group.split(":", 1)[0]
    if base.endswith("_ClassNetCache"):
        base = base[: -len("_ClassNetCache")]
    return base.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def leaf_rule(old: str, new: str) -> str | None:
    """How two class names match, if they do.

    The same name, or the same `_`-separated tokens but one, where both of the
    differing tokens are at most `SHORT_TOKEN` characters -- an ability slot
    (`E` -> `4`) or an index, not a different word.
    """
    a, b = class_leaf(old), class_leaf(new)
    if a == b:
        return "same class name"
    ta, tb = a.split("_"), b.split("_")
    if len(ta) != len(tb):
        return None
    diff = [(x, y) for x, y in zip(ta, tb) if x != y]
    if len(diff) == 1 and len(diff[0][0]) <= SHORT_TOKEN and len(diff[0][1]) <= SHORT_TOKEN:
        return f"one short token {diff[0][0]!r} -> {diff[0][1]!r}"
    return None


@dataclass(frozen=True)
class Successor:
    group: str
    rule: str
    replays: int
    shared: int      # of the old group's reference (name, checksum) pairs


def _rule_rank(rule: str) -> int:
    if rule == "same class name":
        return 0
    return 1 if rule.startswith("one short token") else 2


def find_successors(tally_: Tally, group: str, build: str, window: list[str]) -> list[Successor]:
    """Groups that took over from `group`, which `build` no longer declares:
    the successor rule of the module docstring's `moved` state (`leaf_rule`,
    or one of `group`'s RARE reference pairs)."""
    in_window = set()
    for b in window:
        in_window.update(tally_.group_replays[b])
    kind = group_kind(group)
    fresh = [g for g in tally_.group_replays[build]
             if g not in in_window and group_kind(g) == kind and g != group]
    if not fresh:
        return []
    old_pairs = tally_.pairs(window, group)
    pair_groups = collections.defaultdict(set)
    for b in window:
        for g, declared in tally_.by_group(b).items():
            for (n, c, _) in declared:
                if (n, c) in old_pairs:
                    pair_groups[(n, c)].add(g)
    rare = {p for p in old_pairs if len(pair_groups[p]) <= RARE_PAIR_GROUPS}
    out = []
    for g in fresh:
        cur_pairs = tally_.pairs([build], g)
        rule = leaf_rule(group, g)
        if rule is None and rare & cur_pairs:
            rule = f"shares {len(rare & cur_pairs)} rare field(s)"
        if rule is not None:
            out.append(Successor(g, rule, tally_.group_replays[build][g],
                                 len(old_pairs & cur_pairs)))
    out.sort(key=lambda s: (_rule_rank(s.rule), -s.shared, -s.replays, s.group))
    return out


@dataclass
class Finding:
    build: str
    ref_builds: list
    entry: Entry
    category: str             # field-missing | moved | vanished
    k_ref: int
    c_ref: int
    n_ref: int
    c_cur: int
    n_cur: int
    p: float
    evidenced: bool
    successors: list = field(default_factory=list)
    coverage: str = ""        # covered | lost (moved only)
    coverage_detail: str = ""
    new_fields: list = field(default_factory=list)
    expected: dict | None = None

    @property
    def failing(self) -> bool:
        """Would fail without an expected-list item."""
        if not self.evidenced:
            return False
        return self.category == "field-missing" or (
            self.category == "moved" and self.coverage != "covered")

    @property
    def fails(self) -> bool:
        return self.failing and self.expected is None


UNTYPED = frozenset({normalize_type("FieldType::Raw"), normalize_type("FieldType::Skip")})


def coverage(overlay: Overlay, tally_: Tally, finding: Finding) -> tuple[str, str]:
    """Whether the successor's own declaration of the field still gets the
    entry's type, and how: `covered` when it resolves to the same type, or
    when a `Raw` / `Skip` entry's successor field is untyped too (no typed row
    to lose); anything else is `lost`, including a successor that does not
    declare the field under the same checksum.
    """
    entry, succ = finding.entry, finding.successors[0].group
    if entry.kind in GROUP_ONLY:
        same = (overlay.remaps if entry.kind == "remap" else overlay.alias_entries)
        return ("covered", f"successor is itself a {entry.kind} key") if succ in same \
            else ("lost", f"no {entry.kind} entry names the successor")
    if entry.kind == "route":
        if (succ, entry.name, entry.checksum) in overlay.routes:
            return "covered", "the successor is a route key too"
        return "lost", "no route names the successor"
    wanted = set()
    for b in finding.ref_builds:
        wanted |= tally_.identities[entry].get(b, set())
    succ_fields = tally_.by_group(finding.build).get(succ, ())
    declared = sorted(((n, c, h) for (n, c, h) in succ_fields if (n, c) in wanted),
                      key=lambda t: (str(t[0]), t[1] or 0, t[2] or 0))
    if not declared:
        names = sorted({n for n, _ in wanted}, key=str)
        other = sorted({(n, c) for (n, c, _) in succ_fields if n in names}, key=str)
        if other:
            return "lost", ("the successor declares " + ", ".join(f"{n} {c}" for n, c in other)
                            + " -- not the reference checksum")
        return "lost", f"the successor does not declare {', '.join(map(str, names)) or '?'}"
    results = [(n, c, overlay.resolve(succ, n, c, h)) for n, c, h in declared]
    for n, c, resolved in results:
        if resolved is not None and resolved.ftype == entry.ftype:
            return "covered", f"{n} resolves to the same type ({resolved.step})"
    if entry.ftype in UNTYPED and all(r is None or r.ftype in UNTYPED for _, _, r in results):
        return "covered", f"{entry.ftype.split('::')[-1]} here, untyped under the successor too"
    n, c, resolved = results[0]
    got = "nothing" if resolved is None else f"{resolved.ftype} ({resolved.step})"
    return "lost", f"the successor declares {n} {c}; it resolves to {got}"


@dataclass
class Judgement:
    findings: list
    per_build: dict           # build -> dict of counts
    drift: list               # (build, entry, new identities)
    windows: dict             # build -> reference builds


def judge(tally_: Tally, catalog: Catalog, overlay: Overlay) -> Judgement:
    findings, drift, windows, per_build = [], [], {}, {}
    builds = tally_.builds
    for i, build in enumerate(builds):
        counts = collections.Counter()
        per_build[build] = counts
        for entry in catalog.entries:
            k = tally_.hits[entry][build]
            c = tally_.contexts[entry][build] if entry.kind not in GROUP_ONLY else 0
            counts[f"{entry.kind}:" + ("declared" if k else "missing" if c else "unobserved")] += 1
            if entry.kind == "handle":
                counts["conflicts"] += tally_.conflicts[entry][build]
        if i == 0:
            continue
        window = reference_window(builds, tally_.replays, i)
        windows[build] = window
        n_ref = sum(tally_.replays[b] for b in window)
        n_cur = tally_.replays[build]
        successor_cache: dict = {}
        for entry in catalog.entries:
            k_ref = sum(tally_.hits[entry][b] for b in window)
            if k_ref == 0:
                continue
            counts["judged"] += 1
            c_ref = sum(tally_.contexts[entry][b] for b in window)
            k = tally_.hits[entry][build]
            c = tally_.contexts[entry][build]
            if k:
                counts["survived"] += 1
                if entry.kind in ("table", "handle"):
                    # One reference checksum only: flattened struct members
                    # (`G`, `R` on BombPlayerState, `CurrentValue` on
                    # AresAttributeSet) carry several by design and would list
                    # every build. A new checksum `checksum_table.rs` types the
                    # same way is a second property of that name.
                    seen = set().union(*(tally_.identities[entry].get(b, set()) for b in window))
                    old = {cs for _, cs in seen}
                    new = {(n, cs) for n, cs in tally_.identities[entry].get(build, set())
                           if cs not in old and not (cs in overlay.checksums and
                                                     overlay.checksums[cs].ftype == entry.ftype)}
                    if new and len(old) == 1:
                        drift.append((build, entry, sorted(new, key=str)))
                continue
            if entry.kind in GROUP_ONLY:
                # Keyed on the group alone: its presence IS the entry, so the
                # group's reference count is `k_ref` and it is absent now.
                c_ref, c = k_ref, 0
            if c:
                p = p_absent(c_ref + c, k_ref, c)
                f = Finding(build, window, entry, "field-missing", k_ref, c_ref, n_ref, c,
                            n_cur, p, c >= MIN_CONTEXT and p <= ALPHA)
                if entry.group is not None:
                    before = {n for b in window
                              for (n, _, _) in tally_.by_group(b).get(entry.group, ())}
                    f.new_fields = sorted({str(n) for (n, _, _)
                                           in tally_.by_group(build).get(entry.group, ())
                                           if n not in before})
                findings.append(f)
                continue
            p = p_absent(n_ref + n_cur, c_ref, n_cur)
            evidenced = n_cur >= MIN_CONTEXT and p <= ALPHA
            successors = []
            if entry.group is not None:
                if entry.group not in successor_cache:
                    successor_cache[entry.group] = find_successors(
                        tally_, entry.group, build, window)
                successors = successor_cache[entry.group]
            f = Finding(build, window, entry, "moved" if successors else "vanished", k_ref,
                        c_ref, n_ref, c, n_cur, p, evidenced, successors)
            if successors:
                f.coverage, f.coverage_detail = coverage(overlay, tally_, f)
            findings.append(f)
    return Judgement(findings, per_build, drift, windows)


EXPECTED_KEYS = {"entry", "build", "finding", "reason", "evidence"}


def load_expected(path: Path) -> list[dict]:
    """The reasoned exceptions. Malformed items are a failure, not a skip."""
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("expected")
    if not isinstance(items, list):
        raise ValueError(f"{path}: no `expected` list")
    seen = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != EXPECTED_KEYS:
            raise ValueError(f"{path}: item {i} must have exactly {sorted(EXPECTED_KEYS)}")
        if item["finding"] not in ("field-missing", "moved"):
            raise ValueError(f"{path}: item {i}: finding must be field-missing or moved")
        for k in ("reason", "evidence"):
            if not isinstance(item[k], str) or not item[k].strip():
                raise ValueError(f"{path}: item {i} has no {k}")
        key = (item["entry"], item["build"], item["finding"])
        if key in seen:
            raise ValueError(f"{path}: item {i} repeats {key}")
        seen.add(key)
    return items


def apply_expected(findings: list, items: list[dict]) -> list[dict]:
    """Attach each item to the failing finding it names; return the stale ones."""
    index = {(f.entry.key, short(f.build), f.category): f for f in findings if f.failing}
    stale = []
    for item in items:
        f = index.get((item["entry"], item["build"], item["finding"]))
        if f is None:
            stale.append(item)
        else:
            f.expected = item
    return stale


def _span(builds: list[str]) -> str:
    if not builds:
        return "--"
    ordered = sorted(builds, key=build_version)
    first, last = short(ordered[0]), short(ordered[-1])
    return first if first == last else f"{first}-{last}"


def summary_lines(tally_: Tally, judgement: Judgement) -> list[str]:
    lines = ["states per build: declared / field-missing / not observed"]
    head = f"{'build':<6} {'replays':>7}  " + "  ".join(f"{k:>16}" for k in KINDS)
    lines.append(head)
    for b in tally_.builds:
        c = judgement.per_build[b]
        cells = []
        for k in KINDS:
            cell = f"{c[k + ':declared']}/{c[k + ':missing']}/{c[k + ':unobserved']}"
            cells.append(f"{cell:>16}")
        lines.append(f"{short(b):<6} {tally_.replays[b]:>7}  " + "  ".join(cells))

    lines.append("")
    lines.append("against the reference window: entries it declared, and what became of them")
    lines.append(f"{'build':<6} {'reference':>17} {'judged':>6} {'survived':>8} "
                 f"{'missing ev/weak':>15} {'moved lost/cov/weak':>19} "
                 f"{'vanished ev/weak':>16} {'drift':>5} {'conflicts':>9} {'failing':>7} "
                 f"{'listed':>6}")
    by_build = collections.defaultdict(list)
    for f in judgement.findings:
        by_build[f.build].append(f)
    drift = collections.Counter(b for b, _, _ in judgement.drift)
    for b in tally_.builds[1:]:
        c = judgement.per_build[b]
        fs = by_build[b]
        window = judgement.windows[b]
        ref = f"{_span(window)} ({sum(tally_.replays[x] for x in window)})"
        miss = [f for f in fs if f.category == "field-missing"]
        moved = [f for f in fs if f.category == "moved"]
        van = [f for f in fs if f.category == "vanished"]
        mv = (f"{sum(f.evidenced and f.coverage != 'covered' for f in moved)}/"
              f"{sum(f.evidenced and f.coverage == 'covered' for f in moved)}/"
              f"{sum(not f.evidenced for f in moved)}")
        lines.append(
            f"{short(b):<6} {ref:>17} {c['judged']:>6} {c['survived']:>8} "
            f"{sum(f.evidenced for f in miss):>7}/{sum(not f.evidenced for f in miss):<7} "
            f"{mv:>19} "
            f"{sum(f.evidenced for f in van):>8}/{sum(not f.evidenced for f in van):<7} "
            f"{drift[b]:>5} {c['conflicts']:>9} {sum(f.fails for f in fs):>7} "
            f"{sum(f.expected is not None for f in fs):>6}")
    return lines


def _counts(f: Finding) -> str:
    if f.category == "field-missing":
        return (f"{f.k_ref}/{f.c_ref} group replays -> 0/{f.c_cur}, p={f.p:.1e}")
    return f"group in {f.c_ref}/{f.n_ref} replays -> 0/{f.n_cur}, p={f.p:.1e}"


def _verdict(f: Finding) -> str:
    if f.expected is not None:
        return "expected"
    return "FAIL" if f.fails else "reported"


def finding_lines(judgement: Judgement, verbose: bool) -> list[str]:
    lines = []
    evidenced = [f for f in judgement.findings if f.evidenced]
    missing = [f for f in evidenced if f.category == "field-missing"]
    lines.append(f"structural findings (field-missing with evidence): {len(missing)}")
    for f in missing:
        lines.append(f"  {short(f.build)}  {_verdict(f):<8} {f.entry.label}")
        lines.append(f"         {_counts(f)}; new in the group this build: "
                     + (", ".join(f.new_fields[:8]) + (" ..." if len(f.new_fields) > 8 else "")
                        if f.new_fields else "none"))
        if f.expected is not None:
            lines.append(f"         listed: {f.expected['reason']}")

    moved = [f for f in evidenced if f.category == "moved"]
    groups = collections.OrderedDict()
    for f in moved:
        groups.setdefault((f.build, f.entry.group), []).append(f)
    lost = sum(f.coverage != "covered" for f in moved)
    lines.append("")
    lines.append(f"moves with evidence: {len(groups)} group(s), {len(moved)} entr(y/ies): "
                 f"{lost} lost, {len(moved) - lost} covered")
    for (build, group), fs in groups.items():
        f0 = fs[0]
        best = f0.successors[0]
        lines.append(f"  {short(build)}  {group}")
        lines.append(f"         {_counts(f0)}")
        lines.append(f"         successor: {best.group}")
        lines.append(f"           [{best.rule}; declared in {best.replays}/{f0.n_cur} replays; "
                     f"shares {best.shared} reference field(s)]"
                     + (f" (+{len(f0.successors) - 1} other candidate(s))"
                        if len(f0.successors) > 1 else ""))
        for f in fs:
            lines.append(f"         {_verdict(f):<8} {f.coverage:<7} {f.entry.kind:<8} "
                         f"{f.entry.name or '-'}: {f.coverage_detail}")

    vanished = [f for f in evidenced if f.category == "vanished"]
    vgroups = collections.OrderedDict()
    for f in vanished:
        vgroups.setdefault((f.build, f.entry.group), []).append(f)
    lines.append("")
    lines.append(f"vanished with evidence, no successor found (reported, never failed): "
                 f"{len(vgroups)} group(s), {len(vanished)} entr(y/ies)")
    for (build, group), fs in vgroups.items():
        lines.append(f"  {short(build)}  {group or fs[0].entry.label}: {_counts(fs[0])}, "
                     f"{len(fs)} entr(y/ies)")

    weak = [f for f in judgement.findings if not f.evidenced]
    wc = collections.Counter(f.category for f in weak)
    lines.append("")
    lines.append(f"weak findings (too few replays, or p > {ALPHA:g}; never failed): "
                 f"field-missing {wc['field-missing']}, moved {wc['moved']}, "
                 f"vanished {wc['vanished']}")
    if verbose:
        for f in weak:
            lines.append(f"  {short(f.build)}  {f.category:<13} {f.entry.label}: {_counts(f)}")

    lines.append("")
    lines.append(f"checksum drift (declared, under a checksum the reference never carried; "
                 f"reported): {len(judgement.drift)}")
    for build, entry, new in judgement.drift[: None if verbose else 20]:
        lines.append(f"  {short(build)}  {entry.label}: "
                     + ", ".join(f"{n} {c}" for n, c in new))
    if not verbose and len(judgement.drift) > 20:
        lines.append(f"  ... and {len(judgement.drift) - 20} more (--verbose)")
    return lines


def never_declared(tally_: Tally, catalog: Catalog) -> list[Entry]:
    return [e for e in catalog.entries if not any(tally_.hits[e][b] for b in tally_.builds)]


def show_lines(tally_: Tally, catalog: Catalog, pattern: str) -> list[str]:
    rx = re.compile(pattern)
    chosen = [e for e in catalog.entries if rx.search(e.key)]
    lines = [f"entries matching {pattern!r}: {len(chosen)} -- per build, replays declaring "
             f"the entry / replays declaring its group (of all replays)"]
    lines.append("builds: " + " ".join(f"{short(b)}({tally_.replays[b]})" for b in tally_.builds))
    for e in chosen:
        cells = " ".join(f"{tally_.hits[e][b]}/{tally_.contexts[e][b]}" for b in tally_.builds)
        lines.append(f"  {e.label}")
        lines.append(f"    {cells}")
    return lines


def to_json(tally_: Tally, catalog: Catalog, judgement: Judgement, stale: list) -> dict:
    def fjson(f: Finding) -> dict:
        return {
            "build": short(f.build), "reference": [short(b) for b in f.ref_builds],
            "entry": f.entry.key, "kind": f.entry.kind, "type": f.entry.ftype,
            "category": f.category, "evidenced": f.evidenced, "p": f.p,
            "declared_in_reference": f.k_ref, "group_in_reference": f.c_ref,
            "reference_replays": f.n_ref, "group_in_build": f.c_cur, "build_replays": f.n_cur,
            "successors": [s.__dict__ for s in f.successors],
            "coverage": f.coverage, "coverage_detail": f.coverage_detail,
            "new_fields": f.new_fields, "fails": f.fails,
            "expected": f.expected is not None,
        }
    return {
        "builds": [{"build": short(b), "replays": tally_.replays[b],
                    "reference": [short(x) for x in judgement.windows.get(b, [])],
                    "states": dict(judgement.per_build[b])} for b in tally_.builds],
        "entries": {k: len(v) for k, v in catalog.by_kind().items()},
        "thresholds": {"alpha": ALPHA, "min_context": MIN_CONTEXT,
                       "reference_replays": REFERENCE_REPLAYS, "short_token": SHORT_TOKEN,
                       "rare_pair_groups": RARE_PAIR_GROUPS},
        "findings": [fjson(f) for f in judgement.findings],
        "checksum_drift": [{"build": short(b), "entry": e.key, "new": [list(x) for x in n]}
                           for b, e, n in judgement.drift],
        "never_declared": [e.key for e in never_declared(tally_, catalog)],
        "stale_expected": stale,
    }


def run(replays: list[Replay], catalog: Catalog, expected: list[dict], stats: LoadStats,
        verbose: bool = False, show: str | None = None,
        json_path: Path | None = None, expected_name: str = EXPECTED_JSON.name) -> int:
    overlay = Overlay(catalog)
    t = tally(replays, catalog, overlay)
    if len(t.builds) < 2:
        print(f"FAILED: {len(t.builds)} build(s) in the input -- survival is a comparison "
              f"between builds, so nothing was judged", file=sys.stderr)
        return 2
    judgement = judge(t, catalog, overlay)
    stale = apply_expected(judgement.findings, expected)

    kinds = catalog.by_kind()
    print(f"entry survival: {stats.exports} export(s), {len(t.builds)} builds; "
          f"reference window >= {REFERENCE_REPLAYS} replays, alpha {ALPHA:g}, "
          f"at least {MIN_CONTEXT} replays to judge an absence")
    print("entries: " + ", ".join(f"{len(kinds[k])} {k}" for k in KINDS)
          + f" = {len(catalog.entries)}")
    print(f"declarations: {stats.main_groups} main-stream group(s) with {stats.main_fields} "
          f"field(s) ({stats.fields_without_identity} without a name or checksum); "
          f"{stats.exports_with_checkpoints} export(s) with checkpoint tables, "
          f"{stats.checkpoint_groups} group row(s), {stats.checkpoint_fields} field row(s); "
          f"{stats.orphan_checkpoint_fields} field row(s) joining no group, "
          f"{stats.path_index_mismatches} path-index mismatch(es), "
          f"{stats.repeated_checkpoint_groups} repeated group key(s)")
    print(f"skipped generated directories: {len(stats.skipped)}")
    for s in stats.skipped:
        print(f"  {s}")
    print()
    for line in summary_lines(t, judgement):
        print(line)
    print()
    for line in finding_lines(judgement, verbose):
        print(line)
    dead = never_declared(t, catalog)
    dc = collections.Counter(e.kind for e in dead)
    print()
    print("never declared in any build of the input: "
          + ", ".join(f"{dc[k]} {k}" for k in KINDS))
    if verbose:
        for e in dead:
            print(f"  {e.label}")
    if show:
        print()
        for line in show_lines(t, catalog, show):
            print(line)
    if json_path is not None:
        json_path.write_text(json.dumps(to_json(t, catalog, judgement, stale), indent=1),
                             encoding="utf-8")

    failing = [f for f in judgement.findings if f.fails]
    problems = []
    if failing:
        problems.append(f"{len(failing)} entr(y/ies) declared in their reference window lost "
                        f"their declaration with evidence (field-missing, or moved with the "
                        f"typing lost) and are not in {expected_name}")
    if stale:
        problems.append(f"{len(stale)} item(s) of {expected_name} match no failing "
                        f"finding (STALE): " + "; ".join(
                            f"{i['entry']} {i['build']} {i['finding']}" for i in stale))
    if (stats.orphan_checkpoint_fields or stats.path_index_mismatches
            or stats.repeated_checkpoint_groups):
        problems.append("checkpoint field declarations that join no group, or no one group -- "
                        "the input is inconsistent, so what it declares is not known")
    if stats.fields_without_identity:
        problems.append("main-stream field declarations without a name or checksum -- the "
                        "input is inconsistent, so what it declares is not known")
    if problems:
        print()
        for p in problems:
            print(f"FAILED: {p}", file=sys.stderr)
        return 1
    judged = sum(judgement.per_build[b]["judged"] for b in t.builds[1:])
    print()
    print(f"OK: {judged} (entry, build) pair(s) judged against their reference windows; "
          f"every evidenced loss is covered or listed with its reason")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, action="append", default=[],
                    help="directory whose child directories are exports (repeatable)")
    ap.add_argument("--export", type=Path, action="append", default=[],
                    help="one export directory (repeatable)")
    ap.add_argument("--expected", type=Path, default=EXPECTED_JSON,
                    help="the reasoned expected-findings list")
    ap.add_argument("--show", metavar="REGEX",
                    help="print the per-build counts of every entry whose key matches")
    ap.add_argument("--json", type=Path, help="also write the complete result here")
    ap.add_argument("--verbose", action="store_true",
                    help="list weak findings, all drift and every never-declared entry")
    args = ap.parse_args(argv)

    try:
        catalog = load_catalog(Sources.from_repo())
    except EntryParseError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    try:
        expected = load_expected(args.expected)
    except (OSError, ValueError) as exc:
        print(f"FAILED: expected list: {exc}", file=sys.stderr)
        return 1
    stats = LoadStats()
    try:
        dirs = discover(args.root, args.export, stats)
        if not dirs:
            print("FAILED: no export (a directory holding manifest.json) under "
                  + ", ".join(map(str, args.root + args.export)) if args.root + args.export
                  else "FAILED: name exports with --root or --export", file=sys.stderr)
            return 2
        replays = [load_export(d, stats) for d in dirs]
    except InputError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    try:
        return run(replays, catalog, expected, stats, args.verbose, args.show, args.json,
                   expected_name=args.expected.name)
    except InputError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
