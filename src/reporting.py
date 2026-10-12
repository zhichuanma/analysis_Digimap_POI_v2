"""Deterministic review exports and figures from the current run's saved tables."""
import csv
import json
import math
from pathlib import Path
import random
import sqlite3
import struct
import time

from src.validation import csv_rows, fingerprint, parquet_rows, read_json, validate, write_json


def output_path(data_root, output_dir, summary_dir):
    root = Path(data_root).resolve()
    source = (root / summary_dir).resolve()
    output = (root / output_dir).resolve()
    if (not source.is_relative_to(root / 'outputs') or output == root / 'outputs'
            or not output.is_relative_to(root / 'outputs')
            or output.is_relative_to(source) or source.is_relative_to(output)):
        raise ValueError('report output must be independent and strictly inside outputs/')
    return output


def write_csv(path, rows, fields):
    with Path(path).open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(row.get(k), ensure_ascii=False) if isinstance(row.get(k), (list, dict))
                             else row.get(k) for k in fields})


def select_review(totals, *, primary_radius=1000, seed=20261012, random_count=4):
    rows = sorted((r for r in totals if r['radius_m'] == primary_radius), key=lambda r: r['station_key'])
    reasons = {}
    def add(row, reason):
        if row is not None:
            reasons.setdefault(row['station_key'], set()).add(reason)
    ordered = sorted((r for r in rows if r['observed_count'] is not None), key=lambda r: (r['observed_count'], r['station_key']))
    if ordered:
        add(ordered[0], 'low')
        add(ordered[len(ordered) // 2], 'median')
        add(ordered[-1], 'high')
        add(next((r for r in ordered if r['observed_count'] == 0), None), 'zero')
    edge = [r for r in rows if r['query_eligible'] and not r['coverage_complete_proxy']]
    if edge:
        add(min(edge, key=lambda r: (min(r['gb_boundary_distance_m'], r['order_clearance_m']), r['station_key'])), 'edge')
    add(next((r for r in rows if not r['query_eligible']), None), 'unobservable')
    for row in random.Random(seed).sample(rows, min(random_count, len(rows))):
        add(row, 'random')
    return {key: sorted(value) for key, value in sorted(reasons.items())}


def write_geopackage(path, layers):
    """Write OGC GeoPackage core feature tables, including explicit empty layers."""
    from pyproj import CRS
    from shapely import from_wkb
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    with sqlite3.connect(path) as con:
        con.executescript('''PRAGMA application_id=1196444487; PRAGMA user_version=10300;
            CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT NOT NULL,srs_id INTEGER NOT NULL PRIMARY KEY,
                organization TEXT NOT NULL,organization_coordsys_id INTEGER NOT NULL,definition TEXT NOT NULL,description TEXT);
            CREATE TABLE gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY,data_type TEXT NOT NULL,identifier TEXT UNIQUE,
                description TEXT DEFAULT '',last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                min_x DOUBLE,min_y DOUBLE,max_x DOUBLE,max_y DOUBLE,srs_id INTEGER);
            CREATE TABLE gpkg_geometry_columns (table_name TEXT NOT NULL,column_name TEXT NOT NULL,geometry_type_name TEXT NOT NULL,
                srs_id INTEGER NOT NULL,z TINYINT NOT NULL,m TINYINT NOT NULL,PRIMARY KEY(table_name,column_name));''')
        con.executemany('INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)', [
            ('Undefined Cartesian', -1, 'NONE', -1, 'undefined', ''), ('Undefined geographic', 0, 'NONE', 0, 'undefined', ''),
            ('OSGB36 / British National Grid', 27700, 'EPSG', 27700, CRS.from_epsg(27700).to_wkt('WKT1_GDAL'), 'metres')])
        for name, geometry_type, fields, features in layers:
            # Layer/field identifiers are internal constants, never external SQL.
            columns = ','.join('"' + key + '" ' + kind for key, kind in fields)
            con.execute(f'CREATE TABLE "{name}" (fid INTEGER PRIMARY KEY AUTOINCREMENT, geom BLOB, {columns})')
            con.execute('INSERT INTO gpkg_contents(table_name,data_type,identifier,srs_id) VALUES (?,?,?,?)', (name, 'features', name, 27700))
            con.execute('INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)', (name, 'geom', geometry_type, 27700, 0, 0))
            bounds = []
            for geom, properties in features:
                blob = None if geom is None else b'GP' + bytes((0, 1)) + struct.pack('<i', 27700) + geom.wkb
                values = [blob, *[properties.get(key) for key, _ in fields]]
                con.execute(f'INSERT INTO "{name}" (geom,{",".join(chr(34)+k+chr(34) for k,_ in fields)}) VALUES ({",".join("?" for _ in values)})', values)
                if geom is not None:
                    bounds.append(geom.bounds)
            if bounds:
                con.execute('UPDATE gpkg_contents SET min_x=?,min_y=?,max_x=?,max_y=? WHERE table_name=?',
                    (min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds), max(b[3] for b in bounds), name))
            restored = con.execute(f'SELECT geom,{",".join(chr(34)+k+chr(34) for k,_ in fields)} FROM "{name}" ORDER BY fid').fetchall()
            if len(restored) != len(features):
                raise ValueError('GeoPackage feature count changed')
            for saved, (geom, properties) in zip(restored, features):
                if (saved[0] is None) != (geom is None) or (geom is not None and not from_wkb(saved[0][8:]).equals_exact(geom, 0)):
                    raise ValueError('GeoPackage geometry readback differs')
                if list(saved[1:]) != [properties.get(key) for key, _ in fields]:
                    raise ValueError('GeoPackage property readback differs')
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('GeoPackage SQLite integrity failed')
    return {name: len(features) for name, _, _, features in layers}


def export_review(summary_dir, *, data_root, output_dir, primary_radius=1000, seed=20261012):
    from shapely.geometry import Point
    root = Path(data_root)
    source, out = root / summary_dir, output_path(root, output_dir, summary_dir)
    out.mkdir(parents=True, exist_ok=False)
    summary = read_json(source / 'summary_report.json')
    matching = read_json(root / summary['matching_dir'] / 'matching_report.json')
    station_dir = root / matching['stations']['provenance']['directory']
    stations = {s['station_key']: s for s in parquet_rows(station_dir / 'stations.parquet') if s['selected']}
    totals = list(csv_rows(source / 'station_radius_totals.csv'))
    selected = select_review(totals, primary_radius=primary_radius, seed=seed)
    by_key = {(r['station_key'], r['radius_m']): r for r in totals}
    samples = [dict(station_key=key, selection_reasons=reasons, source_record_nos=stations[key]['source_record_nos'],
        raw_ids=stations[key]['raw_ids'], pua_names=stations[key]['pua_names'], x=stations[key]['xy'][0], y=stations[key]['xy'][1],
        primary_radius_m=primary_radius, observed_count=by_key[key, primary_radius]['observed_count'],
        coverage_complete_proxy=by_key[key, primary_radius]['coverage_complete_proxy'],
        query_eligible=stations[key]['query_eligible']) for key, reasons in selected.items()]
    fields = ['station_key', 'selection_reasons', 'source_record_nos', 'raw_ids', 'pua_names', 'x', 'y',
              'primary_radius_m', 'observed_count', 'coverage_complete_proxy', 'query_eligible']
    write_csv(out / 'sample_stations.csv', samples, fields)
    cells = [r for r in parquet_rows(source / 'composition.parquet') if r['station_key'] in selected]
    write_csv(out / 'sample_composition.csv', cells, ['station_key', 'radius_m', 'level', 'code', 'dictionary_name', 'count', 'total_count', 'share', 'coverage_complete_proxy'])
    matches = [r for r in parquet_rows(root / summary['matching_dir'] / 'matches.parquet') if r['station_key'] in selected]
    match_fields = ['station_key', 'source_path', 'source_layer', 'source_fid', 'ref_no_type', 'ref_no_value',
                    'x', 'y', 'distance_m', 'group_code', 'category_code', 'class_code']
    write_csv(out / 'sample_matches.csv', matches, match_fields)
    for key in selected:
        for radius in (500, 1000):
            count = sum(m['station_key'] == key and m['distance_m'] <= radius for m in matches)
            expected = by_key[key, radius]['observed_count']
            if count != (expected if expected is not None else 0):
                raise ValueError('review filtered match count differs from totals')
    station_fields = [('station_key', 'TEXT'), ('observed_count', 'INTEGER'), ('coverage_complete', 'INTEGER'), ('reasons', 'TEXT')]
    station_features = [(Point(s['x'], s['y']), dict(station_key=s['station_key'], observed_count=s['observed_count'],
        coverage_complete=int(s['coverage_complete_proxy']), reasons=json.dumps(s['selection_reasons']))) for s in samples]
    buffer_features = [(Point(*stations[key]['xy']).buffer(radius, quad_segs=64),
        dict(station_key=key, radius_m=radius, observed_count=by_key[key, radius]['observed_count'])) for key in selected for radius in (500, 1000)]
    match_geo_fields = [(key, 'INTEGER' if key == 'source_fid' else 'REAL' if key in ('distance_m', 'x', 'y') else 'TEXT') for key in match_fields]
    layers = [('stations', 'POINT', station_fields, station_features),
              ('cumulative_buffers', 'POLYGON', [('station_key', 'TEXT'), ('radius_m', 'INTEGER'), ('observed_count', 'INTEGER')], buffer_features),
              ('station_poi_matches', 'POINT', match_geo_fields, [(Point(r['x'], r['y']), r) for r in matches])]
    counts = write_geopackage(out / 'review.gpkg', layers)
    (out / 'REVIEW.md').write_text(f'''# Review sample

Primary radius: {primary_radius} m. Seed: {seed}. Unique stations: {len(samples)}. Match rows: {len(matches)}.
Selection reasons are combined when low/median/high/edge/random picks overlap.
All three EPSG:27700 GeoPackage layers exist even when their feature count is zero.
Buffers use 64 segments per quadrant for display. Counts use exact distance <= radius.
Filter station_poi_matches by station_key and distance_m <= 500 or <= 1000; compare with station_radius_totals.csv.
Shared POIs have one feature per station relationship and can overlap on the map.
Open source_path from this data root, select source_layer, and locate its integer primary key source_fid.
Check ref_no_type/ref_no_value as well as source_fid; string and numeric identities are distinct.
Station source_record_nos are one-based logical records of the original charging CSV (excluding its header).
The independent validation directory contains original-record trace checks. An exported sample is not a claim of manual QGIS review.
''', encoding='utf-8')
    result = dict(status='completed', primary_radius_m=primary_radius, random_seed=seed, station_count=len(samples),
                  composition_rows=len(cells), match_rows=len(matches), layer_counts=counts, crs='EPSG:27700',
                  buffers_are_display_only=True, selection=selected, manual_qgis_review=False)
    write_json(out / 'review_report.json', result)
    return result


def figures(summary_dir, *, data_root, output_dir, primary_radius=1000):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    root, out = Path(data_root), output_path(data_root, output_dir, summary_dir)
    source = root / summary_dir
    out.mkdir(parents=True, exist_ok=False)
    groups = list(csv_rows(source / 'group_summary.csv'))
    radii = list(csv_rows(source / 'radius_summary.csv'))
    fig, axes = plt.subplots(1, 2, figsize=(15, 6), layout='constrained')
    codes = sorted({r['group_code'] for r in groups})
    for i, radius in enumerate((500, 1000)):
        rows = {r['group_code']: r for r in groups if r['radius_m'] == radius and r['scope'] == 'complete_coverage'}
        axes[0].barh([j + (i - .5) * .36 for j in range(len(codes))],
                     [100 * rows[code]['station_mean_share'] if rows[code]['station_mean_share'] is not None else float('nan') for code in codes], height=.34, label=f'{radius} m (n={next(iter(rows.values()))["positive_station_count"] if rows else 0})')
        row = next(r for r in radii if r['radius_m'] == radius and r['scope'] == 'complete_coverage')
        distribution = sorted((int(k), v) for k, v in json.loads(row['count_distribution']).items())
        cumulative, x, y = 0, [], []
        for count, frequency in distribution:
            cumulative += frequency
            x.append(count)
            y.append(cumulative / row['station_count'])
        axes[1].step(x, y, where='post', label=f'{radius} m (n={row["station_count"]})')
    labels = {r['group_code']: r['dictionary_name'] for r in groups}
    axes[0].set_yticks(range(len(codes)), [code + ' ' + (labels[code] or code) for code in codes])
    axes[0].invert_yaxis()
    axes[0].set(title='Equal station mean composition', xlabel='Share (%) — complete coverage, positive totals')
    axes[1].set(title='POI count cumulative distribution', xlabel='POI count (complete coverage, zeros retained)', ylabel='Cumulative station proportion', ylim=(0, 1.02))
    for axis in axes:
        axis.legend()
        axis.grid(axis='x', alpha=.2)
    fig.savefig(out / 'composition_and_counts.png', dpi=160)
    plt.close(fig)
    summary = read_json(source / 'summary_report.json')
    matching = read_json(root / summary['matching_dir'] / 'matching_report.json')
    stations = {r['station_key']: r for r in parquet_rows(root / matching['stations']['provenance']['directory'] / 'stations.parquet')}
    totals = [r for r in csv_rows(source / 'station_radius_totals.csv') if r['radius_m'] == primary_radius]
    fig, axis = plt.subplots(figsize=(8, 10), layout='constrained')
    for state, marker, color in (('complete', 'o', None), ('edge', '^', '#ad6300'), ('unobservable', 'x', '#666666')):
        rows = [r for r in totals if ('unobservable' if not r['query_eligible'] else 'complete' if r['coverage_complete_proxy'] else 'edge') == state]
        x = [stations[r['station_key']]['xy'][0] for r in rows]
        y = [stations[r['station_key']]['xy'][1] for r in rows]
        values = [math.log10(1 + r['observed_count']) for r in rows] if state == 'complete' else color
        points = axis.scatter(x, y, c=values, s=9 if state == 'complete' else 16, marker=marker,
                              label=f'{state} (n={len(rows)})', alpha=.65, **({'cmap': 'viridis'} if state == 'complete' else {}))
        if state == 'complete' and rows:
            fig.colorbar(points, ax=axis, label='log10(1 + POI count); color scale only')
    axis.set(title=f'Station POI counts within {primary_radius} m', xlabel='BNG easting (m)', ylabel='BNG northing (m)', aspect='equal')
    axis.legend()
    fig.savefig(out / 'station_map.png', dpi=160)
    plt.close(fig)
    return {'files': ['composition_and_counts.png', 'station_map.png'], 'primary_radius_m': primary_radius,
            'composition_scope': 'complete_coverage_positive_totals', 'distribution_scope': 'complete_coverage_including_zeros'}


def report(summary_dir, *, data_root, output_dir, primary_radius=1000, seed=20261012):
    root = Path(data_root)
    out = output_path(root, output_dir, summary_dir)
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    status = {'status': 'running', 'summary_dir': summary_dir, 'errors': []}
    try:
        independent = validate(summary_dir, data_root=root, output_dir=str((out / 'validation').relative_to(root)), seed=seed)
        review = export_review(summary_dir, data_root=root, output_dir=str((out / 'review').relative_to(root)), primary_radius=primary_radius, seed=seed)
        images = figures(summary_dir, data_root=root, output_dir=out / 'figures', primary_radius=primary_radius)
        totals = list(csv_rows(root / summary_dir / 'radius_summary.csv'))
        summary = read_json(root / summary_dir / 'summary_report.json')
        text = ['# Digimap POI 分析结果', '', f'本次统计目录：`{summary_dir}`。主展示半径 {primary_radius} m，随机种子 {seed}。', '',
                '|半径 m|总体|站点|正总数|零值|匹配次数|唯一 POI|', '|---:|---|---:|---:|---:|---:|---:|']
        for row in totals:
            text.append('|' + '|'.join(str(row[k]) for k in ('radius_m', 'scope', 'station_count', 'positive_station_count', 'zero_station_count', 'match_count', 'unique_poi_count')) + '|')
        text.extend(['', f'独立核验通过：{independent["check_count"]:,} 项断言；原始 POI 追溯 {independent["trace_record_count"]} 条；最大份额差 {independent["max_share_difference"]:.3g}。',
            f'复核样本包含 {review["station_count"]} 个唯一站点和 {review["match_rows"]:,} 个匹配对，GeoPackage 图层数量以 review_report.json 为准。', '',
            '计数单位为站点与合格 POI 的关系，同一 POI 可供多个站点计数；唯一 POI 按原值和类型去重。500/1000 m 均为累计半径，严格按几何距离纳入。',
            '主要组成是完整覆盖且正总数站点的等权均值；合并匹配份额另列。计数分布保留完整覆盖零站点；不可观测为缺失，边缘值不作为完整覆盖结果。', '',
            '![组成与数量](figures/composition_and_counts.png)', '', '![站点地图](figures/station_map.png)', '',
            '限制：GB/LAD 与订单范围是覆盖代理，不能证明所有设施均被收录。PUA 用 2022 成员与 May-2025 整 LAD 边界，不是历史或精细建成区。',
            '站点采用固定离线七参数近似转换，未做边界容差或转换敏感性分析。POI 属性坐标差异单列，不替代源几何。',
            '原始下载元数据和 charging 加工谱系仍未补齐；来源摘要证明副本字节一致，不证明现实设施状态或完整性。',
            'v3 Class 决定与官方三层分类适用，未实现跨 ID 的同机构/入口归并。未加入城市发展等级、因果解释或统计回归。',
            '本轮为自动核验与可追溯样本导出，不声称已有人工 QGIS、实地或专家验收。', ''])
        (out / 'SUMMARY.md').write_text('\n'.join(text), encoding='utf-8')
        status.update(status='completed', validation_complete=True, independent_validation_status=independent['status'],
            summary_rows=summary['row_counts'], review=review, figures=images,
            artifacts={str(p.relative_to(out)): fingerprint(p) for p in out.rglob('*') if p.is_file()})
    except Exception as error:
        status.update(status='failed', errors=[f'{type(error).__name__}: {error}'])
        raise
    finally:
        status['elapsed_seconds'] = time.monotonic() - started
        write_json(out / 'report.json', status)
    return status
