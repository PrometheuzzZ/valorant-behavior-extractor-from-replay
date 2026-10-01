import unittest


import support  # puts tools/ on sys.path
import check_effect_decoder as checker


class CheckEffectDecoderTests(unittest.TestCase):
    def test_every_named_corruption_is_detected(self):
        names = [case.name for case in checker.CASES]
        # docs/USAGE.md quotes 12 cases; a repeated name would corrupt two.
        self.assertEqual((len(names), len(set(names))), (12, 12))
        # The clean run first: a checker that fails every case would also
        # "detect" every corruption below.
        self.assertEqual(checker.check(), [])

        for name in names:
            with self.subTest(name=name):
                failures = checker.check(name)
                self.assertGreaterEqual(len(failures), 1, name)

    def test_unknown_corruption_name_is_rejected(self):
        self.assertEqual(
            checker.check("not_a_case"),
            ["unknown case to corrupt: not_a_case"],
        )
