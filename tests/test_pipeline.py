"""Steps 16–23: independent corruption checks, state protection and review files."""
import copy
import csv
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

import nbformat
import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point

from src import pipeline, reporting, validation
from src.maintenance import portability
import test_summarization as samples


class IndependentValidationTests(unittest.TestCase):
    def setUp(self):
        helper = samples.SummaryTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.root = helper.root
        self.stations, self.matches = helper.sample(formal=True)
        base = samples.base_rows(self.stations)
        for s in self.stations:
            s.update(main_status='synthetic', within_gb_proxy=True, order_clearance_m=2000.,
                     gb_boundary_distance_m=0. if s['station_key'] == 'S3' else 2000.,
                     query_blockers=[] if s['query_eligible'] else ['invalid_identity'])
        for r in base:
            if r['station_key'] == 'S3':
                r.update(gb_boundary_distance_m=0., coverage_reasons=['GB_boundary_within_radius'])
            if r['station_key'] == 'S4':
                r['coverage_reasons'] = ['invalid_identity', 'not_query_eligible']
        self.tables, _, _ = helper.execute(self.stations, self.matches, base=base)

    def run_validation(self):
        return validation.validate_tables(self.stations, sorted(self.matches, key=lambda r: r['station_key']),
            self.tables['station_radius_totals'], self.tables['composition'], self.tables['radius_summary'],
            self.tables['group_summary'], groups=['01','99'], labels={})

    def test_hand_sample_all_cells_and_denominators(self):
        result = self.run_validation()
        self.assertEqual(result['match_count'], 6)
        self.assertEqual(result['selected_station_count'], 4)
        self.assertEqual([r['match_count'] for r in result['radius_summary']], [4, 2, 6, 3])

    def test_duplicate_match_rejected(self):
        self.matches.append(copy.deepcopy(self.matches[0]))
        with self.assertRaisesRegex(validation.ValidationError, 'pair_unique'):
            self.run_validation()

    def test_offsetting_cell_errors_rejected(self):
        rows = [r for r in self.tables['composition'] if r['level'] == 'class' and r['radius_m'] == 1000]
        rows[0]['count'] += 1
        rows[1]['count'] -= 1
        with self.assertRaisesRegex(validation.ValidationError, 'composition_cell.count'):
            self.run_validation()

    def test_wrong_share_rejected(self):
        self.tables['composition'][0]['share'] += .1
        with self.assertRaisesRegex(validation.ValidationError, 'composition_cell.share'):
            self.run_validation()

    def test_missing_zero_group_rejected(self):
        self.tables['composition'] = [r for r in self.tables['composition'] if not (r['station_key'] == 'S2' and r['radius_m'] == 500 and r['code'] == '99')]
        with self.assertRaises(validation.ValidationError):
            self.run_validation()

    def test_zero_cannot_be_replaced_by_missing(self):
        next(r for r in self.tables['station_radius_totals'] if r['station_key'] == 'S2')['observed_count'] = None
        with self.assertRaisesRegex(validation.ValidationError, 'observed_count'):
            self.run_validation()

    def test_unobservable_cannot_be_replaced_by_zero(self):
        next(r for r in self.tables['station_radius_totals'] if r['station_key'] == 'S4')['observed_count'] = 0
        with self.assertRaisesRegex(validation.ValidationError, 'observed_count'):
            self.run_validation()

    def test_missing_station_total_rejected(self):
        self.tables['station_radius_totals'].pop()
        with self.assertRaisesRegex(validation.ValidationError, 'station_retention'):
            self.run_validation()

    def test_duplicate_total_rejected(self):
        self.tables['station_radius_totals'].append(self.tables['station_radius_totals'][0])
        with self.assertRaisesRegex(validation.ValidationError, 'total_key_unique'):
            self.run_validation()

    def test_outside_distance_rejected(self):
        self.matches[0]['distance_m'] = 1000.00001
        with self.assertRaisesRegex(validation.ValidationError, 'distance_range'):
            self.run_validation()

    def test_wrong_pooled_denominator_rejected(self):
        self.tables['group_summary'][0]['pooled_match_share'] = .1
        with self.assertRaisesRegex(validation.ValidationError, 'pooled_match_share'):
            self.run_validation()

    def test_wrong_unique_count_rejected(self):
        self.tables['radius_summary'][0]['unique_poi_count'] += 1
        with self.assertRaisesRegex(validation.ValidationError, 'unique_poi_count'):
            self.run_validation()

    def test_unselected_match_rejected(self):
        self.matches.append(dict(self.matches[0], station_key='ZZZ'))
        with self.assertRaisesRegex(validation.ValidationError, 'no_unselected_matches'):
            self.run_validation()

    def test_wrong_coverage_rejected(self):
        self.tables['station_radius_totals'][0]['coverage_complete_proxy'] = False
        with self.assertRaisesRegex(validation.ValidationError, 'coverage_complete_proxy'):
            self.run_validation()

    def test_raw_geometry_not_attribute_coordinates(self):
        raw = self.root / 'data/raw/sample.gpkg'
        raw.parent.mkdir(parents=True)
        with sqlite3.connect(raw) as con:
            con.execute('CREATE TABLE points (id INTEGER PRIMARY KEY, ref TEXT, code TEXT, geom BLOB, ex REAL, ny REAL)')
            con.execute('INSERT INTO points VALUES (?,?,?,?,?,?)', (7,' 001 ','01010006',b'GP'+bytes((0,1))+struct.pack('<i',27700)+Point(300,400).wkb, 302., 400.))
        rows = [dict(station_key='S', source_path='data/raw/sample.gpkg', source_layer='points', source_fid=7,
                     ref_no_type='text', ref_no_value=' 001 ', class_code='01010006', x=300., y=400.,
                     station_x=0.,station_y=0.,distance_m=500.)]
        schema = dict(fid_column='id', geometry={'column_name':'geom'}, field_mapping=dict(ref_no='ref',raw_code='code',easting='ex',northing='ny'))
        result = validation.trace_original(rows, data_root=self.root, schema=schema, checks=validation.Checks())
        self.assertEqual(result[0]['raw_geometry_distance_m'],500)
        self.assertEqual(result[0]['attribute_geometry_difference_m'],2)
        rows[0]['source_fid'] = 8
        with self.assertRaisesRegex(validation.ValidationError,'raw_fid_exists'):
            validation.trace_original(rows,data_root=self.root,schema=schema,checks=validation.Checks())


class RunProtectionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.code, self.data = self.base / 'code', self.base / 'data'
        self.code.mkdir()
        (self.data / 'data/raw').mkdir(parents=True)
        for name in ('run.py','requirements.txt','config.yaml'):
            (self.code / name).write_text('')
        (self.code / 'src').mkdir()
        (self.code / 'src/module.py').write_text('x = 1\n')
        (self.data / 'data/raw/input.txt').write_text('source')
        (self.data / 'data/source_manifest.json').write_text('{}')
        self.config = dict(code_root=self.code,data_root=self.data,paths={'output_dir':self.data/'outputs'})
        self.study = {'required_paths':['data/raw/input.txt']}
        self.calls = []

    def executor(self,stage,**kwargs):
        self.calls.append((stage,kwargs['options'].copy()))
        (kwargs['run_dir']/stage/'artifact.txt').write_text('content:'+stage)

    def run_stage(self,stage='inspect',**kwargs):
        return pipeline.run_pipeline(self.config,self.study,run_id='fixture',stage=stage,executor=self.executor,**kwargs)

    def state(self):
        return (self.data/'outputs/fixture/run.json').read_bytes()

    def verify(self,**kwargs):
        return pipeline.verify_run(self.data/'outputs/fixture',code_root=self.code,data_root=self.data,**kwargs)

    def test_partial_and_success_are_distinct(self):
        result = self.run_stage()
        self.assertEqual(result['status'],'partial')
        self.assertEqual(self.verify()['run_status'],'partial')
        with self.assertRaisesRegex(ValueError,'successful'):
            self.verify(require_success=True)
        for stage in pipeline.STAGES[1:]:
            result=self.run_stage(stage)
        self.assertEqual(result['status'],'success')
        self.assertEqual(self.verify()['completed_stages'],list(pipeline.STAGES))

    def test_all_calls_five_stages_in_order(self):
        self.assertEqual(self.run_stage('all')['status'],'success')
        self.assertEqual([s for s,_ in self.calls],list(pipeline.STAGES))

    def test_sampling_enters_snapshot_and_each_stage(self):
        self.run_stage('all',options={'sample_stations':64,'seed':37})
        self.assertTrue(all(o['sample_stations']==64 and o['seed']==37 for _,o in self.calls))

    def test_missing_upstream_has_no_created_run(self):
        with self.assertRaisesRegex(ValueError,'missing upstream'):
            self.run_stage('summarize')
        self.assertFalse((self.data/'outputs/fixture').exists())

    def test_stage_skip_preserves_valid_state(self):
        self.run_stage()
        before=self.state()
        with self.assertRaisesRegex(ValueError,'missing upstream'):
            self.run_stage('summarize')
        self.assertEqual(self.state(),before)

    def test_repeat_attempt_preserves_state(self):
        self.run_stage()
        before=self.state()
        with self.assertRaisesRegex(ValueError,'already attempted'):
            self.run_stage()
        self.assertEqual(self.state(),before)

    def test_runtime_configuration_change_preserves_state(self):
        self.run_stage(options={'sample_stations':64})
        before=self.state()
        with self.assertRaisesRegex(ValueError,'configuration changed'):
            self.run_stage('prepare',options={'sample_stations':63})
        self.assertEqual(self.state(),before)

    def test_code_change_preserves_state(self):
        self.run_stage()
        before=self.state()
        (self.code/'src/module.py').write_text('x = 2\n')
        with self.assertRaisesRegex(ValueError,'code/configuration'):
            self.run_stage('prepare')
        self.assertEqual(self.state(),before)

    def test_selected_configuration_outside_default_folder_is_fingerprinted(self):
        path=self.code/'alternate/settings.yaml'
        path.parent.mkdir()
        path.write_text('explicit configuration')
        self.config['config_path']=path
        self.run_stage()
        before=self.state()
        path.write_text('changed configuration')
        with self.assertRaisesRegex(ValueError,'code/configuration'):
            self.verify()
        self.assertEqual(self.state(),before)

    def test_selected_study_configuration_is_fingerprinted(self):
        path=self.code/'alternate-study.json'
        path.write_text('{}')
        self.config['study_config_path']=path
        self.run_stage()
        path.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError,'code/configuration'):
            self.run_stage('prepare')

    def test_raw_change_detected(self):
        self.run_stage()
        (self.data/'data/raw/input.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError,'original inputs changed'):
            self.verify()

    def test_artifact_tampering_detected_and_preserves_state(self):
        self.run_stage()
        before=self.state()
        (self.data/'outputs/fixture/inspect/artifact.txt').write_text('tampered')
        with self.assertRaisesRegex(ValueError,'artifacts changed'):
            self.run_stage('prepare')
        self.assertEqual(self.state(),before)

    def test_added_untracked_artifact_detected(self):
        self.run_stage()
        (self.data/'outputs/fixture/inspect/extra.txt').write_text('unexpected')
        with self.assertRaisesRegex(ValueError,'artifacts changed'):
            self.verify()

    def test_dependency_change_detected(self):
        self.run_stage()
        with patch.object(pipeline,'dependency_fingerprint',return_value={'changed':True}):
            with self.assertRaisesRegex(ValueError,'dependencies changed'):
                self.verify()

    def test_failure_is_final_for_run_id(self):
        def fail(stage,**kwargs):
            raise ValueError('deliberate stage failure')
        with self.assertRaisesRegex(RuntimeError,'inspect: deliberate'):
            pipeline.run_pipeline(self.config,self.study,run_id='fixture',executor=fail)
        state=json.loads(self.state())
        self.assertEqual(state['status'],'failed')
        self.assertEqual(state['stages']['inspect']['status'],'failed')
        before=self.state()
        with self.assertRaises(ValueError):
            self.run_stage()
        self.assertEqual(self.state(),before)

    def test_input_change_during_execution_fails(self):
        def mutate(stage,**kwargs):
            self.executor(stage,**kwargs)
            (self.data/'data/raw/input.txt').write_text('mutated while running')
        with self.assertRaisesRegex(RuntimeError,'original inputs changed'):
            pipeline.run_pipeline(self.config,self.study,run_id='fixture',executor=mutate)
        self.assertEqual(json.loads(self.state())['status'],'failed')

    def test_verify_is_read_only(self):
        self.run_stage('all')
        before=pipeline.artifact_hashes(self.data/'outputs/fixture')
        self.verify()
        self.assertEqual(pipeline.artifact_hashes(self.data/'outputs/fixture'),before)

    def test_bad_identifiers_and_options(self):
        for run_id in ('../escape','x/y','', 'a'*100):
            with self.assertRaises(ValueError):
                pipeline.run_pipeline(self.config,self.study,run_id=run_id,executor=self.executor)
        for options in ({'sample_stations':0},{'sample_stations':True},{'threads':0},{'memory_limit':'0GB'},{'primary_radius':750}):
            with self.assertRaises(ValueError):
                self.run_stage(options=options)

    def test_portability_station_limit(self):
        for value in (0,25,True):
            with self.assertRaisesRegex(ValueError,'1..24'):
                portability(self.base,code_root=self.code,data_root=self.data,sample_size=value)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)

    def test_sampling_is_reproducible_and_deduplicated(self):
        rows=[dict(station_key=f'S{i}',radius_m=1000,observed_count=i,query_eligible=True,
                   coverage_complete_proxy=i!=8,gb_boundary_distance_m=0 if i==8 else 2000,order_clearance_m=3000) for i in range(10)]
        first=reporting.select_review(rows,seed=31)
        self.assertEqual(first,reporting.select_review(list(reversed(rows)),seed=31))
        self.assertIn('zero',first['S0'])
        self.assertIn('high',first['S9'])
        self.assertIn('edge',first['S8'])
        self.assertLessEqual(len(first),8)

    def test_empty_and_single_sample(self):
        self.assertEqual(reporting.select_review([]),{})
        row=dict(station_key='A',radius_m=1000,observed_count=0,query_eligible=True,coverage_complete_proxy=True)
        result=reporting.select_review([row])
        self.assertEqual(list(result),['A'])
        self.assertEqual(result['A'],['high','low','median','random','zero'])

    def test_empty_geopackage_layers_are_real(self):
        layers=[(name,kind,[('station_key','TEXT')],[]) for name,kind in (
            ('stations','POINT'),('cumulative_buffers','POLYGON'),('station_poi_matches','POINT'))]
        path=self.root/'empty.gpkg'
        self.assertEqual(reporting.write_geopackage(path,layers),dict.fromkeys([l[0] for l in layers],0))
        with sqlite3.connect(path) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM gpkg_geometry_columns WHERE srs_id=27700').fetchone()[0],3)
            self.assertEqual(con.execute('SELECT COUNT(*) FROM station_poi_matches').fetchone()[0],0)

    def test_geopackage_geometry_crs_and_values(self):
        path=self.root/'review.gpkg'
        layers=[('stations','POINT',[('station_key','TEXT'),('count','INTEGER')],[(Point(1,2),{'station_key':'001','count':0})]),
                ('cumulative_buffers','POLYGON',[('radius_m','INTEGER')],[(Point(1,2).buffer(500,quad_segs=64),{'radius_m':500})])]
        self.assertEqual(reporting.write_geopackage(path,layers),{'stations':1,'cumulative_buffers':1})
        with sqlite3.connect(path) as con:
            blob=con.execute('SELECT geom FROM stations').fetchone()[0]
            self.assertEqual(validation.decode_gpkg(blob),Point(1,2))
            ring=validation.decode_gpkg(con.execute('SELECT geom FROM cumulative_buffers').fetchone()[0])
            self.assertEqual(len(ring.exterior.coords),257)
        with self.assertRaises(FileExistsError):
            reporting.write_geopackage(path,layers)

    def test_notebook_template_is_unexecuted_and_reads_saved_results(self):
        path=Path(__file__).resolve().parents[1]/'notebooks/01_review.ipynb'
        nb=nbformat.read(path,as_version=4)
        self.assertTrue(all(c.execution_count is None and not c.outputs for c in nb.cells if c.cell_type=='code'))
        code='\n'.join(c.source for c in nb.cells if c.cell_type=='code')
        self.assertIn('verify_run',code)
        self.assertNotIn('summarize(',code)
        self.assertNotIn('match_files(',code)

    def empty_output(self):
        from src.data import station_parquet_schemas, matching_parquet_schema
        summary=self.root/'outputs/summary'
        matching=self.root/'outputs/matching'
        stations=self.root/'outputs/stations'
        for path in (summary,matching,stations):
            path.mkdir(parents=True)
        validation.write_json(summary/'summary_report.json',{'matching_dir':'outputs/matching'})
        validation.write_json(matching/'matching_report.json',{'stations':{'provenance':{'directory':'outputs/stations'}}})
        pq.write_table(pa.Table.from_pylist([],schema=station_parquet_schemas()[0]),stations/'stations.parquet')
        pq.write_table(pa.Table.from_pylist([],schema=matching_parquet_schema('poi-research-v3.0.0')),matching/'matches.parquet')
        pq.write_table(pa.Table.from_pylist([],schema=samples.analysis.summary_schemas()['composition']),summary/'composition.parquet')
        (summary/'station_radius_totals.csv').write_text('station_key,radius_m,observed_count\n')
        return summary

    def test_empty_review_creates_header_csvs_and_empty_layers(self):
        self.empty_output()
        result=reporting.export_review('outputs/summary',data_root=self.root,output_dir='outputs/review')
        self.assertEqual(result['station_count'],0)
        self.assertEqual(result['match_rows'],0)
        self.assertTrue(all(n==0 for n in result['layer_counts'].values()))
        for name in ('sample_stations.csv','sample_matches.csv','sample_composition.csv'):
            with (self.root/'outputs/review'/name).open() as stream:
                reader=csv.DictReader(stream)
                self.assertTrue(reader.fieldnames)
                self.assertEqual(list(reader),[])

    def test_validation_failure_cannot_produce_successful_report(self):
        with patch.object(reporting,'validate',side_effect=validation.ValidationError('deliberate mismatch')):
            with self.assertRaisesRegex(validation.ValidationError,'deliberate'):
                reporting.report('outputs/summary',data_root=self.root,output_dir='outputs/report')
        result=validation.read_json(self.root/'outputs/report/report.json')
        self.assertEqual(result['status'],'failed')
        self.assertFalse((self.root/'outputs/report/SUMMARY.md').exists())

    def test_report_exports_cannot_escape_outputs_or_overlap_summary(self):
        for operation in (reporting.report, reporting.export_review, reporting.figures):
            for directory in ('data/raw/new','../escape','outputs/summary/child','outputs'):
                with self.subTest(operation=operation.__name__,directory=directory):
                    with self.assertRaisesRegex(ValueError,'strictly inside outputs'):
                        operation('outputs/summary',data_root=self.root,output_dir=directory)
        self.assertFalse((self.root/'data').exists())


if __name__ == '__main__':
    unittest.main()
