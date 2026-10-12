"""Step 14: hand-derived pairs, prepared file contracts and failure evidence."""
import copy
import hashlib
import math
from pathlib import Path
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
from shapely.geometry import Point, box

from src import data, spatial
import test_preparation as preparation
import test_poi_cleaning as cleaning


def query(key, xy, **changes):
    return dict(station_key=key, xy=xy, selected=True, query_eligible=True, in_target_pua=True, **changes)


def candidate(fid, identity, xy, code="01010006", **changes):
    return dict(source_path="data/raw/poi.gpkg", source_layer="POI", source_fid=fid,
        ref_no_type="text", ref_no_value=identity, raw_classification_type="text", raw_classification_value=code,
        x=xy[0], y=xy[1], group_code=code[:2], category_code=code[:4], category_number=code[2:4], class_code=code,
        group_dictionary_name="Group", category_dictionary_name="Category", class_dictionary_name="Class",
        classification_status="known", research_decision="include", classification_reason="included",
        base_eligible=True, analysis_include=True, poi_function_group=None, functional_group_pending=True,
        classification_review=False, analysis_exclusion_reason_json="[]", classification_rule_version="poi-research-v3.0.0",
        **changes)


def sample():
    stations = [query("S1", (0, 0)), query("S2", (10000, 10000)), query("S3", (100, 500))]
    stations.append(dict(query("S4", (20000, 20000)), query_eligible=False))
    pois = [candidate(1, "101", (100, 0)), candidate(2, "102", (300, 400), "01020013"),
            candidate(3, "103", (600, 0))]
    return stations, pois


class MatchingTests(unittest.TestCase):
    def test_sample_b_six_pairs_and_four_at_500(self):
        stations, pois = sample()
        rows = list(spatial.match(stations, pois))
        expected = {("S1", "101"):100, ("S1", "102"):500, ("S1", "103"):600,
                    ("S3", "101"):500, ("S3", "102"):math.sqrt(50000), ("S3", "103"):math.sqrt(500000)}
        self.assertEqual(len(rows), 6)
        self.assertEqual({(r["station_key"], r["ref_no_value"]):r["distance_m"] for r in rows}, expected)
        self.assertEqual(sum(r["distance_m"] <= 500 for r in rows), 4)

    def test_batches_and_input_order_do_not_change_records(self):
        stations, pois = sample()
        before = copy.deepcopy((stations, pois))
        expected = list(spatial.match(stations, pois))
        for size in (1, 2, 100):
            self.assertEqual(list(spatial.match(reversed(stations), reversed(pois), station_batch_size=size)), expected)
        self.assertEqual((stations, pois), before)

    def test_boundaries_have_no_tolerance(self):
        coordinates = [500., math.nextafter(500., math.inf), 1000., math.nextafter(1000., math.inf)]
        rows = list(spatial.match([query("S", (0,0))], [candidate(i,str(i),(x,0)) for i,x in enumerate(coordinates)]))
        self.assertEqual([r["ref_no_value"] for r in rows], ["0","1","2"])
        self.assertEqual(sum(r["distance_m"] <= 500 for r in rows), 1)

    def test_pua_external_and_incomplete_coverage_are_not_filters(self):
        station = dict(query("S", (0,0)), coverage_complete_proxy={500:False,1000:False})
        poi = dict(candidate(1,"out",(1,0)), in_pua=False)
        self.assertEqual(len(list(spatial.match([station],[poi]))), 1)

    def test_typed_ids_whitespace_and_colocated_different_ids_remain(self):
        pois = [candidate(1,"1",(0,0)), dict(candidate(2,"1",(0,0)),ref_no_type="integer"),
                dict(candidate(3,"1.0",(0,0)),ref_no_type="real"),candidate(4," 1 ",(0,0))]
        self.assertEqual([(r["ref_no_type"],r["ref_no_value"]) for r in spatial.match([query("S",(0,0))],pois)],
                         [("text","1"),("integer","1"),("real","1.0"),("text"," 1 ")])

    def test_zero_matches_are_reported_for_each_queried_station(self):
        events = []
        rows = list(spatial.match([query("S",(5000,0))],[candidate(1,"P",(0,0))],progress=events.append))
        self.assertEqual(rows, [])
        self.assertEqual(events[-1],dict(stage="station_done",station_key="S",match_count=0))

    def test_empty_inputs_and_nonquery_stations(self):
        for stations,pois in (([],[]),([],sample()[1]),(sample()[0],[]),
                              ([dict(query("S",(0,0)),query_eligible=False)],sample()[1])):
            self.assertEqual(list(spatial.match(stations,pois)),[])

    def test_station_identity_and_qualification_violations(self):
        for stations in ([query("S",(0,0)),query("S",(1,0))],
                         [query("A",(0,0)),query("B",(0,0))],
                         [dict(query("S",(0,0)),selected=False)],
                         [dict(query("S",(0,0)),in_target_pua=False)],
                         [query("S",(math.nan,0))], [dict(query("S",(0,0)),selected=1)]):
            with self.subTest(stations=stations), self.assertRaises(ValueError):
                list(spatial.match(stations,[]))

    def test_duplicate_ids_and_sources_fail(self):
        first=candidate(1,"P",(0,0))
        for second in (dict(first,source_fid=2),dict(first,ref_no_value="Q")):
            with self.assertRaisesRegex(ValueError,"duplicate"):
                list(spatial.match([], [first,second]))

    def test_invalid_ids_fail_without_normalizing(self):
        for kind,value in (("text"," "),("text",""),("integer","01"),("real","nan"),("real","1"),("blob","00")):
            with self.subTest(kind=kind,value=value),self.assertRaises(ValueError):
                list(spatial.match([], [dict(candidate(1,"P",(0,0)),ref_no_type=kind,ref_no_value=value)]))

    def test_invalid_qualification_classification_and_coordinates_fail(self):
        for change in (dict(base_eligible=False),dict(analysis_include=False),dict(research_decision="review"),
                       dict(classification_status="unknown_dictionary"),dict(classification_review=True),
                       dict(group_code="99"),dict(raw_classification_value="bad"),dict(x=math.inf),
                       dict(functional_group_pending=False),dict(analysis_exclusion_reason_json='["excluded"]')):
            with self.subTest(change=change),self.assertRaises(ValueError):
                list(spatial.match([], [dict(candidate(1,"P",(0,0)),**change)]))
        with self.assertRaises(ValueError):
            list(spatial.match([], [candidate(1,"EV",(0,0),"10540832")]))

    def test_batch_validation_and_legacy_classification_is_never_called(self):
        for value in (True,0,-1,1.5):
            with self.assertRaises(ValueError): list(spatial.match([],[],station_batch_size=value))
        with patch.object(spatial,"classify",side_effect=AssertionError("legacy called")):
            self.assertEqual(len(list(spatial.match(*sample()))),6)


class MatchingFileTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation.PreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.code_root = self.fixture.root, self.fixture.code_root
        self.number = 0
        self.prepare()

    def prepare(self):
        self.number += 1
        self.preparation_dir=f"outputs/matching_prepare{self.number}/full"
        self.prepared=self.fixture.run_prepare(output_dir=self.preparation_dir)
        self.seal()

    def seal(self):
        directory=self.root/self.preparation_dir
        self.prepared=data._read_json(directory/'preparation_report.json')
        self.prepared['artifacts']={name:data._artifact_fingerprint(directory/name) for name in self.prepared['artifacts']}
        data._json_write(directory/'preparation_report.json',self.prepared)
        artifacts={str(p.relative_to(directory.parent)):data._artifact_fingerprint(p) for p in directory.rglob('*') if p.is_file()}
        data._json_write(directory.parent/'verification.json',dict(status='passed',artifacts=artifacts))

    def run_match(self, **changes):
        self.number+=1
        return data.match_files(self.preparation_dir,**dict(dict(data_root=self.root,code_root=self.code_root,
            output_dir=f"outputs/match{self.number}",station_batch_size=1,write_batch_size=2),**changes))

    def fail(self, **changes):
        with self.assertRaises(data.MatchingError) as caught:self.run_match(**changes)
        report=caught.exception.report
        self.assertEqual(report['status'],'failed')
        self.assertFalse(report['validation_complete'])
        self.assertEqual(data._read_json(Path(report['output_dir'])/'matching_report.json')['status'],'failed')
        self.assertFalse((Path(report['output_dir'])/'matches.parquet').exists())
        return report

    def alter_candidates(self, change, schema=None, logical=True):
        directory=self.root/self.preparation_dir
        path=directory/'processed/pois.parquet'
        rows=pq.read_table(path).to_pylist()
        change(rows)
        pq.write_table(pa.Table.from_pylist(rows,schema=schema or data.poi_parquet_schema('poi-research-v3.0.0')),path)
        if logical:
            validation=data._read_json(directory/'validation_report.json')
            digest=hashlib.sha256()
            for row in rows:data._preparation_digest(digest,row)
            validation['logical_content_sha256'][0]=digest.hexdigest()
            data._json_write(directory/'validation_report.json',validation)
        self.seal()

    def test_hand_file_pairs_readback_and_resource_report(self):
        report=self.run_match()
        rows=pq.read_table(Path(report['output_dir'])/'matches.parquet').to_pylist()
        self.assertEqual(report['counts'],dict(candidate_count=4,query_count=2,processed_query_count=2,
            match_count=6,within_500_count=1,within_1000_count=6,unique_matched_candidate_count=4))
        pairs={(r['station_key'],r['ref_no_value']):r['distance_m'] for r in rows}
        a,b=[s['station_key'] for s in self.fixture.stations]
        self.assertEqual(pairs,{(a,'P01'):0.,(a,'P02'):750.,(a,'P08'):750.,
                                (b,'P02'):750.,(b,'P03'):1000.,(b,'P08'):750.})
        self.assertTrue(all(r['functional_group_pending'] and r['poi_function_group'] is None for r in rows))
        self.assertEqual(len(report['processed_stations']),2)
        self.assertTrue(report['input_bytes_unchanged'])
        self.assertEqual(report['input_fingerprints_before'],report['input_fingerprints_after'])
        self.assertGreater(report['elapsed_seconds'],0)
        self.assertGreater(report['peak_rss_kib'],0)
        self.assertEqual(report['status'],'completed')

    def test_file_batches_preserve_all_content(self):
        results=[]
        for station_batch,write_batch in ((1,1),(2,3),(99,100)):
            report=self.run_match(station_batch_size=station_batch,write_batch_size=write_batch)
            results.append(pq.read_table(Path(report['output_dir'])/'matches.parquet').to_pylist())
        self.assertEqual(results[0],results[1]);self.assertEqual(results[1],results[2])

    def test_no_legacy_or_raw_cleaning_calls(self):
        with (patch.object(data,'clean_poi_batch',side_effect=AssertionError('reclean')),
             patch.object(data,'poi_batches',side_effect=AssertionError('raw scan')),
             patch.object(spatial,'match_synthetic',side_effect=AssertionError('legacy'))):
            self.assertEqual(self.run_match()['counts']['match_count'],6)

    def test_empty_candidates_have_fixed_schema_and_queried_zeros(self):
        self.fixture.rebuild([cleaning.record(1,'far','01010006',(4000,0))])
        self.prepare()
        report=self.run_match()
        table=pq.read_table(Path(report['output_dir'])/'matches.parquet')
        self.assertEqual(table.num_rows,0)
        self.assertTrue(table.schema.equals(data.matching_parquet_schema('poi-research-v3.0.0'),check_metadata=True))
        self.assertEqual([s['match_count'] for s in report['processed_stations']],[0,0])

    def test_no_selected_or_queryable_stations_is_empty(self):
        self.fixture.rebuild(preparation.hand_rows(),stations=[preparation.station(1,(0,0),selected=False,query=False)])
        self.prepare()
        report=self.run_match()
        self.assertEqual(report['counts']['candidate_count'],0)
        self.assertEqual(report['counts']['query_count'],0)
        self.assertEqual(report['processed_stations'],[])
        self.assertEqual(pq.read_table(Path(report['output_dir'])/'matches.parquet').num_rows,0)

    def test_one_query_can_have_zero_with_nonempty_candidates(self):
        self.fixture.rebuild([cleaning.record(1,'near','01010006',(0,0))])
        self.prepare()
        report=self.run_match()
        self.assertEqual(sorted(s['match_count'] for s in report['processed_stations']),[0,1])

    def test_unknown_and_charging_never_match_after_v3_preparation(self):
        rows=[cleaning.record(1,'101','01010006',(100,0)),cleaning.record(2,'102','01020013',(300,400)),
              cleaning.record(3,'103','99999999',(600,0)),cleaning.record(4,'104','01010006',(1100,0)),
              cleaning.record(5,'105','10540832',(100,0))]
        stations=[preparation.station(1,(0,0)),preparation.station(2,(100,500))]
        # The synthetic preparation fixture's default PUA is a narrow strip;
        # this test expands only its sealed synthetic PUA, not production data.
        self.fixture.rebuild(rows,stations=stations,coverage=box(-100,-100,5000,2000))
        scope_dir=self.root/self.fixture.options['scope_dir']
        (scope_dir/'study_area.wkb').write_bytes(box(-1,-1,1501,501).wkb)
        self.fixture.fixture.seal(self.fixture.options['scope_dir'])
        station_dir=self.root/self.fixture.station_dir
        definition=data._read_json(station_dir/'coverage_definition.json')
        definition['order']['bounds']=[-100,-100,5000,2000]
        definition['upstream_scope']['verification']=data._artifact_fingerprint(scope_dir.parent/'verification.json')
        definition['upstream_scope']['artifacts']['study_area.wkb']=data._artifact_fingerprint(scope_dir/'study_area.wkb')
        data._json_write(station_dir/'coverage_definition.json',definition)
        self.fixture.mutate_station(lambda r:r['artifacts'].update({'coverage_definition.json':data._artifact_fingerprint(station_dir/'coverage_definition.json')}))
        self.prepare()
        self.assertEqual(self.run_match()['counts']['match_count'],4)

    def test_unsealed_or_failed_preparation_is_rejected(self):
        path=self.root/self.preparation_dir/'preparation_report.json'
        report=data._read_json(path);report['status']='failed';data._json_write(path,report)
        self.fail()
        self.seal();self.fail()

    def test_failed_validation_is_rejected(self):
        path=self.root/self.preparation_dir/'validation_report.json'
        report=data._read_json(path);report['candidate_ids_unique']=False;data._json_write(path,report)
        self.seal();self.fail()

    def test_changed_raw_source_is_rejected(self):
        with (self.root/self.fixture.poi).open('ab') as stream:stream.write(b'changed')
        self.fail()

    def test_coverage_source_subset_is_bound_without_requiring_unrelated_sources(self):
        directory=self.root/self.fixture.station_dir
        definition=data._read_json(directory/'coverage_definition.json')
        definition['input_fingerprints'].pop(self.fixture.fixture.accuracy)
        data._json_write(directory/'coverage_definition.json',definition)
        def change(report):
            report['input_fingerprints_before']=definition['input_fingerprints']
            report['input_fingerprints_after']=definition['input_fingerprints']
            report['artifacts']['coverage_definition.json']=data._artifact_fingerprint(directory/'coverage_definition.json')
        self.fixture.mutate_station(change)
        self.prepare()
        self.assertEqual(self.run_match()['counts']['match_count'],6)

    def test_changed_rules_or_station_bytes_are_rejected(self):
        path=self.code_root/'config/rules.csv'
        original=path.read_bytes();path.write_bytes(original+b'\n')
        self.fail();path.write_bytes(original)
        path=self.root/self.fixture.station_dir/'stations.parquet'
        with path.open('ab') as stream:stream.write(b'changed')
        self.fail()

    def test_bound_station_query_xy_mismatch_is_rejected(self):
        path=self.root/self.preparation_dir/'preparation_report.json'
        report=data._read_json(path);report['stations']['queries'][0]['xy'][0]+=1
        data._json_write(path,report);self.seal();self.fail()

    def test_candidate_schema_and_crs_mismatch_is_rejected(self):
        schema=data.poi_parquet_schema('poi-research-v3.0.0')
        self.alter_candidates(lambda rows:None,schema=schema.with_metadata({**schema.metadata,b'crs':b'EPSG:4326'}))
        self.fail()

    def test_duplicate_candidate_identity_is_rejected(self):
        self.alter_candidates(lambda rows:rows[1].update(ref_no_value=rows[0]['ref_no_value']))
        self.fail()

    def test_geometry_xy_disagreement_is_rejected(self):
        self.alter_candidates(lambda rows:rows[0].update(x=99.0))
        self.fail()

    def test_resealed_unknown_class_and_invalid_eligibility_are_rejected(self):
        self.alter_candidates(lambda rows:rows[0].update(class_code='99999999'))
        self.fail()

    def test_invalid_eligibility_is_not_silently_filtered(self):
        self.alter_candidates(lambda rows:rows[0].update(analysis_include=False))
        self.fail()

    def test_candidate_digest_change_is_rejected(self):
        self.alter_candidates(lambda rows:rows[0].update(raw_fields_json='{}'),logical=False)
        self.fail()

    def test_candidate_outside_all_queries_fails_union_conservation(self):
        def change(rows):
            rows[0].update(x=4000.,y=0.,geometry_wkb=Point(4000,0).wkb,
                           geometry_gpkg=preparation.geometry_blob(Point(4000,0)))
        self.alter_candidates(change)
        self.assertIn('union',self.fail()['errors'][0])

    def test_input_change_during_matching_retains_failure(self):
        def progress(event):
            if event['stage']=='matching':
                with (self.root/self.fixture.poi).open('ab') as stream:stream.write(b'changed')
        self.assertIn('changed during run',self.fail(progress=progress)['errors'][0])

    def test_write_failure_is_retained(self):
        with patch('pyarrow.parquet.ParquetWriter',side_effect=OSError('simulated disk failure')):
            self.assertIn('simulated disk failure',self.fail()['errors'][0])

    def test_readback_corruption_is_rejected(self):
        def progress(event):
            if event['stage']=='readback':
                path=next(self.root.glob('outputs/match*/matches.parquet.tmp'))
                table=pq.read_table(path);rows=table.to_pylist();rows[0]['distance_m']+=1
                pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
        self.assertIn('readback mismatch',self.fail(progress=progress)['errors'][0])

    def test_existing_overlapping_or_escaping_output_is_rejected(self):
        report=self.run_match()
        for output,exception in ((str(Path(report['output_dir']).relative_to(self.root)),FileExistsError),
                                 (self.preparation_dir+'/nested',ValueError),
                                 (self.fixture.station_dir+'/nested',ValueError),('../escaped',ValueError)):
            with self.assertRaises(exception):self.run_match(output_dir=output)

    def test_duplicate_pair_from_producer_fails_readback(self):
        original=spatial.match
        def duplicate(*args,**kwargs):
            for row in original(*args,**kwargs):
                yield row
                yield row
        with patch.object(spatial,'match',side_effect=duplicate):
            self.assertIn('duplicate/out-of-order',self.fail()['errors'][0])

    def test_batch_parameters_reject_bools_and_invalid_values(self):
        for name in ('station_batch_size','write_batch_size'):
            for value in (True,0,-1,1.5):
                with self.assertRaises(ValueError):self.run_match(**{name:value})


if __name__ == '__main__':
    unittest.main()
