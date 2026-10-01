"""Capture independent replay-transform vectors from pinned original PE files.

Requires optional analysis dependencies: pefile and unicorn. The game is never
launched: Unicorn emulates only the recovered reader and its bit-copy helper.
No Windows APIs or game services are provided. Input hashes and function RVAs
come from fixtures/native_transform_readers.json, not from transform Rust code.

Usage:
    python tools/capture_native_transforms.py --binaries ARCHIVE --recovered-binaries RECOVERED --check

Both roots hold <build>/ShooterGame/Binaries/Win64/VALORANT-Win64-Shipping.exe;
RECOVERED is recover_native_binaries.py's output, read for the seven builds the
catalog marks `recovered` (11.06-12.00). Without --check, write the Rust
fixture after every build succeeds.
"""

from __future__ import annotations

import argparse
import json
import random
import struct
from pathlib import Path

from atomic_io import atomic_write_text
from recover_native_binaries import BINARY_PATH, checked_input

REPO = Path(__file__).resolve().parent.parent
CATALOG = REPO / "tools/fixtures/native_transform_readers.json"
OUTPUT = REPO / "crates/vrf-transform/tests/data/native_vectors.rs"


def cases():
    # Same input and staging boundaries as the golden vectors
    # (crates/vrf-transform/tests/data/golden_vectors.rs).
    payload = bytes.fromhex(
        "BFDF6F9EA1F27BA00000C66EAFAF2E0000339C0DD34B0C45C48063038003562A43C0C949"
    )
    for bits in (0, 1, 7, 8, 31, 32, 63, 64, 65, 287, 288):
        yield payload, bits, bits ^ 2
    rng = random.Random(20260924)
    for seed in (0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF, 0xDEADBEEF):
        for bits in (8, 32, 64, 96, 255, 512):
            yield rng.randbytes((bits + 7) // 8), bits, seed
    for _ in range(32):
        bits = rng.randrange(1, 513)
        yield rng.randbytes((bits + 7) // 8), bits, rng.randrange(2**32)


def capture(exe: Path, entry: dict):
    import pefile
    from unicorn import Uc, UC_ARCH_X86, UC_MODE_64
    from unicorn.x86_const import (
        UC_X86_REG_RCX, UC_X86_REG_RDX, UC_X86_REG_R8, UC_X86_REG_RSP,
        UC_X86_REG_RBP, UC_X86_REG_RIP,
    )

    data = checked_input(exe, entry["exe_sha256"])
    pe = pefile.PE(data=data, fast_load=True)
    if pe.FILE_HEADER.Machine != 0x8664:
        raise ValueError("expected x86-64 PE")
    base = pe.OPTIONAL_HEADER.ImageBase
    rva = int(entry["function_rva"], 0)
    if not 0 <= rva < pe.OPTIONAL_HEADER.SizeOfImage:
        raise ValueError("reader RVA is outside the PE image")
    vm = Uc(UC_ARCH_X86, UC_MODE_64)
    vm.mem_map(base, (pe.OPTIONAL_HEADER.SizeOfImage + 4095) & ~4095)
    vm.mem_write(base, pe.get_memory_mapped_image())
    pe.close()
    vm.mem_map(0x100000, 0x200000)
    reader, source, dest, stack, stop = 0x101000, 0x102000, 0x103000, 0x2F0008, 0x100000
    rows = []
    for payload, bits, seed in cases():
        vm.mem_write(reader, bytes(0x100))
        vm.mem_write(source, payload + bytes(128))
        vm.mem_write(dest, bytes(128))
        vm.mem_write(stack, struct.pack("<Q", stop))
        for offset, value in ((0x98, source), (0xA8, len(payload) * 8), (0xB0, 0)):
            vm.mem_write(reader + offset, struct.pack("<Q", value))
        vm.mem_write(reader + 0xB8, struct.pack("<I", seed))
        for reg, value in (
            (UC_X86_REG_RCX, reader), (UC_X86_REG_RDX, dest),
            (UC_X86_REG_R8, bits), (UC_X86_REG_RSP, stack), (UC_X86_REG_RBP, 0),
        ):
            vm.reg_write(reg, value)
        vm.emu_start(base + rva, stop, timeout=5_000_000, count=1_000_000)
        if vm.reg_read(UC_X86_REG_RIP) != stop:
            raise RuntimeError("native reader exceeded instruction/time limit")
        position = struct.unpack("<Q", vm.mem_read(reader + 0xB0, 8))[0]
        if position != bits:
            raise RuntimeError(f"native reader position {position} != {bits}")
        result = bytes(vm.mem_read(dest, (bits + 7) // 8))
        rows.append((bits, seed, payload.hex().upper(), result.hex().upper()))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binaries", type=Path, required=True)
    parser.add_argument("--recovered-binaries", type=Path,
                        help="separate recovered-image root for protected builds")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    protected = [e["build"] for e in catalog if e.get("recovered")]
    if protected and args.recovered_binaries is None:
        parser.error(f"builds {', '.join(protected)} need --recovered-binaries")
    lines = [
        "// Expected output captured from original x86-64 reader functions.",
        "// Regenerate with tools/capture_native_transforms.py; see docs/LEGACY_BUILD_SUPPORT.md.",
        "// Tuple: branch, bit count, seed, input hex, expected output hex.",
        "pub const NATIVE_VECTORS: &[(&str, usize, u32, &str, &str)] = &[",
    ]
    for entry in catalog:
        build = entry["build"]
        root = args.recovered_binaries if entry.get("recovered") else args.binaries
        rows = capture(root / build / BINARY_PATH, entry)
        lines.extend([
            f'    // {build}: executable SHA-256 {entry["exe_sha256"]}',
            f'    // Reader RVA {entry["function_rva"]}.',
        ])
        for bits, seed, payload, expected in rows:
            lines.append(
                f'    ("++Ares-Core+release-{build}", {bits}, {seed}, "{payload}", "{expected}"),'
            )
        print(f"{build}: captured {len(rows)} native cases", flush=True)
    text = "\n".join(lines + ["];", ""])
    if args.check:
        if OUTPUT.read_text(encoding="utf-8") != text:
            raise SystemExit("native transform fixture differs from original machine code")
        print("PASS: committed fixture matches original machine code")
    else:
        atomic_write_text(OUTPUT, text)


if __name__ == "__main__":
    main()
