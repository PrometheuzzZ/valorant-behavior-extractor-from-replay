"""Generate exact group/name/checksum field types from reviewed wire evidence.

Each entry is one exact (group, field, checksum) identity and never propagates
to another group or name. Every type name needs an independent decoder in
validate_type_evidence.py. A RepMovement type carries its rotator width, since
`ReplicatedMovement` has one checksum for both, and a measured
`location_quantization`, since exact consumption cannot catch a wrong level:
join the first update to the actors.parquet spawn (docs/DATA.md,
"`ReplicatedMovement.location` is world units, at a per-class level").

`--check` fails when scoped_types.rs or the fixture's own layout
(`dump_evidence`) is stale; without it, both are rewritten.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from atomic_io import atomic_write_text

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "tools/fixtures/scoped_type_evidence.json"
OUTPUT = ROOT / "crates/vrf-decode/src/scoped_types.rs"

#: Fixture type name -> (its `FieldType` expression,); LEVEL is the entry's
#: location_quantization.
TYPES = {
    "Byte": ("FieldType::Byte",),
    "Bool": ("FieldType::Bool",),
    "Int32": ("FieldType::Int32",),
    "UInt32": ("FieldType::UInt32",),
    "Float": ("FieldType::Float",),
    "Double": ("FieldType::Double",),
    "VectorDouble": ("FieldType::VectorDouble",),
    "FString": ("FieldType::FString",),
    "FTextTree": ("FieldType::FTextTree",),
    "ObjectNetGuid": ("FieldType::ObjectNetGuid",),
    "EnumRemainingBits": ("FieldType::EnumRemainingBits",),
    "RotationShort": ("FieldType::RotationShort",),
    "VectorNetQuantize100": ("FieldType::VectorNetQuantize { scale: 100 }",),
    "RepMovementByte": ("FieldType::RepMovement { rotation: RotatorQuantization::"
                        "ByteComponents, location: VectorQuantization::LEVEL }",),
    "RepMovementShort": ("FieldType::RepMovement { rotation: RotatorQuantization::"
                         "ShortComponents, location: VectorQuantization::LEVEL }",),
}
#: Types naming `RotatorQuantization` and `VectorQuantization`; their import is
#: emitted only when used, as an unused `use` fails clippy's `-D warnings`.
NEEDS_ROTATOR_QUANTIZATION = {"RepMovementByte", "RepMovementShort"}
LOCATION_LEVELS = {"RoundWholeNumber", "RoundOneDecimal", "RoundTwoDecimals"}


def load(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("expected scoped evidence schema_version 1")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("expected non-empty entries")
    seen = set()
    for entry in entries:
        for key in ("group", "field", "evidence"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError(f"missing {key}")
        if not entry["group"].isascii() or not entry["field"].isascii():
            raise ValueError("Rust field identities must be ASCII")
        checksum = entry.get("checksum")
        if type(checksum) is not int or not 0 < checksum <= 0xFFFFFFFF:
            raise ValueError("checksum must be a nonzero u32")
        if entry.get("type") not in TYPES:
            raise ValueError(f"unsupported scoped type {entry.get('type')!r}")
        level = entry.get("location_quantization")
        if entry["type"] in NEEDS_ROTATOR_QUANTIZATION:
            if level not in LOCATION_LEVELS:
                raise ValueError(f"{entry['type']} needs a measured location_quantization, "
                                 f"one of {sorted(LOCATION_LEVELS)}")
        elif level is not None:
            raise ValueError("location_quantization applies only to RepMovement types")
        builds = entry.get("observed_builds")
        if not isinstance(builds, list) or not builds or any(not isinstance(b, str) or not b for b in builds):
            raise ValueError("observed_builds must record the replay evidence scope")
        key = (entry["field"], entry["group"], checksum)
        if key in seen:
            raise ValueError(f"duplicate scoped identity: {key}")
        seen.add(key)
    return sorted(entries, key=lambda e: (e["field"], e["group"], e["checksum"]))


def dump_evidence(document) -> str:
    """The fixture's layout: `json.dumps(indent=2)` with each list of scalars on
    one line. Only a structural newline precedes `]`, so the pattern cannot
    reach into a string or a list holding an object."""
    return re.sub(r"\[\n\s+([^\[\]{}]*?)\n\s+\]",
                  lambda m: "[" + ", ".join(v.strip() for v in m[1].split(",\n")) + "]",
                  json.dumps(document, indent=2)) + "\n"


def render(entries: list[dict]) -> str:
    imports = ["use crate::decode::FieldType;"]
    if any(entry["type"] in NEEDS_ROTATOR_QUANTIZATION for entry in entries):
        imports.append("use crate::types::{RotatorQuantization, VectorQuantization};")
    lines = [
        "//! Generated by `tools/generate_scoped_types.py`; do not edit by hand.",
        "//! Exact field identities whose names alone are ambiguous; the evidence is in",
        "//! tools/fixtures/scoped_type_evidence.json.",
        "", *imports, "",
        "/// Sorted by (field name, group path, compatible checksum).",
        "#[rustfmt::skip]",
        f"pub(crate) static SCOPED_TYPES: [(&str, &str, u32, FieldType); {len(entries)}] = [",
    ]
    for entry in entries:
        (expression,) = TYPES[entry["type"]]
        if entry["type"] in NEEDS_ROTATOR_QUANTIZATION:
            expression = expression.replace("LEVEL", entry["location_quantization"])
        lines.append(f"    ({json.dumps(entry['field'])}, {json.dumps(entry['group'])}, "
                     f"{entry['checksum']}, {expression}),")
    return "\n".join(lines + ["];", ""])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        entries = load(args.evidence)
        wanted = {args.output: render(entries), args.evidence: dump_evidence(
            json.loads(args.evidence.read_text(encoding="utf-8")))}
        if args.check:
            stale = [path.name for path, text in wanted.items()
                     if not path.is_file() or path.read_text(encoding="utf-8") != text]
            if stale:
                print(f"ERROR: {', '.join(stale)} differ from the reviewed evidence; "
                      f"rerun without --check", file=sys.stderr)
                return 1
        else:
            for path, text in wanted.items():
                atomic_write_text(path, text)
        print(f"OK: {len(entries)} scoped field types")
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
