"""In-memory counts and composition for the hand-checkable rewrite samples."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from src.spatial import RADII_M, _station_index


def summarize(
    stations: Iterable[Mapping[str, Any]],
    matches: Iterable[Mapping[str, Any]],
    *,
    group_codes: Iterable[str],
) -> dict[str, list[dict[str, Any]]]:
    """Summarize unique maximum-radius pairs at both cumulative radii.

    Each station supplies coverage_complete_proxy={500: bool, 1000: bool}.
    group_codes is an explicit unique domain covering every matched group;
    it includes UNKNOWN when that group occurs in the supplied matches.
    Missing per-station values and undefined shares are None. Summary scopes
    contain only observable stations; complete_coverage is the main population.
    """
    indexed = _station_index(stations)
    groups = tuple(group_codes)
    if any(not isinstance(code, str) or not code for code in groups):
        raise ValueError("group_codes must contain non-empty strings")
    if len(set(groups)) != len(groups):
        raise ValueError("group_codes must be unique")

    by_station = defaultdict(list)
    seen_pairs = set()
    for source in matches:
        row = dict(source)
        key = row["station_key"]
        if key not in indexed:
            raise ValueError(f"Unknown matched station_key: {key}")
        station = indexed[key]
        if not (station["selected"] and station["query_eligible"]):
            raise ValueError("Matches require a selected, queryable station")
        pair = (key, row["ref_no"])
        if pair in seen_pairs:
            raise ValueError(f"Duplicate station-POI pair: {pair}")
        seen_pairs.add(pair)
        distance = float(row["distance_m"])
        if not math.isfinite(distance) or not 0 <= distance <= max(RADII_M):
            raise ValueError("Match distance must be finite and within 1000 m")
        row["distance_m"] = distance
        if row["group_code"] not in groups:
            raise ValueError(f"Missing matched group in group_codes: {row['group_code']}")
        by_station[key].append(row)

    totals = []
    composition = []
    neighborhoods = {}
    group_counts = {}
    for key, station in indexed.items():
        if not station["selected"]:
            continue
        queryable = station["query_eligible"]
        for radius in RADII_M:
            complete = station["coverage_complete_proxy"][radius]
            if not isinstance(complete, bool):
                raise ValueError("coverage_complete_proxy values must be bools")
            if complete and not queryable:
                raise ValueError("Complete coverage requires query eligibility")
            within = [row for row in by_station[key] if row["distance_m"] <= radius]
            total = len(within) if queryable else None
            totals.append({
                "station_key": key, "radius_m": radius, "selected": True,
                "query_eligible": queryable, "coverage_complete_proxy": complete,
                "observed_count": total,
                "interpretable_count": total if complete else None,
            })
            if not queryable:
                continue
            neighborhoods[key, radius] = within
            for level in ("group", "category", "class"):
                counts = Counter(row[f"{level}_code"] for row in within)
                if level == "group":
                    group_counts[key, radius] = counts
                codes = groups if level == "group" else sorted(counts)
                for code in codes:
                    count = counts[code]
                    composition.append({
                        "station_key": key, "radius_m": radius,
                        "level": level, "code": code, "count": count,
                        "share": count / total if total else None,
                        "coverage_complete_proxy": complete,
                    })

    radius_summary = []
    group_summary = []
    for radius in RADII_M:
        observable = [
            row for row in totals
            if row["radius_m"] == radius and row["query_eligible"]
        ]
        populations = {
            "all_observable": observable,
            "complete_coverage": [row for row in observable if row["coverage_complete_proxy"]],
        }
        for scope, population in populations.items():
            positive = [row for row in population if row["observed_count"] > 0]
            match_count = sum(row["observed_count"] for row in population)
            unique_refs = {
                match["ref_no"] for row in population
                for match in neighborhoods[row["station_key"], radius]
            }
            radius_summary.append({
                "radius_m": radius, "scope": scope,
                "station_count": len(population),
                "positive_station_count": len(positive),
                "zero_station_count": len(population) - len(positive),
                "match_count": match_count, "unique_poi_count": len(unique_refs),
                "count_distribution": dict(sorted(Counter(
                    row["observed_count"] for row in population
                ).items())),
            })
            for code in groups:
                count = sum(
                    group_counts[row["station_key"], radius][code] for row in population
                )
                share_sum = math.fsum(
                    group_counts[row["station_key"], radius][code] / row["observed_count"]
                    for row in positive
                )
                group_summary.append({
                    "radius_m": radius, "scope": scope, "group_code": code,
                    "station_count": len(population),
                    "positive_station_count": len(positive),
                    "group_match_count": count, "total_match_count": match_count,
                    "station_mean_share": share_sum / len(positive) if positive else None,
                    "pooled_match_share": count / match_count if match_count else None,
                })
    return {
        "station_radius_totals": totals, "composition": composition,
        "radius_summary": radius_summary, "group_summary": group_summary,
    }
