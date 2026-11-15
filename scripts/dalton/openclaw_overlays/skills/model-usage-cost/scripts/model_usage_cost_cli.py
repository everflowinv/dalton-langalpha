#!/usr/bin/env python3
"""Auditable local + Dalton model usage; reconciled observations, not a provider invoice."""
import argparse
import collections
import datetime as dt
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import unittest

import usage_sources as sources
from codex_devloop_usage import scan_codex_devloop
from grok_usage import scan_grok
from usage_v3 import scan_compactions, scan_muse
from zcode_usage import scan_zcode

VERSION = '3.1.0'
DEFAULT_PRICES_FILE = Path(__file__).resolve().parent.parent / 'references' / 'prices.json'
DEFAULT_SPLIT = (0.005, 0.065, 0.93)
THINKING_SUFFIXES = ('-low', '-medium', '-high', '-xhigh', '-max', '-minimal')
NOTES = [
    '统计保留在本机可核实用量及 Dalton 生产只读聚合；coverage 显示缺失、失败及无法对账的记录，不等同于云端账单。',
    'output 已包含 reasoning/thinking；cache_read/cache_write 与 fresh 分列。',
    'records 是计量记录数；model-response、gateway-request、agent-run、model-invocation 不是同一调用单位。',
    'cost_usd 使用 Dalton 自有价格表 references/prices.json 按记录日期选择生效价估算；无公开价模型记为「无公开价」且不计入总成本。',
    'Dalton 生产用量通过内部只读接口按模型聚合获取，直连模型供应商且不经过本机命令行网关，因此不与本机来源去重；模型名先去 -oauth 再尝试去思考档位后缀匹配价格。',
    'ZCode 内建与账号登录（如智谱 Coding Plan）调用包括本地留存的主对话、子代理、标题、压缩和工具内部模型请求；数据库优先、rollout 补漏。智谱 GLM 按同一 Coding Plan 端点的 zai catalog 参考价估算，其他缺价用量保留未定价。',
    'Grok CLI：headless 结果 JSON 按整次运行入账，费用用 xAI 服务端回报值（provider_reported_cost_records）；没有结果 JSON 的调用用 unified.jsonl 逐次补足。',
    '按 [from,to) 统计；已关联网关使用完成时间，历史仅保留请求开始时间的记录单列。',
    '跨层关联优先 request ID；历史用量/时间一对一匹配在 coverage.stats 单列，歧义汇总不叠加。',
    'Muse 手动与网关使用当前 Muse catalog 参考价；聚合记录缺单响应上下文时保留基础价并披露。',
    'dev-loop 的 Codex 审查（--ephemeral，无 rollout）按 --json 日志的 turn.completed 整次运行入账；2026-10-24 前旧日志不进总量。',
    'Antigravity 同一模型枚举下带 -b 后缀的名称归并到基础模型名和价格，次数见 agy_variant_label_merged；已知枚举也可由 references/agy_enum_names.json 补充。',
]


def default_state_dir():
    raw = (os.environ.get('DALTON_USAGE_STATE_DIR') or '').strip()
    return Path(raw or '~/.openclaw/usage-accounting').expanduser()


def default_ledger_path():
    return str(default_state_dir() / 'evidence.sqlite')


def default_prices_path():
    raw = (os.environ.get('DALTON_PRICES_FILE') or '').strip()
    return Path(raw).expanduser() if raw else DEFAULT_PRICES_FILE


DEFAULT_LEDGER = default_ledger_path()


def _parse_tier(tier, base_rate, idx):
    bound = tier.get('from')
    if bound is None:
        bound = tier.get('min_prompt_tokens')
    if bound is None and isinstance(tier.get('range'), list) and tier['range']:
        bound = tier['range'][0]
    if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound) or bound < 0:
        raise ValueError(f'invalid tier threshold at index {idx}')
    item = {}
    for k in ('input', 'cacheWrite', 'cacheRead', 'output'):
        val = tier.get(k)
        val = base_rate[k] if val is None else float(val)
        if not math.isfinite(val) or val < 0:
            raise ValueError(f'invalid tier rate for {k}')
        item[k] = val
    item['from'] = bound
    return item


def _parse_price_entry(entry, idx):
    provider = str(entry.get('provider') or '').strip()
    model = str(entry.get('model') or '').strip()
    if not provider or not model:
        raise ValueError(f'missing provider/model at price index {idx}')

    eff_raw = entry.get('effective_from')
    if eff_raw is None or (isinstance(eff_raw, str) and not eff_raw.strip()):
        effective_from = None
    else:
        effective_from = str(eff_raw).strip()

    note = entry.get('note')
    note_str = str(note).strip() if isinstance(note, str) and note.strip() else None

    ref_spec = None
    if isinstance(entry.get('reference_only'), dict):
        raw_ref = entry['reference_only']
        ref_rates = {}
        for k in ('input', 'cacheWrite', 'cacheRead', 'output'):
            val = float(raw_ref.get(k) or 0.0)
            if not math.isfinite(val) or val < 0:
                raise ValueError(f'invalid reference_only rate for {provider}/{model}')
            ref_rates[k] = val
        ref_model = str(raw_ref.get('model') or '').strip() or None
        ref_spec = {**ref_rates, 'model': ref_model}

    if entry.get('input') is None:
        return provider, model, {
            'effective_from': effective_from,
            'public_price': False,
            'input': None,
            'cacheWrite': None if entry.get('cacheWrite') is None else float(entry.get('cacheWrite') or 0.0),
            'cacheRead': None if entry.get('cacheRead') is None else float(entry.get('cacheRead') or 0.0),
            'output': None if entry.get('output') is None else float(entry.get('output') or 0.0),
            'note': note_str,
            'reference_only': ref_spec,
        }

    rate = {}
    for k in ('input', 'cacheWrite', 'cacheRead', 'output'):
        raw_val = entry.get(k)
        val = 0.0 if raw_val is None else float(raw_val)
        if not math.isfinite(val) or val < 0:
            raise ValueError(f'invalid rate {k} for {provider}/{model}')
        rate[k] = val

    tiers = [_parse_tier(t, rate, tidx) for tidx, t in enumerate(entry.get('tiers') or [])]
    out = {
        **rate,
        'effective_from': effective_from,
        'public_price': True,
        'note': note_str,
    }
    if tiers:
        out['tiers'] = sorted(tiers, key=lambda x: x['from'])
    if ref_spec is not None:
        out['reference_only'] = ref_spec
    return provider, model, out


def load_price_catalog(path=None):
    target = default_prices_path() if path is None else Path(os.path.expanduser(str(path)))
    with open(target, encoding='utf-8') as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict) or not isinstance(cfg.get('prices'), list):
        raise ValueError('prices.json must contain a top-level prices list')

    grouped = collections.defaultdict(list)
    effective_notes = []
    seen_notes = set()
    for idx, entry in enumerate(cfg['prices']):
        if not isinstance(entry, dict):
            raise ValueError(f'invalid price entry at index {idx}')
        provider, model, parsed = _parse_price_entry(entry, idx)
        grouped[(provider, model)].append((idx, parsed))
        if parsed.get('effective_from') and parsed.get('note'):
            label = f"{model} ({parsed['effective_from']} 起): {parsed['note']}"
            if label not in seen_notes:
                seen_notes.add(label)
                effective_notes.append(label)

    prices = {}
    for key, items in grouped.items():
        items.sort(key=lambda pair: (pair[1].get('effective_from') or '', pair[0]))
        prices[key] = [pair[1] for pair in items]
    if not prices:
        raise ValueError('no model prices in price catalog')

    meta = {
        'generated': str(cfg.get('generated') or ''),
        'currency': str(cfg.get('currency') or 'USD'),
        'unit': str(cfg.get('unit') or 'per_1m_tokens'),
        'note': str(cfg.get('note') or ''),
        'effective_notes': effective_notes,
        'path': str(target),
    }
    return prices, meta


def load_prices(path=None):
    prices, _ = load_price_catalog(path)
    return prices


def model_price_candidates(model, *, strip_oauth=True):
    """Return ordered model IDs to try when matching a catalog entry."""
    if not isinstance(model, str):
        return []
    text = model.strip()
    if not text:
        return []
    candidates = []

    def add(value):
        if value and value not in candidates:
            candidates.append(value)

    add(text)
    current = text
    if strip_oauth and current.endswith('-oauth'):
        current = current[:-len('-oauth')]
        add(current)
    for suffix in THINKING_SUFFIXES:
        if current.endswith(suffix):
            add(current[:-len(suffix)])
            break
    return candidates


def _date_for_timestamp(ts=None, as_date=None):
    if as_date:
        return str(as_date)[:10]
    if ts is not None:
        return dt.datetime.fromtimestamp(ts, sources.UTC).strftime('%Y-%m-%d')
    return dt.datetime.now(sources.UTC).strftime('%Y-%m-%d')


def _select_effective_entry(entry_or_list, target_date):
    if entry_or_list is None:
        return None
    if isinstance(entry_or_list, dict):
        eff = entry_or_list.get('effective_from')
        if eff and eff > target_date:
            return None
        return entry_or_list
    selected = None
    for item in entry_or_list:
        eff = item.get('effective_from') or ''
        if eff <= target_date:
            selected = item
    return selected


def match_price_entry(prices, provider, model, *, ts=None, as_date=None, strip_oauth=True):
    """Match the effective price entry for (provider, model) at target date."""
    target_date = _date_for_timestamp(ts=ts, as_date=as_date)
    provider_candidates = [provider]
    if provider == 'anthropic':
        provider_candidates.append('claude-cli-gateway')
    # ZCode's bigmodel providers call the same Coding Plan endpoint as zai.
    if isinstance(provider, str) and provider.split(':', 1)[-1].startswith('bigmodel') and isinstance(model, str):
        provider_candidates.append('zai')

    model_candidates = model_price_candidates(model, strip_oauth=strip_oauth)
    if isinstance(provider, str) and provider.split(':', 1)[-1].startswith('bigmodel') and isinstance(model, str):
        for cand in list(model_candidates):
            low = cand.lower()
            if low not in model_candidates:
                model_candidates.append(low)

    for prov in provider_candidates:
        for mod in model_candidates:
            raw = prices.get((prov, mod))
            picked = _select_effective_entry(raw, target_date)
            if picked is not None:
                return picked
    return None


def price_for(prices, provider, model, ts=None, *, as_date=None, strip_oauth=False):
    pr = match_price_entry(
        prices,
        provider,
        model,
        ts=ts,
        as_date=as_date,
        strip_oauth=strip_oauth,
    )
    if pr is None or pr.get('public_price') is False or pr.get('input') is None:
        return None
    return pr


def reported_cost(meta):
    c = (meta or {}).get('reported_cost_usd')
    return float(c) if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) and c >= 0 else None


def _compute_usd_from_rate(rate, v):
    return sum(v[i] * rate[k] for i, k in enumerate(('input', 'cacheWrite', 'cacheRead', 'output'))) / 1e6


def cost_of(prices, provider, model, v, meta=None, ts=None):
    strip_oauth = bool((meta or {}).get('strip_oauth'))
    pr = price_for(prices, provider, model, ts=ts, strip_oauth=strip_oauth)
    if pr is None:
        raw_entry = match_price_entry(prices, provider, model, ts=ts, strip_oauth=strip_oauth)
        if raw_entry is not None and raw_entry.get('public_price') is False:
            return None
        return reported_cost(meta)
    parts = (meta or {}).get('billing_parts')
    if parts and tuple(sum(p[i] for p in parts) for i in range(4)) == tuple(v):
        return sum(cost_of(prices, provider, model, p, {'unit': 'model-response', 'strip_oauth': strip_oauth}, ts=ts) for p in parts)
    rate = pr
    if meta is None or meta.get('unit') == 'model-response':
        for tier in pr.get('tiers', []):
            if sum(v[:3]) >= tier['from']:
                rate = tier
    return _compute_usd_from_rate(rate, v)


def reference_cost_of(prices, provider, model, v, meta=None, ts=None):
    strip_oauth = bool((meta or {}).get('strip_oauth'))
    entry = match_price_entry(prices, provider, model, ts=ts, strip_oauth=strip_oauth)
    if not entry or entry.get('public_price') is not False:
        return None, None, None
    ref = entry.get('reference_only')
    if not isinstance(ref, dict):
        return None, None, entry.get('note')
    usd = _compute_usd_from_rate(ref, v)
    return usd, ref.get('model'), entry.get('note')


def window_result(records, now, days, prices, top=0, by_model=False):
    cut = now - days * 86400
    groups = {}
    total = [0] * 4
    total_calls = 0
    known_cost = 0.0
    unpriced_tokens = 0
    units = collections.Counter()
    estimates = 0
    time_estimates = 0
    tier_unknown = 0
    reported = 0

    for r in records:
        if r['kind'] == 'dalton-prod' and r['meta'].get('window_days') is not None:
            if r['meta']['window_days'] != days:
                continue
        if not cut <= r['ts'] < now:
            continue
        provider, model = r['provider'], r['model']
        v = r['v']
        meta = r['meta']
        strip_oauth = bool(meta.get('strip_oauth'))
        c = cost_of(prices, provider, model, v, meta, ts=r['ts'])
        if meta.get('time_basis') == 'request-start':
            time_estimates += 1
        pr = price_for(prices, provider, model, ts=r['ts'], strip_oauth=strip_oauth) or {}
        raw_entry = match_price_entry(prices, provider, model, ts=r['ts'], strip_oauth=strip_oauth)
        is_no_public = bool(raw_entry is not None and raw_entry.get('public_price') is False)
        is_reported = not pr and not is_no_public and c is not None
        reported += is_reported
        if pr.get('tiers') and meta.get('unit') != 'model-response' and not meta.get('billing_parts'):
            tier_unknown += 1

        is_dalton = bool(
            r['kind'] == 'dalton-prod'
            or r['source'] == sources.DALTON_PROD_CHANNEL
            or meta.get('dalton_prod')
        )
        key = (is_dalton if by_model else r['source'], provider, model)
        b = groups.setdefault(
            key,
            dict(
                source=set(),
                provider=provider,
                model=model,
                dalton_prod=is_dalton,
                records=0,
                calls=0,
                runs=0,
                v=[0] * 4,
                cost_usd=0.0,
                unpriced_tokens=0,
                estimated_split_records=0,
                provider_reported_cost_records=0,
                units=collections.Counter(),
                no_public_price=False,
                reference_cost_usd=0.0,
                has_reference_cost=False,
                reference_model=None,
                price_note=None,
            ),
        )
        b['source'].add(r['source'])
        b['records'] += 1
        rec_calls = int(meta.get('calls', 1))
        b['calls'] += rec_calls
        total_calls += rec_calls
        b['runs'] += int(meta.get('runs', 0))
        b['provider_reported_cost_records'] += is_reported
        b['units'][meta.get('unit', 'unknown')] += 1
        for i in range(4):
            b['v'][i] += v[i]
            total[i] += v[i]
        if c is None:
            b['unpriced_tokens'] += sum(v)
            unpriced_tokens += sum(v)
            if is_no_public:
                b['no_public_price'] = True
                ref_usd, ref_model, ref_note = reference_cost_of(prices, provider, model, v, meta, ts=r['ts'])
                if ref_usd is not None:
                    b['reference_cost_usd'] += ref_usd
                    b['has_reference_cost'] = True
                    b['reference_model'] = ref_model
                if ref_note:
                    b['price_note'] = ref_note
        else:
            b['cost_usd'] += c
            known_cost += c
        if meta.get('estimated'):
            b['estimated_split_records'] += 1
            estimates += 1
        units[meta.get('unit', 'unknown')] += 1

    rows = []
    for b in groups.values():
        v = b.pop('v')
        has_ref = b.pop('has_reference_cost')
        b['source'] = '|'.join(sorted(b['source']))
        b.update(dict(zip(('fresh', 'cache_write', 'cache_read', 'output'), v)))
        b['tokens'] = sum(v)
        b['priced_cost_usd'] = round(b['cost_usd'], 6)
        b['cost_usd'] = None if b['unpriced_tokens'] == b['tokens'] else b['priced_cost_usd']
        b['reference_cost_usd'] = round(b['reference_cost_usd'], 6) if has_ref else None
        if b['cost_usd'] is None:
            b['cost_basis'] = 'unpriced'
        elif b['provider_reported_cost_records'] == b['records']:
            b['cost_basis'] = 'provider-reported'
        else:
            b['cost_basis'] = 'current-catalog-estimate'
        b['estimated_split'] = b['estimated_split_records'] > 0
        rows.append(b)

    rows.sort(key=lambda r: (-(r['cost_usd'] if r['cost_usd'] is not None else -1), -r['tokens']))
    return dict(
        days=days,
        **{
            'from': dt.datetime.fromtimestamp(cut, sources.UTC).isoformat(),
            'to': dt.datetime.fromtimestamp(now, sources.UTC).isoformat(),
        },
        rows=rows[:top] if top else rows,
        row_count=len(rows),
        totals=dict(
            zip(('fresh', 'cache_write', 'cache_read', 'output'), total),
            tokens=sum(total),
            records=sum(units.values()),
            calls=total_calls,
            units=units,
            priced_cost_usd=round(known_cost, 6),
            unpriced_tokens=unpriced_tokens,
            estimated_split_records=estimates,
            cost_complete=unpriced_tokens == 0,
            cost_basis='current-catalog-estimate',
            request_start_time_records=time_estimates,
            tier_context_unknown_records=tier_unknown,
            provider_reported_cost_records=reported,
        ),
    )


def gather(args):
    now = sources.timestamp(args.as_of) if args.as_of else dt.datetime.now(sources.UTC).timestamp()
    if now is None:
        raise ValueError('--as-of must be an ISO timestamp')
    windows = [float(w) for w in (args.days or args.windows).split(',')]
    if not windows or any(not math.isfinite(w) or w <= 0 for w in windows):
        raise ValueError('windows must be positive finite days')
    split = tuple(float(x) for x in args.split.split(','))
    if len(split) != 3 or any(not math.isfinite(x) or x < 0 for x in split) or abs(sum(split) - 1) > 1e-6:
        raise ValueError('--split needs 3 non-negative ratios summing to 1')
    cut = now - max(windows) * 86400
    # Adjacent evidence resolves requests crossing a reporting boundary; final
    # output uses [cut, now), independently of scan time or ledger retention.
    e = sources.Evidence(cut - 86400, now + 86400 if args.as_of else now)

    cache = None if args.no_ledger or args.no_cache else args.ledger + '.scan-cache.sqlite'
    for kind, src in [('rollout-openclaw', 'openclaw'), ('rollout-manual', 'codex-cli(手动)')]:
        sources.cached_scan(e, lambda x, k=kind, s=src: sources.scan_rollouts(x, [(k, s)]), [kind], cache)
    sources.cached_scan(e, scan_compactions, ['rollout-openclaw', 'rollout-manual'], cache)
    sources.cached_scan(e, sources.scan_claude, ['claude'], cache)
    sources.scan_gateway(e, 'claude-gateway', split)
    sources.scan_gateway(e, 'agy-gateway', split)
    sources.cached_scan(e, lambda x: sources.scan_trajectories(x, include_files=False), ['trajectory-db'], cache)
    sources.cached_scan(e, lambda x: sources.scan_trajectories(x, include_db=False), ['trajectory-file'], cache)
    sources.scan_agy(e)
    scan_muse(e)
    sources.cached_scan(e, scan_zcode, ['zcode-db', 'zcode-rollout'], cache)
    scan_grok(e)
    scan_codex_devloop(e)
    if not args.no_ledger:
        sources.persist(e, args.ledger, hydrate=True)
    e.since, e.until = cut, now

    sorted_windows = sorted(set(windows))
    for w in sorted_windows:
        sources.scan_dalton_prod(
            e,
            window_days=w,
            window_since=now - w * 86400,
            window_until=now,
        )
    return e, sorted_windows


def render(out):
    lines = []
    for w in out['windows']:
        lines.append('%gD  %s → %s' % (w['days'], w['from'], w['to']))
        lines.append('source | provider/model | records | fresh | cacheW | cacheR | output | estimated USD')
        for r in w['rows']:
            cost_disp = 'n/a' if r['cost_usd'] is None else '%.4f' % r['cost_usd']
            lines.append(
                '%s | %s/%s | %s | %s | %s | %s | %s | %s'
                % (r['source'], r['provider'], r['model'], r['records'], r['fresh'], r['cache_write'], r['cache_read'], r['output'], cost_disp)
            )
        t = w['totals']
        lines.append('TOTAL (all %d rows): %s tokens, priced estimate $%.4f, unpriced %s tokens' % (w['row_count'], t['tokens'], t['priced_cost_usd'], t['unpriced_tokens']))
        lines.append('')
    dp = out.get('dalton_prod') or {}
    if not dp.get('ok'):
        lines.append('Dalton 生产用量读取失败：%s' % (dp.get('error') or '未知错误'))
    lines.append('Coverage: %s; %d unresolved aggregate observations excluded (may overlap counted records).' % (out['coverage']['status'], out['coverage']['unresolved_aggregate_records']))
    for k, v in out['coverage']['issues'].items():
        lines.append('! %s: %s' % (k, v))
    lines.extend(NOTES)
    return '\n'.join(lines)


def command(args):
    if args.cmd == 'prices':
        prices, catalog_meta = load_price_catalog(args.prices)
        out = {
            'ok': True,
            'version': VERSION,
            'basis': 'current-catalog-estimate',
            'price_catalog': catalog_meta,
            'prices': {p + '/' + m: c for (p, m), c in sorted(prices.items())},
        }
    elif args.cmd == 'self-test':
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='test*.py')
        import io
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        out = {
            'ok': result.wasSuccessful() and result.testsRun > 0,
            'version': VERSION,
            'tests_run': result.testsRun,
            'detail': stream.getvalue(),
            'hint': 'self-test validates accounting fixtures; run summary to inspect actual source coverage',
        }
    else:
        e, windows = gather(args)
        prices, catalog_meta = load_price_catalog(args.prices)
        records, held = sources.reconcile(e)
        records = [r for r in records if e.since <= r['ts'] < e.until]
        held = [r for r in held if e.since <= r['ts'] < e.until]
        out = {
            'ok': True,
            'version': VERSION,
            'generated_at': dt.datetime.fromtimestamp(e.until, sources.UTC).isoformat(),
            'price_catalog': catalog_meta,
            'dalton_prod': e.dalton_prod or {'ok': False, 'error': 'not queried', 'models': 0, 'runs': 0, 'calls': 0},
            'windows': [window_result(records, e.until, w, prices, args.top, args.by_model) for w in windows],
            'coverage': {
                'status': 'partial' if e.issues or held or any(v == 0 for v in e.inventory.values()) else 'local-observations-only',
                'complete': False,
                'inventory': e.inventory,
                'issues': dict(sorted(e.issues.items())),
                'examples': e.examples,
                'stats': dict(e.stats),
                'unresolved_aggregate_records': len(held),
                'unresolved_aggregate_tokens_not_additive': sum(sum(r['v']) for r in held),
            },
            'notes': NOTES,
        }
        if args.audit_records:
            target = Path(args.audit_records).expanduser()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({'included': records, 'unresolved': held}, ensure_ascii=False, separators=(',', ':')))
            os.chmod(target, 0o600)
    print(json.dumps(out, ensure_ascii=False, indent=1) if args.json or args.cmd != 'summary' else render(out))
    return 0 if out['ok'] else 1


def main():
    p = argparse.ArgumentParser(description='Reconciled local + Dalton model usage and explicitly estimated cost')
    p.add_argument('--version', action='version', version=VERSION)
    p.add_argument('--prices', default=None)
    sub = p.add_subparsers(dest='cmd')
    s = sub.add_parser('summary')
    s.add_argument('--windows', default='1,7,30')
    s.add_argument('--days')
    s.add_argument('--as-of')
    s.add_argument('--split', default=','.join(map(str, DEFAULT_SPLIT)))
    s.add_argument('--ledger', default=default_ledger_path())
    s.add_argument('--no-ledger', action='store_true')
    s.add_argument('--no-cache', action='store_true')
    s.add_argument('--top', type=int, default=0)
    s.add_argument('--by-model', action='store_true')
    s.add_argument('--audit-records')
    sub.add_parser('prices')
    sub.add_parser('self-test')
    argv = [a for a in sys.argv[1:] if a != '--json']
    use_json = '--json' in sys.argv
    if not argv or (not any(a in argv for a in ('summary', 'prices', 'self-test', '--help', '-h', '--version'))):
        argv += ['summary']
    args = p.parse_args(argv)
    args.json = use_json
    if getattr(args, 'top', 0) < 0:
        raise ValueError('--top must be non-negative')
    return command(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as error:
        print(json.dumps({'ok': False, 'error': str(error), 'hint': 'Check source readability/schema and CLI arguments. A failed source is not a successful zero-usage report.'}, ensure_ascii=False))
        sys.exit(1)
