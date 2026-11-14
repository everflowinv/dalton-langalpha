"""Synthetic dev-loop Codex review logs. No model calls, prompts, or credentials."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import usage_sources as s
from codex_devloop_usage import scan_codex_devloop

NOW = 1789458000.0
END = NOW - 60


class CodexDevloopUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.roots = {k: [] for k in s.ROOTS}
        self.patcher = patch.object(s, 'ROOTS', self.roots)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def log(self, lines, end=END):
        p = self.root/('codex_%d.log' % len(self.roots['codex-devloop']))
        p.write_text(''.join((l if isinstance(l, str) else json.dumps(l)) + '\n' for l in lines))
        os.utime(p, (end, end))
        self.roots['codex-devloop'].append(str(p))
        return p

    def structured(self, thread='th-1', usage=None, meta=True, end=END):
        lines = [dict(type='devloop.meta', model='gpt-6-astra')] if meta else []
        lines += [dict(type='thread.started', thread_id=thread), dict(type='turn.started'),
                  dict(type='item.completed', item=dict(id='item_0', type='agent_message', text='PRIVATE_SENTINEL')),
                  dict(type='turn.completed', usage=usage or dict(input_tokens=24598, cached_input_tokens=12160,
                       cache_write_input_tokens=0, output_tokens=905, reasoning_output_tokens=800))]
        return self.log(lines, end)

    def read(self, since=NOW-86400, until=NOW):
        e = s.Evidence(since, until)
        scan_codex_devloop(e)
        records, held = s.reconcile(e)
        return e, records, held

    def test_json_log_counts_run_with_cache_split_and_model(self):
        self.structured()
        e, records, held = self.read()
        self.assertEqual(len(records), 1)
        r = records[0]
        self.assertEqual((r['provider'], r['model'], r['meta']['unit']), ('openai', 'gpt-6-astra', 'agent-run'))
        self.assertEqual(tuple(r['v']), (12438, 0, 12160, 905))
        self.assertFalse(held)
        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(records))

    def test_legacy_log_is_disclosed_not_counted(self):
        self.log(['OpenAI Codex v0.164.0', 'model: gpt-6-astra', 'user', 'PRIVATE_SENTINEL', 'tokens used', '69,646', '结论：PRIVATE_SENTINEL'])
        e, records, _ = self.read()
        self.assertFalse(records)
        self.assertEqual(e.stats['codex_devloop_legacy_runs'], 1)
        self.assertEqual(e.stats['codex_devloop_legacy_uncached_tokens'], 69646)

    def test_rollout_for_same_thread_wins(self):
        self.structured(thread='th-2')
        e = s.Evidence(NOW-86400, NOW)
        scan_codex_devloop(e)
        e.put('codex', 'resp', END-5, 'openai', 'gpt-6-astra', 'codex-cli(手动)', (12438, 0, 12160, 905), thread='th-2', turn='u')
        records, _ = s.reconcile(e)
        self.assertEqual([r['kind'] for r in records], ['codex'])
        self.assertEqual(e.stats['codex_devloop_superseded_by_rollout'], 1)

    def test_missing_meta_and_failed_run_are_reported(self):
        self.structured(thread='th-3', meta=False)
        self.log([dict(type='devloop.meta', model='gpt-6-astra'), dict(type='thread.started', thread_id='th-4'),
                  dict(type='turn.failed', error=dict(message='PRIVATE_SENTINEL'))])
        e, records, _ = self.read()
        self.assertEqual([r['model'] for r in records], ['unknown'])
        self.assertEqual(e.issues['codex_devloop_model_unknown'], 1)
        self.assertEqual(e.issues['codex_devloop_usage_unavailable'], 1)
        self.assertEqual(e.issues['codex_devloop_turn_failed'], 1)

    def test_window_uses_completion_time(self):
        self.structured(end=NOW-3*86400)
        _, records, _ = self.read()
        self.assertFalse(records)


if __name__ == '__main__':
    unittest.main()
