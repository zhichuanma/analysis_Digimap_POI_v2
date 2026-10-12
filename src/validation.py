"""Independent streaming Python reconciliation and original-record traceability.

No calls to production classification, matching, SQL aggregation or summaries.
Memory holds one station's matches/cells and distinct POI identities per scope.
"""
from collections import Counter, defaultdict
import csv
import hashlib
import itertools
import json
import math
from pathlib import Path
import random
import resource
import sqlite3
import struct
import time


RADII = (500, 1000)
SCOPES = ('all_observable', 'complete_coverage')
TEXT = {'station_key', 'scope', 'group_code', 'dictionary_name', 'main_status', 'count_distribution'}
FLAGS = {'selected', 'query_eligible', 'within_gb_proxy', 'coverage_complete_proxy'}
FLOATS = {'gb_boundary_distance_m', 'order_clearance_m', 'station_mean_share', 'pooled_match_share'}


def fingerprint(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
            size += len(block)
    return {'size_bytes': size, 'sha256': digest.hexdigest()}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def csv_rows(path):
    with Path(path).open(encoding='utf-8', newline='') as stream:
        for raw in csv.DictReader(stream, strict=True):
            row = {}
            for key, value in raw.items():
                if value == '':
                    row[key] = None
                elif key in TEXT:
                    row[key] = value
                elif key in FLAGS:
                    if value not in ('True', 'False'):
                        raise ValueError(f'invalid boolean {key}: {value}')
                    row[key] = value == 'True'
                elif key in FLOATS:
                    row[key] = float(value)
                elif key == 'coverage_reasons':
                    row[key] = json.loads(value)
                else:
                    row[key] = int(value)
            yield row


def parquet_rows(path, columns=None):
    import pyarrow.parquet as pq
    with pq.ParquetFile(path) as source:
        for batch in source.iter_batches(batch_size=10000, columns=columns):
            yield from batch.to_pylist()


class ValidationError(ValueError):
    pass


class Checks:
    def __init__(self):
        self.counts = Counter()
        self.failures = []
        self.max_share_difference = 0.0

    def require(self, name, condition, detail=''):
        self.counts[name] += 1
        if not condition:
            self.failures.append({'check': name, 'detail': str(detail)})
            raise ValidationError(f'{name}: {detail}')

    def row(self, name, actual, expected):
        self.require(name + '.fields', actual is not None and actual.keys() == expected.keys(), expected)
        for field, value in expected.items():
            got = actual[field]
            if isinstance(value, float):
                delta = abs(got - value) if type(got) in (int, float) and math.isfinite(got) else math.inf
                if 'share' in field and math.isfinite(delta):
                    self.max_share_difference = max(self.max_share_difference, delta)
                self.require(name + '.' + field, delta <= (1e-12 if 'share' in field else 1e-9), (got, value))
            else:
                self.require(name + '.' + field, got == value, (got, value))


def coverage(station, radius):
    reasons = list(station['query_blockers'])
    if not station['query_eligible']:
        reasons.append('not_query_eligible')
    if station['within_gb_proxy'] is not True:
        reasons.append('not_within_GB_proxy')
    for field, reason in (('gb_boundary_distance_m', 'GB_boundary_within_radius'),
                          ('order_clearance_m', 'order_boundary_within_radius')):
        if station[field] is None:
            reasons.append(field + '_unknown')
        elif station[field] < radius:
            reasons.append(reason)
    return not reasons, sorted(set(reasons))


def validate_tables(stations, matches, totals, composition, radius_summary, group_summary,
                    *, groups, labels, decisions=None, checks=None, trace_keys=()):
    """Compare all cells, independently reconstructing each denominator.

    Formal matches and composition are in station-key order. Refuse a broken
    order instead of silently omitting an earlier station. No full match list.
    """
    c = checks if checks is not None else Checks()
    selected = {s['station_key']: s for s in stations if s['selected']}
    c.require('station_keys', len({s['station_key'] for s in stations}) == len(stations))
    actual_totals = {}
    for row in totals:
        key = row['station_key'], row['radius_m']
        c.require('total_key_unique', key not in actual_totals, key)
        actual_totals[key] = row
    c.require('station_retention', set(actual_totals) == {(s, r) for s in selected for r in RADII})
    expected_totals = []
    unique = {(r, scope): set() for r in RADII for scope in SCOPES}
    group_counts = defaultdict(Counter)
    shares = defaultdict(lambda: defaultdict(list))
    composition = iter(composition)
    grouped = iter(itertools.groupby(matches, key=lambda m: m['station_key']))
    pending = next(grouped, None)
    trace = []
    match_count = cell_count = 0
    for key, station in sorted(selected.items()):
        c.require('match_station_order', pending is None or pending[0] >= key, key)
        rows = list(pending[1]) if pending is not None and pending[0] == key else []
        if pending is not None and pending[0] == key:
            pending = next(grouped, None)
        seen = set()
        for row in rows:
            identity = row['ref_no_type'], row['ref_no_value']
            c.require('pair_unique', identity not in seen, (key, identity))
            seen.add(identity)
            c.require('match_queryable', station['query_eligible'], key)
            c.require('identity', identity[0] in ('text', 'integer', 'real') and isinstance(identity[1], str) and bool(identity[1].strip()), identity)
            distance = row['distance_m']
            c.require('distance_range', math.isfinite(distance) and 0 <= distance <= 1000, row)
            code = row['class_code']
            c.require('classification_prefix', row['group_code'] == code[:2] and row['category_code'] == code[:4], code)
            c.require('group_domain', row['group_code'] in groups, row['group_code'])
            if decisions is not None:
                c.require('research_qualification', decisions.get(code) == 'include' and code != '10540832'
                          and row['base_eligible'] is True and row['analysis_include'] is True, code)
            if 'x' in row:
                c.require('station_xy', [row['station_x'], row['station_y']] == list(station['xy']), key)
                calculated = math.hypot(row['x'] - station['xy'][0], row['y'] - station['xy'][1])
                c.require('match_geometry_distance', calculated == distance, (calculated, distance))
        match_count += len(rows)
        if key in trace_keys and rows:
            # Deterministic first/last/closest/furthest records for each sampled station.
            chosen = {0, len(rows) - 1, min(range(len(rows)), key=lambda i: rows[i]['distance_m']),
                      max(range(len(rows)), key=lambda i: rows[i]['distance_m'])}
            trace.extend(rows[i] for i in sorted(chosen))
        previous_total = 0
        previous_classes = Counter()
        expected_cells = []
        for radius in RADII:
            complete, reasons = coverage(station, radius)
            within = [m for m in rows if m['distance_m'] <= radius]
            total = len(within) if station['query_eligible'] else None
            expected = dict(station_key=key, radius_m=radius, selected=True, query_eligible=station['query_eligible'],
                main_status=station['main_status'], within_gb_proxy=station['within_gb_proxy'],
                gb_boundary_distance_m=station['gb_boundary_distance_m'], order_clearance_m=station['order_clearance_m'],
                coverage_complete_proxy=complete, coverage_reasons=reasons, observed_count=total,
                interpretable_count=total if complete else None)
            c.row('station_total', actual_totals[key, radius], expected)
            expected_totals.append(expected)
            if total is None:
                continue
            c.require('cumulative_total', total >= previous_total, key)
            previous_total = total
            classes = Counter(m['class_code'] for m in within)
            c.require('cumulative_class', all(classes[k] >= n for k, n in previous_classes.items()), key)
            previous_classes = classes
            for level in ('group', 'category', 'class'):
                counts = Counter(m[level + '_code'] for m in within)
                c.require('layer_conservation', sum(counts.values()) == total, (key, radius, level))
                for code in groups if level == 'group' else sorted(counts):
                    expected_cells.append(dict(station_key=key, radius_m=radius, level=level, code=code,
                        dictionary_name=labels.get((level, code)), count=counts[code], total_count=total,
                        share=counts[code] / total if total else None, coverage_complete_proxy=complete))
                if level == 'group':
                    for scope in SCOPES if complete else ('all_observable',):
                        group_counts[radius, scope].update(counts)
                        unique[radius, scope].update((m['ref_no_type'], m['ref_no_value']) for m in within)
                        if total:
                            for code in groups:
                                shares[radius, scope][code].append(counts[code] / total)
            for level in ('group', 'category', 'class'):
                if total:
                    c.require('share_conservation', abs(math.fsum(x['share'] for x in expected_cells
                              if x['radius_m'] == radius and x['level'] == level) - 1) <= 1e-12)
        for expected in sorted(expected_cells, key=lambda r: (r['radius_m'], r['level'], r['code'])):
            c.row('composition_cell', next(composition, None), expected)
            cell_count += 1
    c.require('no_unselected_matches', pending is None)
    c.require('no_extra_composition', next(composition, None) is None)
    expected_radius, expected_groups = [], []
    for radius in RADII:
        observed = [r for r in expected_totals if r['radius_m'] == radius and r['query_eligible']]
        for scope in SCOPES:
            population = observed if scope == 'all_observable' else [r for r in observed if r['coverage_complete_proxy']]
            n = sum(r['observed_count'] for r in population)
            positive = sum(r['observed_count'] > 0 for r in population)
            expected_radius.append(dict(radius_m=radius, scope=scope, selected_station_count=len(selected),
                observable_station_count=len(observed), unobservable_station_count=len(selected) - len(observed),
                incomplete_station_count=sum(not r['coverage_complete_proxy'] for r in observed), station_count=len(population),
                positive_station_count=positive, zero_station_count=len(population) - positive, match_count=n,
                unique_poi_count=len(unique[radius, scope]),
                count_distribution=json.dumps(dict(sorted(Counter(r['observed_count'] for r in population).items())), separators=(',', ':'))))
            for code in groups:
                count = group_counts[radius, scope][code]
                values = shares[radius, scope][code]
                expected_groups.append(dict(radius_m=radius, scope=scope, group_code=code,
                    dictionary_name=labels.get(('group', code)), station_count=len(population), positive_station_count=positive,
                    group_match_count=count, total_match_count=n, station_mean_share=math.fsum(values) / positive if positive else None,
                    pooled_match_share=count / n if n else None))
    for name, actual, expected, fields in (
        ('radius_summary', radius_summary, expected_radius, ('radius_m', 'scope')),
        ('group_summary', group_summary, expected_groups, ('radius_m', 'scope', 'group_code'))):
        actual = list(actual)
        c.require(name + '.rows', len(actual) == len(expected))
        key = lambda r: tuple(r[k] for k in fields)
        for a, e in zip(sorted(actual, key=key), sorted(expected, key=key)):
            c.row(name, a, e)
    return dict(match_count=match_count, selected_station_count=len(selected), composition_rows=cell_count,
                radius_summary=expected_radius, trace_matches=trace)


def decode_gpkg(blob):
    from shapely import from_wkb
    if blob[:2] != b'GP' or len(blob) < 8:
        raise ValidationError('raw GPKG header')
    envelope = (blob[3] >> 1) & 7
    sizes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    if envelope not in sizes:
        raise ValidationError('raw GPKG envelope')
    srs = struct.unpack('<i' if blob[3] & 1 else '>i', blob[4:8])[0]
    if srs != 27700:
        raise ValidationError('raw GPKG CRS')
    return from_wkb(blob[8 + sizes[envelope]:])


def trace_original(rows, *, data_root, schema, checks):
    """Read each requested FID using SQLite read-only, not the production reader."""
    root = Path(data_root).resolve()
    result = []
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    for (relative, layer), records in itertools.groupby(sorted(rows, key=lambda r: (r['source_path'], r['source_layer'])),
                                                      key=lambda r: (r['source_path'], r['source_layer'])):
        path = (root / relative).resolve()
        checks.require('raw_path', path.is_relative_to(root / 'data/raw'))
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as con:
            con.row_factory = sqlite3.Row
            for match in records:
                raw = con.execute(f'SELECT * FROM {quote(layer)} WHERE {quote(schema["fid_column"])}=?', (match['source_fid'],)).fetchone()
                checks.require('raw_fid_exists', raw is not None, match['source_fid'])
                fields = schema['field_mapping']
                identity = raw[fields['ref_no']]
                kind = 'text' if isinstance(identity, str) else 'integer' if isinstance(identity, int) else 'real'
                checks.require('raw_identity', (kind, str(identity)) == (match['ref_no_type'], match['ref_no_value']), match['source_fid'])
                geom = decode_gpkg(raw[schema['geometry']['column_name']])
                checks.require('raw_geometry', geom.geom_type == 'Point' and not geom.is_empty and [geom.x, geom.y] == [match['x'], match['y']])
                distance = math.hypot(geom.x - match['station_x'], geom.y - match['station_y'])
                checks.require('raw_distance', distance == match['distance_m'] and distance <= 1000, match['source_fid'])
                checks.require('raw_class', str(raw[fields['raw_code']]) == match['class_code'], match['source_fid'])
                ax, ay = raw[fields['easting']], raw[fields['northing']]
                try:
                    attribute_delta = math.hypot(float(ax) - geom.x, float(ay) - geom.y)
                    if not math.isfinite(attribute_delta):
                        attribute_delta = None
                except (TypeError, ValueError):
                    attribute_delta = None
                result.append(dict(station_key=match['station_key'], source_path=relative, source_layer=layer,
                    source_fid=match['source_fid'], ref_no_type=kind, ref_no_value=str(identity), class_code=match['class_code'],
                    station_x=match['station_x'], station_y=match['station_y'], raw_geometry_x=geom.x, raw_geometry_y=geom.y,
                    stored_distance_m=match['distance_m'], raw_geometry_distance_m=distance,
                    distance_difference_m=distance - match['distance_m'], attribute_geometry_difference_m=attribute_delta))
    return result


def trace_stations(stations, *, source_path, fields, coordinate_operation, checks):
    from pyproj import Transformer
    transformer = Transformer.from_pipeline(coordinate_operation)
    requested = {n: s for s in stations for n in s['source_record_nos']}
    rows = []
    with Path(source_path).open(newline='', encoding='utf-8-sig') as stream:
        for number, raw in enumerate(csv.DictReader(stream), 1):
            if number not in requested:
                continue
            station = requested[number]
            lon, lat = float(raw[fields['longitude']]), float(raw[fields['latitude']])
            checks.require('raw_station_location', (lon, lat) == (station['longitude'], station['latitude']), number)
            checks.require('raw_station_id', raw[fields['id']] in station['raw_ids'], number)
            xy = transformer.transform(lon, lat, errcheck=True)
            checks.require('raw_station_projected_xy', list(xy) == station['xy'], number)
            rows.append(dict(station_key=station['station_key'], source_record_no=number, raw_id=raw[fields['id']],
                longitude=lon, latitude=lat, projected_x=xy[0], projected_y=xy[1], source_path=str(source_path)))
    checks.require('raw_station_records_present', len(rows) == len(requested))
    return rows


def validate(summary_dir, *, data_root, output_dir, seed=20261012, trace_station_count=12):
    """Validate sealed formal output, writing an independent report or failure."""
    root = Path(data_root).resolve()
    source = (root / summary_dir).resolve()
    output = (root / output_dir).resolve()
    if not source.is_relative_to(root / 'outputs') or not output.is_relative_to(root / 'outputs') or source == output or output.is_relative_to(source):
        raise ValueError('validation paths must be independent outputs directories')
    output.mkdir(parents=True, exist_ok=False)
    started, checks = time.monotonic(), Checks()
    result = dict(status='running', summary_dir=str(source), errors=[], geometry_policy='raw GPKG geometry; attributes audited separately')
    try:
        report = read_json(source / 'summary_report.json')
        checks.require('summary_completed', report['status'] == 'completed' and report['validation_complete'])
        watched = dict(report['input_fingerprints_after'])
        watched.update({str(source / name): fp for name, fp in report['artifacts'].items()})
        watched[str(source / 'summary_report.json')] = fingerprint(source / 'summary_report.json')
        for name, expected in watched.items():
            checks.require('sealed_input_artifact', fingerprint(name) == expected, name)
        matching_dir = root / report['matching_dir']
        matching = read_json(matching_dir / 'matching_report.json')
        preparation = read_json(root / matching['preparation_dir'] / 'preparation_report.json')
        station_dir = root / matching['stations']['provenance']['directory']
        dictionary_dir = root / matching['upstream']['upstream']['step07']['directory']
        stations = list(parquet_rows(station_dir / 'stations.parquet'))
        labels = {}
        with (dictionary_dir / 'native_dictionary.csv').open(newline='') as stream:
            for row in csv.DictReader(stream):
                labels[row['level'], row['code']] = row['dictionary_name'] or None
        groups = {code for level, code in labels if level == 'group'}
        with (dictionary_dir / 'classification_dictionary.csv').open(newline='') as stream:
            for row in csv.DictReader(stream):
                if len(row['class_code']) == 8 and row['class_code'].isascii() and row['class_code'].isdigit():
                    groups.add(row['class_code'][:2])
        with (root / matching['preparation_dir'] / 'research_rules_snapshot.csv').open(newline='') as stream:
            decisions = {r['class_code']: r['decision'] for r in csv.DictReader(stream)}
        keys = sorted(s['station_key'] for s in stations if s['selected'] and s['query_eligible'])
        trace_keys = random.Random(seed).sample(keys, min(trace_station_count, len(keys)))
        calculated = validate_tables(stations, parquet_rows(matching_dir / 'matches.parquet'),
            csv_rows(source / 'station_radius_totals.csv'), parquet_rows(source / 'composition.parquet'),
            csv_rows(source / 'radius_summary.csv'), csv_rows(source / 'group_summary.csv'),
            groups=sorted(groups), labels=labels, decisions=decisions, checks=checks, trace_keys=trace_keys)
        traces = trace_original(calculated.pop('trace_matches'), data_root=root, schema=preparation['input_schema'], checks=checks)
        inspection = read_json(root / matching['upstream']['upstream']['step06']['directory'] / 'input_inspection.json')
        definition = read_json(station_dir / 'coverage_definition.json')
        station_traces = trace_stations([s for s in stations if s['station_key'] in trace_keys],
            source_path=root / inspection['stations']['path'], fields=inspection['stations']['field_mapping'],
            coordinate_operation=definition['coordinate_operation'], checks=checks)
        station_fields = ['station_key','source_record_no','raw_id','longitude','latitude','projected_x','projected_y','source_path']
        with (output / 'original_station_trace.csv').open('x', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=station_fields)
            writer.writeheader()
            writer.writerows(station_traces)
        fields = list(traces[0]) if traces else ['station_key', 'source_path', 'source_layer', 'source_fid', 'ref_no_type', 'ref_no_value', 'raw_geometry_distance_m']
        with (output / 'original_record_trace.csv').open('x', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(traces)
        for name, expected in watched.items():
            checks.require('inputs_outputs_unchanged', fingerprint(name) == expected, name)
        result.update(calculated, status='passed', trace_station_keys=sorted(trace_keys), trace_record_count=len(traces),
                      station_source_record_count=len(station_traces), random_seed=seed, input_artifact_fingerprints=watched, input_bytes_unchanged=True)
    except Exception as error:
        result.update(status='failed', errors=[f'{type(error).__name__}: {error}'])
        raise
    finally:
        result.update(check_count=sum(checks.counts.values()), check_counts=dict(checks.counts), failures=checks.failures,
            max_share_difference=checks.max_share_difference, elapsed_seconds=time.monotonic() - started,
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        with (output / 'validation_checks.csv').open('x', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['check', 'assertions', 'passed'])
            writer.writeheader()
            writer.writerows(dict(check=k, assertions=n, passed=not any(f['check'] == k for f in checks.failures)) for k, n in sorted(checks.counts.items()))
        write_json(output / 'independent_validation.json', result)
    return result
