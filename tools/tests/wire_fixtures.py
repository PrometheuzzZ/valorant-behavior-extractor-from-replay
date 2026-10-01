"""Wire and Parquet builders shared by the extractor tests.

Written from the format, not from the tools: a fixture built here is the
evidence a reader is checked against, so nothing here imports tools/.
"""
from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq


def packed(*values: int) -> bytes:
    """UE IntPacked bytes for each value: 7 bits a byte, low bit = more follow."""
    out = bytearray()
    for value in values:
        while True:
            rest = value >> 7
            out.append(((value & 127) << 1) | bool(rest))
            if not rest:
                break
            value = rest
    return bytes(out)


class BitWriter:
    """LSB-first bit sink producing (bytes, bit_count) like an exported row."""

    def __init__(self):
        self.bits = []

    def __len__(self):
        return len(self.bits)

    def write(self, value, width):
        self.bits.extend((value >> i) & 1 for i in range(width))
        return self

    def packed(self, value):
        for byte in packed(value):
            self.write(byte, 8)
        return self

    def raw(self, payload: bytes, width: int):
        """The first `width` bits of `payload`, LSB first."""
        self.bits.extend((payload[i // 8] >> (i % 8)) & 1 for i in range(width))
        return self

    def extend(self, other):
        self.bits.extend(other.bits)
        return self

    def to_bytes(self):
        out = bytearray((len(self.bits) + 7) // 8)
        for i, bit in enumerate(self.bits):
            out[i // 8] |= bit << (i % 8)
        return bytes(out), len(self.bits)


def pack_bits(*fields: tuple[int, int]) -> tuple[bytes, int]:
    """(value, width) pairs, LSB first, as (bytes, bit_count)."""
    out = BitWriter()
    for value, width in fields:
        out.write(value, width)
    return out.to_bytes()


def array(elements) -> tuple[bytes, int]:
    """A replicated dynamic array as (bytes, bit_count).

    Capacity (highest element index + 1), then per `(index, fields)` element
    its index + 1 and one `(handle + 1, width, payload)` leaf per field, each
    list closed by 0.
    """
    out = BitWriter().packed(max((index for index, _ in elements), default=-1) + 1)
    for index, fields in elements:
        out.packed(index + 1)
        for handle, width, raw in fields:
            out.packed(handle + 1).packed(width).raw(raw, width)
        out.packed(0)
    return out.packed(0).to_bytes()


#: fields.parquet's columns as the extractors read them, names as plain strings.
FIELD_SCHEMA = pa.schema([
    ("time_ms", pa.uint32()), ("packet_id", pa.uint32()), ("channel_index", pa.uint32()),
    ("actor_net_guid", pa.uint32()), ("object_net_guid", pa.uint32()),
    ("group_path", pa.string()), ("handle", pa.uint32()), ("field_name", pa.string()),
    ("compatible_checksum", pa.uint32()), ("bit_count", pa.uint32()), ("raw_bits", pa.binary()),
    ("value_i64", pa.int64()), ("value_f64", pa.float64()), ("value_bool", pa.bool_()),
    ("value_str", pa.string()),
])
CHECKPOINT_FIELD_SCHEMA = pa.schema(
    [("checkpoint_index", pa.uint32()), ("checkpoint_id", pa.string()), *FIELD_SCHEMA]
)
_CP = ("checkpoint_index", pa.uint32())
#: Export tables the kill extractors open that these fixtures leave empty.
EMPTY_TABLE_SCHEMAS = {
    "checkpoint_fields": CHECKPOINT_FIELD_SCHEMA,
    "net_guids": pa.schema([("net_guid", pa.uint32())]),
    "checkpoint_actors": pa.schema([_CP, ("actor_net_guid", pa.uint32())]),
    "checkpoint_net_guids": pa.schema([_CP, ("net_guid", pa.uint32())]),
    "checkpoint_export_groups": pa.schema([_CP, ("ordinal", pa.uint32()), ("group_path", pa.string())]),
    "checkpoint_export_fields": pa.schema([_CP, ("group_ordinal", pa.uint32()), ("handle", pa.uint32()),
                                           ("rendered_name", pa.string()),
                                           ("compatible_checksum", pa.uint32())]),
}


def write_empty_tables(root) -> None:
    for name, schema in EMPTY_TABLE_SCHEMAS.items():
        pq.write_table(pa.Table.from_pylist([], schema=schema), root / f"{name}.parquet")


def field_row(defaults=(), **kw) -> dict:
    """A fields.parquet row: every column null unless `defaults` or `kw` sets it."""
    return {**dict.fromkeys(FIELD_SCHEMA.names), **dict(defaults), **kw}
