import unittest

from src.classification import classify


# Synthetic labels only; the official dictionary is introduced in step 07.
TEST_DICTIONARY = {
    "01010006": "sample class A",
    "01020013": "sample class B",
}


class ClassificationTests(unittest.TestCase):
    def assert_classification(self, raw_code, result, codes, status, include, reasons):
        self.assertIs(result.raw_code, raw_code)
        self.assertEqual(
            (result.group_code, result.category_code, result.class_code), codes
        )
        self.assertEqual(result.classification_status, status)
        self.assertIs(result.include_in_count, include)
        self.assertEqual(result.audit_reasons, reasons)

    def test_known_codes_keep_leading_zero(self):
        cases = [
            ("01010006", ("01", "0101", "01010006")),
            ("01020013", ("01", "0102", "01020013")),
        ]
        for raw_code, codes in cases:
            with self.subTest(raw_code=raw_code):
                self.assert_classification(
                    raw_code, classify(raw_code, TEST_DICTIONARY),
                    codes, "known", True, (),
                )

    def test_unknown_dictionary_codes_keep_their_prefixes(self):
        cases = [
            ("99999999", ("99", "9999", "99999999")),
            ("01019999", ("01", "0101", "01019999")),
        ]
        for raw_code, codes in cases:
            with self.subTest(raw_code=raw_code):
                self.assert_classification(
                    raw_code, classify(raw_code, TEST_DICTIONARY),
                    codes, "unknown_dictionary", True, ("unknown_dictionary_code",),
                )

    def test_missing_values_are_retained_and_audited(self):
        for raw_code in (None, "", float("nan")):
            with self.subTest(raw_code=raw_code):
                self.assert_classification(
                    raw_code, classify(raw_code, TEST_DICTIONARY),
                    ("UNKNOWN", "UNKNOWN", "UNKNOWN"),
                    "missing", True, ("missing_code",),
                )

    def test_malformed_values_are_retained_without_repairs(self):
        cases = (
            "0101000", "001010006", "0101000X", " 01010006", "01010006 ",
            "01010006\n", "\t01010006", "   ", "０１０１０００６", "٠١٠١٠٠٠٦",
            1010006, 10540832.0, True, "nan", "None",
        )
        for raw_code in cases:
            with self.subTest(raw_code=raw_code):
                self.assert_classification(
                    raw_code, classify(raw_code, TEST_DICTIONARY),
                    ("UNKNOWN", "UNKNOWN", "UNKNOWN"),
                    "malformed", True, ("malformed_code",),
                )

    def test_non_missing_integer_is_checked_as_a_string(self):
        raw_code = 99999999
        self.assert_classification(
            raw_code, classify(raw_code, TEST_DICTIONARY),
            ("99", "9999", "99999999"),
            "unknown_dictionary", True, ("unknown_dictionary_code",),
        )

    def test_charging_code_is_excluded_by_default(self):
        for raw_code in ("10540832", 10540832):
            with self.subTest(raw_code=raw_code):
                self.assert_classification(
                    raw_code, classify(raw_code, TEST_DICTIONARY),
                    ("10", "1054", "10540832"), "unknown_dictionary", False,
                    ("unknown_dictionary_code", "charging_facility_excluded"),
                )

    def test_charging_switch_is_independent_of_dictionary_membership(self):
        cases = [
            (TEST_DICTIONARY, "unknown_dictionary", ("unknown_dictionary_code",)),
            ({**TEST_DICTIONARY, "10540832": "sample facility"}, "known", ()),
        ]
        for dictionary, status, dictionary_reasons in cases:
            for raw_code in ("10540832", 10540832):
                for enabled in (False, True):
                    with self.subTest(raw_code=raw_code, status=status, enabled=enabled):
                        reasons = dictionary_reasons
                        if not enabled:
                            reasons += ("charging_facility_excluded",)
                        self.assert_classification(
                            raw_code,
                            classify(raw_code, dictionary, include_ev_charging=enabled),
                            ("10", "1054", "10540832"), status, enabled, reasons,
                        )

    def test_switch_does_not_change_other_classifications(self):
        for raw_code in ("01010006", "99999999", None, "", "0101000X", "10540832 "):
            with self.subTest(raw_code=raw_code):
                disabled = classify(raw_code, TEST_DICTIONARY, include_ev_charging=False)
                enabled = classify(raw_code, TEST_DICTIONARY, include_ev_charging=True)
                self.assertEqual(disabled, enabled)
                self.assertTrue(disabled.include_in_count)

    def test_switch_requires_an_explicit_boolean(self):
        for flag in (None, 0, 1, "False", "True"):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(TypeError, "include_ev_charging must be a bool"):
                    classify("10540832", TEST_DICTIONARY, include_ev_charging=flag)


if __name__ == "__main__":
    unittest.main()
