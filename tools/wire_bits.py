"""Bit and Parquet row readers shared by the research extractors."""
from __future__ import annotations

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

MAX_ELEMENTS = 4096
MAX_FIELDS = 128


class InputError(ValueError):
    pass


class WireError(InputError):
    """A window that cannot be decoded exactly; the message is the reason."""


class Bits:
    """LSB-first reader over a window, bounded to [pos, end). The class
    attributes are the rejection reasons a tool records; subclasses rename them."""

    __slots__ = ("raw", "pos", "end")
    TRUNCATED = "truncated"
    OVERRUN = "payload_overrun"
    PACKED_OVERFLOW = "packed_overflow"
    PACKED_UNTERMINATED = "packed_unterminated"

    def __init__(self, raw: bytes, bit_count: int):
        if (not isinstance(raw, bytes) or type(bit_count) is not int or bit_count < 0
                or len(raw) != (bit_count + 7) // 8):
            raise WireError("invalid_window")
        self.raw, self.pos, self.end = raw, 0, bit_count

    def remaining(self) -> int:
        return self.end - self.pos

    def _next(self, width: int) -> int:
        start, shift = divmod(self.pos, 8)
        value = int.from_bytes(self.raw[start:(self.pos + width + 7) // 8], "little")
        self.pos += width
        return (value >> shift) & ((1 << width) - 1)

    def read(self, width: int) -> int:
        if width < 0 or width > 64 or width > self.remaining():
            raise WireError(self.TRUNCATED)
        return self._next(width)

    def take(self, width: int) -> "Bits":
        """A reader over exactly the next `width` bits, which this one skips."""
        if width < 0 or width > self.remaining():
            raise WireError(self.OVERRUN)
        sub = object.__new__(type(self))
        sub.raw, sub.pos, sub.end = self.raw, self.pos, self.pos + width
        self.pos += width
        return sub

    def take_bytes(self, width: int) -> bytes:
        """The next `width` bits as bytes, LSB first from bit 0."""
        if width < 0 or width > self.remaining():
            raise WireError(self.OVERRUN)
        return self._next(width).to_bytes((width + 7) // 8, "little")

    def i32(self) -> int:
        value = self.read(32)
        return value - (1 << 32) if value >= 1 << 31 else value

    def packed(self) -> int:
        """Unreal SerializeIntPacked: 7 value bits and a continue bit per byte."""
        value = 0
        for index in range(5):
            byte = self.read(8)
            if index == 4 and byte >> 1 > 15:
                raise WireError(self.PACKED_OVERFLOW)
            value |= (byte >> 1) << (7 * index)
            if not byte & 1:
                return value
        raise WireError(self.PACKED_UNTERMINATED)

    def serialize_int(self, value_max: int) -> int:
        """Unreal FBitReader::SerializeInt: bits LSB first while
        value + mask < value_max, so the width depends on the bits read."""
        if value_max < 2:
            raise WireError("serialize_int_max")
        value, mask = 0, 1
        while value + mask < value_max:
            if self.read(1):
                value |= mask
            mask <<= 1
        return value


class ArrayBits(Bits):
    """The reasons the replicated-array readers report."""

    TRUNCATED = "truncated IntPacked"
    OVERRUN = "invalid leaf width"
    PACKED_OVERFLOW = "IntPacked exceeds u32"
    PACKED_UNTERMINATED = "unterminated IntPacked"


def fastarray_header(bits: Bits):
    """(array key, base key, deleted item IDs, changed item count) after the
    FastArray support bit; the rest of `bits` holds the changed items."""
    if bits.read(1) != 1:
        raise WireError("unsupported_support_bit")
    array_key, base_key, deletes, changed = [bits.i32() for _ in range(4)]
    if min(deletes, changed) < 0:
        raise WireError("negative_count")
    # A deleted ID is 32 bits; a changed item at least 32 + 8.
    if deletes * 32 + changed * 40 > bits.remaining():
        raise WireError("count_bounds")
    return array_key, base_key, [bits.i32() for _ in range(deletes)], changed


def parse_array(raw, bit_count, allowed=None):
    """A replicated array window as (capacity, elements, [(index, handle,
    width, payload)]); every bit must be consumed."""
    if raw is None or type(bit_count) is not int or bit_count <= 0 or len(raw) != (bit_count + 7) // 8:
        raise InputError("invalid raw array window")
    bits = ArrayBits(raw, bit_count)
    capacity = bits.packed()
    if capacity > MAX_ELEMENTS:
        raise InputError("array capacity exceeds limit")
    leaves, indices = [], set()
    while True:
        encoded = bits.packed()
        if encoded == 0:
            if bits.remaining():
                raise InputError("array has residual root bits")
            return capacity, len(indices), leaves
        index = encoded - 1
        if index >= capacity or len(indices) >= MAX_ELEMENTS or index in indices:
            raise InputError("array index is duplicate or exceeds bound")
        indices.add(index)
        handles = set()
        while encoded := bits.packed():
            if len(handles) >= MAX_FIELDS:
                raise InputError("array field count exceeds limit")
            handle = encoded - 1
            if handle in handles:
                raise InputError(f"duplicate array handle {handle}")
            handles.add(handle)
            if allowed is not None and handle not in allowed:
                raise InputError(f"unexpected nested handle {handle}")
            width = bits.packed()
            if width <= 0:
                raise InputError("invalid leaf width")
            leaves.append((index, handle, width, bits.take_bytes(width)))


def exact_ref(raw, width):
    """A packed ObjectNetGuid that fills its whole leaf."""
    bits = ArrayBits(raw, width)
    value = bits.packed()
    if bits.remaining():
        raise InputError("ObjectNetGuid did not consume its leaf")
    return value


def weapon_theme(raw, width):
    """An FString after one leading 1 bit: i32 length (negative for UTF-16),
    then the terminated text."""
    bits = ArrayBits(raw, width)
    if width < 33 or bits.read(1) != 1:
        raise InputError("WeaponTheme framing differs")
    length = bits.i32()
    units, unit_bits = abs(length), 16 if length < 0 else 8
    if units * (unit_bits // 8) > 64 * 1024 or 33 + units * unit_bits != width:
        raise InputError("WeaponTheme length/framing differs")
    if units == 0:
        return ""
    payload = bits.take_bytes(units * unit_bits)
    if length < 0:
        if payload[-2:] != b"\0\0":
            raise InputError("WeaponTheme lacks UTF-16 terminator")
        return payload[:-2].decode("utf-16-le")
    if payload[-1:] != b"\0":
        raise InputError("WeaponTheme lacks byte terminator")
    return payload[:-1].decode("utf-8")


def load_net_guids(export, *columns):
    """{net_guid: its `columns`' values, one value for one column} from
    net_guids.parquet. The writer emits each GUID once, so a repeat raises."""
    table = pq.read_table(export / "net_guids.parquet", columns=["net_guid", *columns], use_threads=False)
    guids = table.column("net_guid").to_pylist()
    values = [table.column(column).to_pylist() for column in columns]
    out = dict(zip(guids, values[0] if len(columns) == 1 else zip(*values)))
    if len(out) != len(guids):
        raise InputError("net_guids.parquet repeats a NetGUID")
    return out


def text(batch, name):
    """A batch column as strings, for Arrow-side masks."""
    return pc.cast(batch.column(name), pa.string())


def iter_selected(path, columns, mask):
    """(physical row ordinal, row) for each row of a Parquet file where
    `mask(batch)` holds; the filter runs in Arrow, before any row dict."""
    ordinal = 0
    for batch in pq.ParquetFile(path).iter_batches(batch_size=65536, columns=columns, use_threads=False):
        positions = pc.indices_nonzero(pc.fill_null(mask(batch), False))
        for index, row in zip(positions.to_pylist(), batch.take(positions).to_pylist()):
            yield ordinal + index, row
        ordinal += batch.num_rows
