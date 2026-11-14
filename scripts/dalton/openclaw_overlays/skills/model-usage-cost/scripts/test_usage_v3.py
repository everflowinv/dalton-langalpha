import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from compression import zstd
import usage_sources as s
import usage_v3 as v3
import model_usage_cost_cli as cli
import test_usage_accounting as fixtures
from test_usage_accounting import NOW, ev, token, counter, write, pb, iso


class NewSources(unittest.TestCase):
    setUp=fixtures.Accounting.setUp
    tearDown=fixtures.Accounting.tearDown
    e=fixtures.Accounting.e
    attach=fixtures.Accounting.attach
    rollout=fixtures.Accounting.rollout
    # Inherit the fixture helpers, not a second execution of the original tests.
    def test_compaction_response_is_counted_and_copied_history_deduplicates(self):
        comp=ev('compacted',dict(compaction_response_id='resp',latest_token_usage_record=dict(response_id='resp',thread_id='t',turn_id='u',usage=counter(100,20,60))))
        events=[ev('session_meta',{'id':'t'}),ev('turn_context',{'model':'m','turn_id':'u'}),comp]
        self.rollout('a.jsonl',events);self.rollout('b.jsonl',events)
        e=self.e();v3.scan_compactions(e)
        self.assertEqual(len(e.records),1);self.assertEqual(s.summed(list(e.records.values())),(40,0,60,20))
    def test_compaction_already_present_as_token_event_not_counted_twice(self):
        e=self.e();e.put('codex','c',NOW-20,'openai','m','s',(40,0,60,20),turn='u',thread='t')
        e.put('codex-compaction','resp',NOW-20,'openai','m','s',(40,0,60,20),turn='u',thread='t')
        rs,_=s.reconcile(e);self.assertEqual(len(rs),1)
    def test_compaction_usage_resolves_trajectory_difference(self):
        e=self.e();e.put('codex','c',NOW-30,'openai','m','s',(100,0,900,20),turn='u',thread='t')
        e.put('codex-compaction','resp',NOW-20,'openai','m','s',(40,0,60,20),turn='u',thread='t')
        e.put('trajectory','t',NOW-10,'openai','m','s',(140,0,960,40),turn='u',thread='t')
        rs,held=s.reconcile(e);self.assertEqual(len(rs),2);self.assertFalse(held)
    def test_agy_metadata_names_claude_without_google_guess(self):
        c=sqlite3.connect(':memory:');c.execute('CREATE TABLE gen_metadata(data BLOB)')
        meta=pb([(19,b'claude-opus-5-5-medium'),(20,pb([(1,b'model_enum'),(2,b'MODEL_PLACEHOLDER_M401')]))])
        c.execute('INSERT INTO gen_metadata VALUES(?)',(pb([(1,meta)]),))
        self.assertEqual(v3.agy_models(c)[1401],{'claude-opus-5-5'});c.close()
    def test_agy_b_variant_label_shares_enum_and_base_model(self):
        self.assertEqual(v3.VARIANT_SUFFIXES,frozenset({'-b','-d'}))
        c=sqlite3.connect(':memory:');c.execute('CREATE TABLE gen_metadata(data BLOB)')
        for name in (b'gemini-4-argon',b'gemini-4-argon-b',b'gemini-4-argon-d'):
            meta=pb([(19,name),(20,pb([(1,b'model_enum'),(2,b'MODEL_PLACEHOLDER_M417')]))])
            c.execute('INSERT INTO gen_metadata VALUES(?)',(pb([(1,meta)]),))
        e=self.e();self.assertEqual(v3.agy_models(c,e)[1417],{'gemini-4-argon'});c.close()
        self.assertEqual(e.stats['agy_variant_label_merged'],2)
    def test_enum_from_retained_ledger_resolves_using_named_evidence(self):
        e=self.e()
        for ident,model in [('a','agy-enum-1318'),('b','gemini-3.8-flash')]:
            e.put('agy',ident,NOW-10,'antigravity-cli-gateway',model,'s',(1,0,10,2),conversation=ident,enum=1318)
        rs,_=s.reconcile(e);self.assertEqual({r['model'] for r in rs},{'gemini-3.8-flash'})
    def test_conflicting_enum_names_do_not_relabel_known_record(self):
        e=self.e()
        for ident,model in [('a','agy-enum-1'),('b','gemini-a'),('c','gemini-b')]:
            e.put('agy',ident,NOW-10,'antigravity-cli-gateway',model,'s',(1,0,10,2),conversation=ident,enum=1)
        rs,_=s.reconcile(e);self.assertEqual({r['model'] for r in rs},{'agy-enum-1','gemini-a','gemini-b'})
    def test_missing_readonly_database_never_creates_empty_file(self):
        p=self.p/'missing.db'
        with self.assertRaises(FileNotFoundError):s.dbopen(p)
        self.assertFalse(p.exists())
    def test_compressed_transcripts_are_projected(self):
        c=sqlite3.connect(':memory:');c.execute('CREATE TABLE transcript_events(event_zstd BLOB)')
        c.execute('INSERT INTO transcript_events VALUES(?)',(zstd.compress(json.dumps({'message':{'provider':'p','usage':{'input':3}}}).encode()),))
        got=[];v3.project_compressed_transcripts(c,got.append);self.assertEqual(got,[{'provider':'p','usage':{'input':3}}]);c.close()
    def test_muse_journal_dedup_uses_run_and_source_id(self):
        row=dict(recorded_at=int((NOW-20)*1e6),id='copy',payload=dict(run_id='run',source_run_record_id='original',event=dict(kind='model_completed',model='muse-spark-1.3',usage=dict(input_tokens=100,cached_tokens=80,output_tokens=10,reasoning_tokens=8))))
        self.attach('muse-sessions',write(self.p/'session.jsonl',[row,row]))
        e=self.e();v3.scan_muse(e);self.assertEqual(len(e.records),1);self.assertEqual(s.summed(list(e.records.values())),(20,0,80,10))
    def test_muse_subresponses_replace_aggregate_keep_background_model(self):
        e=self.e()
        e.put('muse','a',NOW-20,'muse-cli-gateway','main','openclaw',(100,0,0,10),request='req')
        e.put('muse','b',NOW-20,'muse-cli-gateway','contributor','openclaw',(20,0,0,2),request='req')
        e.put('gateway','req',NOW-10,'muse-cli-gateway','main','openclaw',(120,0,0,12),request='req',start=NOW-30)
        rs,held=s.reconcile(e);self.assertEqual(len(rs),2);self.assertFalse(held);self.assertEqual({r['model'] for r in rs},{'main','contributor'})
    def test_half_open_windows_do_not_repeat_boundary_event(self):
        e=s.Evidence(NOW-200000,NOW+1);e.put('x','a',NOW-86400,'p','m','s',(10,0,0,2))
        rs=list(e.records.values());self.assertEqual(cli.window_result(rs,NOW,1,{})['totals']['tokens'],12)
        self.assertEqual(cli.window_result(rs,NOW-86400,1,{})['totals']['tokens'],0)
    def test_tiers_use_response_context_not_aggregate_context(self):
        rates={('p','m'):dict(input=1,cacheWrite=0,cacheRead=.1,output=2,tiers=[dict(input=3,cacheWrite=0,cacheRead=.3,output=4,**{'from':100})])}
        self.assertAlmostEqual(cli.cost_of(rates,'p','m',(100,0,0,2),{'unit':'model-response'}),.000308)
        self.assertAlmostEqual(cli.cost_of(rates,'p','m',(120,0,0,4),{'unit':'gateway-request','billing_parts':[[60,0,0,2],[60,0,0,2]]}),.000128)
    def test_manual_and_gateway_price_isolation_for_anthropic_via_agy(self):
        prices={('claude-cli-gateway','claude-opus-5-5'):dict(input=1,cacheWrite=0,cacheRead=0,output=1)}
        self.assertIsNone(cli.cost_of(prices,'antigravity-cli-gateway','claude-opus-5-5',(100,0,0,1)))
    def test_old_ledger_is_upgraded_without_accumulating_new_observations(self):
        p=self.p/'ledger.db';e=self.e();e.put('agy','r',NOW-10,'antigravity-cli-gateway','agy-enum-1318','s',(10,0,0,2),conversation='c',enum=1318);s.persist(e,p)
        e=self.e();e.put('agy','r',NOW-10,'antigravity-cli-gateway','gemini-3.8-flash','s',(10,0,0,2),conversation='c',enum=1318);s.persist(e,p);s.persist(e,p)
        self.assertEqual(len(e.records),1);self.assertEqual(next(iter(e.records.values()))['model'],'gemini-3.8-flash')

if __name__=='__main__':unittest.main()
