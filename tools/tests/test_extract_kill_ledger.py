import copy
import json
from pathlib import Path
import struct
import os
import subprocess
import sys
import tempfile
import unittest

import pyarrow as pa
import pyarrow.parquet as pq
from support import TempDirTestCase
import extract_kill_ledger as tool
from test_extract_kill_observations import SCHEMA, array, row as field_row
from wire_fixtures import write_empty_tables

from extract_kill_ledger import (
    ActorIdentityIndex, DEATH_NAME, InputError, match_deaths, validate_death_payload,
)

CHARACTER = '/Game/Characters/Test/Test_PC.Test_PC_C'


def actor(time=10, packet=1, event='open', guid=100, group=CHARACTER):
    return {'time_ms':time, 'packet_id':packet, 'event':event, 'actor_net_guid':guid, 'class_path':group}


def prop(time=10, packet=1, value=20, **kwargs):
    row = {'time_ms':time, 'packet_id':packet, 'actor_net_guid':100,
           'object_net_guid':None, 'group_path':CHARACTER, 'field_name':'PlayerState',
           'value_i64':value, 'value_f64':None, 'value_bool':None, 'value_str':None,
           'raw_bits':bytes([value*2]) if type(value) is int and value < 128 else b'\x00', 'bit_count':8}
    return dict(row, **kwargs)


class IdentityTests(unittest.TestCase):
    def test_prior_identity_dormancy_and_closed_actor(self):
        index = ActorIdentityIndex([actor(), actor(30,3,'dormant'), actor(50,5,'close')], [(42,prop())])
        value = index.resolve(100,40)
        self.assertEqual((value['status'],value['player_state_ref'],value['mapping_field_rows']),('resolved',20,[42]))
        self.assertEqual(index.resolve(100,50)['status'],'event_time_lifecycle_boundary')
        self.assertEqual(index.resolve(100,51)['status'],'actor_closed')

    def test_reopen_does_not_inherit_prior_player_state(self):
        index = ActorIdentityIndex([actor(),actor(20,2,'close'),actor(30,3)],[(0,prop())])
        self.assertEqual(index.resolve(100,40)['status'],'no_prior_player_state')

    def test_unknown_and_conflicting_latest_values_clear_identity(self):
        for extra,expected in [([(1,prop(20,2,None))],'untyped_player_state'),
                               ([(1,prop(20,2,20)),(2,prop(20,2,21))],'ambiguous_player_state')]:
            index=ActorIdentityIndex([actor()],[(0,prop()),*extra])
            self.assertEqual(index.resolve(100,30)['status'],expected)

    def test_equal_event_time_mapping_is_not_given_packet_order(self):
        index=ActorIdentityIndex([actor()],[(0,prop()),(1,prop(30,3,21))])
        self.assertEqual(index.resolve(100,30)['status'],'event_time_mapping_boundary')
        self.assertEqual(index.resolve(100,31)['player_state_ref'],21)

    def test_nested_foreign_group_and_future_mappings_do_not_resolve(self):
        for row in [prop(object_net_guid=500),prop(group_path='/Game/Other'),prop(time=40,packet=4)]:
            index=ActorIdentityIndex([actor()],[(0,row)])
            self.assertEqual(index.resolve(100,30)['status'],'no_prior_player_state')

    def test_raw_reference_and_python_bool_cannot_masquerade_as_id(self):
        for row in [prop(value=20,raw_bits=b'\x2a'),prop(value=True),prop(value=20,value_bool=True)]:
            with self.assertRaises(InputError):
                ActorIdentityIndex([actor()],[(0,row)]).resolve(100,30)

    def test_lifecycle_ambiguity_and_regression_are_visible(self):
        index=ActorIdentityIndex([actor(),actor(group='/Game/Characters/Other')],[(0,prop())])
        self.assertEqual(index.resolve(100,30)['status'],'ambiguous_actor_lifecycle')
        index=ActorIdentityIndex([actor(),actor(5,2)],[(0,prop())])
        self.assertEqual(index.resolve(100,30)['status'],'lifecycle_time_regression')


class JoinTests(unittest.TestCase):
    @staticmethod
    def death(time=100):
        return {'time1':time,'payload_issue':None,
                'round_identity':{'status':'resolved','round_number':3},
                'killer_identity':{'status':'resolved','player_state_ref':20},
                'victim_identity':{'status':'resolved','player_state_ref':21}}

    @staticmethod
    def observation(time=109):
        return {'time_ms':time,'actor_net_guid':20,'members':{'victim_ref':21,'round_number':3}}

    def test_identity_is_required_in_addition_to_window(self):
        d=self.death();k=self.observation()
        self.assertEqual(match_deaths([d],[k])[0],[(0,0)])
        k['members']['victim_ref']=22
        self.assertEqual(match_deaths([d],[k])[0],[])

    def test_both_sides_must_be_unique_without_greedy_consumption(self):
        self.assertEqual(match_deaths([self.death()],[self.observation(),self.observation()])[0],[])
        self.assertEqual(match_deaths([self.death(),self.death(105)],[self.observation()])[0],[])
        # Greedy order could force k1 after consuming e0 for k0; this refuses it.
        self.assertEqual(match_deaths([self.death(100),self.death(130)],
                                     [self.observation(109),self.observation(139)])[0],[])

    def test_time_direction_boundary_and_unvalidated_events(self):
        for time in (99,151):
            self.assertEqual(match_deaths([self.death()],[self.observation(time)])[0],[])
        for time in (100,150):
            self.assertEqual(match_deaths([self.death()],[self.observation(time)])[0],[(0,0)])
        d=self.death();d['payload_issue']='payload_tag'
        self.assertEqual(match_deaths([d],[self.observation()])[0],[])

    def test_cross_round_and_unresolved_round_are_not_joined(self):
        d=self.death(); d['round_identity']['round_number']=2
        self.assertEqual(match_deaths([d],[self.observation()])[0],[])
        d['round_identity'].update(status='ambiguous_round',round_number=3)
        self.assertEqual(match_deaths([d],[self.observation()])[0],[])

    def test_round_boundaries_and_invalid_latest_do_not_fall_back(self):
        first={'time1':10,'word0':3,'payload_issue':None,'event_row_ordinal':8}
        self.assertEqual(tool.resolve_round([first],11)['round_number'],3)
        self.assertEqual(tool.resolve_round([first],10)['status'],'event_time_round_boundary')
        self.assertEqual(tool.resolve_round([first],9)['status'],'no_prior_round')
        self.assertEqual(tool.resolve_round([first,first],11)['status'],'ambiguous_round')
        later=dict(first,time1=20,payload_issue='payload_tag')
        self.assertEqual(tool.resolve_round([first,later],21)['status'],'unvalidated_round')


class PayloadTests(unittest.TestCase):
    @staticmethod
    def event(wide=False):
        text=DEATH_NAME.encode('utf-16-le' if wide else 'utf-8')+(b'\0\0' if wide else b'\0')
        length=-(len(DEATH_NAME)+1) if wide else len(text)
        raw=struct.pack('<IIIi',8,100,200,length)+text+struct.pack('<f',0.5)
        return dict(raw_payload=raw,payload_size=len(raw),word0=100,word1=200,
                    payload_tag=8,payload_name=DEATH_NAME,payload_seconds=0.5,time1=500)

    def test_complete_narrow_and_wide_payload(self):
        self.assertIsNone(validate_death_payload(self.event()))
        self.assertIsNone(validate_death_payload(self.event(True)))

    def test_malformed_or_inconsistent_payload_is_visible(self):
        for key,value in [('word0',101),('word1',None),('payload_tag',9),('payload_seconds',0.6),
                          ('payload_seconds',0.5+1e-10),('payload_seconds',True),
                          ('payload_name','wrong'),('time1',510),('payload_size',1)]:
            row=copy.deepcopy(self.event());row[key]=value
            self.assertIsNotNone(validate_death_payload(row),key)
        for raw in [self.event()['raw_payload'][:-1],self.event()['raw_payload']+b'\0']:
            row=self.event();row.update(raw_payload=raw,payload_size=len(raw))
            self.assertIsNotNone(validate_death_payload(row))


def make_export(root, build='++Ares-Core+release-13.05'):
    """Small real Parquet export with two pawn-to-PlayerState bridges and a kill."""
    root.mkdir()
    producer=tool.observation_extractor
    declarations={0:producer.PARENT,**producer.DECL}
    manifest={'replay_build':build,
              'net_field_export_groups':[{'path':producer.GROUP,'fields':[
                  {'handle':h,'name':v[0],'compatible_checksum':v[1]} for h,v in declarations.items()]}]}
    (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    leaves=[(3,8,b'\x2a','value_i64',21),(4,8,b'\0','value_i64',0),
            (5,33,b'\1\0\0\0\0','value_str',''),(9,8,b'\0','value_i64',0),
            (10,32,struct.pack('<f',10.0),'value_f64',10.0),
            (11,8,b'\2','value_i64',2),(12,32,struct.pack('<f',0.5),'value_f64',0.5),
            (13,32,struct.pack('<f',0.1),'value_f64',float(struct.unpack('<f',struct.pack('<f',0.1))[0])),
            (14,32,struct.pack('<i',3),'value_i64',3),(15,1,b'\0','value_bool',False)]
    rows=[field_row(**prop()),field_row(**prop(value=21,actor_net_guid=200))]
    for handle,width,raw,column,value in leaves:
        item=field_row(time_ms=509,actor_net_guid=20,handle=handle,
                       field_name='KillData[0].'+producer.DECL[handle][0],
                       raw_bits=raw,bit_count=width,value_bool=None)
        item[column]=value
        rows.append(item)
    raw,width=array([(0,[(h,w,r) for h,w,r,_,_ in leaves])])
    rows.append(field_row(time_ms=509,actor_net_guid=20,handle=0,field_name='KillData',
                          compatible_checksum=producer.PARENT[1],raw_bits=raw,bit_count=width,value_bool=None))
    pq.write_table(pa.Table.from_pylist(rows,schema=SCHEMA),root/'fields.parquet')
    write_empty_tables(root)
    pq.write_table(pa.Table.from_pylist([actor(),actor(guid=200),actor(guid=20),actor(guid=21)]),root/'actors.parquet')
    name='EReplayEventGroup::RoundStart'; text=name.encode()+b'\0'
    raw=struct.pack('<IIi',2,3,len(text))+text+struct.pack('<f',0.125)
    start=dict(PayloadTests.event(),raw_payload=raw,payload_size=len(raw),word0=3,word1=None,
               payload_tag=2,payload_name=name,payload_seconds=0.125,time1=125,
               group='roundStarted',id='round-3',metadata='3',time2=125)
    death=dict(PayloadTests.event(),group='characterDeath',id='death-1',metadata='',time2=500)
    pq.write_table(pa.Table.from_pylist([start,death]),root/'events.parquet')
    return root


class IntegrationTests(TempDirTestCase):
    def test_real_cli_retains_physical_sources(self):
        root=self.tmp(); export=make_export(root/'export'); out=root/'ledger.json'
        command=[sys.executable,'-W','error',str(Path(tool.__file__)),
                 '--export',str(export),'--out',str(out)]
        run=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='strict',env=dict(os.environ,PYTHONIOENCODING='utf-8'))
        self.assertEqual(run.returncode,0,run.stderr)
        result=json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(result['counts']['matched_pairs'],1)
        self.assertEqual(result['death_events'][0]['event_row_ordinal'],1)
        self.assertEqual(result['death_events'][0]['killer_identity']['mapping_field_rows'],[0])
        self.assertEqual(result['death_events'][0]['victim_identity']['mapping_field_rows'],[1])
        self.assertEqual(result['death_events'][0]['round_identity']['round_event_row_ordinal'],0)
        self.assertEqual(result['state_projection']['entities'][0]['base']['source']['physical_parent_row_ordinal'],12)

    def test_measured_13_06_export_is_joined(self):
        t = self.tmp()
        export=make_export(t/'export',build='++Ares-Core+release-13.06')
        got=tool.extract(export)
        self.assertEqual(got['provenance']['replay_build'],'++Ares-Core+release-13.06')
        self.assertEqual(got['counts']['matched_pairs'],1)
        self.assertEqual(got['counts']['state']['entities'],1)

    def test_unmeasured_build_fails_through_the_cli_without_writing(self):
        root=self.tmp(); export=make_export(root/'export',build='++Ares-Core+release-13.07')
        out=root/'ledger.json'
        run=subprocess.run([sys.executable,'-W','error',str(Path(tool.__file__)),
                            '--export',str(export),'--out',str(out)],
                           capture_output=True,text=True,encoding='utf-8',errors='strict',env=dict(os.environ,PYTHONIOENCODING='utf-8'))
        self.assertEqual(run.returncode,1,run.stderr)
        self.assertIn('outside the measured KillData set',run.stderr)
        self.assertFalse(out.exists())

    def test_missing_raw_event_is_retained_unjoined(self):
        t = self.tmp()
        export=make_export(t/'export'); path=export/'events.parquet'
        table=pq.read_table(path); rows=table.to_pylist(); rows[1]['raw_payload']=None
        pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
        got=tool.extract(export)
        self.assertEqual(got['counts']['matched_pairs'],0)
        self.assertEqual(got['death_events'][0]['payload_issue'],'payload_size')
        self.assertIsNone(got['death_events'][0]['raw_payload_hex'])
        self.assertEqual(len(got['unmatched_main_observations']),1)

    def test_nullable_event_and_round_times_are_retained_unresolved(self):
        for event_index,issue,round_status in ((1,'payload_time','invalid_event_time'),
                                                (0,None,'unorderable_round')):
            with self.subTest(event_index=event_index), tempfile.TemporaryDirectory() as t:
                export=make_export(Path(t)/'export'); path=export/'events.parquet'
                table=pq.read_table(path); rows=table.to_pylist(); rows[event_index]['time1']=None
                pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
                got=tool.extract(export)
                self.assertEqual(got['counts']['matched_pairs'],0)
                self.assertEqual(got['death_events'][0]['payload_issue'],issue)
                self.assertEqual(got['death_events'][0]['round_identity']['status'],round_status)
                self.assertEqual(got['death_events'][0]['raw_payload_hex'],rows[1]['raw_payload'].hex())

    def test_different_killer_context_remains_unjoined(self):
        t = self.tmp()
        export=make_export(t/'export'); path=export/'fields.parquet'
        table=pq.read_table(path); rows=table.to_pylist()
        rows[0].update(value_i64=21,raw_bits=b'\x2a')
        pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
        got=tool.extract(export)
        self.assertEqual(got['counts']['matched_pairs'],0)
        self.assertEqual(got['counts']['unmatched_with_different_killer_context'],1)
        context=got['unmatched_main_observations'][0]['different_killer_same_victim_context']
        self.assertEqual(context,[{'event_row_ordinal':1,'event_killer_player_state_ref':21,'replication_lag_ms':9}])

    def test_cli_refuses_source_aliases_without_writing(self):
        root=self.tmp(); export=make_export(root/'export')
        for output in (export/'events.parquet',Path(tool.__file__),Path(tool.__file__).with_name('kill_state.py')):
            before=output.read_bytes()
            run=subprocess.run([sys.executable,'-W','error',str(Path(tool.__file__)),
                                '--export',str(export),'--out',str(output)],
                               capture_output=True,text=True,encoding='utf-8',errors='strict',env=dict(os.environ,PYTHONIOENCODING='utf-8'))
            self.assertEqual(run.returncode,1,run.stderr)
            self.assertIn('FAILED:',run.stderr)
            self.assertEqual(output.read_bytes(),before)
