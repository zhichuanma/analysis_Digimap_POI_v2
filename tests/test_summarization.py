"""Step 15: hand-derived cells, reference comparison and sealed file failures."""
import copy
import json
import math
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from src import analysis, data, spatial
import test_analysis as reference
import test_preparation as preparation
import test_poi_cleaning as cleaning


def base_rows(stations):
    return [dict(station_key=s['station_key'], radius_m=r, selected=True, query_eligible=s['query_eligible'],
        main_status='synthetic', within_gb_proxy=True, gb_boundary_distance_m=2000., order_clearance_m=2000.,
        coverage_complete_proxy=s['coverage_complete_proxy'][r], coverage_reasons=[])
        for s in stations if s['selected'] for r in (500,1000)]


def match_row(station, identity, distance=100., code='01010006', kind='text'):
    return dict(station_key=station,ref_no_type=kind,ref_no_value=identity,distance_m=distance,
                group_code=code[:2],category_code=code[:4],class_code=code)


CORE_SCHEMA=pa.schema([(k,pa.string()) for k in ('station_key','ref_no_type','ref_no_value')]
    + [('distance_m',pa.float64())] + [(k,pa.string()) for k in ('group_code','category_code','class_code')])


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.number=0

    def execute(self, stations, matches, *, groups=('01','99'), labels=(), base=None, threads=1):
        self.number+=1
        path=self.root/f'matches{self.number}.parquet'
        pq.write_table(pa.Table.from_pylist(matches,schema=CORE_SCHEMA),path)
        base=base_rows(stations) if base is None else base
        events=[]
        with analysis.connection(path,temp_directory=self.root/f'tmp{self.number}',threads=threads) as con:
            result=analysis.summarize(con,pa.Table.from_pylist(base,schema=data.station_parquet_schemas()[1]),
                                     group_codes=groups,labels=labels,progress=events.append)
            tables={n:con.execute(f'SELECT * FROM {n} ORDER BY {data.SUMMARY_ORDER[n]}').fetch_arrow_table().to_pylist()
                    for n in analysis.summary_schemas()}
        return tables,result,events

    def compare_reference(self, stations, rows, groups):
        adapted=[dict(r,ref_no=r['ref_no_value']) for r in rows]
        expected=analysis.summarize_synthetic(stations,adapted,group_codes=groups)
        actual,_,_=self.execute(stations,rows,groups=groups)
        for name,records in expected.items():
            keys=data.SUMMARY_ORDER[name].split(',')
            wanted=sorted(records,key=lambda r:tuple(r[k] for k in keys))
            self.assertEqual(len(actual[name]),len(wanted))
            for a,e in zip(actual[name],wanted):
                for field,value in e.items():
                    observed=a[field]
                    if field=='count_distribution':observed={int(k):v for k,v in json.loads(observed).items()}
                    if isinstance(value,float):self.assertAlmostEqual(observed,value,places=12)
                    else:self.assertEqual(observed,value,(name,field,a,e))
        return actual

    def sample(self, *, formal=False):
        stations,pois=reference.sample_b()
        if formal:pois[2]['raw_code']='01010006'
        original=spatial.match_synthetic(stations,pois,reference.TEST_DICTIONARY)['matches']
        rows=[dict(r,ref_no_type='text',ref_no_value=r['ref_no']) for r in original]
        return stations,rows

    def test_original_sample_b_every_reference_field(self):
        tables=self.compare_reference(*self.sample(),('01','99'))
        self.assertEqual(len(tables['composition']),32)

    def test_formal_sample_b_hand_counts_and_composition(self):
        tables=self.compare_reference(*self.sample(formal=True),('01','99'))
        self.assertEqual([(r['observed_count'],r['interpretable_count']) for r in tables['station_radius_totals']],
                         [(2,2),(3,3),(0,0),(0,0),(2,None),(3,None),(None,None),(None,None)])
        cells={(r['station_key'],r['radius_m'],r['level'],r['code']):r for r in tables['composition']}
        for key in ('S1','S3'):
            self.assertEqual(cells[key,1000,'group','01']['share'],1)
            self.assertEqual(cells[key,1000,'class','01010006']['count'],2)
            self.assertAlmostEqual(cells[key,1000,'category','0101']['share'],2/3)

    def test_complete_and_observable_scopes_have_distinct_denominators(self):
        tables,_,_=self.execute(*self.sample(formal=True))
        actual={(r['radius_m'],r['scope']):(r['station_count'],r['positive_station_count'],r['zero_station_count'],
                r['match_count'],r['unique_poi_count']) for r in tables['radius_summary']}
        self.assertEqual(actual,{(500,'all_observable'):(3,2,1,4,2),(500,'complete_coverage'):(2,1,1,2,2),
                                 (1000,'all_observable'):(3,2,1,6,3),(1000,'complete_coverage'):(2,1,1,3,3)})

    def test_equal_station_mean_is_not_pooled_share(self):
        stations=[reference.station(k,(i*5000,0)) for i,k in enumerate(('A','B','Z'))]
        rows=[match_row(k,f'{k}{i}',code='01010006' if i<n else '99999999')
              for k,n,total in (('A',1,2),('B',9,10)) for i in range(total)]
        tables,_,_=self.execute(stations,rows)
        for row in tables['group_summary']:
            if row['group_code']=='01':
                self.assertAlmostEqual(row['station_mean_share'],.7)
                self.assertAlmostEqual(row['pooled_match_share'],10/12)
                self.assertEqual((row['station_count'],row['positive_station_count']),(3,2))
        self.assertTrue(all(json.loads(r['count_distribution'])=={'0':1,'2':1,'10':1} for r in tables['radius_summary']))

    def test_zero_and_missing_cells_and_sparse_layers(self):
        tables,_,_=self.execute(*self.sample())
        zero=[r for r in tables['composition'] if r['station_key']=='S2']
        self.assertEqual(len(zero),4)
        self.assertTrue(all(r['count']==0 and r['share'] is None and r['level']=='group' for r in zero))
        self.assertFalse(any(r['station_key']=='S4' for r in tables['composition']))

    def test_unknown_expands_globally_only_when_matched(self):
        stations=[reference.station('A',(0,0)),reference.station('Z',(5000,0))]
        row=match_row('A','x',750.)
        row.update(group_code='UNKNOWN',category_code='UNKNOWN',class_code='UNKNOWN')
        tables,result,_=self.execute(stations,[row],groups=('01',))
        self.assertEqual(result['group_codes'],['01','UNKNOWN'])
        self.assertEqual(len([r for r in tables['composition'] if r['level']=='group' and r['code']=='UNKNOWN']),4)
        _,result,_=self.execute(stations,[],groups=('01','UNKNOWN'))
        self.assertEqual(result['group_codes'],['01'])
        self.assertFalse(result['unknown_expanded'])

    def test_valid_unknown_prefix_is_preserved_with_null_name(self):
        tables,_,_=self.execute([reference.station('A',(0,0))],[match_row('A','x',code='99999999')])
        self.assertTrue(any(r['code']=='99' and r['count']==1 and r['dictionary_name'] is None
                            for r in tables['composition']))

    def test_official_and_unmatched_domain_groups_keep_zeros_and_names(self):
        labels=[dict(level='group',code='01',dictionary_name='Official'),dict(level='group',code='02',dictionary_name='Unused')]
        tables,_,_=self.execute([reference.station('A',(0,0))],[match_row('A','x')],groups=('01','02','99'),labels=labels)
        cells=[r for r in tables['composition'] if r['level']=='group']
        self.assertEqual(len(cells),6)
        self.assertTrue(all(r['count']==0 and r['share']==0 for r in cells if r['code']!='01'))
        self.assertTrue(all(r['dictionary_name']=='Official' for r in cells if r['code']=='01'))

    def test_strict_cumulative_boundary(self):
        rows=[match_row('A',str(i),d) for i,d in enumerate((500.,math.nextafter(500.,math.inf),1000.))]
        tables,_,_=self.execute([reference.station('A',(0,0))],rows)
        self.assertEqual([r['observed_count'] for r in tables['station_radius_totals']],[1,3])

    def test_identity_types_whitespace_and_shared_poi(self):
        stations=[reference.station('A',(0,0)),reference.station('B',(5000,0))]
        rows=[match_row('A','1',kind='text'),match_row('A','1',kind='integer'),
              match_row('A',' 1 '),match_row('B','1',kind='text')]
        tables,_,_=self.execute(stations,rows)
        self.assertTrue(all((r['match_count'],r['unique_poi_count'])==(4,3) for r in tables['radius_summary']))

    def test_coverage_population_changes_at_each_radius(self):
        stations=[reference.station('A',(0,0),complete=(True,False)),reference.station('Z',(5000,0))]
        tables,_,_=self.execute(stations,[match_row('A','x')])
        complete=[r for r in tables['radius_summary'] if r['scope']=='complete_coverage']
        self.assertEqual([(r['station_count'],r['match_count']) for r in complete],[(2,1),(1,0)])

    def test_empty_matches_keep_selected_totals_and_null_shares(self):
        stations,_=self.sample()
        tables,_,_=self.execute(stations,[])
        self.assertEqual(len(tables['station_radius_totals']),8)
        self.assertEqual(len(tables['composition']),12)
        self.assertTrue(all(r['share'] is None for r in tables['composition']))

    def test_no_observable_stations(self):
        tables,_,_=self.execute([reference.station('A',(0,0),queryable=False,complete=(False,False))],[])
        self.assertEqual(tables['composition'],[])
        self.assertTrue(all(r['observed_count'] is None for r in tables['station_radius_totals']))
        self.assertTrue(all(r['station_count']==0 and r['count_distribution']=='{}' for r in tables['radius_summary']))

    def test_no_selected_stations_preserves_summary_domain(self):
        tables,result,_=self.execute([],[])
        self.assertEqual(result['row_counts'],dict(station_radius_totals=0,composition=0,radius_summary=4,group_summary=8))
        self.assertTrue(all(r['station_mean_share'] is None and r['pooled_match_share'] is None for r in tables['group_summary']))

    def test_order_and_threads_do_not_change_cells(self):
        stations,rows=self.sample()
        one,_,events=self.execute(stations,rows)
        two,_,_=self.execute(list(reversed(stations)),list(reversed(rows)),threads=2)
        self.assertEqual(one,two)
        self.assertEqual(events,['station_totals_verified','composition_verified','summaries_verified'])

    def test_random_small_reference_all_fields(self):
        rng=random.Random(20261012)
        stations=[reference.station(str(i),(i*5000,0),complete=(True,i%2==0)) for i in range(6)]
        rows=[match_row(str(i),str(j),rng.choice([0,500,500.000001,999,1000]),
              rng.choice(['01010006','01020013','99999999'])) for i in range(6) for j in range(i*10,(i+1)*10)]
        self.compare_reference(stations,rows,('01','02','99'))

    def test_duplicate_pairs_rejected(self):
        row=match_row('A','x')
        with self.assertRaisesRegex(ValueError,'duplicate station-POI'):
            self.execute([reference.station('A',(0,0))],[row,row])

    def test_missing_group_rejected(self):
        with self.assertRaisesRegex(ValueError,'missing matched group'):
            self.execute([reference.station('A',(0,0))],[match_row('A','x',code='99999999')],groups=('01',))

    def test_invalid_distances_rejected(self):
        for distance in (None,-1,math.nan,math.inf,math.nextafter(1000.,math.inf)):
            with self.subTest(distance=distance),self.assertRaises(ValueError):
                self.execute([reference.station('A',(0,0))],[match_row('A','x',distance)])

    def test_inconsistent_classification_and_identity_rejected(self):
        for change in (dict(group_code='02'),dict(class_code=None),dict(ref_no_type=None),dict(ref_no_value=None)):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.execute([reference.station('A',(0,0))],[dict(match_row('A','x'),**change)])
        with self.assertRaisesRegex(ValueError,'conflicting classification'):
            self.execute([reference.station('A',(0,0)),reference.station('B',(5000,0))],
                         [match_row('A','x'),match_row('B','x',code='99999999')])

    def test_unknown_or_nonquery_station_rejected(self):
        for stations in ([],[reference.station('A',(0,0),queryable=False,complete=(False,False))]):
            with self.assertRaisesRegex(ValueError,'selected queryable'):
                self.execute(stations,[match_row('A','x')])

    def test_bad_base_rows_rejected(self):
        good=base_rows([reference.station('A',(0,0))])
        for base in (good[:1],good+[good[0]],[dict(good[0],selected=False),good[1]],
                     [dict(good[0],coverage_complete_proxy=False),good[1]],
                     [dict(r,query_eligible=False) for r in good]):
            with self.assertRaises(ValueError):self.execute([],[],base=base)

    def test_duplicate_labels_and_domain_rejected(self):
        label=dict(level='group',code='01',dictionary_name='A')
        for args in (dict(labels=[label,label]),dict(groups=['01','01']),dict(groups=['1'])):
            with self.assertRaises(ValueError):self.execute([],[],**args)

    def test_connection_settings_rejected(self):
        for kwargs in (dict(threads=True),dict(threads=0),dict(memory_limit='unlimited'),dict(memory_limit='0MB')):
            with self.assertRaises(ValueError):
                with analysis.connection(self.root/'missing',temp_directory=self.root/'spill',**kwargs):pass


class SummaryFileTests(unittest.TestCase):
    def setUp(self):
        self.fixture=preparation.PreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root,self.code_root=self.fixture.root,self.fixture.code_root
        self.number=0
        self.build_matching()

    def seal(self, relative):
        directory=self.root/relative
        artifacts={str(p.relative_to(directory.parent)):data._artifact_fingerprint(p)
                   for p in directory.rglob('*') if p.is_file()}
        data._json_write(directory.parent/'verification.json',dict(status='passed',artifacts=artifacts))

    def build_matching(self):
        self.number+=1
        station_dir=self.root/self.fixture.station_dir
        data._write_parquet(station_dir/'station_radius_base.parquet',spatial.station_radius_base(self.fixture.stations),
                            data.station_parquet_schemas()[1])
        report=data._read_json(station_dir/'station_preparation_report.json')
        report['artifacts']['station_radius_base.parquet']=data._artifact_fingerprint(station_dir/'station_radius_base.parquet')
        data._json_write(station_dir/'station_preparation_report.json',report)
        self.fixture.fixture.seal(self.fixture.station_dir)
        prepared=f'outputs/summary_prepare{self.number}/full'
        self.fixture.run_prepare(output_dir=prepared)
        self.seal(prepared)
        self.matching=f'outputs/summary_matching{self.number}/full'
        self.matched=data.match_files(prepared,data_root=self.root,code_root=self.code_root,output_dir=self.matching)
        self.seal(self.matching)

    def run_summary(self, **changes):
        self.number+=1
        return data.summarize_files(self.matching,**dict(dict(data_root=self.root,code_root=self.code_root,
            output_dir=f'outputs/summary{self.number}',write_batch_size=2),**changes))

    def fail(self, **changes):
        with self.assertRaises(data.SummaryError) as caught:self.run_summary(**changes)
        report=caught.exception.report
        self.assertEqual(report['status'],'failed')
        self.assertFalse(report['validation_complete'])
        self.assertEqual(data._read_json(Path(report['output_dir'])/'summary_report.json')['status'],'failed')
        self.assertFalse((Path(report['output_dir'])/'composition.parquet').exists())
        return report

    def alter_match(self, change=None, schema=None, reseal=True):
        directory=self.root/self.matching
        path=directory/'matches.parquet'
        table=pq.read_table(path)
        rows=table.to_pylist()
        if change:change(rows)
        pq.write_table(pa.Table.from_pylist(rows,schema=schema or table.schema),path)
        if reseal:
            report=data._read_json(directory/'matching_report.json')
            report['artifacts']['matches.parquet']=data._artifact_fingerprint(path)
            data._json_write(directory/'matching_report.json',report)
            self.seal(self.matching)

    def test_formal_files_hand_counts_and_readback(self):
        report=self.run_summary()
        self.assertEqual(report['status'],'completed')
        self.assertEqual(report['row_counts']['station_radius_totals'],4)
        self.assertEqual(report['match_counts']['match_count'],6)
        self.assertTrue(report['validation_complete'])
        self.assertFalse(report['user_accepted'])
        self.assertEqual(report['input_fingerprints_before'],report['input_fingerprints_after'])
        self.assertGreater(report['peak_rss_kib'],0)
        output=Path(report['output_dir'])
        schemas=analysis.summary_schemas()
        totals=list(data._summary_csv_rows(output/'station_radius_totals.csv',schemas['station_radius_totals']))
        self.assertEqual(sorted(r['observed_count'] for r in totals),[0,1,3,3])
        self.assertTrue(all(r['interpretable_count'] is None for r in totals))
        groups=list(data._summary_csv_rows(output/'group_summary.csv',schemas['group_summary']))
        self.assertTrue(any(r['group_code']=='01' for r in groups))
        self.assertTrue(all(r['station_mean_share'] is None for r in groups if r['scope']=='complete_coverage'))
        self.assertTrue(pq.read_schema(output/'composition.parquet').equals(schemas['composition'],check_metadata=True))

    def test_batches_and_threads_preserve_typed_content(self):
        outputs=[]
        for batch,threads in ((1,1),(99,2)):
            report=self.run_summary(write_batch_size=batch,threads=threads)
            output=Path(report['output_dir'])
            outputs.append(data._read_json(output/'validation_report.json')['tables'])
        self.assertEqual(outputs[0],outputs[1])

    def test_no_cleaning_matching_or_geometry_queries_called(self):
        with (patch.object(data,'poi_batches',side_effect=AssertionError('raw scan')),
              patch.object(data,'clean_poi_batch',side_effect=AssertionError('clean')),
              patch.object(spatial,'match',side_effect=AssertionError('match')),
              patch.object(analysis,'summarize_synthetic',side_effect=AssertionError('reference used'))):
            self.assertEqual(self.run_summary()['match_counts']['match_count'],6)

    def test_zero_candidates_have_fixed_output_schema(self):
        self.fixture.rebuild([cleaning.record(1,'far','01010006',(4000,0))])
        self.build_matching()
        report=self.run_summary()
        self.assertEqual(report['match_counts']['match_count'],0)
        table=pq.read_table(Path(report['output_dir'])/'composition.parquet')
        self.assertGreater(table.num_rows,0)
        self.assertTrue(all(r['level']=='group' and r['count']==0 and r['share'] is None for r in table.to_pylist()))

    def test_no_selected_stations_write_readable_empty_files(self):
        self.fixture.rebuild(preparation.hand_rows(),stations=[preparation.station(1,(0,0),selected=False,query=False)])
        self.build_matching()
        report=self.run_summary()
        output=Path(report['output_dir'])
        self.assertEqual(pq.read_table(output/'composition.parquet').num_rows,0)
        self.assertEqual(list(data._summary_csv_rows(output/'station_radius_totals.csv',analysis.summary_schemas()['station_radius_totals'])),[])
        self.assertEqual(report['row_counts']['radius_summary'],4)

    def test_nonquery_selected_station_keeps_null_totals(self):
        self.fixture.rebuild(preparation.hand_rows(),stations=[preparation.station(1,(0,0),query=False)])
        self.build_matching()
        report=self.run_summary()
        totals=list(data._summary_csv_rows(Path(report['output_dir'])/'station_radius_totals.csv',
                                           analysis.summary_schemas()['station_radius_totals']))
        self.assertEqual(len(totals),2)
        self.assertTrue(all(r['observed_count'] is None for r in totals))

    def test_changed_match_bytes_rejected(self):
        self.alter_match(lambda rows:rows[0].update(distance_m=1.),reseal=False)
        self.fail()

    def test_failed_matching_report_rejected(self):
        path=self.root/self.matching/'matching_report.json'
        report=data._read_json(path);report['status']='failed';data._json_write(path,report)
        self.seal(self.matching)
        self.fail()

    def test_failed_matching_validation_rejected(self):
        directory=self.root/self.matching
        validation=data._read_json(directory/'validation_report.json');validation['pairs_unique']=False
        data._json_write(directory/'validation_report.json',validation)
        report=data._read_json(directory/'matching_report.json')
        report['artifacts']['validation_report.json']=data._artifact_fingerprint(directory/'validation_report.json')
        data._json_write(directory/'matching_report.json',report);self.seal(self.matching)
        self.fail()

    def test_match_schema_mismatch_rejected(self):
        schema=data.matching_parquet_schema('poi-research-v3.0.0').with_metadata({b'crs':b'EPSG:4326'})
        self.alter_match(schema=schema)
        self.fail()

    def test_match_candidate_fields_or_station_binding_rejected(self):
        self.alter_match(lambda rows:rows[0].update(group_code='99'))
        self.fail()

    def test_unknown_class_cannot_enter_formal_statistics(self):
        self.alter_match(lambda rows:rows[0].update(group_code='UNKNOWN',category_code='UNKNOWN',class_code='UNKNOWN'))
        self.fail()

    def test_inconsistent_matching_counts_rejected(self):
        directory=self.root/self.matching
        report=data._read_json(directory/'matching_report.json');report['counts']['match_count']+=1
        data._json_write(directory/'matching_report.json',report);self.seal(self.matching)
        self.fail()

    def test_changed_radius_base_rejected(self):
        path=self.root/self.fixture.station_dir/'station_radius_base.parquet'
        table=pq.read_table(path);rows=table.to_pylist();rows[0]['coverage_complete_proxy']=True
        pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
        self.fail()

    def test_input_changed_during_summary_rejected(self):
        path=self.root/self.matching/'matching_report.json'
        def change(event):
            if event['stage']=='summaries_verified':path.write_text(path.read_text()+' ')
        self.fail(progress=change)

    def test_write_and_readback_failures_retained(self):
        for name in ('_write_summary_tables','_verify_summary_tables'):
            with patch.object(data,name,side_effect=OSError('injected I/O failure')):self.fail()

    def test_readback_detects_changed_cell(self):
        original=data._write_summary_tables
        def corrupt(*args,**kwargs):
            result=original(*args,**kwargs)
            path=args[1]/'composition.parquet.tmp'
            table=pq.read_table(path);rows=table.to_pylist();rows[0]['count']+=1
            pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
            return result
        with patch.object(data,'_write_summary_tables',side_effect=corrupt):self.fail()

    def test_reused_nested_and_outside_output_rejected(self):
        report=self.run_summary()
        for directory in (str(Path(report['output_dir']).relative_to(self.root)),
                          self.matching+'/nested',self.fixture.station_dir+'/nested','data/raw/summary','../outside'):
            with self.assertRaises((ValueError,FileExistsError)):self.run_summary(output_dir=directory)
