"""Native cipher oracle and archive integrity checks; no game files required."""
import hashlib
import json

from support import TempDirTestCase
import recover_native_binaries as recovery


class RecoveryTests(TempDirTestCase):
    def test_bulk_blocks_match_native_code(self):
        fixture = json.loads(recovery.CATALOG.with_name("native_bulk_vectors.json").read_text(encoding="utf-8"))
        for case in fixture["blocks"]:
            with self.subTest(offset=case["offset"]):
                self.assertEqual(
                    recovery.bulk_block(bytes.fromhex(fixture["key"]),
                                        bytes.fromhex(fixture["nonce"]), case["offset"]),
                    bytes.fromhex(case["output"]),
                )

    def test_vectorized_bulk_matches_native_stream(self):
        fixture = json.loads(recovery.CATALOG.with_name("native_bulk_vectors.json").read_text(encoding="utf-8"))
        expected = bytes.fromhex(fixture["bulk_output"])
        source = bytes.fromhex(fixture["bulk_source"])
        self.assertEqual(recovery.bulk_decode(bytes(len(expected)), source), expected)
        # Nonzero ciphertext also checks XOR direction and data preservation.
        data = bytes(range(128))
        self.assertEqual(recovery.bulk_decode(data, source),
                         bytes(a ^ b for a, b in zip(data, expected)))
        with self.assertRaises(ValueError):
            recovery.bulk_decode(bytes(63), source)
        with self.assertRaises(ValueError):
            recovery.bulk_decode(bytes(64), source[:-1])

    def test_hash_guard_rejects_changed_input(self):
        directory = self.tmp()
        path = directory / "archive.bin"
        path.write_bytes(b"original")
        digest = hashlib.sha256(b"original").hexdigest()
        self.assertEqual(recovery.checked_input(path, digest), b"original")
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            recovery.checked_input(path, digest)
        self.assertEqual(path.read_bytes(), b"changed")

    def test_recovered_hashes_pin_the_captured_readers(self):
        catalog = json.loads(recovery.CATALOG.read_text(encoding="utf-8"))
        readers = json.loads(recovery.CATALOG.with_name("native_transform_readers.json").read_text(encoding="utf-8"))
        hashes = {row["build"]: row["exe_sha256"] for row in readers}
        self.assertEqual(len(catalog), 7)
        for entry in catalog:
            with self.subTest(build=entry["build"]):
                self.assertEqual(entry["recovered_sha256"], hashes[entry["build"]])
                self.assertNotEqual(entry["exe_sha256"], entry["recovered_sha256"])
