"""A failed audit must remain visible, and table wording must have one meaning."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_docs as guard


class BuildVerificationDocsTests(unittest.TestCase):
    # The shape of crates/vrf-transform/src/lib.rs's registry block.
    REGISTRY = 'transforms! {\n    V1106 V11_06 "11.06" 0x3325_e3bd  0x3d: add 8, sbox;\n}\n'
    REPORT = {"executable_changed": False, "builds": {
        "++Ares-Core+release-11.06": {"passed": 2, "failed": 1, "replays": 3,
                                      "checkpoint_evidence": "observed",
                                      "counts": {"checkpoint_content_blocks": 5,
                                                 "checkpoint_overlay_decoded_ok": 8},
                                      "input_sha256": ["a", "b", "c"]}}}
    README = "| **11.06** | `release-11.06` | 2/3 | Validation + checkpoints + typed/raw |"
    USAGE = "| Payload transform (1 builds) | `vrf-transform` | none |"

    def check(self, readme=None, usage=None, report=None, registry=None):
        return guard.check_build_verification(
            self.README if readme is None else readme,
            self.USAGE if usage is None else usage,
            self.REGISTRY if registry is None else registry,
            self.REPORT if report is None else report)

    def test_accurate_mixed_result_is_allowed(self):
        self.assertEqual(self.check(), [])

    def test_failed_replay_cannot_be_relabelled_clean(self):
        self.assertTrue(self.check(readme=self.README.replace("2/3", "3/3")))

    def test_verification_methods_cannot_diverge(self):
        self.assertTrue(self.check(readme=self.README.replace(guard.BUILD_METHOD, "golden vectors")))
        self.assertTrue(self.check(usage=self.USAGE.replace("(1 builds)", "(8 builds)")))

    def test_missing_or_duplicate_build_is_detected(self):
        self.assertTrue(self.check(readme=""))
        self.assertTrue(self.check(readme=self.README + "\n" + self.README))

    def test_report_must_cover_registry_and_unchanged_executable(self):
        report = deepcopy(self.REPORT)
        report["builds"] = {}
        self.assertTrue(self.check(report=report))
        report = deepcopy(self.REPORT)
        del report["executable_changed"]
        self.assertTrue(self.check(report=report))

    def test_empty_or_unreadable_registry_and_inconsistent_report_cannot_pass(self):
        self.assertTrue(self.check(registry="transforms! {\n}\n"))
        self.assertTrue(self.check(registry="pub const ALL_VERSIONS: &[T] = &[V1106];"))
        report = deepcopy(self.REPORT)
        report["builds"]["++Ares-Core+release-11.06"]["failed"] = 0
        self.assertTrue(self.check(report=report))

        report = deepcopy(self.REPORT)
        report["builds"]["++Ares-Core+release-11.06"]["input_sha256"] = ["a", "a", "c"]
        self.assertTrue(self.check(report=report))

    def test_checkpoint_claim_requires_real_work_and_no_build_errors(self):
        for key in ("checkpoint_content_blocks", "checkpoint_overlay_decoded_ok"):
            for bad in (0, None, True, -1):
                with self.subTest(key=key, bad=bad):
                    report = deepcopy(self.REPORT)
                    report["builds"]["++Ares-Core+release-11.06"]["counts"][key] = bad
                    self.assertTrue(self.check(report=report))
        report = deepcopy(self.REPORT)
        report["build_errors"] = ["checkpoint work absent"]
        self.assertTrue(self.check(report=report))


if __name__ == "__main__":
    unittest.main()
