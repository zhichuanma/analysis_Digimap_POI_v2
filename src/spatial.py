"""Simple projected-coordinate distance queries for the rewrite samples."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from src.classification import classify


RADII_M = (500, 1000)


def _coordinate(value: Iterable[float], name: str) -> tuple[float, float]:
    """Return a finite two-dimensional coordinate or raise a clear error."""
    try:
        x_raw, y_raw = value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain exactly two coordinates") from exc

    try:
        x = float(x_raw)
        y = float(y_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain numeric coordinates") from exc

    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"{name} must contain finite coordinates")
    return x, y


def naive_neighbors(
    station_xy: Iterable[float],
    poi_xy: Iterable[Iterable[float]],
    radius_m: float,
) -> list[tuple[int, float]]:
    """Return POIs within ``radius_m`` of a station.

    Coordinates are projected metre coordinates.  Each result is ``(index,
    distance_m)``, where ``index`` is the POI's position in the input
    iterable.  The input order and duplicate coordinates are preserved.
    """
    try:
        radius = float(radius_m)
    except (TypeError, ValueError) as exc:
        raise ValueError("radius_m must be a finite positive number") from exc
    if not math.isfinite(radius) or radius <= 0:
        raise ValueError("radius_m must be a finite positive number")

    station_x, station_y = _coordinate(station_xy, "station_xy")
    neighbors: list[tuple[int, float]] = []
    for poi_index, poi in enumerate(poi_xy):
        poi_x, poi_y = _coordinate(poi, f"poi_xy[{poi_index}]")
        distance_m = math.hypot(poi_x - station_x, poi_y - station_y)
        if distance_m <= radius:
            neighbors.append((poi_index, distance_m))
    return neighbors


def _station_index(stations: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Check identifiers and explicit selection/query flags of prepared stations."""
    indexed = {}
    for station in stations:
        key = station["station_key"]
        if not isinstance(key, str) or not key:
            raise ValueError("station_key must be a non-empty string")
        if key in indexed:
            raise ValueError(f"Duplicate station_key: {key}")
        for flag in ("selected", "query_eligible"):
            if not isinstance(station[flag], bool):
                raise ValueError(f"{flag} must be a bool")
        indexed[key] = station
    return indexed


def match(
    stations: Iterable[Mapping[str, Any]],
    pois: Iterable[Mapping[str, Any]],
    dictionary: Mapping[str, object],
    *,
    include_ev_charging: bool = False,
) -> dict[str, Any]:
    """Match prepared synthetic records within the fixed maximum radius.

    Stations provide station_key, xy, selected and query_eligible. POIs provide
    ref_no (a string), xy, raw_code and explicit in_pua, passes_business_filter,
    passes_cleaning booleans. These flags are supplied, not inferred from data.
    Stations must already be merged and POI identifiers must already be unique;
    contract violations raise rather than inventing source-cleaning decisions.
    Return matches and an audit row for every input POI, including excluded ones.
    """
    if not isinstance(include_ev_charging, bool):
        raise TypeError("include_ev_charging must be a bool")
    indexed = _station_index(stations)
    eligible_pois = []
    poi_audit = []
    seen_refs = set()
    for poi in pois:
        ref_no = poi["ref_no"]
        if not isinstance(ref_no, str) or not ref_no:
            raise ValueError("ref_no must be a non-empty string")
        if ref_no in seen_refs:
            raise ValueError(f"Duplicate ref_no in prepared POIs: {ref_no}")
        seen_refs.add(ref_no)
        classification = classify(
            poi["raw_code"], dictionary, include_ev_charging=include_ev_charging
        )
        columns = {
            "raw_code": classification.raw_code,
            "group_code": classification.group_code,
            "category_code": classification.category_code,
            "class_code": classification.class_code,
            "classification_status": classification.classification_status,
            "include_in_count": classification.include_in_count,
            "audit_reasons": classification.audit_reasons,
        }
        exclusions = []
        for flag, reason in (
            ("in_pua", "outside_pua"),
            ("passes_business_filter", "business_filter_excluded"),
            ("passes_cleaning", "cleaning_excluded"),
        ):
            if not isinstance(poi[flag], bool):
                raise ValueError(f"{flag} must be a bool")
            if not poi[flag]:
                exclusions.append(reason)
        if not classification.include_in_count:
            exclusions.append("charging_facility_excluded")
        poi_audit.append({
            "ref_no": ref_no, **columns,
            "eligible_for_matching": not exclusions,
            "exclusion_reasons": tuple(exclusions),
        })
        if not exclusions:
            eligible_pois.append({
                "ref_no": ref_no, "xy": _coordinate(poi["xy"], f"POI {ref_no}"),
                **columns,
            })

    matches = []
    seen_xy = set()
    poi_xy = [poi["xy"] for poi in eligible_pois]
    for key, station in indexed.items():
        if not (station["selected"] and station["query_eligible"]):
            continue
        station_xy = _coordinate(station["xy"], f"Station {key}")
        if station_xy in seen_xy:
            raise ValueError("Queryable station coordinates must already be merged")
        seen_xy.add(station_xy)
        for index, distance in naive_neighbors(station_xy, poi_xy, max(RADII_M)):
            # The index belongs to the filtered list, not the original POI list.
            poi = eligible_pois[index]
            matches.append({
                "station_key": key, "distance_m": distance,
                **{name: value for name, value in poi.items() if name != "xy"},
            })
    return {
        "matches": matches,
        "poi_audit": poi_audit,
        "include_ev_charging": include_ev_charging,
    }
