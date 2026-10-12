import math
import unittest

from src.analysis import summarize
from src.spatial import match_synthetic as match


TEST_DICTIONARY = {"01010006": "sample class A", "01020013": "sample class B"}


def station(key, xy, *, selected=True, queryable=True, complete=(True, True)):
    return {
        "station_key": key, "xy": xy, "selected": selected,
        "query_eligible": queryable,
        "coverage_complete_proxy": {500: complete[0], 1000: complete[1]},
    }


def poi(ref_no, xy, code, **eligibility):
    return {
        "ref_no": ref_no, "xy": xy, "raw_code": code,
        "in_pua": True, "passes_business_filter": True, "passes_cleaning": True,
        **eligibility,
    }


def sample_b():
    """Prepared synthetic entities; S3 differs from the outdated route sample."""
    stations = [
        station("S1", (0, 0)),
        station("S2", (10000, 10000)),
        station("S3", (100, 500), complete=(False, False)),
        station("S4", (20000, 20000), queryable=False, complete=(False, False)),
    ]
    pois = [
        poi("101", (100, 0), "01010006"),
        poi("102", (300, 400), "01020013"),
        poi("103", (600, 0), "99999999"),
        poi("104", (1100, 0), "01010006"),
        poi("105", (100, 0), "10540832"),
    ]
    return stations, pois


def analyze_sample(stations, pois, *, groups=("01", "99"), include_ev_charging=False):
    matched = match(stations, pois, TEST_DICTIONARY, include_ev_charging=include_ev_charging)
    return {
        **matched,
        **summarize(stations, matched["matches"], group_codes=groups),
    }


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.stations, self.pois = sample_b()

    def test_sample_b_unique_pairs_distances_and_audit(self):
        result = analyze_sample(self.stations, self.pois)
        expected = {
            ("S1", "101"): 100.0, ("S1", "102"): 500.0, ("S1", "103"): 600.0,
            ("S3", "101"): 500.0, ("S3", "102"): math.sqrt(50000),
            ("S3", "103"): math.sqrt(500000),
        }
        pairs = {(row["station_key"], row["ref_no"]): row for row in result["matches"]}
        self.assertEqual(len(result["matches"]), 6)
        self.assertEqual(set(pairs), set(expected))
        for pair, distance in expected.items():
            self.assertAlmostEqual(pairs[pair]["distance_m"], distance)
        self.assertEqual(pairs["S1", "103"]["group_code"], "99")
        self.assertEqual(pairs["S1", "103"]["audit_reasons"], ("unknown_dictionary_code",))
        audit = {row["ref_no"]: row for row in result["poi_audit"]}
        self.assertEqual(len(audit), 5)
        self.assertTrue(audit["104"]["eligible_for_matching"])
        self.assertFalse(audit["105"]["eligible_for_matching"])
        self.assertEqual(audit["105"]["exclusion_reasons"], ("charging_facility_excluded",))

    def test_sample_b_totals_keep_zero_missing_and_incomplete_separate(self):
        result = analyze_sample(self.stations, self.pois)
        actual = {
            (row["station_key"], row["radius_m"]):
            (row["observed_count"], row["interpretable_count"])
            for row in result["station_radius_totals"]
        }
        self.assertEqual(actual, {
            ("S1", 500): (2, 2), ("S1", 1000): (3, 3),
            ("S2", 500): (0, 0), ("S2", 1000): (0, 0),
            ("S3", 500): (2, None), ("S3", 1000): (3, None),
            ("S4", 500): (None, None), ("S4", 1000): (None, None),
        })
        self.assertEqual(len(result["station_radius_totals"]), 8)

    def test_three_layers_have_independent_totals_and_shares(self):
        result = analyze_sample(self.stations, self.pois)
        rows = result["composition"]
        self.assertEqual(len(rows), 32)
        for key in ("S1", "S3"):
            for radius, total in ((500, 2), (1000, 3)):
                expected = {
                    "group": {"01": 2, "99": 0 if radius == 500 else 1},
                    "category": {"0101": 1, "0102": 1},
                    "class": {"01010006": 1, "01020013": 1},
                }
                if radius == 1000:
                    expected["category"]["9999"] = 1
                    expected["class"]["99999999"] = 1
                for level, counts in expected.items():
                    with self.subTest(station=key, radius=radius, level=level):
                        cells = [
                            r for r in rows
                            if (r["station_key"], r["radius_m"], r["level"]) == (key, radius, level)
                        ]
                        self.assertEqual({r["code"]: r["count"] for r in cells}, counts)
                        self.assertEqual(sum(r["count"] for r in cells), total)
                        self.assertAlmostEqual(sum(r["share"] for r in cells), 1.0)
                        for row in cells:
                            self.assertAlmostEqual(row["share"], counts[row["code"]] / total)
                            self.assertEqual(row["coverage_complete_proxy"], key == "S1")
        zero_cells = [r for r in rows if r["station_key"] == "S2"]
        self.assertEqual(len(zero_cells), 4)
        self.assertTrue(all(
            r["level"] == "group" and r["count"] == 0 and r["share"] is None
            for r in zero_cells
        ))
        self.assertFalse(any(r["station_key"] == "S4" for r in rows))

    def test_sample_b_summary_denominators_and_unique_pois(self):
        result = analyze_sample(self.stations, self.pois)
        expected = {
            (500, "all_observable"): (3, 2, 1, 4, 2, {0: 1, 2: 2}),
            (500, "complete_coverage"): (2, 1, 1, 2, 2, {0: 1, 2: 1}),
            (1000, "all_observable"): (3, 2, 1, 6, 3, {0: 1, 3: 2}),
            (1000, "complete_coverage"): (2, 1, 1, 3, 3, {0: 1, 3: 1}),
        }
        actual = {
            (r["radius_m"], r["scope"]): tuple(r[field] for field in (
                "station_count", "positive_station_count", "zero_station_count",
                "match_count", "unique_poi_count", "count_distribution",
            )) for r in result["radius_summary"]
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(result["group_summary"]), 8)
        for row in result["group_summary"]:
            shares = {500: {"01": 1.0, "99": 0.0}, 1000: {"01": 2 / 3, "99": 1 / 3}}
            share = shares[row["radius_m"]][row["group_code"]]
            self.assertAlmostEqual(row["station_mean_share"], share)
            self.assertAlmostEqual(row["pooled_match_share"], share)
            expected_positive = 1 if row["scope"] == "complete_coverage" else 2
            self.assertEqual(row["positive_station_count"], expected_positive)

    def test_charging_switch_adds_counts_group_and_colocated_poi(self):
        result = analyze_sample(
            self.stations, self.pois, groups=("01", "99", "10"), include_ev_charging=True
        )
        self.assertEqual(len(result["matches"]), 8)
        for key in ("S1", "S3"):
            refs = {r["ref_no"] for r in result["matches"] if r["station_key"] == key}
            self.assertEqual(refs, {"101", "102", "103", "105"})
            for radius, total in ((500, 3), (1000, 4)):
                row = next(
                    r for r in result["station_radius_totals"]
                    if (r["station_key"], r["radius_m"]) == (key, radius)
                )
                self.assertEqual(row["observed_count"], total)
                cell = next(
                    r for r in result["composition"]
                    if (r["station_key"], r["radius_m"], r["level"], r["code"])
                    == (key, radius, "group", "10")
                )
                self.assertEqual(cell["count"], 1)
                self.assertAlmostEqual(cell["share"], 1 / total)
        for row in result["station_radius_totals"]:
            if row["station_key"] in ("S2", "S4"):
                self.assertEqual(row["observed_count"], 0 if row["station_key"] == "S2" else None)

    def test_filtering_preserves_ref_no_and_cannot_be_bypassed_by_charging_switch(self):
        pois = [
            poi("outside", (0, 0), "10540832", in_pua=False),
            poi("business", (0, 0), "01010006", passes_business_filter=False),
            poi("invalid", (float("nan"), 0), "01010006", passes_cleaning=False),
            poi("kept", (100, 0), "99999999"),
        ]
        for enabled in (False, True):
            result = analyze_sample([self.stations[0]], pois, groups=("01", "99", "10"), include_ev_charging=enabled)
            self.assertEqual([r["ref_no"] for r in result["matches"]], ["outside", "kept"] if enabled else ["kept"])
            self.assertEqual(result["matches"][-1]["distance_m"], 100.0)
            audit = {r["ref_no"]: r for r in result["poi_audit"]}
            self.assertNotIn("outside_pua", audit["outside"]["exclusion_reasons"])
            self.assertEqual(audit["business"]["exclusion_reasons"], ("business_filter_excluded",))
            self.assertEqual(audit["invalid"]["exclusion_reasons"], ("cleaning_excluded",))

    def test_empty_pois_preserve_observable_zeros_and_undefined_shares(self):
        result = analyze_sample(self.stations, [])
        self.assertEqual(result["matches"], [])
        for row in result["station_radius_totals"]:
            self.assertEqual(row["observed_count"], None if row["station_key"] == "S4" else 0)
        self.assertEqual(len(result["composition"]), 12)
        self.assertTrue(all(r["count"] == 0 and r["share"] is None for r in result["composition"]))
        for row in result["group_summary"]:
            self.assertIsNone(row["station_mean_share"])
            self.assertIsNone(row["pooled_match_share"])

    def test_selection_and_per_radius_coverage_are_independent(self):
        self.stations[2]["selected"] = False
        self.stations[0]["coverage_complete_proxy"][1000] = False
        result = analyze_sample(self.stations, self.pois)
        self.assertEqual(len(result["matches"]), 3)
        self.assertFalse(any(r["station_key"] == "S3" for r in result["station_radius_totals"]))
        totals = {(r["station_key"], r["radius_m"]): r for r in result["station_radius_totals"]}
        self.assertEqual(totals["S1", 500]["interpretable_count"], 2)
        self.assertEqual(totals["S1", 1000]["observed_count"], 3)
        self.assertIsNone(totals["S1", 1000]["interpretable_count"])
        full = {r["radius_m"]: r for r in result["radius_summary"] if r["scope"] == "complete_coverage"}
        self.assertEqual((full[500]["station_count"], full[500]["match_count"]), (2, 2))
        self.assertEqual((full[1000]["station_count"], full[1000]["match_count"]), (1, 0))
        for row in result["group_summary"]:
            if (row["scope"], row["radius_m"]) == ("complete_coverage", 1000):
                self.assertIsNone(row["station_mean_share"])

    def test_missing_and_malformed_codes_count_as_unknown(self):
        pois = [poi("a", (0, 0), None), poi("b", (0, 0), "bad"), poi("c", (0, 0), "01010006")]
        result = analyze_sample([self.stations[0]], pois, groups=("01", "UNKNOWN"))
        self.assertEqual(len(result["matches"]), 3)
        for row in result["station_radius_totals"]:
            self.assertEqual(row["observed_count"], 3)
        unknown = [r for r in result["composition"] if r["code"] == "UNKNOWN"]
        self.assertEqual(len(unknown), 6)
        for row in unknown:
            self.assertEqual(row["count"], 2)
            self.assertAlmostEqual(row["share"], 2 / 3)
        audit = {r["ref_no"]: r for r in result["poi_audit"]}
        self.assertEqual(audit["a"]["audit_reasons"], ("missing_code",))
        self.assertIsNone(audit["a"]["raw_code"])
        self.assertEqual(audit["b"]["audit_reasons"], ("malformed_code",))
        self.assertEqual(audit["b"]["raw_code"], "bad")

    def test_station_weighted_share_differs_from_pooled_share(self):
        stations = [station("A", (0, 0)), station("B", (5000, 0)), station("Z", (10000, 0))]
        pois = [
            poi("a", (0, 0), "01010006"), poi("b", (5000, 0), "01010006"),
            poi("c", (5000, 0), "99999999"), poi("d", (5000, 0), "99999999"),
        ]
        result = analyze_sample(stations, pois)
        for row in result["radius_summary"]:
            self.assertEqual(row["count_distribution"], {0: 1, 1: 1, 3: 1})
        for row in result["group_summary"]:
            self.assertEqual(row["station_count"], 3)
            self.assertEqual(row["positive_station_count"], 2)
            self.assertEqual(row["total_match_count"], 4)
            self.assertAlmostEqual(row["station_mean_share"], 2 / 3 if row["group_code"] == "01" else 1 / 3)
            self.assertAlmostEqual(row["pooled_match_share"], 1 / 2)

    def test_boundary_and_cumulative_membership_use_no_tolerance(self):
        pois = [
            poi("edge500", (500, 0), "01010006"),
            poi("past500", (500.000001, 0), "01010006"),
            poi("edge1000", (1000, 0), "01010006"),
            poi("past1000", (1000.000001, 0), "01010006"),
        ]
        result = analyze_sample([self.stations[0]], pois)
        at500 = {r["ref_no"] for r in result["matches"] if r["distance_m"] <= 500}
        at1000 = {r["ref_no"] for r in result["matches"]}
        self.assertEqual(at500, {"edge500"})
        self.assertEqual(at1000, {"edge500", "past500", "edge1000"})
        self.assertLess(at500, at1000)
        self.assertEqual([r["observed_count"] for r in result["station_radius_totals"]], [1, 3])

    def test_prepared_records_cannot_duplicate_station_entities_or_refs(self):
        with self.assertRaisesRegex(ValueError, "already be merged"):
            match([station("A", (0, 0)), station("B", (0, 0))], self.pois, TEST_DICTIONARY)
        with self.assertRaisesRegex(ValueError, "Duplicate station_key"):
            match([station("A", (0, 0)), station("A", (10, 0))], self.pois, TEST_DICTIONARY)
        with self.assertRaisesRegex(ValueError, "Duplicate ref_no"):
            match(self.stations, [self.pois[0], self.pois[0]], TEST_DICTIONARY)

    def test_summary_rejects_duplicate_pairs_or_omitted_groups(self):
        matched = match(self.stations, self.pois, TEST_DICTIONARY)["matches"]
        with self.assertRaisesRegex(ValueError, "Duplicate station-POI pair"):
            summarize(self.stations, [*matched, matched[0]], group_codes=("01", "99"))
        with self.assertRaisesRegex(ValueError, "Missing matched group"):
            summarize(self.stations, matched, group_codes=("01",))

    def test_no_observable_stations_do_not_create_zero_station_observations(self):
        result = analyze_sample([self.stations[3]], self.pois)
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["composition"], [])
        self.assertTrue(all(r["observed_count"] is None for r in result["station_radius_totals"]))
        for row in result["radius_summary"]:
            self.assertEqual(row["station_count"], 0)
            self.assertEqual(row["count_distribution"], {})
        self.assertTrue(all(r["station_mean_share"] is None for r in result["group_summary"]))


if __name__ == "__main__":
    unittest.main()
