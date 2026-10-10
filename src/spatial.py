"""Simple projected-coordinate distance queries for the rewrite samples."""

from __future__ import annotations

import math
from collections.abc import Iterable


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
