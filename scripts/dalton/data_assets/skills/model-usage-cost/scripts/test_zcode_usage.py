"""Synthetic ZCode usage fixtures. No model calls, prompts, or credentials."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import usage_sources as s
import model_usage_cost_cli as cli
from zcode_usage import scan_zcode, DB_COLUMNS

NOW = 1789458000.0
PROVIDER = 'builtin:bigmodel-coding-plan'


def iso(t):
    return s.dt.datetime.fromtimestamp(t, s.UTC).isoformat()


class ZCodeUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.roots = {k: [] for k in s.ROOTS}
        self.patcher = patch.object(s, 'ROOTS', self.roots)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def dbrow(self, ident='logical-1', source='main_turn', **extra):
        row = dict.fromkeys(DB_COLUMNS)
        row.update(id=ident, logical_request_id=ident, attempt_index=0,
                   session_id='session', turn_id='turn', trace_id='trace',
                   provider_id=PROVIDER, model_id='GLM-5.3', query_source=source,
                   status='completed', started_at=(NOW-20)*1000, completed_at=(NOW-10)*1000,
                   input_tokens=1000, output_tokens=100, reasoning_tokens=60,
                   cache_creation_input_tokens=20, cache_read_input_tokens=900,
                   computed_total_tokens=1100, raw_usage_json='{"inputTokens":1000,"outputTokens":100}')
        row.update(extra)
        return row

    def logrow(self, request='transport-1', source='main_turn', **extra):
        o = dict(type='model_io', requestId=request, attempt=1, sessionId='session',
                 turnId='turn', traceId='trace', querySource=source,
                 startedAt=iso(NOW-19.9), completedAt=iso(NOW-10.01),
                 model=dict(providerId=PROVIDER, modelId='GLM-5.3'),
                 response=dict(usage=dict(inputTokens=1000, outputTokens=100,
                               cacheWriteTokens=20, cacheReadTokens=900,
                               reasoningTokens=60, totalTokens=1100)),
                 request={'messages': [{'content': 'PRIVATE_SENTINEL'}]})
        o.update(extra)
        return o

    def database(self, rows, name='db.sqlite', wal=False):
        p = self.root/name
        c = sqlite3.connect(p)
        if wal:
            c.execute('PRAGMA journal_mode=WAL')
        c.execute('CREATE TABLE model_usage (' + ','.join(DB_COLUMNS) + ')')
        c.execute('CREATE TABLE turn_usage(input_tokens INTEGER)')
        c.execute('INSERT INTO turn_usage VALUES (9999999)')
        for row in rows:
            c.execute('INSERT INTO model_usage VALUES (' + ','.join('?' for _ in DB_COLUMNS) + ')',
                      [row[k] for k in DB_COLUMNS])
        c.commit()
        self.roots['zcode-db'].append(str(p))
        if wal:
            return c
        c.close()
        return p

    def logs(self, rows):
        p = self.root/('r-%d.jsonl' % len(self.roots['zcode-rollout']))
        p.write_text(''.join(json.dumps(o)+'\n' for o in rows))
        self.roots['zcode-rollout'].append(str(p))
        return p

    def read(self, since=NOW-86400, until=NOW):
        e = s.Evidence(since, until)
        scan_zcode(e)
        records, held = s.reconcile(e)
        return e, records, held

    def test_database_all_builtin_sources_and_cache_reasoning_partition(self):
        for_source = ['main_turn','subagent','session_title','compact']
        self.database([self.dbrow(str(i), source=x) for i,x in enumerate(for_source)])
        e, records, held = self.read()
        self.assertEqual(len(records), 4)
        self.assertEqual(s.summed(records), (320,80,3600,400))
        self.assertFalse(held)
        self.assertFalse(e.issues)
        self.assertEqual({r['meta']['query_source'] for r in records}, set(for_source))

    def test_database_and_rollout_ids_differ_but_count_once(self):
        self.database([self.dbrow()])
        log = self.logrow()
        self.logs([log, log])
        self.logs([log])
        e, records, held = self.read()
        self.assertEqual(s.summed(records), (80,20,900,100))
        self.assertEqual(e.stats['zcode_db_rollout_reconciled'], 1)
        self.assertFalse(held)

    def test_web_search_only_rollout_is_added(self):
        self.database([self.dbrow()])
        self.logs([self.logrow(), self.logrow('search', source='web_search_tool')])
        e, records, held = self.read()
        self.assertEqual(len(records), 2)
        self.assertEqual(e.stats['zcode_rollout_fallback'], 1)
        self.assertFalse(held)

    def test_equal_sized_real_requests_and_retry_attempts_are_distinct(self):
        a=self.logrow('a')
        b=self.logrow('a', attempt=2, startedAt=iso(NOW-9), completedAt=iso(NOW-8))
        c=self.logrow('b', startedAt=iso(NOW-7), completedAt=iso(NOW-6))
        self.logs([a,a,b,c])
        _, records, held=self.read()
        self.assertEqual(len(records),3)
        self.assertEqual(s.summed(records),(240,60,2700,300))
        self.assertFalse(held)

    def test_failed_unknown_vs_failed_positive_usage(self):
        self.database([self.dbrow('unknown',status='error',input_tokens=0,output_tokens=0,
                                  cache_creation_input_tokens=0,cache_read_input_tokens=0,
                                  reasoning_tokens=0,computed_total_tokens=0,raw_usage_json=None),
                       self.dbrow('partial',status='cancelled')])
        e, records, held=self.read()
        self.assertEqual(len(records),1)
        self.assertEqual(e.issues['zcode_failed_or_cancelled_usage_unknown'],1)
        self.assertEqual(e.issues['zcode_failed_partial_usage_retained'],1)

    def test_custom_provider_not_double_counted(self):
        self.database([self.dbrow(provider_id='custom:anthropic')])
        self.logs([self.logrow(model={'providerId':'custom:anthropic','modelId':'GLM-5.3'})])
        e, records, held=self.read()
        self.assertFalse(records)
        self.assertEqual(e.stats['zcode_custom_provider_skipped'],2)

    def test_invalid_usage_and_missing_identity_reported(self):
        self.database([self.dbrow('bad-cache',cache_read_input_tokens=2000)])
        self.logs([self.logrow(requestId=None),self.logrow('bad-reasoning',response={'usage':{
            'inputTokens':100,'outputTokens':1,'reasoningTokens':False}})])
        e, records, held=self.read()
        self.assertFalse(records)
        self.assertEqual(e.issues['zcode_invalid_usage'],2)
        self.assertEqual(e.issues['zcode_identity_or_start_missing'],1)

    def test_ambiguous_match_held_not_added(self):
        self.database([self.dbrow('a'),self.dbrow('b')])
        self.logs([self.logrow()])
        e, records, held=self.read()
        self.assertEqual(len(records),2)
        self.assertEqual(len(held),1)
        self.assertEqual(e.issues['zcode_db_rollout_unreconciled'],1)

    def test_conflicting_usage_held_not_added(self):
        self.database([self.dbrow()])
        o=self.logrow()
        o['response']['usage']['outputTokens']=110
        o['response']['usage']['totalTokens']=1110
        self.logs([o])
        _, records, held=self.read()
        self.assertEqual(s.summed(records),(80,20,900,100))
        self.assertEqual(len(held),1)

    def test_two_log_requests_cannot_both_claim_one_db_record(self):
        self.database([self.dbrow()])
        self.logs([self.logrow('a'),self.logrow('b')])
        _, records, held=self.read()
        self.assertEqual(len(records),1)
        self.assertEqual(len(held),2)

    def test_ledger_backfill_replaces_fallback_and_is_idempotent(self):
        ledger=self.root/'ledger.sqlite'
        self.logs([self.logrow()])
        e,_,_=self.read()
        s.persist(e,ledger)
        self.database([self.dbrow()])
        e,_,_=self.read()
        s.persist(e,ledger)
        s.persist(e,ledger)
        records,held=s.reconcile(e)
        self.assertEqual(len(records),1)
        self.assertFalse(held)
        self.roots['zcode-db']=[]
        e,_,_=self.read()
        s.persist(e,ledger)
        records,held=s.reconcile(e)
        self.assertEqual(len(records),1)
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(list(e.records.values())))

    def test_near_boundary_log_uses_database_completion_time(self):
        self.database([self.dbrow(completed_at=(NOW+0.2)*1000)])
        self.logs([self.logrow(completedAt=iso(NOW-0.1))])
        _,records,_=self.read()
        self.assertFalse(records)
        _,records,_=self.read(since=NOW,until=NOW+1)
        self.assertEqual(len(records),1)

    def test_read_live_wal_and_cache_invalidation(self):
        c=self.database([],wal=True)
        cache=self.root/'scan.sqlite'
        e=s.Evidence(NOW-86400,NOW)
        s.cached_scan(e,scan_zcode,['zcode-db','zcode-rollout'],cache)
        row=self.dbrow()
        c.execute('INSERT INTO model_usage VALUES ('+','.join('?' for _ in DB_COLUMNS)+')',
                  [row[k] for k in DB_COLUMNS])
        c.commit()
        e=s.Evidence(NOW-86400,NOW)
        s.cached_scan(e,scan_zcode,['zcode-db','zcode-rollout'],cache)
        c.close()
        self.assertEqual(len(s.reconcile(e)[0]),1)

    def test_broken_db_and_missing_schema_fail(self):
        p=self.root/'broken.sqlite';p.write_text('broken')
        self.roots['zcode-db']=[str(p)]
        with self.assertRaises(sqlite3.DatabaseError):
            self.read()
        p=self.root/'old.sqlite';c=sqlite3.connect(p);c.execute('CREATE TABLE model_usage(id)');c.close()
        self.roots['zcode-db']=[str(p)]
        with self.assertRaises(sqlite3.OperationalError):
            self.read()

    def test_account_login_provider_counted(self):
        account='account:bigmodel-individual-coding-plan'
        self.database([self.dbrow(provider_id=account)])
        self.logs([self.logrow(model={'providerId':account,'modelId':'GLM-5.3'})])
        e, records, held=self.read()
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['provider'],account)
        self.assertEqual(e.stats['zcode_db_rollout_reconciled'],1)
        self.assertFalse(e.stats['zcode_custom_provider_skipped'])

    def test_bigmodel_priced_from_same_coding_plan_catalog(self):
        self.database([self.dbrow(provider_id='account:bigmodel-individual-coding-plan')])
        _, records, _=self.read()
        rates={('zai','glm-5.3'):{'input':1,'output':2,'cacheRead':0.5,'cacheWrite':0}}
        out=cli.window_result(records,NOW,1,rates)
        self.assertEqual(out['totals']['unpriced_tokens'],0)
        self.assertAlmostEqual(out['rows'][0]['cost_usd'],(80*1+20*0+900*0.5+100*2)/1e6)

    def test_unpriced_tokens_remain_in_totals(self):
        self.database([self.dbrow(provider_id='builtin:other-vendor')])
        _, records, _=self.read()
        rates={('zai','GLM-5.3'):{'input':1,'output':2,'cacheRead':0,'cacheWrite':0}}
        out=cli.window_result(records,NOW,1,rates)
        self.assertEqual(out['totals']['tokens'],1100)
        self.assertEqual(out['totals']['unpriced_tokens'],1100)
        self.assertIsNone(out['rows'][0]['cost_usd'])

    def test_cli_gather_includes_zcode_without_ledger(self):
        self.database([self.dbrow()])
        import argparse
        args=argparse.Namespace(as_of=iso(NOW),days='1',windows='1,7,30',
             split='0.005,0.065,0.93',cmd='summary',no_ledger=True,no_cache=True,ledger='unused')
        evidence,windows=cli.gather(args)
        records,held=s.reconcile(evidence)
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['source'],'zcode(内建)')
