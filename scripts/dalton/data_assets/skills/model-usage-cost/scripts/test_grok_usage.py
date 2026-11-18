"""Synthetic Grok CLI usage fixtures. No model calls, prompts, or credentials."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import usage_sources as s
import model_usage_cost_cli as cli
from grok_usage import scan_grok

NOW = 1789458000.0
RUN_START = NOW - 600   # matches the stamp in the result file name below
RUN_END = NOW - 60


def iso(t):
    return s.dt.datetime.fromtimestamp(t, s.UTC).isoformat().replace('+00:00', 'Z')


def stamp(t):
    return s.dt.datetime.fromtimestamp(t, s.UTC).strftime('%Y%m%dT%H%M%SZ')


class GrokUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.roots = {k: [] for k in s.ROOTS}
        self.patcher = patch.object(s, 'ROOTS', self.roots)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def session(self, sid, served=('grok-4.7-build',), current='grok-4.7'):
        d = self.root/'sessions'/'cwd'/sid
        d.mkdir(parents=True)
        (d/'summary.json').write_text(json.dumps({'info': {'id': sid}, 'current_model_id': current}))
        lines = [{'type': 'user', 'content': 'PRIVATE_SENTINEL'}]
        lines += [{'type': 'assistant', 'content': 'PRIVATE_SENTINEL', 'model_id': m} for m in served]
        (d/'chat_history.jsonl').write_text(''.join(json.dumps(o)+'\n' for o in lines))
        self.roots['grok-sessions'] = [str(p) for p in (self.root/'sessions').glob('*/*/summary.json')]

    def result(self, sid='run-session', request='req-1', start=RUN_START, end=RUN_END, **extra):
        o = dict(text='PRIVATE_SENTINEL', sessionId=sid, requestId=request, num_turns=2,
                 usage=dict(input_tokens=300, cache_read_input_tokens=1700, cache_creation_input_tokens=0,
                            output_tokens=90, reasoning_tokens=50, total_tokens=2090),
                 modelUsage={'grok-4.7-build': dict(inputTokens=300, outputTokens=90, cacheReadInputTokens=1700,
                                                    cacheCreationInputTokens=0, modelCalls=2, costUSD=0.0123)},
                 total_cost_usd=0.0123, total_cost_usd_ticks=123000000)
        legacy = extra.pop('_legacy_name', False)
        o.update(extra)
        # Older dev-loop scripts named results without the pid suffix.
        name = 'grok_%s.json' % stamp(start) if legacy else 'grok_%s_%d.json' % (stamp(start), len(self.roots['grok-headless']))
        p = self.root/name
        p.write_text(json.dumps(o))
        os.utime(p, (end, end))
        self.roots['grok-headless'].append(str(p))
        return p

    def event(self, sid, ts, loop, prompt, cached, out):
        return dict(ts=iso(ts), src='shell', pid=1, lvl='info', sid=sid, msg='shell.turn.inference_done',
                    ctx=dict(loop_index=loop, prompt_tokens=prompt, cached_prompt_tokens=cached,
                             completion_tokens=out, reasoning_tokens=out//2))

    def log(self, events, name='unified.jsonl'):
        p = self.root/name
        noise = dict(ts=iso(NOW-500), msg='AuthManager::new auth.json load result', ctx={'key_prefix': 'PRIVATE_SENTINEL'})
        p.write_text(''.join(json.dumps(o)+'\n' for o in [noise]+events))
        self.roots['grok-log'].append(str(p))

    def read(self, since=NOW-86400, until=NOW):
        e = s.Evidence(since, until)
        scan_grok(e)
        records, held = s.reconcile(e)
        return e, records, held

    def test_headless_run_supersedes_its_logged_calls(self):
        self.session('run-session')
        self.result()
        self.log([self.event('run-session', RUN_START+30, 1, 1000, 800, 40),
                  self.event('run-session', RUN_START+60, 2, 1000, 900, 50)])
        e, records, held = self.read()
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual((r['kind'], r['provider'], r['model'], r['source']), ('grok-headless', 'xai', 'grok-4.7-build', 'grok-cli(手动)'))
        self.assertEqual(tuple(r['v']), (300, 0, 1700, 90))
        self.assertEqual(r['ts'], RUN_END)
        self.assertEqual(e.stats['grok_log_superseded_by_headless'], 2)
        self.assertEqual(e.stats['grok_headless_log_matched'], 1)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(records))

    def test_calls_outside_any_run_count_per_response(self):
        self.session('interactive')
        self.session('run-session')
        self.result()
        self.log([self.event('interactive', NOW-3000, 1, 5000, 4000, 70),
                  # Same session as the run but before it started: a killed earlier attempt.
                  self.event('run-session', RUN_START-900, 1, 2000, 0, 30)])
        e, records, held = self.read()
        logged = sorted((r for r in records if r['kind'] == 'grok-log'), key=lambda r: r['ts'])
        self.assertEqual([tuple(r['v']) for r in logged], [(1000, 0, 4000, 70), (2000, 0, 0, 30)])
        self.assertEqual({r['model'] for r in logged}, {'grok-4.7-build'})
        self.assertEqual(e.stats['grok_log_only_records'], 2)
        self.assertEqual({r['meta']['unit'] for r in logged}, {'model-response'})

    def test_legacy_result_name_still_bounds_the_run(self):
        self.session('run-session')
        self.result(_legacy_name=True)
        self.log([self.event('run-session', RUN_START-900, 1, 2000, 0, 30)])
        e, records, held = self.read()
        self.assertEqual(sorted(r['kind'] for r in records), ['grok-headless', 'grok-log'])
        self.assertFalse(e.stats['grok_headless_start_unknown'])

    def test_partial_log_coverage_keeps_run_total(self):
        self.session('run-session')
        self.result()
        self.log([self.event('run-session', RUN_START+30, 1, 1000, 800, 40)])
        e, records, held = self.read()
        self.assertEqual([tuple(r['v']) for r in records], [(300, 0, 1700, 90)])
        self.assertEqual(e.stats['grok_headless_log_partial'], 1)

    def test_reported_cost_used_only_without_catalog_price(self):
        self.session('interactive', served=())
        self.result()
        self.log([self.event('interactive', NOW-3000, 1, 1000000, 0, 0)])
        _, records, _ = self.read()
        out = cli.window_result(records, NOW, 1, {('xai', 'grok-4.7'): {'input': 2, 'output': 6, 'cacheRead': 0.5, 'cacheWrite': 0}})
        rows = {r['model']: r for r in out['rows']}
        self.assertAlmostEqual(rows['grok-4.7-build']['cost_usd'], 0.0123)
        self.assertEqual(rows['grok-4.7-build']['cost_basis'], 'provider-reported')
        # No assistant turn recorded: fall back to the session's current model and its catalog price.
        self.assertAlmostEqual(rows['grok-4.7']['cost_usd'], 2.0)
        self.assertEqual(out['totals']['provider_reported_cost_records'], 1)
        self.assertEqual(out['totals']['unpriced_tokens'], 0)

    def test_partial_cost_is_not_reported_and_stays_unpriced(self):
        self.result(cost_is_partial=True, total_cost_usd_ticks=None)
        _, records, _ = self.read()
        out = cli.window_result(records, NOW, 1, {})
        self.assertIsNone(out['rows'][0]['cost_usd'])
        self.assertEqual(out['totals']['unpriced_tokens'], 2090)

    def test_missing_usage_and_bad_json_reported_not_zero(self):
        self.result(usage=None, modelUsage=None)
        bad = self.root/'grok_bad.json'
        bad.write_text('{not json')
        os.utime(bad, (RUN_END, RUN_END))
        self.roots['grok-headless'].append(str(bad))
        e, records, held = self.read()
        self.assertFalse(records)
        self.assertEqual(e.issues['grok_headless_usage_unavailable'], 1)
        self.assertEqual(e.issues['grok_headless_invalid_json'], 1)

    def test_rotated_log_event_survives_through_ledger(self):
        self.session('interactive')
        self.log([self.event('interactive', NOW-3000, 1, 1000, 600, 10)])
        ledger = self.root/'ledger.sqlite'
        e = s.Evidence(NOW-86400, NOW); scan_grok(e); s.persist(e, ledger)
        self.roots['grok-log'] = []
        e = s.Evidence(NOW-86400, NOW); scan_grok(e); s.persist(e, ledger)
        records, _ = s.reconcile(e)
        self.assertEqual([tuple(r['v']) for r in records], [(400, 0, 600, 10)])
        e = s.Evidence(NOW-86400, NOW); scan_grok(e); s.persist(e, ledger)
        self.assertEqual(len(s.reconcile(e)[0]), 1)

    def test_collector_scans_grok(self):
        self.session('interactive')
        self.log([self.event('interactive', NOW-3000, 1, 1000, 600, 10)])
        import argparse
        args = argparse.Namespace(as_of=iso(NOW), days='1', windows='1', split='0.005,0.065,0.93',
                                  cmd='collect-broker', no_ledger=True, no_cache=True, ledger='unused')
        e, _ = cli.gather(args)
        self.assertEqual(len([r for r in e.records.values() if r['kind'] == 'grok-log']), 1)


if __name__ == '__main__':
    unittest.main()
