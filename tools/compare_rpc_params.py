"""Compare RPC parameter values between our Rust Parquet and the C# NDJSON export.

For the parameters in `RPCS_TO_CHECK`, a run passes only if both checks do:

  - per parameter, the multiset of values -- the table this prints; and
  - per record, its identity (packet, actor, subobject, channel, function)
    together with every value above, as a multiset of records. Only this one
    tells a missing record from one the other side has at another packet with
    the same values, and an expected difference is keyed on it.

The C# side is the C# reference parser's `export` of the 13.01 reference
replay, kept machine-local because it carries per-player values; docs/USAGE.md
section 6 has the commands that produce it.

`EXPECTED_DIFFERENCES` lists records whose absence from the C# export is
explained, keyed by the replay's SHA-256 (from the `manifest.json` the export
writes beside the reference, which must stay there), the record's identity and
its values; an entry is excluded from both checks only when all of that holds
exactly. On its replay, an entry that does not occur exactly as listed is
STALE and fails the run. Without a readable manifest the replay is unknown:
nothing is applied or checked for staleness, and the run is incomplete.

Exit status:
  0  every parameter and every record matched, after the expected differences
  1  a value or a record differs, or an expected difference is STALE
  2  an input is missing, a parameter carried no value on either side, or the
     replay is unknown so the expected differences could not be checked

Usage:
    python tools/compare_rpc_params.py [--reference EVENTS] [--ours PARQUET]
"""

import collections
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_combat_report import (  # noqa: E402
    REFERENCE_DIR, missing_input, parse_args, verdict)
from to_valplay_bundle import REGIONAL_DAMAGE_MAP as REGIONAL_DAMAGE_NAMES  # noqa: E402

#: The export's rpc_received lines for `RPCS_TO_CHECK` (the whole events.ndjson
#: works too); its manifest.json must stay beside it, for the replay's SHA-256.
DEFAULT_REFERENCE = REFERENCE_DIR + r"\rpc_params.ndjson"

# (param_name, value_type) per function, value_type being how the C# JSON
# stores it; 'enum_byte' is a C# string that vrfkit stores as i64.
RPCS_TO_CHECK = {
    "MulticastNotifyKilledEnemy": [
        ("KillerCharacter", "int"),
        ("KilledCharacter", "int"),
        ("MultikillLevel", "int"),
    ],
    "MulticastNotifyDamage_Point": [
        ("DamageDealt", "float"),
        ("DamageTaken", "float"),
        ("RegionalDamage", "enum_byte"),
        ("bDamageKilledTarget", "bool"),
    ],
    "MulticastEndRound": [
        ("NewRoundNumber", "int"),
    ],
}

#: C# EAresRegionalDamage names -> the byte vrfkit stores (the adapter's table).
REGIONAL_DAMAGE_MAP = {name: n for n, name in REGIONAL_DAMAGE_NAMES.items()}

#: Parameters the C# payload names differently from the replay.
CS_FIELD_ALIASES = {"bDamageKilledTarget": "DamageKilledTarget"}

#: The vrfkit value column each C# value type is read from.
RUST_VALUE_COLUMN = {
    "int": "value_i64",
    "enum_byte": "value_i64",
    "float": "value_f64",
    "bool": "value_bool",
}


#: Decimal places float parameters are rounded to before comparison; `MATCH`
#: means equal to this precision and no further, as the verdict line states.
FLOAT_PLACES = 2

#: How many differing records each side lists before summarising.
SHOW_RECORDS = 10


def norm(v, vtype):
    """Normalise a value for multiset comparison."""
    if v is None:
        return None
    if vtype == "int":
        return int(v)
    if vtype == "float":
        return round(float(v), FLOAT_PLACES)
    if vtype == "bool":
        return 1 if v else 0
    if vtype == "enum_byte":
        # An unknown C# name stays a str, so it differs rather than guesses.
        return REGIONAL_DAMAGE_MAP.get(v, v) if isinstance(v, str) else int(v)
    return str(v)


@dataclass(frozen=True)
class ExpectedDifference:
    """A record vrfkit exports and the C# reference, for a known reason, does not.

    Excluded only when the replay matches, exactly one vrfkit record at `key`
    carries exactly `values` (every compared parameter, normalised by `norm`)
    and no C# record sits at `key`. Anything else on this replay is STALE.
    """

    replay_sha256: str
    replay: str
    packet_id: int
    actor_net_guid: int
    object_net_guid: int
    channel: int
    function: str
    values: tuple
    reason: str

    @property
    def key(self):
        return (self.packet_id, self.actor_net_guid, self.object_net_guid,
                self.channel, self.function)

    def describe(self):
        return (f"{self.replay} packet {self.packet_id} actor "
                f"{self.actor_net_guid} object {self.object_net_guid} channel "
                f"{self.channel} {self.function} {dict(self.values)}")


#: docs/FOLLOWUP.md, "The damage record only vrfkit emits", has the evidence.
EXPECTED_DIFFERENCES = (
    ExpectedDifference(
        replay_sha256=("266644d3c74157ee222bb98f561ba75f7d0289516cf9d5a1"
                       "59221fcaa2d0d07b"),
        replay="02d4d478-1dfb-4412-9a77-29ca29105a9d",
        packet_id=391880,
        actor_net_guid=27232,
        object_net_guid=27244,
        channel=194,
        function="MulticastNotifyDamage_Point",
        values=(("DamageDealt", 29.45), ("DamageTaken", 20.0),
                ("RegionalDamage", 0), ("bDamageKilledTarget", 1)),
        reason=(
            "the killing blow on the 'Damageable' component of Gekko's Dizzy: "
            "its block names no class NetGUID and the C# subobject table lacks "
            "'Damageable', so the reference skips the block undecoded"),
    ),
)


def record_key(packet_id, actor_net_guid, object_net_guid, channel, function):
    """The identity both sides share. An RPC on the actor itself has no
    subobject: the C# export reports the actor GUID as its object and vrfkit
    leaves `object_net_guid` null, so both are read as the actor."""
    if object_net_guid is None:
        object_net_guid = actor_net_guid
    return (packet_id, actor_net_guid, object_net_guid, channel, function)


def load_cs_records(path):
    """C# rpc_received lines as `[(key, {param: normalised value})]`."""
    records = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            if "rpc_received" not in line:
                continue
            rec = json.loads(line)
            if rec.get("type") != "rpc_received":
                continue
            func = rec.get("function_name", "")
            if func not in RPCS_TO_CHECK:
                continue
            payload = rec.get("payload", {})
            if not payload:
                continue
            params = {}
            for pname, vtype in RPCS_TO_CHECK[func]:
                val = payload.get(pname)
                if val is None:
                    val = payload.get(CS_FIELD_ALIASES.get(pname))
                if val is not None:
                    params[pname] = norm(val, vtype)
            key = record_key(rec.get("packet_id"), rec.get("actor_net_guid"),
                             rec.get("object_net_guid"), rec.get("channel"), func)
            records.append((key, params))
    return records


def load_rust_records(path):
    """vrfkit's parameter rows as `[(key, {param: normalised value})]`.

    A record is the run of consecutive rows sharing a key; a parameter
    repeating inside the run starts the next record (two invocations in one
    bunch). A null stays None: a value vrfkit did not type is a difference.
    """
    columns = ["packet_id", "channel_index", "actor_net_guid", "object_net_guid",
               "field_name", "value_i64", "value_f64", "value_bool", "value_str"]
    vtypes = {func: dict(params) for func, params in RPCS_TO_CHECK.items()}
    wanted = [f"{func}.{param}" for func, params in vtypes.items() for param in params]
    t = pq.read_table(str(path), columns=columns)
    # Only the compared parameter rows, in file order, become Python objects.
    names = t.column("field_name").cast(pa.string())
    t = t.filter(pc.fill_null(pc.is_in(names, value_set=pa.array(wanted)), False))
    cols = {name: t.column(name).to_pylist() for name in columns}

    records = []
    current_key, current = None, None
    for i, field_name in enumerate(cols["field_name"]):
        func, param = field_name.split(".", 1)
        vtype = vtypes[func][param]
        key = record_key(cols["packet_id"][i], cols["actor_net_guid"][i],
                         cols["object_net_guid"][i], cols["channel_index"][i], func)
        if key != current_key or param in current:
            current_key, current = key, {}
            records.append((key, current))
        current[param] = norm(cols[RUST_VALUE_COLUMN[vtype]][i], vtype)
    return records


def values_by_param(records, rpcs=None):
    """`{(function, param): Counter of values}` -- what `compare` takes."""
    result = {(func, pname): collections.Counter()
              for func, params in (rpcs or RPCS_TO_CHECK).items()
              for pname, _ in params}
    for key, params in records:
        for pname, val in params.items():
            counter = result.get((key[4], pname))
            if counter is not None and val is not None:
                counter[val] += 1
    return result


def record_differences(cs_records, rust_records):
    """`(vrfkit-only, C#-only)` records, each a Counter of `(key, values)`."""
    def as_multiset(records):
        return collections.Counter((key, frozenset(params.items()))
                                   for key, params in records)
    cs, rust = as_multiset(cs_records), as_multiset(rust_records)
    return rust - cs, cs - rust


def apply_expected_differences(cs_records, rust_records, replay_sha256,
                               expected=EXPECTED_DIFFERENCES):
    """`(vrfkit records kept, applied, stale, not for this replay)`, `stale`
    as `[(entry, why)]`. An unknown replay (`replay_sha256` None) applies
    nothing."""
    kept = list(rust_records)
    applied, stale, other = [], [], []
    for entry in expected:
        if replay_sha256 is None or entry.replay_sha256 != replay_sha256.lower():
            other.append(entry)
            continue
        theirs = [params for key, params in cs_records if key == entry.key]
        ours = [i for i, (key, _params) in enumerate(kept) if key == entry.key]
        want = dict(entry.values)
        if theirs:
            stale.append((entry, f"the C# export now has {len(theirs)} "
                                 f"record(s) there"))
        elif len(ours) != 1:
            stale.append((entry, f"vrfkit has {len(ours)} record(s) there, "
                                 f"not 1"))
        elif kept[ours[0]][1] != want:
            stale.append((entry, f"vrfkit's values there are "
                                 f"{kept[ours[0]][1]}"))
        else:
            del kept[ours[0]]
            applied.append(entry)
    return kept, applied, stale, other


def reference_replay_sha256(reference):
    """`(SHA-256 or None, where it came from or why there is none)`, from the
    manifest.json beside the reference: the only statement there of which
    replay it describes."""
    manifest = Path(reference).with_name("manifest.json")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"{manifest} could not be read ({e.__class__.__name__})"
    sha = data.get("source_sha256") if isinstance(data, dict) else None
    if not (isinstance(sha, str) and len(sha) == 64
            and all(c in "0123456789abcdefABCDEF" for c in sha)):
        return None, f"{manifest} has no SHA-256 source_sha256"
    return sha.lower(), f"source_sha256 in {manifest}"


def compare(cs, rust, rpcs=None):
    """`(printable rows, everything matched, how many were compared)`."""
    rows, all_match, checked = [], True, 0
    for func_name, params in (rpcs or RPCS_TO_CHECK).items():
        for pname, _vtype in params:
            cs_vals = cs.get((func_name, pname), collections.Counter())
            rust_vals = rust.get((func_name, pname), collections.Counter())
            text, matched, compared = verdict(cs_vals, rust_vals)
            all_match &= matched
            checked += compared
            rows.append(f"{func_name:<35} {pname:<25} {sum(cs_vals.values()):>5} "
                        f"{sum(rust_vals.values()):>5}  {text}")
    return rows, all_match, checked


def print_records(label, differing):
    total = sum(differing.values())
    print(f"{label}: {total}")
    for (key, values), n in sorted(differing.items(), key=repr)[:SHOW_RECORDS]:
        packet, actor, obj, channel, func = key
        print(f"  packet {packet} actor {actor} object {obj} channel {channel} "
              f"{func} {dict(sorted(values))}" + (f" x{n}" if n > 1 else ""))
    if len(differing) > SHOW_RECORDS:
        print(f"  ... {len(differing) - SHOW_RECORDS} more")


def main(argv=None, *, cs_records=None, rust_records=None, rpcs=None,
         replay_sha256=None, expected=EXPECTED_DIFFERENCES):
    """Exit 0 only if every parameter and record matches and nothing is stale;
    exit 2 when a parameter carried nothing on either side, since it was not
    compared (every one is present in the reference replay). `cs_records` /
    `rust_records` and `replay_sha256` stand in for the files, for tests.
    """
    if cs_records is None or rust_records is None:
        reference, parquet = parse_args(
            argv, __doc__, DEFAULT_REFERENCE,
            "rpc_received lines; the export's manifest.json must be beside it")
        if missing_input(reference if cs_records is None else None,
                         parquet if rust_records is None else None):
            return 2
        print(f"C# source: {reference}")
        print(f"Rust source: {parquet}")
        replay_sha256, source = reference_replay_sha256(reference)
        cs_records = load_cs_records(reference) if cs_records is None else cs_records
        rust_records = load_rust_records(parquet) if rust_records is None else rust_records
    else:
        source = "given by the caller" if replay_sha256 else "not given"

    print(f"Replay: {replay_sha256 or 'UNKNOWN'} ({source})")
    if replay_sha256 is None:
        # Neither applicable nor checkable: which replay these records come
        # from is exactly what an entry is keyed on.
        rust_kept, applied, stale, other = list(rust_records), [], [], []
        unchecked = list(expected)
    else:
        rust_kept, applied, stale, other = apply_expected_differences(
            cs_records, rust_records, replay_sha256, expected)
        unchecked = []
    print(f"Expected differences: {len(applied)} applied, {len(stale)} stale, "
          f"{len(other)} not for this replay, {len(unchecked)} unchecked")
    if unchecked:
        print("  the replay is unknown, so none was applied and none could be "
              "checked for staleness")
    for entry in applied:
        print(f"  APPLIED  vrfkit-only record, {entry.describe()}")
        print(f"           because C# lacks {entry.reason}")
    for entry, why in stale:
        print(f"  STALE    {entry.describe()}: {why}")
    print()

    checked_rpcs = rpcs or RPCS_TO_CHECK
    cs = values_by_param(cs_records, checked_rpcs)
    rust = values_by_param(rust_kept, checked_rpcs)
    rows, all_match, checked = compare(cs, rust, checked_rpcs)

    print(f"{'Function':<35} {'Param':<25} {'C#':>5} {'Rust':>5}  Verdict")
    print("-" * 100)
    print("\n".join(rows))

    print()
    rust_only, cs_only = record_differences(cs_records, rust_kept)
    print(f"Records: {len(cs_records)} C#, {len(rust_kept)} vrfkit "
          f"({len(rust_records) - len(rust_kept)} excluded as expected)")
    print_records("vrfkit-only records", rust_only)
    print_records("C#-only records", cs_only)
    records_match = not rust_only and not cs_only

    print()
    total = sum(len(params) for params in checked_rpcs.values())
    failed = bool(stale) or not all_match or not records_match
    if stale:
        print(f"STALE EXPECTED DIFFERENCES: {len(stale)} -- each no longer "
              f"occurs as listed; update EXPECTED_DIFFERENCES and "
              f"docs/FOLLOWUP.md")
    if not all_match or not records_match:
        print("SOME VALUES OR RECORDS DIFFER -- see above")
    if not failed and (checked < total or unchecked):
        if checked < total:
            print(f"INCOMPLETE: {total - checked} of the {total} RPC parameters "
                  f"carry no value on either side, so they were not compared. "
                  f"This is not agreement -- check the parquet path and the "
                  f"reference.")
        if unchecked:
            print(f"INCOMPLETE: the replay is unknown, so {len(unchecked)} "
                  f"expected difference(s) could not be checked -- keep "
                  f"the export's manifest.json beside the reference.")
        return 2
    if not failed:
        print(f"ALL {checked} RPC PARAMETER VALUES AND ALL {len(cs_records)} "
              f"RECORDS MATCH (floats to {FLOAT_PLACES} decimal places; "
              f"{len(applied)} expected difference(s) excluded)")

    print()
    print("=== Sample values (first 5 per function) ===")
    for func_name, params in checked_rpcs.items():
        pname0, _vtype0 = params[0]
        key = (func_name, pname0)
        cs_sample = list(cs.get(key, collections.Counter()).most_common(5))
        rust_sample = list(rust.get(key, collections.Counter()).most_common(5))
        print(f"\n{func_name}.{pname0}:")
        print(f"  C#:   {cs_sample}")
        print(f"  Rust: {rust_sample}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(argv=sys.argv[1:]))
