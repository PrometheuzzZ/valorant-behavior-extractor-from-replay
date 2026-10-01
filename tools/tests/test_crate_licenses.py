"""Every crate carries the workspace's license text and the earlier MIT notice.

`cargo package` builds a crate's archive from the crate directory, so the root
LICENSE and NOTICE.md never reach it; each crate's own files ship beside the
`license` field it inherits. `cargo metadata` reads only that field, so it
passes a crate LICENSE that holds another license's text.

Code contributed before the change to Apache-2.0 was contributed under MIT,
which allows other terms only while its notice stays with the code. The root
NOTICE.md keeps that notice in its first section, before the third-party
notices, and every crate's NOTICE.md opens with the same section.
"""
import hashlib
import tomllib
import unittest
from pathlib import Path
from support import REPO


#: SHA-256 of each license's text with LF line endings, by the SPDX id the
#: workspace declares. Apache-2.0 is apache.org's LICENSE-2.0.txt.
LICENSE_TEXT_SHA256 = {
    "Apache-2.0": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
}

THIRD_PARTY_HEADING = "\n# Third-party notices\n"
DISCLAIMER_HEADING = "\n# Disclaimer\n"
THIRD_PARTY_COPYRIGHT = "Copyright (c) 2026 Michel Giehl"

#: Crates holding code ported from, or generated out of, the MIT-licensed
#: third-party source. A crate archive carries only its own NOTICE.md.
DERIVED_CRATES = ("vrf-bitio", "vrf-decode", "vrf-transform", "vrfkit")


def lf(data: bytes) -> bytes:
    # A core.autocrlf checkout writes CRLF; the committed files are LF.
    return data.replace(b"\r\n", b"\n")


def crates() -> list[Path]:
    return sorted(path.parent for path in (REPO / "crates").glob("*/Cargo.toml"))


class CrateLicenseTests(unittest.TestCase):
    def setUp(self):
        self.crates = crates()
        # A glob that found nothing would pass vacuously.
        self.assertIn(REPO / "crates" / "vrfkit", self.crates)

    def test_every_crate_license_is_the_declared_license_text(self):
        workspace = tomllib.loads(
            (REPO / "Cargo.toml").read_text(encoding="utf-8-sig"))
        declared = workspace["workspace"]["package"]["license"]
        self.assertIn(declared, LICENSE_TEXT_SHA256)
        text = lf((REPO / "LICENSE").read_bytes())
        self.assertEqual(hashlib.sha256(text).hexdigest(), LICENSE_TEXT_SHA256[declared])
        for crate in self.crates:
            with self.subTest(crate=crate.name):
                package = tomllib.loads(
                    (crate / "Cargo.toml").read_text(encoding="utf-8-sig"))["package"]
                self.assertEqual(package.get("license"), {"workspace": True})
                self.assertEqual(lf((crate / "LICENSE").read_bytes()), text,
                                 f"{crate.name}/LICENSE is not the {declared} text")

    def test_every_crate_notice_opens_with_the_earlier_mit_notice(self):
        notice = lf((REPO / "NOTICE.md").read_bytes()).decode("utf-8")
        project, heading, _ = notice.partition(THIRD_PARTY_HEADING)
        self.assertTrue(heading, "NOTICE.md has no third-party section to end the project's")
        # An emptied section would be a prefix of anything.
        self.assertIn("Copyright (c) 2026 vrfkit contributors", project)
        self.assertIn("Permission is hereby granted", project)
        for crate in self.crates:
            with self.subTest(crate=crate.name):
                path = crate / "NOTICE.md"
                self.assertTrue(path.is_file(), f"{crate.name} has no NOTICE.md")
                text = lf(path.read_bytes()).decode("utf-8")
                self.assertTrue(text.startswith(project),
                                f"{crate.name}/NOTICE.md does not open with the root's "
                                "project section")

    def test_every_derived_crate_ships_the_third_party_notice(self):
        notice = lf((REPO / "NOTICE.md").read_bytes()).decode("utf-8")
        project, heading, rest = notice.partition(THIRD_PARTY_HEADING)
        third_party = heading + rest.partition(DISCLAIMER_HEADING)[0]
        # An emptied section would be a substring of anything.
        self.assertIn(THIRD_PARTY_COPYRIGHT, third_party)
        self.assertIn("Permission is hereby granted", third_party)
        for name in DERIVED_CRATES:
            with self.subTest(crate=name):
                path = REPO / "crates" / name / "NOTICE.md"
                text = lf(path.read_bytes()).decode("utf-8")
                self.assertEqual(text, project + third_party,
                                 f"{name}/NOTICE.md is not the root's project and "
                                 "third-party sections")
