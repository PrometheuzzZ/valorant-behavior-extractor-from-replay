"""Recover archived 11.06--12.00 code for native replay-reader verification.

Inputs are SHA-256 pinned EXE/stub pairs. Only .text is recovered; the output
retains archived imports and metadata and is an analysis input, not a runnable
installation. The page fragments execute in Unicorn with no OS API hooks.
Requires optional pefile, unicorn and numpy packages. Never writes input files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import math
from pathlib import Path
import re
import struct

CATALOG = Path(__file__).with_name("fixtures") / "native_recovery.json"
BINARY_PATH = Path("ShooterGame/Binaries/Win64/VALORANT-Win64-Shipping.exe")
M = 0xffffffff


def rol(value, count):
    value &= M
    return ((value << count) | (value >> (32 - count))) & M


def rol8(value, count):
    return ((value << count) | (value >> (8 - count))) & 255


# The 11.06 native bulk block, RVAs 0x1c2afe8..0x1c2b72f. This deliberately
# preserves the measured round dependencies and decompiler temporary names
# for auditing against that block. It is not standard Salsa/ChaCha.
def bulk_block(key, nonce, offset):
    p = bytearray(64)
    for i, v in [(0, 0xdbba7da9), (20, 0x43bb2815), (40, 0x9b790f44), (60, 0x60d7d951)]:
        struct.pack_into('<I', p, i, v)
    p[4:20] = key[:16]
    p[44:60] = key[16:]
    p[24:32] = nonce
    p[32:36] = bytes([offset >> 14 & 255, offset >> 30 & 255, offset >> 6 & 255, offset >> 22 & 255])
    s = [p[i + 2] | p[i] << 8 | p[i + 3] << 16 | p[i + 1] << 24 for i in range(0, 64, 4)]
    x = s.copy()
    aa0 = s[3]
    v5 = s[6]
    v13 = s[1]
    v16 = s[2]
    v1 = s[7]
    a98 = s[9]
    a90 = s[13]
    v17 = s[14]
    v7 = s[12]
    v14 = s[4]
    v19 = s[5]
    v8 = s[11]
    for _ in range(10):
        v17 = rol(v7 + v17, 18) ^ v17
        v7 = aa0 + v17 & M
        v17 = rol(v7, 23) ^ v17
        v7 = rol(v17 + aa0, 18) ^ v17
        v18 = rol(v14 + v5, 18) ^ v5
        v17 = v18 + v13 & M
        v18 = rol(v17, 23) ^ v18
        v14 = v18 + v13 & M
        v10 = rol(v19 + v1, 18) ^ v1
        v17 = v10 + a98 & M
        v10 = rol(v17, 23) ^ v10
        v17 = a98 + v10 & M
        v15 = rol(v8 + v16, 18) ^ v16
        v8 = a90
        v19 = v15 + v8 & M
        v15 = rol(v19, 23) ^ v15
        v17 = (rol(v17, 18) ^ v10) + v8 & M
        v8 = rol(v17, 18) ^ v8
        v17 = v8 + v15 & M
        v8 = rol(v17, 23) ^ v8
        v19 = rol(v8 + v15, 18) ^ v8
        v17 = rol(v16 + v13, 18) ^ v13
        v16 = v17 + v5 & M
        v17 = rol(v16, 23) ^ v17
        v16 = rol(v17 + v5, 18) ^ v17
        v14 = (rol(v14, 18) ^ v18) + v1 & M
        v18 = rol(v14, 18) ^ v1
        v14 = v18 + v7 & M
        v18 = rol(v14, 23) ^ v18
        v14 = rol(v18 + v7, 18) ^ v18
    v13 = rol(v10 + aa0, 18) ^ aa0
    v13 = rol(v13 + a98, 23) ^ v13
    v5 = a98 + v13 & M
    for i, v in [(14, v17), (12, v7), (4, v14), (5, v19), (2, v16),
                 (11, v8), (10, v15), (15, v13), (8, rol(v5, 18) ^ v13), (0, v18)]:
        x[i] = v
    z = bytearray()
    for a, b in zip(s, x):
        w = a + b & M
        z.extend([w >> 8 & 255, w >> 24 & 255, w & 255, w >> 16 & 255])
    return bytes(z)


def checked_input(path, expected_hash):
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise ValueError(f"input SHA-256 mismatch: {path}")
    return data


def unpack_stub(data):
    import pefile

    pe = pefile.PE(data=data, fast_load=True)
    if pe.FILE_HEADER.Machine != 0x8664 or pe.OPTIONAL_HEADER.SizeOfImage > 2**28:
        raise ValueError("expected bounded x64 stub image")
    sections = [s for s in pe.sections if not s.SizeOfRawData
                and not s.PointerToRawData and not s.Characteristics & 0x80]
    if len(sections) != 7:
        raise ValueError("expected seven compressed stub sections")
    # The LZMA section table is found by its exact destination-RVA sequence.
    pattern = b"".join(b"...." + re.escape(struct.pack("<I", s.VirtualAddress))
                       for s in sections)
    matches = list(re.finditer(pattern, data, re.DOTALL))
    if len(matches) != 1 or matches[0].start() < 8:
        raise ValueError("ambiguous or absent LZMA section table")
    offset = matches[0].start() - 8
    prop_rva, prop_len = struct.unpack_from("<II", data, offset)
    if prop_len != 5:
        raise ValueError("unexpected LZMA property length")
    props = pe.get_data(prop_rva, 5)
    lc, lp, pb = props[0] % 9, props[0] // 9 % 5, props[0] // 45
    dictionary = int.from_bytes(props[1:], "little")
    if lc + lp > 4 or pb > 4 or not 4096 <= dictionary <= 2**27:
        raise ValueError("LZMA properties exceed bounds")
    image = bytearray(pe.get_memory_mapped_image())
    image.extend(bytes(pe.OPTIONAL_HEADER.SizeOfImage - len(image)))
    for index, section in enumerate(sections, 1):
        source, dest = struct.unpack_from("<II", data, offset + index * 8)
        bound = (section.Misc_VirtualSize + 4095) & ~4095
        if dest != section.VirtualAddress or dest + bound > len(image):
            raise ValueError("invalid decompressed section bounds")
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
            "id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb,
            "dict_size": dictionary,
        }])
        output = decoder.decompress(data[pe.get_offset_from_rva(source):],
                                    max_length=bound + 1)
        if not decoder.eof or len(output) > bound:
            raise ValueError("incomplete or oversized compressed section")
        image[dest:dest + len(output)] = output
    base = pe.OPTIONAL_HEADER.ImageBase
    pe.close()
    return bytes(image), base


def bulk_decode(data, source):
    import numpy as np

    if len(source) != 32 or len(data) % 64:
        raise ValueError("bulk input must contain complete blocks and a 32-byte key source")
    key = bytes(x ^ i for i, x in enumerate(source))
    initial = bytes(x ^ (((i + 1) << i) & 255) for i, x in enumerate(source[:8]))
    header = bytes([0] + [rol8(initial[i], i) ^ initial[i] for i in range(1, 8)])
    nonce = bytes(rol8(x, (i + 1) % 8) ^ x ^ rol8(x, (7 - i) % 8)
                  for i, x in enumerate(header))
    stream = bulk_block(key, nonce, 0)
    # The counter enters only state word 8, added after the rounds: the other
    # 60 output bytes are constant for this key/nonce (native_bulk_vectors.json).
    initial_word = int.from_bytes(bytes([stream[34], stream[32], stream[35], stream[33]]), "little")
    blocks = np.frombuffer(data, dtype=np.uint8).reshape(-1, 64)
    result = np.bitwise_xor(blocks, np.frombuffer(stream, dtype=np.uint8))
    words = np.arange(len(blocks), dtype=np.uint32) + np.uint32(initial_word)
    for column, shift in ((32, 8), (33, 24), (34, 0), (35, 16)):
        result[:, column] = blocks[:, column] ^ ((words >> shift) & 255).astype(np.uint8)
    return bytearray(result.tobytes())


class PageCipher:
    def __init__(self, image, base, parameters):
        from unicorn import Uc, UC_ARCH_X86, UC_MODE_64
        from unicorn import x86_const as regs

        self.image, self.base, self.p = image, base, parameters
        self.regs = regs
        self.vm = Uc(UC_ARCH_X86, UC_MODE_64)
        self.vm.mem_map(base, (len(image) + 4095) & ~4095)
        self.vm.mem_write(base, image)
        self.stack, self.bp, self.page = 0x50000000, 0x50010000, 0x60000000
        self.vm.mem_map(self.stack, 0x20000)
        self.vm.mem_map(self.page, 4096)
        self.targets = [parameters["tablebase"] + value for value in
                        struct.unpack_from("<97i", image, parameters["table"])]
        if len(set(self.targets)) != 97 or any(not 0 <= v < len(image) for v in self.targets):
            raise ValueError("invalid native page dispatch table")

    def stream(self, index):
        p, vm, regs = self.p, self.vm, self.regs
        start = p["source"] + (index % p["rows"]) * p["stride"]
        source = self.image[start:start + 32]
        if len(source) != 32:
            raise ValueError("page key source outside stub")
        key = bytes(x ^ i for i, x in enumerate(source))
        nonce = bytes(x ^ (((i + 1) << i) & 255) for i, x in enumerate(source[:8]))
        obj = self.base + p["obj"]
        vm.mem_write(obj + 0x118, key + nonce)
        vm.mem_write(self.page, bytes(4096))
        vm.mem_write(self.stack, bytes(0x20000))
        for name in ("RAX", "RBX", "RCX", "RDX", "RSI", "RDI", "R8", "R9",
                     "R10", "R11", "R12", "R13", "R14", "R15"):
            vm.reg_write(getattr(regs, "UC_X86_REG_" + name), 0)
        for name, value in (("RBP", self.bp), ("RSP", self.stack + 0xf000),
                            ("R8", 4096), ("RDI", obj)):
            vm.reg_write(getattr(regs, "UC_X86_REG_" + name), value)
        for slot, value in (("obj_slot", 0), ("key_slot", obj + 0x118),
                            ("page_slot", self.page), ("len_slot", 4096)):
            vm.mem_write(self.bp + p[slot], struct.pack("<Q", value))
        end = self.base + p["stop"]
        vm.emu_start(self.base + self.targets[index % 97], end,
                     timeout=5_000_000, count=5_000_000)
        if vm.reg_read(regs.UC_X86_REG_RIP) != end:
            raise RuntimeError("native page fragment exceeded its execution bound")
        return bytes(vm.mem_read(self.page, 4096))


def recover(binaries, output, entry):
    import numpy as np
    import pefile

    build = entry["build"]
    exe_path = binaries / build / BINARY_PATH
    destination = output / build / BINARY_PATH
    if destination.resolve() == exe_path.resolve():
        raise ValueError("output must differ from the archived input")
    if destination.exists():
        checked_input(destination, entry["recovered_sha256"])
        print(f"{build}: existing recovered output verified", flush=True)
        return
    data = checked_input(exe_path, entry["exe_sha256"])
    stub, base = unpack_stub(checked_input(exe_path.with_name("stub.dll"), entry["stub_sha256"]))
    pe = pefile.PE(data=data, fast_load=True)
    sections = [s for s in pe.sections if s.Name.rstrip(b"\0") == b".text"]
    if len(sections) != 1:
        raise ValueError("expected one code section")
    section = sections[0]
    if section.SizeOfRawData % 4096:
        raise ValueError("code section does not contain complete pages")
    start = int(entry["key_source_rva"], 0)
    code = bulk_decode(section.get_data(), stub[start:start + 32])
    parameters = {k: int(v, 0) if isinstance(v, str) else v
                  for k, v in entry["page_cipher"].items()}
    cipher = PageCipher(stub, base, parameters)
    period = math.lcm(parameters["rows"], 97)
    pages = np.frombuffer(code, dtype=np.uint8).reshape(-1, 4096)
    first_page = section.VirtualAddress // 4096
    for index in range(period):
        stream = np.frombuffer(cipher.stream(index), dtype=np.uint8)
        first = (index - first_page) % period
        pages[first::period] ^= stream
        if index % 1000 == 0:
            print(f"{build}: {index}/{period} native page streams", flush=True)
    result = bytearray(data)
    raw_start = section.PointerToRawData
    result[raw_start:raw_start + len(code)] = code
    pe.close()
    if hashlib.sha256(result).hexdigest() != entry["recovered_sha256"]:
        raise ValueError(f"{build}: recovered image hash mismatch; no output written")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(result)
    print(f"{build}: recovered image hash verified", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binaries", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build", action="append", dest="builds")
    args = parser.parse_args()
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    wanted = set(args.builds or (e["build"] for e in catalog))
    unknown = wanted - {e["build"] for e in catalog}
    if unknown:
        parser.error("unknown build(s): " + ", ".join(sorted(unknown)))
    for entry in catalog:
        if entry["build"] in wanted:
            recover(args.binaries, args.output, entry)


if __name__ == "__main__":
    main()
