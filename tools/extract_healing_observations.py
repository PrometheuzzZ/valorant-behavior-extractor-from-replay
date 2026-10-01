"""Extract conservative healing observations from one vrfkit export.

The MulticastNotifyHeal amount and sections come from the section parser
(extract_section_observations.parse_group); this adds the declaration and
heal-name gates, the heal source and recipient corroboration and the summaries."""

from __future__ import annotations
import collections, json, re
from pathlib import Path
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

import extract_section_observations as sections
from atomic_io import run_json_cli, sha256_file as sha
from player_identity import load_player_bodies
from wire_bits import InputError, iter_selected, load_net_guids, text
IntegrityError = sections.IntegrityError
SCHEMA_VERSION = 1
ROUTE = "MulticastNotifyHeal"
OUTER_GROUP = sections.OUTER_GROUP
PARAM_GROUP = "/Script/ShooterGame.DamageableComponent:MulticastNotifyHeal"
OUTER = (6, ROUTE, 791426194)
DECL = {
    0: ("HealTaken", 1894010429),
    1: ("LifeChangeBySection", 163390906),
    2: ("ChangedComponent", 432306714),
    3: ("LifeResult", 1180204994),
    4: ("DeltaLife", 3211876783),
    5: ("bAliveAfterChange", 2136162893),
    7: ("EventInstigator", 3087885251),
    8: ("EventInstigatorPawn", 3901949544),
    9: ("HealCauser", 546618027),
}
TOP = {f"{ROUTE}.{n}": (h, c) for h, (n, c) in DECL.items() if h in (0, 1, 7, 8, 9)}
#: The optional top-level references, by their declared handle.
OPTIONAL = {name: handle for name, (handle, _) in TOP.items() if handle >= 7}
RX = re.compile(
    r"^MulticastNotifyHeal\.LifeChangeBySection\[(\d+)\]\.(ChangedComponent|LifeResult|DeltaLife|bAliveAfterChange)$"
)
FIELD_COLS = sections.FIELDS
CHECKPOINT_FIELD_COLS = sections.CP_FIELDS
#: Every status `edge()` can return, so the per-edge tally prints zeros too.
EDGE_STATUSES = ("present", "null", "absent", "duplicate", "invalid")
#: Every status `active_instance()` can return.
LIFECYCLE_STATUSES = (
    "active", "lifecycle_time_regression", "ambiguous_actor_lifecycle",
    "lifecycle_boundary_same_time", "no_prior_actor_open",
    "actor_reopened_without_unique_active_instance", "actor_close_without_open", "actor_closed",
)
#: Every final source status: a non-present causer edge, a non-active causer
#: lifecycle, or the reference outcome of an active one.
SOURCE_STATUSES = (
    *EDGE_STATUSES[1:], *LIFECYCLE_STATUSES[1:], "reference_update_same_time",
    "corroborated_static_manifest_character", "conflicting_manifest_characters",
    "no_manifest_character_reference",
)
#: The export files read.
INPUT_NAMES = (
    "manifest.json",
    "fields.parquet",
    "checkpoint_fields.parquet",
    "actors.parquet",
    "net_guids.parquet",
)
SOURCES = [Path(__file__).resolve(), *(Path(__file__).with_name(n) for n in (
    "extract_section_observations.py", "wire_bits.py", "atomic_io.py", "player_identity.py"))]


def iter_selected_fields(path, include_references, columns=FIELD_COLS):
    def mask(batch):
        names = text(batch, "field_name")
        heal = pc.starts_with(names, pattern="MulticastNotifyHeal.")
        if not include_references:
            return heal
        return pc.or_(heal, pc.is_in(names, value_set=pa.array(["Owner", "Instigator"])))
    return iter_selected(path, columns, mask)


def typed_ref(r):
    """A typed ObjectNetGuid; an untyped one is a stale export, not corruption."""
    if r["value_i64"] is None:
        raise IntegrityError("untyped reference; re-export with a parser that types it")
    return sections.reference(r)


def declarations(manifest):
    groups = collections.defaultdict(list)
    for g in manifest.get("net_field_export_groups", []):
        groups[g.get("path")].append(g)
    if len(groups[OUTER_GROUP]) != 1 or len(groups[PARAM_GROUP]) != 1:
        raise InputError("heal declaration group missing or duplicate")
    outer = [
        (x.get("handle"), x.get("name"), x.get("compatible_checksum"))
        for x in groups[OUTER_GROUP][0].get("fields", [])
    ]
    if OUTER not in outer:
        raise InputError("outer heal declaration differs")
    got = {}
    for x in groups[PARAM_GROUP][0].get("fields", []):
        h = x.get("handle")
        if h in got:
            raise InputError("duplicate heal parameter declaration")
        got[h] = (x.get("name"), x.get("compatible_checksum"))
    for h, identity in DECL.items():
        if h <= 5 and got.get(h) != identity:
            raise InputError("required heal parameter declarations differ")
        if h >= 7 and h in got and got[h] != identity:
            raise InputError("optional heal parameter declaration differs")
    return got


def validate_row_schema(r):
    name = r["field_name"]
    if r["group_path"] != OUTER_GROUP or r["handle"] != 6:
        raise InputError("foreign heal row scope")
    if name in TOP and r["compatible_checksum"] != TOP[name][1]:
        raise InputError("heal parameter checksum differs")
    if RX.match(name or "") and r["compatible_checksum"] is not None:
        raise InputError("flattened child unexpectedly has checksum")
    if name not in TOP and not RX.match(name or ""):
        raise InputError("unknown heal parameter row")


def active_instance(rows, guid, event):
    seq = rows.get(guid, [])
    if any(
        (b["time_ms"], b["packet_id"]) < (a["time_ms"], a["packet_id"])
        for a, b in zip(seq, seq[1:])
    ):
        return None, "lifecycle_time_regression"
    if len({(x["time_ms"], x["packet_id"]) for x in seq}) != len(seq):
        return None, "ambiguous_actor_lifecycle"
    if any(x["time_ms"] == event[0] for x in seq):
        return None, "lifecycle_boundary_same_time"
    prior = [x for x in seq if (x["time_ms"], x["packet_id"]) < event]
    if not prior:
        return None, "no_prior_actor_open"
    active = None
    for boundary in prior:
        if boundary["event"] == "open":
            if active is not None:
                return None, "actor_reopened_without_unique_active_instance"
            active = boundary
        elif boundary["event"] == "close":
            if active is None:
                return None, "actor_close_without_open"
            active = None
    return (active, "active") if active is not None else (None, "actor_closed")


def edge(name, by):
    q = by.get(name, [])
    if not q:
        return {"status": "absent", "value": None, "source_rows": []}
    if len(q) != 1:
        return {"status": "duplicate", "value": None, "source_rows": [x[0] for x in q]}
    try:
        v = typed_ref(q[0][1])
    except IntegrityError:
        raise
    except InputError as e:
        return {
            "status": "invalid",
            "value": None,
            "source_rows": [q[0][0]],
            "error": str(e),
        }
    return {
        "status": "null" if v == 0 else "present",
        "value": v,
        "source_rows": [q[0][0]],
    }


def amount_of(group):
    """The section parser's result as a heal amount: validated only for an
    exact array whose HealTaken equals its delta sum, with no schema error."""
    state = group["section_state"]
    if group["schema_errors"]:
        error = "foreign or invalid schema"
    elif state["status"] == "parentless_rpc":
        error = "missing or duplicate amount parent"
    elif state["status"] != "validated_array":
        error = state["error"]
    elif not state["relation"]["matches"]:
        error = "HealTaken does not equal section delta sum"
    else:
        return {"status": "validated", "heal_taken": state["scalar"],
                "sections": state["sections"], "array_capacity": state["array_capacity"]}
    return {"status": "invalid", "error": error, "heal_taken": None, "sections": []}


def parse_observation(key, items, guid_paths, actors, refs, players, segments, declared):
    group = sections.parse_group(ROUTE, key, items, guid_paths, segments, declared)
    # The section parser keeps a declared extra parameter raw; here it is a changed schema.
    reported = {e["source_row"] for e in group["schema_errors"]}
    group["schema_errors"] += [{"source_row": o, "error": "unknown heal parameter row"} for o, r in items
                               if o not in reported and r["field_name"] not in TOP and not RX.match(r["field_name"] or "")]
    by = collections.defaultdict(list)
    for ordinal, r in items:
        by[r["field_name"]].append((ordinal, r))
    top = {n: edge(n, by) for n in (
        "MulticastNotifyHeal.EventInstigator",
        "MulticastNotifyHeal.EventInstigatorPawn",
        "MulticastNotifyHeal.HealCauser",
    )}
    amount = amount_of(group)
    reasons = set(group["ambiguity_reasons"]) - {"array_validation_failed",
                                                 "scalar_delta_relation_mismatch"}
    if amount["status"] != "validated":
        reasons.add("amount_invalid")
    if group["schema_errors"]:
        reasons.add("foreign_or_invalid_schema")
    event = key[:2]
    causer = top["MulticastNotifyHeal.HealCauser"]
    source = {
        "status": causer["status"],
        "causer": causer,
        "event_instigator": top["MulticastNotifyHeal.EventInstigator"],
        "event_instigator_pawn": top["MulticastNotifyHeal.EventInstigatorPawn"],
        "actor": None,
        "owner": {"status": "not_evaluated"},
        "instigator": {"status": "not_evaluated"},
        "manifest_character_candidates": [],
    }
    if causer["status"] == "present":
        inst, st = active_instance(actors, causer["value"], event)
        source["status"] = st
        source["lifecycle_evidence"] = [
            {
                "physical_row_ordinal": x["_ordinal"],
                "time_ms": x["time_ms"],
                "packet_id": x["packet_id"],
                "channel_index": x["channel_index"],
                "event": x["event"],
                "class_path": x["class_path"],
            }
            for x in actors.get(causer["value"], [])
            if (x["time_ms"], x["packet_id"]) <= event
        ]
        if inst:
            source["actor"] = {
                "actor_net_guid": causer["value"],
                "channel_index": inst["channel_index"],
                "class_path": inst["class_path"],
                "open_source_row": inst["_ordinal"],
            }
            scoped = [
                x
                for x in refs.get(causer["value"], [])
                if x[1]["channel_index"] == inst["channel_index"]
                and x[1]["group_path"] == inst["class_path"]
                and (inst["time_ms"], inst["packet_id"])
                <= (x[1]["time_ms"], x[1]["packet_id"])
                < event
            ]
            source["reference_history"] = [
                sections.raw(row, o, "main_reference_evidence") for o, row in scoped
            ]
            if any(x[1]["time_ms"] == event[0] for x in refs.get(causer["value"], [])):
                source["status"] = "reference_update_same_time"
            else:
                for field in ("Owner", "Instigator"):
                    q = [x for x in scoped if x[1]["field_name"] == field]
                    if not q:
                        source[field.lower()] = {
                            "status": "absent",
                            "value": None,
                            "source_rows": [],
                        }
                        continue
                    p = max((x[1]["time_ms"], x[1]["packet_id"]) for x in q)
                    q = [x for x in q if (x[1]["time_ms"], x[1]["packet_id"]) == p]
                    vals = {typed_ref(row) for _, row in q}
                    source[field.lower()] = {
                        "status": "present" if len(vals) == 1 else "conflict",
                        "value": next(iter(vals)) if len(vals) == 1 else None,
                        "source_rows": [
                            sections.raw(row, o, "main_reference_evidence")
                            for o, row in q
                        ],
                    }
                candidates = {
                    e["value"]
                    for e in (source["owner"], source["instigator"])
                    if e["status"] == "present" and e["value"] in players
                }
                source["manifest_character_candidates"] = sorted(candidates)
                source["status"] = (
                    "corroborated_static_manifest_character"
                    if len(candidates) == 1
                    else (
                        "conflicting_manifest_characters"
                        if len(candidates) > 1
                        else "no_manifest_character_reference"
                    )
                )
    pawn = source["event_instigator_pawn"]
    pawn["static_manifest_character"] = pawn.get("value") in players
    pawn["static_manifest_subject"] = players.get(pawn.get("value"))
    source["event_instigator"]["semantics"] = (
        "PlayerController NetGUID (the pawn's Controller and Owner); does not join "
        "to actors.parquet or net_guids; no heal-credit meaning"
    )
    recipient_instance, recipient_status = active_instance(actors, key[3], event)
    recipient = {
        "actor_net_guid": key[3],
        "lifecycle_status": recipient_status,
        "static_manifest_character": key[3] in players,
        "subject": players.get(key[3]),
    }
    if recipient_instance:
        recipient.update(
            channel_index=recipient_instance["channel_index"],
            class_path=recipient_instance["class_path"],
            open_source_row=recipient_instance["_ordinal"],
        )
    return {
        "population": "main",
        "identity": group["identity"],
        "identity_semantics": "coordinate-grouped observation; not a proven gameplay call",
        "source_rows": group["source_rows"],
        "schema_errors": group["schema_errors"],
        "ambiguity_reasons": sorted(reasons),
        "amount": amount,
        "source_corroboration": source,
        "recipient_corroboration": recipient,
    }


def extract(export):
    inputs = [export / n for n in INPUT_NAMES]
    before = {p.name: sha(p) for p in inputs}
    manifest = json.loads((export / "manifest.json").read_text(encoding="utf-8"))
    declared = declarations(manifest)
    section_declarations = sections.declarations(manifest, (ROUTE,))
    bodies = load_player_bodies(export, manifest)
    players = bodies.subjects
    paths = load_net_guids(export, "path")
    actors = collections.defaultdict(list)
    for o, r in enumerate(pq.read_table(
        export / "actors.parquet",
        columns=["time_ms", "packet_id", "channel_index", "actor_net_guid", "event", "class_path"],
        use_threads=False,
    ).to_pylist()):
        if r["event"] in ("open", "close"):
            r["_ordinal"] = o
            actors[r["actor_net_guid"]].append(r)
    refs = collections.defaultdict(list)
    groups = collections.defaultdict(list)
    selected = []
    segments = collections.Counter()
    last_key = None
    last_selected_ordinal = None
    for o, r in iter_selected_fields(export / "fields.parquet", True):
        n = r["field_name"] or ""
        if n in ("Owner", "Instigator"):
            refs[r["actor_net_guid"]].append((o, r))
        if n.startswith("MulticastNotifyHeal."):
            if (
                n in OPTIONAL
                and r["group_path"] == OUTER_GROUP
                and r["handle"] == 6
                and OPTIONAL[n] not in declared
            ):
                raise IntegrityError("observed optional heal row lacks its declaration")
            selected.append((o, r))
            k = tuple(r[c] for c in sections.COORDINATE_COLUMNS)
            groups[k].append((o, r))
            if (
                k != last_key
                or last_selected_ordinal is None
                or o != last_selected_ordinal + 1
            ):
                segments[k] += 1
            last_key = k
            last_selected_ordinal = o
        else:
            last_key = None
            last_selected_ordinal = o
    cp = []
    for o, r in iter_selected_fields(
        export / "checkpoint_fields.parquet", False, CHECKPOINT_FIELD_COLS
    ):
        record = sections.raw(r, o, "checkpoint")
        try:
            validate_row_schema(r)
            record["schema_status"] = "matches_main_route"
        except InputError as e:
            record["schema_status"] = "foreign_or_invalid"
            record["schema_error"] = str(e)
        cp.append(record)
    observations = [
        parse_observation(k, v, paths, actors, refs, players, segments[k], section_declarations)
        for k, v in sorted(groups.items())
    ]
    valid = [
        x
        for x in observations
        if x["amount"]["status"] == "validated" and not x["ambiguity_reasons"]
    ]
    # Zeros included: an "invalid" edge leaves the amount validated, so only
    # this tally brings it to the summary.
    edge_status = {
        k: dict.fromkeys(EDGE_STATUSES, 0)
        for k in ("causer", "event_instigator", "event_instigator_pawn")
    }
    source_status = dict.fromkeys(SOURCE_STATUSES, 0)
    recipient_status = dict.fromkeys(LIFECYCLE_STATUSES, 0)
    for x in observations:
        for k, tally in edge_status.items():
            tally[x["source_corroboration"][k]["status"]] += 1
        source_status[x["source_corroboration"]["status"]] += 1
        recipient_status[x["recipient_corroboration"]["lifecycle_status"]] += 1
    by_section = collections.Counter()
    by_recipient = collections.Counter()
    for x in valid:
        for s in x["amount"]["sections"]:
            by_section[s["changed_component_path"] or "<unresolved>"] += s["delta_life"]
            by_recipient[str(x["identity"]["actor_net_guid"])] += s["delta_life"]
    after = {p.name: sha(p) for p in inputs}
    if before != after:
        raise IntegrityError("input changed during extraction")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "vrfkit_healing_observations",
        "export_id": export.name,
        "source": str(export.resolve()),
        "source_hashes": before,
        "player_identity": bodies.counts,
        "provenance": {
            "replay_build": manifest.get("replay_build"),
            "input_sha256_before": before,
            "input_sha256_after": after,
            "implementation_sha256": {p.name: sha(p) for p in SOURCES},
        },
        "observations": observations,
        "checkpoint_observations": cp,
        "summaries": {
            "population": "main validated unambiguous serialized HealTaken/DeltaLife observations; no effective-HP or player-credit semantics",
            "validated_observations": len(valid),
            "serialized_heal_amount_sum_by_recipient_actor_net_guid": dict(
                by_recipient
            ),
            "serialized_heal_amount_sum_by_section_path": dict(by_section),
        },
        "counts": {
            "main_coordinate_groups": len(observations),
            "main_selected_rows": len(selected),
            "checkpoint_selected_rows": len(cp),
            "amount_validated": sum(
                x["amount"]["status"] == "validated" for x in observations
            ),
            "amount_invalid": sum(
                x["amount"]["status"] != "validated" for x in observations
            ),
            "ambiguous_groups": sum(bool(x["ambiguity_reasons"]) for x in observations),
            "source_edge_status": edge_status,
            "source_status": source_status,
            "recipient_lifecycle_status": recipient_status,
        },
    }


def summary(d, out):
    yield f"wrote {out} ({d['counts']['main_coordinate_groups']} observations)"
    for key in ("source_status", "recipient_lifecycle_status", "source_edge_status"):
        yield f"  {key}: {json.dumps(d['counts'][key], sort_keys=True)}"


def main(argv=None):
    return run_json_cli(__doc__, extract, summary, argv, sources=SOURCES,
                        indent=2, sort_keys=True, allow_nan=False)


if __name__ == "__main__":
    raise SystemExit(main())
