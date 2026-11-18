"""Read-only metering of dev-loop Codex reviews; never retain prompts, answers or credentials.

review_codex.sh runs `codex exec --ephemeral`, so no rollout is written and the
rollout scanner never sees these runs. Since 2026-10-24 the script adds `--json`
and a first `devloop.meta` line naming the model; each `turn.completed` event
then carries the run's full usage (input includes cached and cache-write input,
output includes reasoning). Older logs only end with Codex's "tokens used"
figure, which leaves out cached input: those runs stay out of totals and are
disclosed through stats.
"""
import json
import os
import re
import usage_sources as s

SOURCE = 'codex-cli(dev-loop)'
PROVIDER = 'openai'
MARKERS = ('"devloop.meta"', '"thread.started"', '"turn.completed"', '"turn.failed"')
LEGACY_TOTAL = re.compile(r'^\d[\d,]*$')


def _legacy(e, path):
    """Old plain-text log: header names the model, last lines give the blended total."""
    lines = [line.rstrip('\n') for _, line in e.lines(path)]
    total = None
    for i, line in enumerate(lines[:-1]):
        if line.strip() == 'tokens used' and LEGACY_TOTAL.match(lines[i + 1].strip()):
            total = int(lines[i + 1].strip().replace(',', ''))
    if total is None:
        e.issue('codex_devloop_usage_unavailable', os.path.basename(path))
        return
    e.stats['codex_devloop_legacy_runs'] += 1
    e.stats['codex_devloop_legacy_uncached_tokens'] += total
    e.issue('codex_devloop_legacy_log_without_cache_split', os.path.basename(path))


def scan_codex_devloop(e):
    for path in e.paths('codex-devloop'):
        end = os.path.getmtime(path)
        if not e.since <= end <= e.until:
            continue
        model, thread, turns, failed, structured = None, None, [], 0, False
        for n, line in e.lines(path):
            if not any(m in line for m in MARKERS):
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue  # stderr text interleaved with the event stream
            if not isinstance(o, dict):
                continue
            typ = o.get('type')
            if typ == 'devloop.meta':
                structured = True
                model = o.get('model') if isinstance(o.get('model'), str) else model
            elif typ == 'thread.started':
                structured = True
                thread = o.get('thread_id') or thread
            elif typ == 'turn.completed':
                structured = True
                turns.append(o.get('usage'))
            elif typ == 'turn.failed':
                structured = True
                failed += 1
        if not structured:
            _legacy(e, path)
            continue
        if failed:
            e.issue('codex_devloop_turn_failed', os.path.basename(path))
        if not turns:
            e.issue('codex_devloop_usage_unavailable', os.path.basename(path))
            continue
        if not model:
            e.issue('codex_devloop_model_unknown', os.path.basename(path))
        if not thread:
            e.issue('codex_devloop_thread_missing', os.path.basename(path))
        ident = thread or s.digest(os.path.basename(path), end)
        for i, u in enumerate(turns):
            v = e.usage(u, ('input_tokens', 'cache_write_input_tokens', 'cached_input_tokens', 'output_tokens'), os.path.basename(path))
            if v is None:
                continue
            inp, cw, cr, out = v
            if cr + cw > inp:
                e.issue('codex_devloop_cache_exceeds_input', ident)
                continue
            e.put('codex-devloop', '%s:%d' % (ident, i), end, PROVIDER, model or 'unknown', SOURCE, (inp - cr - cw, cw, cr, out),
                  thread=thread, reasoning=(u or {}).get('reasoning_output_tokens'), unit='agent-run', time_basis='completion')


def reconcile_codex_devloop(e, active, held):
    # A run made without --ephemeral also leaves a rollout; its per-response records win.
    threads = {r['meta'].get('thread') for r in active.values() if r['kind'] in ('codex', 'codex-compaction')}
    for r in list(active.values()):
        if r['kind'] == 'codex-devloop' and r['meta'].get('thread') in threads:
            active.pop(r['id'], None)
            e.stats['codex_devloop_superseded_by_rollout'] += 1
