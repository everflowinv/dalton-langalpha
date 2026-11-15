"""Read-only Grok CLI metering; never retain prompts, answers or credentials.

Headless runs leave a result JSON (dev-loop logs) whose usage covers the whole
prompt, including finished subagents, plus the server-stamped cost. The CLI's
unified.jsonl logs every model call but is rotated within hours, so the
per-minute collector persists those events. A logged call inside a headless
run of the same session is superseded by that run; calls outside any run
(interactive sessions, runs killed before writing JSON) count on their own.
"""
import collections
import datetime as dt
import json
import math
import os
import re
from pathlib import Path
import usage_sources as s

SOURCE = 'grok-cli(手动)'
PROVIDER = 'xai'
STAMP = re.compile(r'grok_(\d{8}T\d{6}Z)[_.]')
# Runs without a start stamp may only claim calls this long before completion.
MAX_RUN_SECONDS = 4 * 3600


def _stamp(path):
    m = STAMP.search(os.path.basename(path))
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), '%Y%m%dT%H%M%SZ').replace(tzinfo=s.UTC).timestamp()


def _cost(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _headless(e):
    for path in e.paths('grok-headless'):
        try:
            j = json.loads(Path(path).read_text(encoding='utf-8', errors='replace'))
        except (OSError, ValueError):
            e.issue('grok_headless_invalid_json', os.path.basename(path))
            continue
        end = os.path.getmtime(path)
        if not isinstance(j, dict) or not e.since <= end <= e.until:
            continue
        u = j.get('usage')
        if not isinstance(u, dict):
            # A prompt that never reached the model carries no spend fields.
            e.issue('grok_headless_usage_unavailable', os.path.basename(path))
            continue
        try:
            fresh, cw, cr, out = s.vector(u, ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens', 'output_tokens'))
        except ValueError:
            e.issue('grok_headless_invalid_usage', os.path.basename(path))
            continue
        total = u.get('total_tokens')
        if total is not None and total != fresh + cw + cr + out:
            e.issue('grok_headless_total_mismatch', os.path.basename(path))
        if j.get('usage_is_incomplete'):
            e.issue('grok_headless_usage_incomplete', os.path.basename(path))
        session, request = j.get('sessionId'), j.get('requestId')
        if not session:
            e.issue('grok_headless_session_missing', os.path.basename(path))
        start = _stamp(path)
        if start is None:
            e.stats['grok_headless_start_unknown'] += 1
        partial = bool(j.get('cost_is_partial'))
        run_cost = None if partial or j.get('total_cost_usd_ticks') is None else _cost(j['total_cost_usd_ticks'] / 1e10)
        rows = []
        mu = j.get('modelUsage')
        if isinstance(mu, dict) and mu:
            try:
                for model, m in mu.items():
                    rows.append((model, s.vector(m, ('inputTokens', 'cacheCreationInputTokens', 'cacheReadInputTokens', 'outputTokens')),
                                 None if partial else _cost(m.get('costUSD')), m.get('modelCalls')))
            except (ValueError, AttributeError):
                rows = []
            if rows and s.summed([{'v': v} for _, v, _, _ in rows]) != (fresh, cw, cr, out):
                e.issue('grok_headless_model_split_mismatch', os.path.basename(path))
                rows = []
        if len(rows) == 1:
            rows = [(rows[0][0], rows[0][1], run_cost if run_cost is not None else rows[0][2], rows[0][3])]
        if not rows:
            rows = [('unknown', (fresh, cw, cr, out), run_cost, None)]
            e.issue('grok_headless_model_unknown', os.path.basename(path))
        ident = request or s.digest(os.path.basename(path), session, end)
        for model, v, cost, calls in rows:
            e.put('grok-headless', ident if len(rows) == 1 else ident + ':' + model, end, PROVIDER, model, SOURCE, v,
                  session=session, request=request, start=start, unit='agent-run', model_calls=calls,
                  reported_cost_usd=cost, time_basis='completion')


class _Models:
    """Session id -> model that served the turns; read lazily, text discarded."""
    def __init__(self, e):
        self.e, self.paths, self.cache = e, {}, {}
        for path in e.paths('grok-sessions'):
            sid = Path(path).parent.name
            self.paths.setdefault(sid, path)

    def __call__(self, sid):
        if sid in self.cache:
            return self.cache[sid]
        model, path = None, self.paths.get(sid)
        if path:
            served = set()
            history = Path(path).with_name('chat_history.jsonl')
            if history.is_file():
                for n, line in self.e.lines(str(history)):
                    if '"model_id"' not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(o, dict) and o.get('type') == 'assistant' and isinstance(o.get('model_id'), str):
                        served.add(o['model_id'])
            if len(served) == 1:
                model = served.pop()
            else:
                try:
                    current = json.loads(Path(path).read_text()).get('current_model_id')
                except (OSError, ValueError, AttributeError):
                    current = None
                if isinstance(current, str) and not served:
                    model = current
                    self.e.stats['grok_log_model_from_summary'] += 1
        self.cache[sid] = model
        return model


def _log(e):
    models = None
    for path in e.paths('grok-log'):
        for n, line in e.lines(path):
            if 'shell.turn.inference_done' not in line:
                continue
            o = e.parse(line, path, n)
            if o.get('msg') != 'shell.turn.inference_done':
                continue
            ts, sid, ctx = s.timestamp(o.get('ts')), o.get('sid'), o.get('ctx') or {}
            if ts is None or not e.since <= ts <= e.until:
                continue
            try:
                prompt, cached, out = s.vector(ctx, ('prompt_tokens', 'cached_prompt_tokens', 'completion_tokens'))
            except ValueError:
                e.issue('grok_log_usage_unavailable', sid)
                continue
            if cached > prompt:
                e.issue('grok_log_cache_exceeds_prompt', sid)
                continue
            if not sid:
                e.issue('grok_log_session_missing', '%s:%s' % (path, n))
                continue
            if models is None:
                models = _Models(e)
            model = models(sid)
            if model is None:
                e.issue('grok_log_model_unknown', sid)
            e.put('grok-log', s.digest(sid, o.get('ts'), o.get('pid'), ctx.get('loop_index'), prompt, cached, out),
                  ts, PROVIDER, model or 'unknown', SOURCE, (prompt - cached, 0, cached, out),
                  session=sid, loop=ctx.get('loop_index'), reasoning=ctx.get('reasoning_tokens'),
                  unit='model-response', time_basis='completion')


def scan_grok(e):
    _headless(e)
    _log(e)


def reconcile_grok(e, active, held):
    runs = collections.defaultdict(list)
    for r in active.values():
        if r['kind'] == 'grok-headless' and r['meta'].get('session'):
            runs[r['meta']['session']].append(r)
    covered = collections.defaultdict(list)
    for r in list(active.values()):
        if r['kind'] != 'grok-log':
            continue
        hits = []
        for h in runs.get(r['meta'].get('session'), []):
            lo = h['meta'].get('start')
            lo = h['ts'] - MAX_RUN_SECONDS if lo is None else lo
            if lo - 2 <= r['ts'] <= h['ts'] + 2:
                hits.append(h)
        requests = {h['meta'].get('request') or h['id'] for h in hits}
        if requests:
            # The run's result JSON already includes this call.
            active.pop(r['id'], None)
            e.stats['grok_log_superseded_by_headless'] += 1
            if len(requests) == 1:
                covered[requests.pop()].append(r)
        else:
            e.stats['grok_log_only_records'] += 1
    totals = collections.defaultdict(list)
    for rows in runs.values():
        for h in rows:
            totals[h['meta'].get('request') or h['id']].append(h)
    for request, calls in covered.items():
        if s.summed(calls) == s.summed(totals[request]):
            e.stats['grok_headless_log_matched'] += 1
        else:
            # Partial log coverage (rotation mid-run) is expected; the run total wins.
            e.stats['grok_headless_log_partial'] += 1
