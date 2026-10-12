"""Native POI composition: a Python reference and formal DuckDB aggregation."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.spatial import RADII_M, _station_index


def category_labels(dictionary, level_counts, name_frequencies):
    """Choose labels by complete code: source mode, official name, then code.

    Empty/whitespace source names are missing. Ties use Python Unicode string
    order, without changing the original spelling or spaces. All candidates
    remain in the separate name-frequency artifact; conflicts are explicit.
    """
    names = defaultdict(Counter)
    name_totals = defaultdict(int)
    seen = set()
    for row in name_frequencies:
        key = row["level"], row["code"]
        identity = (*key, row["raw_type"], row["raw_value"])
        count = row["count"]
        if identity in seen or type(count) is not int or count <= 0:
            raise ValueError("invalid or duplicate source-name frequency")
        seen.add(identity)
        if key[0] not in level_counts or key[1] not in level_counts[key[0]]:
            raise ValueError("source name has no corresponding source code")
        name_totals[key] += count
        if row["raw_type"] == "text" and row["raw_value"].strip():
            names[key][row["raw_value"]] += count
        elif row["raw_type"] not in ("text", "null"):
            raise ValueError("source classification names must be text or NULL")
    result = []
    for level, official in (("group", dictionary.groups), ("category", dictionary.categories), ("class", dictionary.classes)):
        for code in sorted(set(official) | set(level_counts[level])):
            key = level, code
            count = level_counts[level].get(code, 0)
            if name_totals[key] != count:
                raise ValueError(f"source name counts disagree for {key}")
            candidates = names[key]
            source = min(candidates, key=lambda name: (-candidates[name], name)) if candidates else ""
            name = official.get(code, {}).get("dictionary_name", "")
            label, origin = (source, "source") if source else (name, "dictionary") if name.strip() else (code, "code_fallback")
            matched = code in official and code in level_counts[level]
            status = "not_applicable" if code == "UNKNOWN" else "matched" if matched else "dictionary_only" if code in official else "source_only"
            result.append({"level": level, "code": code, "source_count": count,
                           "source_name": source, "dictionary_name": name, "label": label,
                           "label_source": origin, "dictionary_status": status,
                           "source_name_conflict": len(candidates) > 1,
                           "source_name_candidate_count": len(candidates),
                           "source_name_missing_count": count - sum(candidates.values()),
                           "source_dictionary_name_difference": bool(source and name.strip() and source != name)})
    return result


def summarize_synthetic(
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


@contextmanager
def connection(matches_path, *, temp_directory, memory_limit="512MB", threads=1):
    """Scan Parquet directly; spill files belong to the caller's new run."""
    import duckdb
    if type(threads) is not int or threads <= 0:
        raise ValueError("threads must be a positive integer")
    if not isinstance(memory_limit, str) or not re.fullmatch(r"[1-9][0-9]*(MB|GB)", memory_limit):
        raise ValueError("memory_limit must be a positive integer MB/GB value")
    temporary = Path(temp_directory)
    temporary.mkdir(parents=True, exist_ok=False)
    con = duckdb.connect(config={"threads": threads, "memory_limit": memory_limit,
                                 "temp_directory": str(temporary)})
    try:
        con.from_parquet(str(matches_path)).create_view("matches")
        yield con
    finally:
        con.close()


def _require_empty(con, query, message):
    if con.execute(query).fetchone() is not None:
        raise ValueError(message)


def summary_schemas():
    """Stable nullable Arrow types, including empty files and CSV readback."""
    import pyarrow as pa
    from src.data import station_parquet_schemas
    text, integer, number, flag = pa.string(), pa.int64(), pa.float64(), pa.bool_()
    station = list(station_parquet_schemas()[1]) + [pa.field("observed_count", integer),
                                                  pa.field("interpretable_count", integer)]
    composition = [("station_key", text), ("radius_m", integer), ("level", text), ("code", text),
                   ("dictionary_name", text), ("count", integer), ("total_count", integer),
                   ("share", number), ("coverage_complete_proxy", flag)]
    radius = [("radius_m", integer), ("scope", text)] + [(name, integer) for name in (
        "selected_station_count", "observable_station_count", "unobservable_station_count",
        "incomplete_station_count", "station_count", "positive_station_count", "zero_station_count",
        "match_count", "unique_poi_count")] + [("count_distribution", text)]
    group = [("radius_m", integer), ("scope", text), ("group_code", text), ("dictionary_name", text)]
    group += [(name, integer) for name in ("station_count", "positive_station_count", "group_match_count",
                                         "total_match_count")]
    group += [("station_mean_share", number), ("pooled_match_share", number)]
    return {name: pa.schema(fields, metadata={b"schema_version": b"1", b"step": b"15",
                b"table": name.encode(), b"composition_mode": b"native_group"})
            for name, fields in (("station_radius_totals", station), ("composition", composition),
                                 ("radius_summary", radius), ("group_summary", group))}


def summarize(con, base, *, group_codes, labels, progress=None):
    """Create four typed SQL tables for already prepared matches and radius rows.

    Qualification is checked by summarize_files; this arithmetic layer also
    supports explicitly synthetic UNKNOWN/legacy fixtures. No POI is filtered
    by its label, original functional group or PUA membership here.
    """
    import pyarrow as pa
    groups = list(group_codes)
    if (len(groups) != len(set(groups)) or any(not isinstance(g, str)
            or not re.fullmatch(r"[0-9]{2}|UNKNOWN", g) for g in groups)):
        raise ValueError("group_codes must be unique two-digit codes or UNKNOWN")
    con.register("base", base)
    label_rows = list(labels)
    keys = [(r["level"], r["code"]) for r in label_rows]
    if len(keys) != len(set(keys)) or any(k[0] not in ("group", "category", "class") for k in keys):
        raise ValueError("duplicate or invalid classification label key")
    con.register("labels", pa.Table.from_pylist(label_rows, schema=pa.schema([
        ("level", pa.string()), ("code", pa.string()), ("dictionary_name", pa.string())])))
    _require_empty(con, """SELECT 1 FROM base WHERE station_key IS NULL OR station_key=''
        OR radius_m IS NULL OR radius_m NOT IN (500,1000) OR selected IS NOT TRUE
        OR query_eligible IS NULL OR coverage_complete_proxy IS NULL
        OR (coverage_complete_proxy AND NOT query_eligible) LIMIT 1""", "invalid station-radius base")
    _require_empty(con, """SELECT station_key FROM base GROUP BY station_key
        HAVING count(*)<>2 OR count(DISTINCT radius_m)<>2
        OR count(DISTINCT query_eligible)<>1 LIMIT 1""", "duplicate or incomplete station-radius base")
    _require_empty(con, """SELECT a.station_key FROM base a JOIN base b USING(station_key)
        WHERE a.radius_m=1000 AND b.radius_m=500 AND a.coverage_complete_proxy
        AND NOT b.coverage_complete_proxy LIMIT 1""", "coverage must be cumulative")
    _require_empty(con, """SELECT 1 FROM matches WHERE station_key IS NULL
        OR ref_no_type IS NULL OR ref_no_value IS NULL OR ref_no_value=''
        OR ref_no_type NOT IN ('text','integer','real')
        OR distance_m IS NULL OR NOT isfinite(distance_m) OR distance_m<0 OR distance_m>1000
        OR group_code IS NULL OR category_code IS NULL OR class_code IS NULL
        OR NOT ((regexp_full_match(class_code,'[0-9]{8}') AND group_code=left(class_code,2)
                 AND category_code=left(class_code,4))
                OR (class_code='UNKNOWN' AND category_code='UNKNOWN' AND group_code='UNKNOWN'))
        LIMIT 1""", "invalid match identity, distance or classification")
    _require_empty(con, """SELECT station_key,ref_no_type,ref_no_value FROM matches GROUP BY ALL
        HAVING count(*)<>1 LIMIT 1""", "duplicate station-POI pair")
    _require_empty(con, """SELECT ref_no_type,ref_no_value FROM matches GROUP BY ALL
        HAVING count(DISTINCT (group_code,category_code,class_code))<>1 LIMIT 1""",
        "same POI identity has conflicting classification")
    _require_empty(con, """SELECT m.station_key FROM matches m LEFT JOIN base b
        ON m.station_key=b.station_key AND b.radius_m=500
        WHERE b.query_eligible IS NOT TRUE LIMIT 1""", "matches require a selected queryable station")
    has_unknown = con.execute("SELECT EXISTS(SELECT 1 FROM matches WHERE group_code='UNKNOWN')").fetchone()[0]
    groups = sorted(set(groups) - {"UNKNOWN"} | ({"UNKNOWN"} if has_unknown else set()))
    con.register("group_domain", pa.table({"code": pa.array(groups, type=pa.string())}))
    _require_empty(con, """SELECT DISTINCT group_code FROM matches m
        ANTI JOIN group_domain g ON m.group_code=g.code LIMIT 1""", "missing matched group in group domain")
    con.execute("CREATE TEMP TABLE radii AS SELECT * FROM (VALUES (500::BIGINT),(1000::BIGINT)) r(radius_m)")
    con.execute("""CREATE TEMP TABLE radius_scopes AS SELECT * FROM radii CROSS JOIN
        (VALUES ('all_observable'),('complete_coverage')) s(scope)""")
    con.execute("""CREATE TEMP TABLE radius_matches AS SELECT m.station_key,m.ref_no_type,m.ref_no_value,
        m.group_code,m.category_code,m.class_code,r.radius_m
        FROM matches m CROSS JOIN radii r WHERE m.distance_m<=r.radius_m""")
    con.execute("""CREATE TEMP TABLE station_radius_totals AS SELECT b.*,
        CASE WHEN b.query_eligible THEN coalesce(c.n,0)::BIGINT ELSE NULL END AS observed_count,
        CASE WHEN b.coverage_complete_proxy THEN coalesce(c.n,0)::BIGINT ELSE NULL END AS interpretable_count
        FROM base b LEFT JOIN (SELECT station_key,radius_m,count(*) AS n FROM radius_matches GROUP BY ALL) c
        USING(station_key,radius_m)""")
    _require_empty(con, """SELECT r.radius_m FROM radii r LEFT JOIN
        (SELECT radius_m,sum(observed_count) n FROM station_radius_totals GROUP BY ALL) t USING(radius_m)
        LEFT JOIN (SELECT radius_m,count(*) n FROM radius_matches GROUP BY ALL) m USING(radius_m)
        WHERE coalesce(t.n,0)<>coalesce(m.n,0)""", "station totals not conserved")
    if progress:
        progress("station_totals_verified")

    con.execute("""CREATE TEMP TABLE native_counts AS
        SELECT station_key,radius_m,'group' AS level,group_code AS code,count(*)::BIGINT AS count
        FROM radius_matches GROUP BY ALL UNION ALL
        SELECT station_key,radius_m,'category',category_code,count(*)::BIGINT FROM radius_matches GROUP BY ALL
        UNION ALL SELECT station_key,radius_m,'class',class_code,count(*)::BIGINT
        FROM radius_matches GROUP BY ALL""")
    con.execute("""CREATE TEMP TABLE composition AS WITH cells AS (
        SELECT t.station_key,t.radius_m,'group' AS level,g.code,coalesce(n.count,0)::BIGINT AS count
        FROM station_radius_totals t CROSS JOIN group_domain g LEFT JOIN native_counts n
        ON n.station_key=t.station_key AND n.radius_m=t.radius_m AND n.level='group' AND n.code=g.code
        WHERE t.query_eligible
        UNION ALL SELECT * FROM native_counts WHERE level<>'group')
        SELECT c.station_key,c.radius_m,c.level,c.code,l.dictionary_name,c.count,
        t.observed_count AS total_count,c.count::DOUBLE/nullif(t.observed_count,0) AS share,
        t.coverage_complete_proxy FROM cells c JOIN station_radius_totals t USING(station_key,radius_m)
        LEFT JOIN labels l USING(level,code)""")
    _require_empty(con, """SELECT t.station_key,t.radius_m,l.level FROM station_radius_totals t
        CROSS JOIN (VALUES ('group'),('category'),('class')) l(level)
        LEFT JOIN (SELECT station_key,radius_m,level,sum(count) n,sum(share) p FROM composition GROUP BY ALL) c
        USING(station_key,radius_m,level) WHERE t.query_eligible AND
        (coalesce(c.n,0)<>t.observed_count OR (t.observed_count>0 AND (c.p IS NULL OR abs(c.p-1)>1e-12)))""",
        "classification counts or shares not conserved")
    if progress:
        progress("composition_verified")

    con.execute("""CREATE TEMP TABLE scope_population AS SELECT t.*,s.scope FROM station_radius_totals t
        JOIN radius_scopes s USING(radius_m) WHERE t.query_eligible
        AND (s.scope='all_observable' OR t.coverage_complete_proxy)""")
    con.execute("""CREATE TEMP TABLE radius_summary AS WITH sizes AS (
        SELECT radius_m,count(*)::BIGINT selected_station_count,
        count(*) FILTER (WHERE query_eligible)::BIGINT observable_station_count,
        count(*) FILTER (WHERE NOT query_eligible)::BIGINT unobservable_station_count,
        count(*) FILTER (WHERE query_eligible AND NOT coverage_complete_proxy)::BIGINT incomplete_station_count
        FROM station_radius_totals GROUP BY ALL), counts AS (
        SELECT radius_m,scope,count(*)::BIGINT station_count,
        count(*) FILTER (WHERE observed_count>0)::BIGINT positive_station_count,
        count(*) FILTER (WHERE observed_count=0)::BIGINT zero_station_count,
        sum(observed_count)::BIGINT match_count FROM scope_population GROUP BY ALL), unique_pois AS (
        SELECT p.radius_m,p.scope,count(DISTINCT (m.ref_no_type,m.ref_no_value))::BIGINT unique_poi_count
        FROM scope_population p JOIN radius_matches m USING(station_key,radius_m) GROUP BY ALL), distributions AS (
        SELECT radius_m,scope,to_json(map(list(observed_count::VARCHAR ORDER BY observed_count),
            list(n ORDER BY observed_count)))::VARCHAR count_distribution FROM (
            SELECT radius_m,scope,observed_count,count(*)::BIGINT n FROM scope_population GROUP BY ALL) h GROUP BY ALL)
        SELECT s.radius_m,s.scope,coalesce(z.selected_station_count,0)::BIGINT selected_station_count,
        coalesce(z.observable_station_count,0)::BIGINT observable_station_count,
        coalesce(z.unobservable_station_count,0)::BIGINT unobservable_station_count,
        coalesce(z.incomplete_station_count,0)::BIGINT incomplete_station_count,
        coalesce(c.station_count,0)::BIGINT station_count,coalesce(c.positive_station_count,0)::BIGINT positive_station_count,
        coalesce(c.zero_station_count,0)::BIGINT zero_station_count,coalesce(c.match_count,0)::BIGINT match_count,
        coalesce(u.unique_poi_count,0)::BIGINT unique_poi_count,coalesce(d.count_distribution,'{}') count_distribution
        FROM radius_scopes s LEFT JOIN sizes z USING(radius_m) LEFT JOIN counts c USING(radius_m,scope)
        LEFT JOIN unique_pois u USING(radius_m,scope) LEFT JOIN distributions d USING(radius_m,scope)""")
    con.execute("""CREATE TEMP TABLE group_summary AS WITH counts AS (
        SELECT c.radius_m,p.scope,c.code,sum(c.count)::BIGINT group_match_count,avg(c.share) station_mean_share
        FROM composition c JOIN scope_population p USING(station_key,radius_m) WHERE c.level='group' GROUP BY ALL)
        SELECT r.radius_m,r.scope,g.code group_code,l.dictionary_name,r.station_count,r.positive_station_count,
        coalesce(c.group_match_count,0)::BIGINT group_match_count,r.match_count total_match_count,
        c.station_mean_share,coalesce(c.group_match_count,0)::DOUBLE/nullif(r.match_count,0) pooled_match_share
        FROM radius_summary r CROSS JOIN group_domain g LEFT JOIN counts c
        ON r.radius_m=c.radius_m AND r.scope=c.scope AND g.code=c.code
        LEFT JOIN labels l ON l.level='group' AND l.code=g.code""")
    _require_empty(con, """SELECT r.radius_m,r.scope FROM radius_summary r LEFT JOIN (
        SELECT radius_m,scope,sum(group_match_count) n,sum(station_mean_share) s,sum(pooled_match_share) p
        FROM group_summary GROUP BY ALL) g USING(radius_m,scope) WHERE coalesce(g.n,0)<>r.match_count
        OR (r.positive_station_count>0 AND (g.s IS NULL OR abs(g.s-1)>1e-12))
        OR (r.match_count>0 AND (g.p IS NULL OR abs(g.p-1)>1e-12))""", "summary counts or shares not conserved")
    if progress:
        progress("summaries_verified")
    return {"group_codes": groups, "unknown_expanded": has_unknown,
            "row_counts": {name: con.execute(f'SELECT count(*) FROM {name}').fetchone()[0]
                           for name in summary_schemas()}}
