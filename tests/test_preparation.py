"""Step 13: hand-derived unions, complete streaming and sealed file failures."""
from collections import Counter
import copy
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point, box

from src import data
from src.spatial import FIXED_COORDINATE_OPERATION
import test_poi_cleaning as cleaning
from test_inspection import geometry_blob


def hand_rows():
    return [cleaning.record(i, f'P{i:02}', '01010006', xy) for i, xy in enumerate(
        [(0, 0), (750, 0), (2500, 0), (2500.000001, 0), (4000, 0), (0, 0), (0, 0), (750, 0)], 1)]


def station(i, xy, selected=True, query=True):
    lon, lat = 1.0 + i/100, 51.0
    key = 'loc_' + hashlib.sha256((lon.hex()+','+lat.hex()).encode('ascii')).hexdigest()
    return dict(station_key=key, source_record_nos=[i], raw_ids=[str(i)], normalized_ids=[str(i)],
        source_row_count=1, entity_resolved=True, longitude=lon, latitude=lat, xy=list(xy),
        lad_codes=['LAD'], pua_names=['Synthetic City'], target_pua_names=['Synthetic City'],
        in_target_pua=True, selected=selected, identity_eligible=query, identity_reasons=[], audit_reasons=[],
        within_gb_proxy=True, within_ni_proxy=False, within_order=True, gb_boundary_distance_m=1.0,
        order_clearance_m=1.0, spatial_status='within_GB_proxy', query_eligible=selected and query,
        main_status='within_GB_proxy', query_blockers=[] if query else ['duplicate_station_id'])


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cleaning.POIEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.poi, self.layer = self.fixture.root, self.fixture.poi, self.fixture.layer
        self.code_root = self.fixture.code_root
        self.number = 0
        rows = hand_rows()
        rows[5]['geom'] = None
        rows[6]['pointx_class'] = '10540832'
        self.rebuild(rows)

    def rebuild(self, rows, stations=None, coverage=None, scope='full_source'):
        """Build new synthetic upstream runs, never use or modify real artifacts."""
        self.number += 1
        self.rows = copy.deepcopy(rows)
        coverage=coverage if coverage is not None else box(-100,-100,5000,100)
        fixture = self.fixture
        raw_fixture = fixture.fixture.fixture
        raw_fixture.make_poi([(r['source_fid'], r['ref_no'], r['pointx_class'], r['feature_easting'],
                              r['feature_northing'], r['pos_accuracy'], r['geom']) for r in rows])
        table = '"'+self.layer.replace('"','""')+'"'
        with sqlite3.connect(self.root/self.poi) as db:
            for column in ('groupname','categoryname','classname'):
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} TEXT')
        entries=[]
        for name in fixture.paths:
            fp=data._artifact_fingerprint(self.root/name)
            entries.append(dict(path=name, **fp, source_location='synthetic', copied_at='2026-10-12T00:00:00+00:00',
                                source_sha256_before_copy=fp['sha256'],source_sha256_after_copy=fp['sha256']))
        fixture.write(self.root/'data/source_manifest.json',dict(schema_version=1,files=entries))
        prior=f'outputs/prior{self.number}/full'
        data.inspect_inputs(self.poi,raw_fixture.station,data_root=self.root,output_dir=prior,layer=self.layer)
        fixture.seal(prior)
        dictionary_dir=f'outputs/dict{self.number}/full'
        data.inspect_dictionary(*fixture.fixture.paths,self.poi,data_root=self.root,inspection_dir=prior,
                                output_dir=dictionary_dir,layer=self.layer,required_paths=fixture.paths)
        fixture.seal(dictionary_dir)
        current={p:data._fingerprint(p,self.root,1048576) for p in fixture.paths}
        base=dict(status='completed',validation_complete=True,input_bytes_unchanged=True,
                  input_fingerprints_before=current,input_fingerprints_after=current)
        scope_dir=f'outputs/scope{self.number}/full'
        directory=self.root/scope_dir;directory.mkdir(parents=True)
        fixture.write(directory/'pua_scope_report.json',dict(base,scope_complete=True))
        fixture.write(directory/'scope_definition.json',dict(target_puas=['Synthetic City']))
        (directory/'study_area.wkb').write_bytes(box(-1,-1,1501,1).intersection(coverage).wkb)
        fixture.seal(scope_dir)
        upstream=dict(scope_dir=scope_dir,verification=data._artifact_fingerprint(directory.parent/'verification.json'),
                      artifacts={p.name:data._artifact_fingerprint(p) for p in directory.iterdir()})
        self.station_dir=f'outputs/stations{self.number}/full'
        directory=self.root/self.station_dir;directory.mkdir(parents=True)
        self.stations = copy.deepcopy(stations if stations is not None else [station(1,(0,0)),station(2,(1500,0))])
        data._write_parquet(directory/'stations.parquet',self.stations,data.station_parquet_schemas()[0])
        inspection=data._read_json(self.root/prior/'input_inspection.json')
        keys={k:inspection['poi'][k] for k in ('layers','layer','fields','field_mapping','fid_column','geometry','crs',
                     'crs_epsg','coordinate_units','source_count','cached_feature_count','cached_count_matches','extent_is_coverage_boundary')}
        definition=dict(crs='EPSG:27700',units='metre',predicate='covers',target_puas=['Synthetic City'],
             upstream_scope=upstream,input_fingerprints=current,
             order=dict(poi_path=self.poi,poi_layer=self.layer,poi_metadata=keys,bounds=[-100,-100,5000,100]),
             coordinate_operation=FIXED_COORDINATE_OPERATION,radii_m=[500,1000], sample_keys=None)
        fixture.write(directory/'coverage_definition.json',definition)
        for name in ('gb_proxy.wkb','data_coverage_proxy.wkb'):
            (directory/name).write_bytes(coverage.wkb)
        report=dict(base,scope='full_source',full_source_identity_checked=True,entity_record_count=len(self.stations),
                    source_record_count=len(self.stations),selected_entity_count=sum(s['selected'] for s in self.stations),
                    query_eligible_count=sum(s['query_eligible'] for s in self.stations),target_puas=['Synthetic City'])
        report['artifacts']={p.name:data._artifact_fingerprint(p) for p in directory.iterdir()}
        fixture.write(directory/'station_preparation_report.json',report)
        fixture.seal(self.station_dir)
        coverage_dir=self.station_dir
        if scope=='sample':
            self.station_dir=f'outputs/sample_stations{self.number}/full'
            sample=self.root/self.station_dir;sample.mkdir(parents=True)
            for p in directory.iterdir():
                (sample/p.name).write_bytes(p.read_bytes())
            definition['sample_keys']=sorted(s['station_key'] for s in self.stations if s['selected'])
            fixture.write(sample/'coverage_definition.json',definition)
            report.update(scope='sample',sample_size=len(definition['sample_keys']),random_seed=20261011)
            report['artifacts']['coverage_definition.json']=data._artifact_fingerprint(sample/'coverage_definition.json')
            fixture.write(sample/'station_preparation_report.json',report)
            fixture.seal(self.station_dir)
        self.options=dict(fixture.options,inspection_dir=prior,dictionary_dir=dictionary_dir,
                          scope_dir=scope_dir,coverage_dir=coverage_dir)

    def run_prepare(self, **changes):
        self.number+=1
        args=dict(station_dir=self.station_dir,data_root=self.root,code_root=self.code_root,
                  output_dir=f'outputs/prepare{self.number}',context_options=self.options,batch_size=2)
        args.update(changes)
        return data.prepare(self.poi,**args)

    def outputs(self, report):
        directory=Path(report['output_dir'])/'processed'
        return [pq.read_table(directory/name).to_pylist() for name in ('pois.parquet','poi_issues.parquet')]

    def fail(self, **changes):
        with self.assertRaises(data.PreparationError) as caught:
            self.run_prepare(**changes)
        report=caught.exception.report
        self.assertEqual(report['status'],'failed')
        self.assertFalse(report['validation_complete'])
        self.assertEqual(data._read_json(Path(report['output_dir'])/'preparation_report.json')['status'],'failed')
        self.assertFalse((Path(report['output_dir'])/'processed/pois.parquet').exists())
        return report

    def mutate_station(self, callback, table=False, reseal=True):
        directory=self.root/self.station_dir
        name='stations.parquet' if table else 'station_preparation_report.json'
        path=directory/name
        value=pq.read_table(path).to_pylist() if table else data._read_json(path)
        callback(value)
        if table:
            pq.write_table(pa.Table.from_pylist(value,schema=data.station_parquet_schemas()[0]),path)
            report=data._read_json(directory/'station_preparation_report.json')
            report['artifacts'][name]=data._artifact_fingerprint(path)
            self.fixture.write(directory/'station_preparation_report.json',report)
        else:self.fixture.write(path,value)
        if reseal:self.fixture.seal(self.station_dir)

    def test_hand_union_complete_audit_and_conservation(self):
        report=self.run_prepare()
        candidates,audits=self.outputs(report)
        self.assertEqual([r['ref_no_value'] for r in candidates],['P01','P02','P03','P08'])
        self.assertEqual(report['counts']['preparation_status_counts'],dict(ineligible=2,eligible_not_neighbor=2,saved=4))
        self.assertEqual((report['counts']['input_count'],report['counts']['analysis_included_count'],len(audits)),(8,6,8))
        self.assertEqual([r['source_fid'] for r in audits],list(range(1,9)))
        self.assertFalse(audits[5]['neighbor_evaluated'])
        self.assertIsNone(audits[5]['is_neighbor'])
        self.assertTrue(audits[3]['neighbor_evaluated'])
        self.assertFalse(audits[3]['is_neighbor'])
        self.assertEqual(json.loads(audits[5]['audit_json'])['geometry']['status'],'null')
        self.assertTrue(report['input_bytes_unchanged'])
        self.assertEqual(report['poi_scope'],'full_source')

    def test_direct_station_union_matches_with_shared_and_boundary_points(self):
        valid={1,2,3,4,5,8}  # hand-confirmed, not obtained from production cleaning
        expected={r['source_fid'] for r in self.rows if r['source_fid'] in valid
                  and any(math.hypot(r['feature_easting']-x,r['feature_northing']-y)<=1000 for x,y in ((0,0),(1500,0)))}
        candidates,_=self.outputs(self.run_prepare())
        self.assertEqual({r['source_fid'] for r in candidates},expected)
        self.assertEqual(expected,{1,2,3,8})

    def test_batch_sizes_preserve_all_fields_and_counts(self):
        reports=[self.run_prepare(batch_size=n) for n in (1,2,7,30)]
        for report in reports[1:]:
            self.assertEqual(self.outputs(reports[0]),self.outputs(report))
            self.assertEqual(reports[0]['counts'],report['counts'])

    def test_no_query_stations_still_reads_all_source(self):
        self.rebuild(self.rows,stations=[station(1,(0,0),query=False),station(2,(1500,0),selected=False)])
        report=self.run_prepare()
        self.assertEqual(report['counts']['preparation_status_counts'],dict(ineligible=2,eligible_not_neighbor=6,saved=0))
        self.assertEqual([len(r) for r in self.outputs(report)],[0,8])
        schema=pq.read_schema(Path(report['output_dir'])/'processed/pois.parquet')
        self.assertTrue(schema.equals(data.poi_parquet_schema('poi-research-v3.0.0'),check_metadata=True))

    def test_empty_station_table_and_empty_source_keep_both_schemas(self):
        self.rebuild([],stations=[])
        report=self.run_prepare()
        self.assertEqual(self.outputs(report),[[],[]])
        self.assertEqual(report['counts']['input_count'],0)
        self.assertIsNone(report['counts']['classification_review_proportion'])
        schema=pq.read_schema(Path(report['output_dir'])/'processed/poi_issues.parquet')
        self.assertTrue(schema.equals(data.preparation_audit_schema('poi-research-v3.0.0'),check_metadata=True))

    def test_first_batch_ineligible_then_candidates(self):
        self.rebuild([cleaning.record(10,'bad','01010006',geom=b'broken'),cleaning.record(50,'good','01010006',xy=(0,0))])
        report=self.run_prepare(batch_size=1)
        self.assertEqual([r['ref_no_value'] for r in self.outputs(report)[0]],['good'])
        self.assertEqual(report['counts']['input_count'],2)

    def test_all_ineligible_and_all_eligible_non_neighbors(self):
        for code,xy,expected in [('10540832',(0,0),'ineligible'),('01010006',(4000,0),'eligible_not_neighbor')]:
            self.rebuild([cleaning.record(10,'one',code,xy)])
            report=self.run_prepare()
            self.assertEqual(self.outputs(report)[0],[])
            self.assertEqual(report['counts']['preparation_status_counts'][expected],1)

    def test_global_duplicate_far_outside_coverage_blocks_near_row(self):
        self.rebuild([cleaning.record(10,'dup','01010006',(0,0)),cleaning.record(70,'dup','01010006',(9000,0))])
        report=self.run_prepare(batch_size=1)
        self.assertEqual(report['counts']['base_exclusion_reasons']['global_duplicate_ref_no'],2)
        self.assertEqual(report['counts']['base_exclusion_reasons']['outside_data_coverage'],1)
        self.assertEqual(report['counts']['candidate_count'],0)

    def test_coverage_excludes_near_point_without_pua_crop(self):
        coverage=box(-100,-100,5000,100).difference(box(700,-10,800,10))
        self.rebuild(self.rows,coverage=coverage)
        report=self.run_prepare()
        self.assertEqual([r['ref_no_value'] for r in self.outputs(report)[0]],['P01','P03'])
        self.assertEqual(report['counts']['base_exclusion_reasons']['outside_data_coverage'],2)

    def test_unselected_and_blocked_stations_do_not_contribute(self):
        self.rebuild(self.rows,stations=[station(1,(0,0)),station(2,(1500,0),selected=False)])
        self.assertEqual([r['ref_no_value'] for r in self.outputs(self.run_prepare())[0]],['P01','P02','P08'])

    def test_incomplete_radius_coverage_does_not_block_candidates(self):
        self.assertTrue(all(s['gb_boundary_distance_m']<500 for s in self.stations))
        self.assertEqual(self.run_prepare()['counts']['candidate_count'],4)

    def test_station_sample_scope_is_separate_from_full_poi_scope(self):
        self.rebuild(self.rows,scope='sample')
        report=self.run_prepare()
        self.assertEqual((report['station_scope'],report['poi_scope']),('sample','full_source'))
        self.assertEqual(report['counts']['input_count'],8)

    def test_typed_id_leading_zeros_pending_groups_and_unknown_class(self):
        ids=['001','1',1,1.0,' ID ']
        rows=[cleaning.record(i,v,'01010006',(0,0)) for i,v in enumerate(ids,10)]
        rows.append(cleaning.record(90,'unknown','99999999',(0,0)))
        self.rebuild(rows)
        report=self.run_prepare()
        candidates,audits=self.outputs(report)
        self.assertEqual([r['ref_no_type'] for r in candidates],['text','text','integer','real','text'])
        self.assertEqual([r['ref_no_value'] for r in candidates],['001','1','1','1.0',' ID '])
        self.assertTrue(all(r['functional_group_pending'] and r['category_code']=='0101' for r in candidates))
        self.assertEqual(report['counts']['functional_group_pending_count'],5)
        self.assertTrue(audits[-1]['base_eligible'])
        self.assertFalse(audits[-1]['analysis_include'])

    def test_ev_switch_changes_base_only(self):
        report=self.run_prepare(include_ev_charging=True)
        self.assertEqual(report['counts']['base_eligible_count'],7)
        self.assertEqual(report['counts']['analysis_included_count'],6)
        self.assertEqual(report['counts']['base_eligible_research_excluded_count'],1)

    def test_resealed_failed_station_report_rejected(self):
        self.mutate_station(lambda r:r.update(status='failed'))
        self.fail()

    def test_unsealed_station_artifact_rejected(self):
        self.mutate_station(lambda r:r.update(entity_record_count=0),reseal=False)
        self.fail()

    def test_stale_station_input_rejected(self):
        def mutate(r):r['input_fingerprints_before'][self.poi]['sha256']='0'*64
        self.mutate_station(mutate)
        self.fail()

    def test_resealed_station_count_mismatch_rejected(self):
        self.mutate_station(lambda r:r.update(selected_entity_count=99))
        self.fail()

    def test_resealed_station_key_and_coordinates_rejected(self):
        self.mutate_station(lambda r:r[0].update(station_key='bad'),table=True)
        self.fail()

    def test_resealed_nonfinite_station_coordinates_rejected(self):
        self.mutate_station(lambda r:r[0].update(xy=[float('nan'),0]),table=True)
        self.fail()

    def test_resealed_station_outside_pua_rejected(self):
        self.mutate_station(lambda r:r[0].update(xy=[2500,0]),table=True)
        self.fail()

    def test_station_schema_metadata_rejected(self):
        directory=self.root/self.station_dir
        path=directory/'stations.parquet'
        table=pq.read_table(path).replace_schema_metadata({b'crs':b'EPSG:4326'})
        pq.write_table(table,path)
        self.mutate_station(lambda r:r['artifacts'].update({'stations.parquet':data._artifact_fingerprint(path)}))
        self.fail()

    def test_resealed_coverage_crs_rejected(self):
        directory=self.root/self.options['coverage_dir']
        definition=data._read_json(directory/'coverage_definition.json')
        definition['crs']='EPSG:4326'
        self.fixture.write(directory/'coverage_definition.json',definition)
        self.fixture.seal(self.options['coverage_dir'])
        self.fail()

    def test_failed_dictionary_evidence_rejected(self):
        directory=self.root/self.options['dictionary_dir']
        report=data._read_json(directory/'dictionary_validation.json');report['status']='failed'
        self.fixture.write(directory/'dictionary_validation.json',report);self.fixture.seal(self.options['dictionary_dir'])
        self.fail()

    def test_incomplete_duplicate_evidence_rejected(self):
        directory=self.root/self.options['inspection_dir']
        report=data._read_json(directory/'input_inspection.json');report['duplicate_list_complete']=False
        self.fixture.write(directory/'input_inspection.json',report);self.fixture.seal(self.options['inspection_dir'])
        self.fail()

    def test_all_real_v3_rules_and_eight_pending_groups_through_preparation(self):
        # The rule integration unit swaps only the already tested context loader;
        # other tests exercise real sealing of every synthetic upstream file.
        dictionary,rules,_=cleaning.configured_dictionary()
        self.rebuild([cleaning.record(i,code,code,(0,0)) for i,code in enumerate(rules.rows,1)])
        context=data.load_poi_cleaning_context(self.poi,data_root=self.root,code_root=self.code_root,**self.options)
        context=replace(context,dictionary=dictionary,rules=rules)
        with patch('src.data.load_poi_cleaning_context',return_value=context):
            report=self.run_prepare(code_root=cleaning.CODE_ROOT,batch_size=7)
        candidates,_=self.outputs(report)
        self.assertEqual(report['counts']['preparation_status_counts'],dict(ineligible=17,eligible_not_neighbor=0,saved=585))
        pending=[r for r in candidates if r['functional_group_pending']]
        self.assertEqual(len(pending),8)
        self.assertTrue(all(r['analysis_include'] and r['research_decision']=='include' for r in pending))
        self.assertTrue(all(r['group_dictionary_name']==dictionary.groups[r['group_code']]['dictionary_name'] for r in candidates))

    def test_truncated_reader_rejected(self):
        original=data.poi_batches
        def reader(*args,**kwargs):
            yield next(original(*args,**kwargs))
        with patch('src.data.poi_batches',reader):self.fail()

    def test_cross_batch_duplicate_and_reordered_fids_rejected(self):
        original=data.poi_batches
        def reader(*args,**kwargs):
            batches=list(original(*args,**kwargs))
            yield batches[0]
            yield batches[0]
        with patch('src.data.poi_batches',reader):self.fail()

    def test_raw_source_change_during_run_rejected(self):
        changed=False
        def progress(info):
            nonlocal changed
            if info['stage']=='streaming' and not changed:
                changed=True
                with (self.root/self.poi).open('ab') as stream:stream.write(b'changed')
        self.fail(progress=progress)

    def test_upstream_change_after_streaming_rejected(self):
        def progress(info):
            if info['stage']=='readback':
                self.mutate_station(lambda r:r.update(status='failed'))
        self.fail(progress=progress)

    def test_rule_change_after_streaming_rejected(self):
        def progress(info):
            if info['stage']=='readback':
                with (self.code_root/'config/rules.csv').open('a') as stream:stream.write('\n')
        self.fail(progress=progress)

    def test_readback_detects_candidate_corruption(self):
        def progress(info):
            if info['stage']=='readback':
                path=next((self.root/'outputs').glob('prepare*/processed/pois.parquet.tmp'))
                table=pq.read_table(path);rows=table.to_pylist();rows[0]['x']=123
                pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
        self.fail(progress=progress)

    def test_readback_detects_audit_corruption(self):
        def progress(info):
            if info['stage']=='readback':
                path=next((self.root/'outputs').glob('prepare*/processed/poi_issues.parquet.tmp'))
                table=pq.read_table(path);rows=table.to_pylist();rows[0]['is_neighbor']=False
                pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
        self.fail(progress=progress)

    def test_write_failure_preserves_failed_report(self):
        with patch('src.data._preparation_digest',side_effect=OSError('synthetic write failure')):
            self.fail()

    def test_existing_directory_invalid_parameters_and_path_escape(self):
        for changes in ({'batch_size':0},{'batch_size':True},{'include_ev_charging':1},{'output_dir':'outputs/..'},{'output_dir':'../escape'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):self.run_prepare(**changes)
        report=self.run_prepare()
        with self.assertRaises(FileExistsError):self.run_prepare(output_dir=str(Path(report['output_dir']).relative_to(self.root)))

    def test_reader_always_receives_none_limit_and_no_spatial_crop(self):
        with patch('src.data.poi_batches',wraps=data.poi_batches) as reader:
            self.run_prepare()
        self.assertIsNone(reader.call_args.kwargs['limit'])
        self.assertEqual(set(reader.call_args.kwargs),{'data_root','layer','fields','batch_size','limit'})


if __name__=='__main__':unittest.main()
