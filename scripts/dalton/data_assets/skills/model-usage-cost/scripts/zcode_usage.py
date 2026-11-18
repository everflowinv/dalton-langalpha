"""Read-only ZCode builtin/account metering; never retain model text or credentials."""
import collections
import json
import usage_sources as s

DB_COLUMNS = (
    'id', 'logical_request_id', 'attempt_index', 'session_id', 'turn_id',
    'trace_id', 'provider_id', 'model_id', 'query_source', 'status',
    'started_at', 'completed_at', 'input_tokens', 'output_tokens',
    'reasoning_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens',
    'computed_total_tokens', 'raw_usage_json',
)
SOURCE = 'zcode(内建)'


def builtin(provider):
    # Bundled providers and vendor-account logins bill ZCode directly. Custom
    # providers may point at a gateway that is metered elsewhere.
    return isinstance(provider, str) and provider.startswith(('builtin:', 'account:'))


def normalized(e, usage, ident):
    # ZCode's SDK inputTokens includes cache, outputTokens includes reasoning.
    if not isinstance(usage, dict) or not any(usage.get(k) is not None for k in ('inputTokens', 'outputTokens')):
        e.issue('zcode_usage_unavailable', ident)
        return None
    try:
        inp, cw, cr, out = s.vector(usage, ('inputTokens', 'cacheWriteTokens', 'cacheReadTokens', 'outputTokens'))
        reasoning = s.number(0 if usage.get('reasoningTokens') is None else usage['reasoningTokens'])
        if cr + cw > inp:
            raise ValueError('cache exceeds input')
        if reasoning > out:
            raise ValueError('reasoning exceeds output')
        total = usage.get('totalTokens')
        if total is not None and s.number(total) != inp + out:
            e.issue('zcode_provider_total_mismatch', ident)
        return (inp - cw - cr, cw, cr, out), reasoning
    except ValueError:
        e.issue('zcode_invalid_usage', ident)
        return None


def _db(e):
    for path in e.paths('zcode-db'):
        c = s.dbopen(path)
        try:
            # Named columns deliberately fail on schema drift instead of zeroing.
            for values in c.execute('SELECT ' + ','.join(DB_COLUMNS) + ' FROM model_usage'):
                row = dict(zip(DB_COLUMNS, values))
                if not builtin(row['provider_id']):
                    e.stats['zcode_custom_provider_skipped'] += 1
                    continue
                ts = s.timestamp(row['completed_at'])
                start = s.timestamp(row['started_at'])
                if ts is None:
                    if start is not None and e.since <= start <= e.until:
                        e.issue('zcode_completion_time_unavailable', row['id'])
                    continue
                if not e.since <= ts <= e.until:
                    continue
                if not row['id'] or start is None or not row['session_id'] or not row['model_id']:
                    e.issue('zcode_identity_or_start_missing', row['id'])
                    continue
                try:
                    raw = json.loads(row['raw_usage_json']) if row['raw_usage_json'] else None
                except (ValueError, TypeError):
                    raw = None
                    e.issue('zcode_raw_usage_invalid_json', row['id'])
                counters = dict(inputTokens=row['input_tokens'], outputTokens=row['output_tokens'],
                                cacheWriteTokens=row['cache_creation_input_tokens'],
                                cacheReadTokens=row['cache_read_input_tokens'],
                                reasoningTokens=row['reasoning_tokens'], totalTokens=row['computed_total_tokens'])
                # Error/cancelled rows default every counter to 0 without usage.
                if not any(counters[k] or 0 for k in ('inputTokens','outputTokens','cacheReadTokens','cacheWriteTokens')):
                    if not isinstance(raw, dict) or not raw:
                        e.issue('zcode_failed_or_cancelled_usage_unknown' if row['status'] in ('error','cancelled') else 'zcode_usage_unavailable', row['id'])
                    else:
                        e.stats['zcode_zero_usage'] += 1
                    continue
                result = normalized(e, counters, row['id'])
                if result is None:
                    continue
                v, reasoning = result
                if row['status'] in ('error', 'cancelled'):
                    e.issue('zcode_failed_partial_usage_retained', row['id'])
                e.put('zcode-db', row['id'], ts, row['provider_id'], row['model_id'], SOURCE, v,
                      session=row['session_id'], turn=row['turn_id'], trace=row['trace_id'],
                      query_source=row['query_source'], request=row['logical_request_id'],
                      attempt=row['attempt_index'], start=start, status=row['status'],
                      reasoning=reasoning, unit='model-invocation')
        finally:
            c.close()


def _rollout(e):
    for path in e.paths('zcode-rollout'):
        for n, line in e.lines(path):
            o = e.parse(line, path, n)
            if o.get('type') != 'model_io':
                continue
            model = o.get('model') or {}
            if not isinstance(model, dict):
                e.issue('zcode_model_metadata_invalid', path)
                continue
            provider = model.get('providerId')
            if not builtin(provider):
                e.stats['zcode_custom_provider_skipped'] += 1
                continue
            ts, start = s.timestamp(o.get('completedAt')), s.timestamp(o.get('startedAt'))
            if ts is None:
                e.issue('zcode_completion_time_unavailable', o.get('requestId'))
                continue
            if not e.since <= ts <= e.until:
                continue
            request, session, name = o.get('requestId'), o.get('sessionId'), model.get('modelId')
            if not request or not session or not name or start is None:
                e.issue('zcode_identity_or_start_missing', path)
                continue
            try:
                attempt = s.number(o.get('attempt'))
            except ValueError:
                e.issue('zcode_attempt_unavailable', request)
                continue
            response = o.get('response') or {}
            if not isinstance(response, dict):
                e.issue('zcode_usage_unavailable', request)
                continue
            result = normalized(e, response.get('usage'), request)
            if result is None:
                continue
            v, reasoning = result
            e.put('zcode-rollout', s.digest(provider, session, request, attempt), ts, provider, name, SOURCE, v,
                  session=session, turn=o.get('turnId'), trace=o.get('traceId'),
                  query_source=o.get('querySource'), request=request, attempt=attempt,
                  start=start, reasoning=reasoning, unit='model-invocation')


def context(r):
    m = r['meta']
    return (r['provider'], r['model'], m.get('session'), m.get('turn'),
            m.get('trace'), m.get('query_source'))


def candidates(r, db_index):
    # DB IDs are logical request/message IDs; rollout IDs are transport UUIDs.
    # Require shared trace/turn identity and the same completion neighborhood.
    # A retry may begin later than its logical request, hence containment.
    m = r['meta']
    if not m.get('session') or not (m.get('turn') or m.get('trace')) or not m.get('query_source'):
        return []
    return [d for d in db_index.get(context(r), [])
            if d['meta']['start'] - 1 <= m['start'] <= d['ts'] + 1
            and abs(d['ts'] - r['ts']) <= 1]


def scan_zcode(e):
    # Look just beyond window boundaries to assign matched log observations the
    # DB completion time, avoiding two copies falling into adjacent windows.
    work = s.Evidence(e.since - 2, e.until + 2)
    _db(work)
    _rollout(work)
    index = collections.defaultdict(list)
    for r in work.records.values():
        if r['kind'] == 'zcode-db':
            index[context(r)].append(r)
    for r in work.records.values():
        meta = dict(r['meta'])
        ts = r['ts']
        if r['kind'] == 'zcode-rollout':
            matches = [d for d in candidates(r, index) if tuple(d['v']) == tuple(r['v'])]
            if len(matches) == 1:
                meta['matched_db'] = matches[0]['id']
                meta['observed_completion'] = ts
                ts = matches[0]['ts']
        e.put(r['kind'], r['id'].split(':', 1)[1], ts, r['provider'], r['model'], r['source'], r['v'], **meta)
    e.inventory.update(work.inventory)
    e.issues.update(work.issues)
    e.stats.update(work.stats)
    for k, examples in work.examples.items():
        e.examples.setdefault(k, []).extend(examples[:max(0, 3-len(e.examples.get(k, [])))])


def reconcile_zcode(e, active, held):
    db = collections.defaultdict(list)
    logs = []
    for r in list(active.values()):
        if r['kind'] == 'zcode-db':
            db[context(r)].append(r)
        elif r['kind'] == 'zcode-rollout':
            logs.append(r)
    choices = {}
    for r in logs:
        nearby = candidates(r, db)
        matches = [d for d in nearby if tuple(d['v']) == tuple(r['v'])]
        linked = active.get(r['meta'].get('matched_db'))
        if linked is not None and linked['kind'] == 'zcode-db' and context(linked) == context(r):
            if tuple(linked['v']) == tuple(r['v']):
                matches = [linked]
            elif linked not in nearby:
                nearby.append(linked)
        choices[r['id']] = (nearby, matches)
    reverse = collections.Counter(ds[0]['id'] for _, ds in choices.values() if len(ds) == 1)
    for r in logs:
        nearby, matches = choices[r['id']]
        if len(matches) == 1 and reverse[matches[0]['id']] == 1:
            active.pop(r['id'], None)
            e.stats['zcode_db_rollout_reconciled'] += 1
        elif nearby:
            active.pop(r['id'], None)
            held.append(r)
            e.issue('zcode_db_rollout_unreconciled', r['id'])
        else:
            e.stats['zcode_rollout_fallback'] += 1
