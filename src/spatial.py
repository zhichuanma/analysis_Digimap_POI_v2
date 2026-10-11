"""Fixed offline station conversion and projected-coordinate distance queries."""

from __future__ import annotations

import math
import hashlib
import random
from collections import defaultdict
from collections.abc import Iterable, Mapping
from numbers import Real
from typing import Any

from src.classification import classify


RADII_M = (500, 1000)

# All steps and parameters are explicit: no CRS-operation selection or grids.
# Helmert parameters describe OSGB36 -> WGS84; +inv applies the reverse.
FIXED_COORDINATE_OPERATION = (
    "+proj=pipeline "
    "+step +proj=unitconvert +xy_in=deg +xy_out=rad "
    "+step +proj=push +v_3 "
    "+step +proj=cart +ellps=WGS84 "
    "+step +inv +proj=helmert +x=446.448 +y=-125.157 +z=542.06 "
    "+rx=0.1502 +ry=0.247 +rz=0.8421 +s=-20.4894 +convention=position_vector "
    "+step +inv +proj=cart +ellps=airy "
    "+step +proj=pop +v_3 "
    "+step +proj=tmerc +lat_0=49 +lon_0=-2 +k=0.9996012717 "
    "+x_0=400000 +y_0=-100000 +ellps=airy"
)


def validate_coordinate_operation(operation: str) -> str:
    """Accept only the SPEC operation; whitespace may vary, tokens may not.

    This is a string check, with no PROJ imports or coordinate execution.
    """
    if not isinstance(operation, str) or " ".join(operation.split()) != FIXED_COORDINATE_OPERATION:
        raise ValueError("coordinate_operation: must be the SPEC fixed offline pipeline")
    return FIXED_COORDINATE_OPERATION


def _angular_column(values: Iterable[float], name: str, limit: float) -> list[float]:
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError(f"{name}: must be a sequence of numeric coordinates")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{name}: must be a sequence of numeric coordinates") from exc
    result = []
    for index, value in enumerate(iterator):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError(f"{name}[{index}]: must be numeric, not bool or text")
        try:
            coordinate = float(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"{name}[{index}]: must be finite and within [-{limit}, {limit}]") from exc
        if not math.isfinite(coordinate) or not -limit <= coordinate <= limit:
            raise ValueError(f"{name}[{index}]: must be finite and within [-{limit}, {limit}]")
        result.append(coordinate)
    return result


def transform_coordinates(
    longitude: Iterable[float], latitude: Iterable[float], coordinate_operation: str,
) -> tuple[list[float], list[float]]:
    """Convert parsed WGS84 (longitude, latitude) degrees to BNG (x, y) metres.

    Assumes ellipsoid height zero; output is the approximate seven-parameter
    result, not an OSTN15 survey solution. Applies no selection or cleaning.
    Preserves count, order and duplicate coordinates; any invalid input or
    projection failure rejects the batch. Legal global angles alone do not
    establish GB/PUA eligibility or the operation's geographical accuracy.
    """
    operation = validate_coordinate_operation(coordinate_operation)
    lon = _angular_column(longitude, "longitude", 180)
    lat = _angular_column(latitude, "latitude", 90)
    if len(lon) != len(lat):
        raise ValueError("longitude/latitude: must have equal lengths")
    if not lon:
        return [], []

    # Lazy import keeps --check-config independent of PROJ initialization.
    from pyproj import Transformer
    from pyproj.exceptions import ProjError

    try:
        transformer = Transformer.from_pipeline(operation)
        x, y = transformer.transform(lon, lat, errcheck=True)
    except ProjError as exc:
        raise ValueError(f"coordinate transformation failed: {exc}") from exc
    if len(x) != len(lon) or len(y) != len(lat) or any(
        not math.isfinite(value) for value in (*x, *y)
    ):
        raise ValueError("coordinate transformation failed: non-finite or incomplete output")
    return list(x), list(y)


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


def prepare_stations(rows, coverage, *, target_puas, sample_size=None, random_seed=None):
    """Merge an entire step-09 source audit, then select/query station entities.

    rows must contain every logical source row, including outside-PUA records.
    The file driver verifies this contract against the original CSV and evidence.
    Coordinates are step-09 projected xy; entity identity uses exact parsed WGS84
    coordinates. Missing/duplicate/conflicting IDs block the entire location.
    """
    from shapely import prepare
    from shapely.geometry import Point
    from src.data import _number
    from src.geography import boundary_distances

    targets = sorted(target_puas)
    if not targets or any(not isinstance(p, str) or not p for p in targets) or len(targets) != len(set(targets)):
        raise ValueError("explicit unique target_puas required")
    if sample_size is not None and (type(sample_size) is not int or sample_size <= 0 or type(random_seed) is not int):
        raise ValueError("sample_size requires a positive integer and explicit integer random_seed")
    if sample_size is None and random_seed is not None:
        raise ValueError("random_seed only applies to sample mode")
    records, ids, locations = [], defaultdict(list), defaultdict(list)
    seen = set()
    for source in rows:
        row = dict(source)
        number = row["source_record_no"]
        if type(number) is not int or number <= 0 or number in seen:
            raise ValueError("source_record_no must be unique positive integers")
        seen.add(number)
        raw_id = row["raw_id"]
        if raw_id is not None and not isinstance(raw_id, str):
            raise ValueError("StationID must preserve CSV text or None")
        normalized = raw_id.strip() if raw_id is not None else ""
        row["normalized_id"] = normalized or None
        row["identity_reasons"] = []
        if normalized:
            ids[normalized].append(row)
        else:
            row["identity_reasons"].append("missing_station_id")
        if raw_id is not None and normalized != raw_id:
            row["identity_reasons"].append("station_id_trimmed")
        state = row["in_target_pua"]
        if state is not None and type(state) is not bool:
            raise ValueError("in_target_pua must be True/False/None")
        for name in ("lad_codes", "pua_names", "target_pua_names"):
            values = row[name]
            if (not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)
                    or values != sorted(set(values))):
                raise ValueError(f"{name} must contain sorted unique names")
        if (row["target_pua_names"] != sorted(set(row["pua_names"]) & set(targets))
                or bool(row["target_pua_names"]) != (state is True)):
            raise ValueError("target PUA state and memberships disagree")
        lon, lat = _number(row["raw_longitude"]), _number(row["raw_latitude"])
        xy = row.get("xy")
        position = None
        if xy is not None:
            if (lon is None or lat is None or not -180 <= lon <= 180 or not -90 <= lat <= 90
                    or state is None or row.get("coordinate_reasons")):
                raise ValueError("projected position contradicts source coordinates or geography state")
            xy = list(_coordinate(xy, "station xy"))
            position = (0.0 if lon == 0 else lon, 0.0 if lat == 0 else lat)
        elif state is not None or row["pua_names"] or row["lad_codes"]:
            raise ValueError("unlocated source row cannot have known geography")
        row["xy"] = xy
        row["position"] = position
        # Hex floats give exact, order-independent numeric identity, including a
        # canonical representation for signed zero. Unlocated records never merge.
        key = ("loc_" + hashlib.sha256((position[0].hex() + "," + position[1].hex()).encode("ascii")).hexdigest()
               if position is not None else f"unresolved_{number:012d}")
        row["station_key"] = key
        locations[key].append(row)
        records.append(row)
    for normalized, group in ids.items():
        if len(group) > 1:
            reasons = ["duplicate_station_id"]
            if len({r["position"] for r in group if r["position"] is not None}) > 1:
                reasons.append("station_id_position_conflict")
            if len({r["raw_id"] for r in group}) > 1:
                reasons.append("station_id_normalization_collision")
            for row in group:
                row["identity_reasons"].extend(reasons)
    stations = []
    for key, group in sorted(locations.items()):
        group.sort(key=lambda r: r["source_record_no"])
        first = group[0]
        for row in group[1:]:
            if any(row[k] != first[k] for k in ("position", "xy", "in_target_pua", "lad_codes", "pua_names", "target_pua_names")):
                raise ValueError("co-located source geography/coordinates disagree")
        identity_reasons = sorted({reason for row in group for reason in row["identity_reasons"]})
        bad_id = bool(set(identity_reasons) & {"missing_station_id", "duplicate_station_id", "station_id_position_conflict"})
        reasons = sorted({reason for row in group for reason in row.get("audit_reasons", [])} | set(identity_reasons))
        if len({r["normalized_id"] for r in group if r["normalized_id"]}) > 1:
            reasons.append("co_located_different_ids_merged")
        position = first["position"]
        stations.append({"station_key": key, "source_record_nos": [r["source_record_no"] for r in group],
                         "raw_ids": [r["raw_id"] for r in group],
                         "normalized_ids": sorted({r["normalized_id"] for r in group if r["normalized_id"]}),
                         "source_row_count": len(group), "entity_resolved": position is not None,
                         "longitude": position[0] if position else None, "latitude": position[1] if position else None,
                         "xy": first["xy"], "lad_codes": first["lad_codes"], "pua_names": first["pua_names"],
                         "target_pua_names": first["target_pua_names"], "in_target_pua": first["in_target_pua"],
                         "selected": first["in_target_pua"] is True, "identity_eligible": not bad_id,
                         "identity_reasons": identity_reasons, "audit_reasons": sorted(set(reasons))})
    distances = boundary_distances([r["xy"] for r in stations], coverage["gb"])
    for name in ("gb", "ni", "order"):
        prepare(coverage[name])
    minx, miny, maxx, maxy = coverage["order_bounds"]
    for row, distance_m in zip(stations, distances, strict=True):
        located = row["xy"] is not None
        point = Point(row["xy"]) if located else None
        gb = bool(coverage["gb"].covers(point)) if located else None
        ni = bool(coverage["ni"].covers(point)) if located else None
        margin = min(row["xy"][0] - minx, maxx - row["xy"][0], row["xy"][1] - miny, maxy - row["xy"][1]) if located else None
        in_order = margin >= 0 if located else None
        spatial = ("unlocated" if not located else "NI" if ni else "outside_GB_proxy" if not gb
                   else "outside_order" if not in_order else "on_GB_boundary" if distance_m == 0 else "within_GB_proxy")
        blockers = []
        if not located:
            blockers.append("invalid_station_position")
        blockers.extend(r for r in ("missing_station_id", "station_id_position_conflict", "duplicate_station_id") if r in row["identity_reasons"])
        if row["in_target_pua"] is None:
            blockers.append("unknown_target_pua")
        elif not row["in_target_pua"]:
            blockers.append("outside_target_pua")
        if ni:
            blockers.append("NI")
        if gb is False:
            blockers.append("outside_GB_proxy")
        if in_order is False:
            blockers.append("outside_order")
        if row["selected"] and gb is not True:
            raise ValueError("selected PUA position lies outside same-source GB proxy")
        row.update(within_gb_proxy=gb, within_ni_proxy=ni, within_order=in_order,
                   gb_boundary_distance_m=distance_m, order_clearance_m=margin, spatial_status=spatial,
                   query_eligible=bool(row["selected"] and located and row["identity_eligible"] and gb and not ni and in_order),
                   main_status=blockers[0] if blockers else spatial, query_blockers=blockers,
                   audit_reasons=sorted(set(row["audit_reasons"] + blockers + [spatial])))
    sample_keys = None
    if sample_size is not None:
        pool = sorted(r["station_key"] for r in stations if r["selected"] and r["main_status"] == "within_GB_proxy")
        sample_keys = sorted(random.Random(random_seed).sample(pool, min(sample_size, len(pool))))
        chosen = set(sample_keys)
        for row in stations:
            if row["selected"] and row["station_key"] not in chosen:
                row["selected"] = row["query_eligible"] = False
                row["query_blockers"].append("not_sampled")
                row["audit_reasons"] = sorted(set(row["audit_reasons"] + ["not_sampled"]))
                row["main_status"] = "not_sampled"
    entities = {r["station_key"]: r for r in stations}
    audit = []
    for row in sorted(records, key=lambda r: r["source_record_no"]):
        entity = entities[row["station_key"]]
        normalized = row["normalized_id"]
        exported = {k: v for k, v in row.items() if k != "position"}
        exported.update(identity_reasons=sorted(set(row["identity_reasons"])),
                        id_source_record_nos=sorted(r["source_record_no"] for r in ids[normalized]) if normalized else [],
                        merge_action="unresolved" if not entity["entity_resolved"] else "merged_same_coordinates" if entity["source_row_count"] > 1 else "single",
                        selected=entity["selected"], query_eligible=entity["query_eligible"],
                        main_status=entity["main_status"], entity_query_blockers=entity["query_blockers"],
                        entity_audit_reasons=entity["audit_reasons"])
        audit.append(exported)
    return {"stations": stations, "audit": audit, "sample_keys": sample_keys,
            "target_puas": targets, "sample_size": sample_size, "random_seed": random_seed}


def station_radius_base(stations):
    """One selected entity × fixed cumulative radius; no POI counts are inferred."""
    rows = []
    indexed = _station_index(stations)
    for key, station in sorted(indexed.items()):
        if not station["selected"]:
            if station["query_eligible"]:
                raise ValueError("unselected station cannot be query eligible")
            continue
        for radius in RADII_M:
            reasons = list(station["query_blockers"])
            if not station["query_eligible"]:
                reasons.append("not_query_eligible")
            if station["within_gb_proxy"] is not True:
                reasons.append("not_within_GB_proxy")
            for field, reason in (("gb_boundary_distance_m", "GB_boundary_within_radius"),
                                  ("order_clearance_m", "order_boundary_within_radius")):
                value = station[field]
                if value is None:
                    reasons.append(field + "_unknown")
                elif not math.isfinite(value):
                    raise ValueError(f"nonfinite {field}")
                elif value < radius:
                    reasons.append(reason)
            rows.append({"station_key": key, "radius_m": radius, "selected": True,
                         "query_eligible": station["query_eligible"], "main_status": station["main_status"],
                         "within_gb_proxy": station["within_gb_proxy"],
                         "gb_boundary_distance_m": station["gb_boundary_distance_m"],
                         "order_clearance_m": station["order_clearance_m"],
                         "coverage_complete_proxy": not reasons, "coverage_reasons": sorted(set(reasons))})
    return rows


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
