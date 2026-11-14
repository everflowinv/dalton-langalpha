"""Accounting contracts with synthetic metadata only; never call a model."""
import copy
import gzip
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import usage_sources as s
import model_usage_cost_cli as cli

NOW=1789048800.0

def iso(t):return s.dt.datetime.fromtimestamp(t,s.UTC).isoformat()
def write(path,objs,prefix=''):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(''.join(prefix+json.dumps(o)+'\n' for o in objs));return str(path)
def ev(typ,payload,t=NOW-30):return {'type':typ,'timestamp':iso(t),'payload':payload}
def counter(inp,out,cr=0):return dict(input_tokens=inp,cached_input_tokens=cr,output_tokens=out,total_tokens=inp+out)
def token(total,last,t=NOW-30):return ev('event_msg',dict(type='token_count',info={'total_token_usage':total,'last_token_usage':last}),t)
def vwire(n):
    b=bytearray()
    while n>=128:b.append((n&127)|128);n>>=7
    b.append(n);return bytes(b)
def pb(fields):
    return b''.join(vwire(k*8+(2 if isinstance(v,bytes) else 0))+(vwire(len(v))+v if isinstance(v,bytes) else vwire(v)) for k,v in fields)


class Accounting(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.p=Path(self.temp.name)
        self.roots={k:[] for k in s.ROOTS};self.patch=patch.object(s,'ROOTS',self.roots);self.patch.start()
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def e(self):return s.Evidence(NOW-86400,NOW)
    def attach(self,kind,path):self.roots[kind].append(str(path))
    def rollout(self,name,events):
        path=self.p/name;write(path,events);self.attach('rollout-openclaw',path)
    def trajectory(self,provider='openai',usage=None,**data):
        return dict(type='model.completed',traceId='trace',runId='run',seq=3,ts=iso(NOW-10),provider=provider,modelId='m',data=dict(usage=usage or {'input':100,'output':20},**data))
    def test_codex_status_repeat_but_real_equal_calls_both_count(self):
        a=counter(100,20,60);b=counter(200,40,120)
        self.rollout('r.jsonl',[ev('session_meta',{'id':'thread'}),ev('turn_context',{'turn_id':'turn','model':'m'}),token(a,a,NOW-50),token(a,a,NOW-40),token(b,a,NOW-30)])
        e=self.e();s.scan_rollouts(e)
        self.assertEqual(s.summed(list(e.records.values())),(80,0,120,40));self.assertEqual(len(e.records),2)
        self.assertEqual(e.stats['rollout-openclaw_unchanged_counter_tokens'],120)
    def test_repeat_after_window_cut_not_recounted(self):
        u=counter(100,20)
        self.rollout('r.jsonl',[ev('session_meta',{'id':'t'}),token(u,u,NOW-90000),token(u,u,NOW-30)])
        e=self.e();s.scan_rollouts(e);self.assertEqual(len(e.records),0)
    def test_fork_copied_history_deduplicates_by_turn_and_original_timestamp(self):
        u=counter(100,20)
        history=[ev('turn_context',{'turn_id':'original-turn','model':'m'}),token(u,u)]
        self.rollout('parent.jsonl',[ev('session_meta',{'id':'parent'})]+history)
        self.rollout('child.jsonl',[ev('session_meta',{'id':'child','forked_from_id':'parent'})]+history+[ev('turn_context',{'turn_id':'new-turn','model':'m'}),token(counter(200,40),u,NOW-10)])
        e=self.e();s.scan_rollouts(e);self.assertEqual(len(e.records),2)
    def test_counter_reset_and_model_switch_preserve_new_usage(self):
        self.rollout('r.jsonl',[ev('session_meta',{'id':'t'}),ev('turn_context',{'model':'old','turn_id':'a'}),token(counter(100,20),counter(100,20)),ev('turn_context',{'model':'new','turn_id':'b'}),token(counter(10,2),counter(10,2),NOW-5)])
        e=self.e();s.scan_rollouts(e);self.assertEqual({r['model'] for r in e.records.values()},{'old','new'});self.assertEqual(sum(sum(r['v']) for r in e.records.values()),132)
    def test_claude_chunks_children_and_cross_file_replay(self):
        def msg(mid,t=NOW-20):return {'type':'assistant','timestamp':iso(t),'sessionId':'session','message':{'id':mid,'model':'m','usage':{'input_tokens':10,'cache_creation_input_tokens':20,'cache_read_input_tokens':100,'output_tokens':5}}}
        write(self.p/'project'/'main.jsonl',[msg('main'),msg('main')]);write(self.p/'project'/'session'/'subagents'/'child.jsonl',[msg('child'),msg('child'),msg('main')]);self.attach('claude',self.p/'project'/'**'/'*.jsonl')
        e=self.e();s.scan_claude(e);self.assertEqual(len(e.records),2);self.assertEqual(s.summed(list(e.records.values())),(20,40,200,10))
    def test_native_openai_restored_and_runtime_aggregate_skipped(self):
        e=self.e();e.put('codex','x',NOW-20,'openai','m','openclaw',(100,0,0,20),thread='t',turn='u')
        e.put('trajectory','native',NOW-10,'openai','m','openclaw',(50,0,0,5),thread=None)
        e.put('trajectory','runtime',NOW-10,'openai','m','openclaw',(100,0,0,20),thread='t',turn='u')
        rs,held=s.reconcile(e);self.assertEqual(s.summed(rs),(150,0,0,25));self.assertEqual(held,[])
    def test_runtime_last_response_projection_is_covered(self):
        e=self.e()
        for i in range(2):e.put('codex',str(i),NOW-20+i,'openai','m','openclaw',(100+i,0,0,20),thread='t',turn='u')
        e.put('trajectory','r',NOW-10,'openai','m','openclaw',(101,0,0,20),thread='t',turn='u')
        rs,held=s.reconcile(e);self.assertEqual(len(rs),2);self.assertFalse(held)
    def test_wal_and_import_archive_share_identity(self):
        p=self.p/'agent.sqlite';c=sqlite3.connect(p);c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE trajectory_runtime_events(event_json TEXT)');c.commit()
        o=self.trajectory(provider='xai');c.execute('INSERT INTO trajectory_runtime_events VALUES (?)',(json.dumps(o),));c.commit()
        self.attach('trajectory-db',p);self.attach('trajectory-file',write(self.p/'archive.jsonl',[o]))
        e=self.e();s.scan_trajectories(e);c.close();self.assertEqual(len(e.records),1)
    def test_corrupt_database_is_error_not_successful_zero(self):
        p=self.p/'broken.sqlite';p.write_text('broken');self.attach('trajectory-db',p)
        with self.assertRaises(sqlite3.DatabaseError):s.scan_trajectories(self.e())
    def test_negative_timezone_offset_preserved(self):
        self.assertEqual(s.timestamp('2026-09-10T10:00:00-04:00'),s.timestamp('2026-09-10T14:00:00Z'))
    def test_future_events_excluded(self):
        e=self.e();e.put('x','future',NOW+1,'p','m','s',(100,0,0,2));self.assertFalse(e.records)
    def test_invalid_tokens_are_not_silently_zero(self):
        for x in [True,False,-1,float('nan'),float('inf'),'100',1.2]:
            with self.subTest(x=x),self.assertRaises(ValueError):s.vector({'input':x},('input','output'))
    def test_legacy_gateway_split_preserves_prompt_total(self):
        rows=[dict(event='start',request='r',model='m',ts=iso(NOW-20)),dict(event='done',request='r',ts=iso(NOW-10),usage={'prompt_tokens':101,'completion_tokens':2})]
        self.attach('claude-gateway',write(self.p/'gateway.log',rows,'[CLIGW] '))
        e=self.e();s.scan_gateway(e,'claude-gateway',cli.DEFAULT_SPLIT);self.assertEqual(sum(next(iter(e.records.values()))['v']),103)
    def test_rotated_log_duplicate_done_counts_once(self):
        rows=[dict(event='start',request='r',model='m',ts=iso(NOW-20)),dict(event='done',request='r',ts=iso(NOW-10),usage={'prompt_tokens':100,'completion_tokens':2})]
        p=write(self.p/'g.log',rows,'[CLIGW] ');self.attach('claude-gateway',p)
        with gzip.open(self.p/'g.log.1.gz','wt') as f:f.write(Path(p).read_text())
        self.attach('claude-gateway',self.p/'g.log.1.gz');e=self.e();s.scan_gateway(e,'claude-gateway',cli.DEFAULT_SPLIT);self.assertEqual(len(e.records),1)
    def test_agy_database_output_already_includes_thinking(self):
        p=self.p/'c.db';c=sqlite3.connect(p);c.execute('CREATE TABLE steps(idx INTEGER,metadata BLOB)')
        usage=pb([(1,1318),(2,100),(3,70),(5,900),(9,50),(10,20),(11,b'response')]);metadata=pb([(8,pb([(1,int(NOW-10))])),(9,usage)])
        c.execute('INSERT INTO steps VALUES(1,?)',(metadata,));c.commit();c.close();self.attach('agy-db',p)
        e=self.e();s.scan_agy(e);self.assertEqual(s.summed(list(e.records.values())),(100,0,900,70));self.assertFalse(e.issues)
    def test_agy_database_without_steps_table_is_reported_not_fatal(self):
        p=self.p/'empty.db';c=sqlite3.connect(p);c.execute('PRAGMA journal_mode=WAL');c.close();self.attach('agy-db',p)
        e=self.e();s.scan_agy(e);self.assertFalse(e.records);self.assertEqual(e.issues['agy_db_no_steps_table'],1)
    def test_agy_legacy_gateway_and_db_replace_not_add(self):
        e=self.e();e.put('agy','response',NOW-10,'antigravity-cli-gateway','agy-enum-1','agy-cli',(100,0,900,70),conversation='c',enum=1,thinking=50)
        e.put('gateway','request',NOW-9,'antigravity-cli-gateway','gemini-test','openclaw',(1000,0,0,120),start=NOW-20,legacy_out=True)
        rs,held=s.reconcile(e);self.assertEqual(s.summed(rs),(100,0,900,70));self.assertFalse(held);self.assertEqual(rs[0]['model'],'gemini-test')
    def test_long_gateway_request_message_start_timestamp_matches(self):
        e=self.e();e.put('gateway','g',NOW-10,'claude-cli-gateway','m','openclaw',(100,30,900,20),request='12345678',start=NOW-1000)
        e.put('gateway-message','msg',NOW-1000,'claude-cli-gateway','m','openclaw',(100,0,900,20),response='chatcmpl-claude-cli-12345678-rest')
        rs,held=s.reconcile(e);self.assertEqual(len(rs),1);self.assertEqual(rs[0]['v'],(100,30,900,20))
    def test_failed_claude_request_keeps_completed_message_usage(self):
        events=[dict(event='start',request='r',model='m',ts=iso(NOW-20)),dict(event='usage',request='r',messageId='msg',model='m',ts=iso(NOW-10),usage={'prompt_tokens':100,'prompt_tokens_details':{'cached_tokens':90},'completion_tokens':20}),dict(event='error',request='r',ts=iso(NOW-5))]
        self.attach('claude-gateway',write(self.p/'g.log',events,'[CLIGW] '));e=self.e();s.scan_gateway(e,'claude-gateway',cli.DEFAULT_SPLIT);rs,held=s.reconcile(e);self.assertEqual(s.summed(rs),(10,0,90,20));self.assertTrue(e.issues)
    def test_successful_claude_aggregate_replaces_partial_messages(self):
        e=self.e();e.put('claude','msg',NOW-20,'anthropic','m','openclaw',(100,0,0,20),request='r')
        e.put('gateway','r',NOW-10,'claude-cli-gateway','m','openclaw',(200,0,0,40),request='r',start=NOW-30)
        rs,_=s.reconcile(e);self.assertEqual(s.summed(rs),(200,0,0,40))
    def test_ledger_rescan_is_idempotent_and_survives_source_removal(self):
        path=self.p/'ledger.sqlite';e=self.e();e.put('claude','msg',NOW-10,'anthropic','m','manual',(100,0,0,20));s.persist(e,path);s.persist(e,path)
        reread=self.e();s.persist(reread,path);self.assertEqual(len(reread.records),1);self.assertEqual(s.summed(list(reread.records.values())),(100,0,0,20))
    def test_prices_stay_provider_qualified(self):
        rates={('a','same'):{'input':1,'cacheWrite':0,'cacheRead':0,'output':1},('b','same'):{'input':5,'cacheWrite':0,'cacheRead':0,'output':5}}
        self.assertEqual(cli.cost_of(rates,'a','same',(1000000,0,0,0)),1);self.assertEqual(cli.cost_of(rates,'b','same',(1000000,0,0,0)),5);self.assertIsNone(cli.cost_of(rates,'c','same',(1,0,0,0)))
    def test_top_and_by_model_preserve_complete_totals_and_unpriced(self):
        e=self.e();e.put('x','a',NOW-1,'p','a','s',(100,0,0,2));e.put('x','b',NOW-2,'p','b','s',(200,0,0,4))
        rates={('p','a'):{'input':1,'cacheWrite':0,'cacheRead':0,'output':1}}
        a=cli.window_result(list(e.records.values()),NOW,1,rates,0);b=cli.window_result(list(e.records.values()),NOW,1,rates,1,True)
        self.assertEqual(a['totals'],b['totals']);self.assertEqual(b['totals']['tokens'],306);self.assertEqual(b['totals']['unpriced_tokens'],204);self.assertEqual(len(b['rows']),1)
    def test_cache_filters_windows_and_invalidates_on_source_change(self):
        p=self.p/'source.jsonl';write(p,[{'v':100}]);self.attach('claude',p)
        cache=self.p/'cache.sqlite';calls=[]
        def scan(e):
            calls.append(1);o=json.loads(p.read_text());e.put('claude','msg',NOW-10,'anthropic','m','manual',(o['v'],0,0,20))
        e=self.e();s.cached_scan(e,scan,['claude'],cache)
        e=self.e();s.cached_scan(e,scan,['claude'],cache);self.assertEqual(len(calls),1)
        write(p,[{'v':200}]);e=self.e();s.cached_scan(e,scan,['claude'],cache)
        self.assertEqual(len(calls),2);self.assertEqual(s.summed(list(e.records.values())),(200,0,0,20))
    def test_missing_gateway_log_uses_nonoverlapping_trajectory(self):
        e=self.e();e.put('trajectory','run',NOW-10,'claude-cli-gateway','m','openclaw',(100,0,0,20),start=NOW-20,responses=[])
        records,held=s.reconcile(e);self.assertEqual(s.summed(records),(100,0,0,20));self.assertFalse(held)
    def test_ambiguous_gateway_overlap_is_never_blindly_added(self):
        e=self.e();e.put('trajectory','run',NOW-10,'claude-cli-gateway','m','openclaw',(100,0,0,20),start=NOW-20,responses=[])
        e.put('gateway','g',NOW-15,'claude-cli-gateway','m','openclaw',(50,0,0,5),start=NOW-20,request='r')
        records,held=s.reconcile(e);self.assertEqual(s.summed(records),(50,0,0,5));self.assertEqual(len(held),1)
    def test_scan_cache_wal_append_invalidates_even_if_database_unchanged(self):
        p=self.p/'source.sqlite';c=sqlite3.connect(p);c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE t(v INTEGER)');c.commit();self.attach('trajectory-db',p)
        cache=self.p/'cache.sqlite';calls=[]
        def scan(e):
            calls.append(1);db=s.dbopen(p);v=db.execute('SELECT sum(v) FROM t').fetchone()[0] or 0;db.close();e.put('dalton','fixture',NOW-10,'p','m','s',(v,0,0,0))
        s.cached_scan(self.e(),scan,['trajectory-db'],cache);before=p.stat().st_mtime_ns
        c.execute('INSERT INTO t VALUES(100)');c.commit();self.assertEqual(p.stat().st_mtime_ns,before)
        e=self.e();s.cached_scan(e,scan,['trajectory-db'],cache);c.close();self.assertEqual(len(calls),2);self.assertEqual(s.summed(list(e.records.values())),(100,0,0,0))
    def test_persisted_final_message_survives_rescan_of_older_partial(self):
        path=self.p/'ledger.sqlite';e=self.e();e.put('claude','msg',NOW-10,'anthropic','m','manual',(100,0,0,20));s.persist(e,path)
        e=self.e();e.put('claude','msg',NOW-10,'anthropic','m','manual',(100,0,0,1));s.persist(e,path)
        self.assertEqual(s.summed(list(e.records.values())),(100,0,0,20))
    def test_protobuf_truncation_is_detected(self):
        with self.assertRaises(ValueError):s.wire(b'\x0a\x05hi')

if __name__=='__main__':unittest.main()
