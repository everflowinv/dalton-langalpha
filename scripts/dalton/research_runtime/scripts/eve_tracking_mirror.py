#!/usr/bin/env python3
"""把发到飞书 Eve tracking 群的卡片，同步一份到 Discord 私聊。

各发送脚本在飞书返回 code=0 之后调用 mirror_after_feishu()。同步失败只写日志和
stderr，飞书那边已经发成功，调用方不能因此重发飞书。目标和开关在
config/eve_tracking_mirror.json；单次跳过设环境变量 EVE_TRACKING_MIRROR=0。

    python3 scripts/eve_tracking_mirror.py preview --file digest.md --title "标题"
    python3 scripts/eve_tracking_mirror.py preview --card-json-file card.json
    python3 scripts/eve_tracking_mirror.py send --card-json-file card.json --label 测试
    python3 scripts/eve_tracking_mirror.py self-test
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

_DEFAULT_CODE_ROOT = Path(__file__).resolve().parents[1]


def _resolve_research_roots(default_code_root: Path = _DEFAULT_CODE_ROOT) -> tuple[Path, Path]:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    # Legacy compat: when new vars are unset and DALTON_OCW_ROOT is set, keep old single-root layout.
    if raw_ocw and not raw_code and not raw_home:
        ocw = Path(raw_ocw).expanduser().resolve()
        return ocw, ocw
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code_root.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return code_root, home_root


def _resolve_config_path() -> Path:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    if raw_ocw and not raw_code and not raw_home:
        return Path(raw_ocw).expanduser().resolve() / "config" / "eve_tracking_mirror.json"
    runtime_cfg = DATA_ROOT / "config" / "eve_tracking_mirror.json"
    if runtime_cfg.is_file():
        return runtime_cfg
    return CODE_ROOT / "config" / "eve_tracking_mirror.json"


CODE_ROOT, DATA_ROOT = _resolve_research_roots()
WS = DATA_ROOT
CONFIG = _resolve_config_path()
STATE = DATA_ROOT / 'state/eve-tracking-mirror'
MAX_CHARS = 1900
# channels.discord.maxLinesPerMessage 默认 17，超过会被通道按长度硬切，切口可能落在段落中间
MAX_LINES = 17
SOURCE_NOTE = '同步自飞书 Eve tracking 群'

TAG_RE = re.compile(r'</?(?:font|text_tag|span|b|i|u|strong|em)\b[^>]*>', re.I)
AT_RE = re.compile(r'<at\b[^>]*>.*?</at>|<at\b[^>]*/?>', re.I)
BR_RE = re.compile(r'<br\s*/?>', re.I)
LINK_RE = re.compile(r'\[([^\]\n]+)\]\((?!<)(https?://[^)\s]+)\)')
URL_CHARS = r"[A-Za-z0-9\-._~:/?#@!$&'*+,;=%]"
BARE_URL_RE = re.compile(r'(?<![<(])\bhttps?://' + URL_CHARS + '+')
DEEP_HEADING_RE = re.compile(r'^#{4,}\s*(.+?)\s*#*\s*$', re.M)
TABLE_SEP_RE = re.compile(r'^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$')
HEADING_RE = re.compile(r'^(?:#{1,3}\s+\S.*|\*\*[^*\n]+\*\*[^\n]{0,30})$')


def load_config():
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        return {'enabled': False}


def should_mirror(chat_id, receive_id_type='chat_id', config=None):
    config = load_config() if config is None else config
    if not config.get('enabled') or os.environ.get('EVE_TRACKING_MIRROR') == '0':
        return False
    if receive_id_type not in (None, '', 'chat_id'):
        return False
    chat_id = (chat_id or '').strip()
    if chat_id.startswith('chat:'):
        chat_id = chat_id[len('chat:'):]
    return bool(chat_id) and chat_id == config.get('source_chat_id')


# ─── 飞书卡片 → Discord 文本 ─────────────────────────────────────────

def _table_to_bullets(lines):
    cells = [[c.strip() for c in line.strip().strip('|').split('|')] for line in lines]
    header = None
    if len(cells) >= 2 and TABLE_SEP_RE.match(lines[1]):
        header, cells = cells[0], cells[2:]
    out = []
    for row in cells:
        if header:
            parts = [f'{h}：{v}' if h else v for h, v in zip(header, row) if v]
        else:
            parts = [v for v in row if v]
        if parts:
            out.append('- ' + ' · '.join(parts))
    return out


def _convert_tables(text):
    lines, out, i = text.split('\n'), [], 0
    while i < len(lines):
        if lines[i].lstrip().startswith('|') and lines[i].rstrip().endswith('|'):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith('|'):
                j += 1
            out.extend(_table_to_bullets(lines[i:j]))
            i = j
        else:
            out.append(lines[i])
            i += 1
    return '\n'.join(out)


def _bare_url(match):
    url = match.group(0)
    tail = ''
    while url and url[-1] in ".,;:!?'":
        tail = url[-1] + tail
        url = url[:-1]
    return f'<{url}>{tail}'


def feishu_md_to_discord(text):
    text = BR_RE.sub('\n', text or '')
    text = AT_RE.sub('', text)
    text = TAG_RE.sub('', text)
    text = _convert_tables(text)
    text = DEEP_HEADING_RE.sub(lambda m: f'**{m.group(1)}**', text)
    text = LINK_RE.sub(lambda m: f'[{m.group(1)}](<{m.group(2)}>)', text)
    text = BARE_URL_RE.sub(_bare_url, text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _content(node):
    if isinstance(node, dict):
        return node.get('content') or ''
    return node if isinstance(node, str) else ''


def _render(el, blocks, images):
    if not isinstance(el, dict):
        return
    tag = el.get('tag')
    if tag in ('markdown', 'lark_md', 'plain_text'):
        blocks.append(el.get('content') or '')
    elif tag == 'div':
        blocks.append(_content(el.get('text')))
        for field in el.get('fields') or []:
            blocks.append(_content(field.get('text')))
    elif tag == 'hr':
        blocks.append('')
    elif tag == '__img__':
        if el.get('path'):
            images.append(el['path'])
    elif tag == 'collapsible_panel':
        blocks.append(_content((el.get('header') or {}).get('title')))
        for child in el.get('elements') or []:
            _render(child, blocks, images)
    elif tag == 'column_set':
        for column in el.get('columns') or []:
            for child in column.get('elements') or []:
                _render(child, blocks, images)
    elif tag == 'table':
        names = [(c.get('name'), c.get('display_name') or c.get('name')) for c in el.get('columns') or []]
        for row in el.get('rows') or []:
            parts = [f'{label}：{row.get(key)}' for key, label in names if row.get(key) not in (None, '')]
            if parts:
                blocks.append('- ' + ' · '.join(str(p) for p in parts))
    else:
        for child in el.get('elements') or []:
            _render(child, blocks, images)


def card_to_text(card):
    """返回 (Discord 文本, 本地图片路径)。图片要在飞书上传替换前取，之后只剩 img_key。"""
    header = card.get('header') or {}
    title = _content(header.get('title')).strip()
    meta = [_content(header.get('subtitle')).strip()]
    meta += [_content(t.get('text')).strip() for t in header.get('text_tag_list') or []]
    meta = [m for m in meta if m] + [SOURCE_NOTE]
    head = (f'**{title}**\n' if title else '') + '-# ' + ' · '.join(meta)
    body = card.get('body')
    elements = body.get('elements') if isinstance(body, dict) else card.get('elements')
    blocks, images = [], []
    for el in elements or []:
        _render(el, blocks, images)
    text = '\n\n'.join(feishu_md_to_discord(b) for b in blocks if b and b.strip())
    return feishu_md_to_discord(head) + '\n\n' + text, images


# ─── 切条 ─────────────────────────────────────────────────────────────

def _split_long_line(line, max_chars):
    out = []
    while len(line) > max_chars:
        window = line[:max_chars]
        cut = max(window.rfind(p) for p in ('。', '；', ';', '，', ', ', ' '))
        cut = cut + 1 if cut >= max_chars // 2 else max_chars
        out.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    out.append(line)
    return out


def chunk(text, max_chars=MAX_CHARS, max_lines=MAX_LINES):
    """按段落装箱：每条不超过 max_chars 字符、max_lines 行，小标题不落在一条的末尾。"""
    pieces = []
    for para in re.split(r'\n\s*\n', text):
        if not para.strip():
            continue
        cur = []
        for line in para.split('\n'):
            for seg in _split_long_line(line, max_chars):
                if cur and (len('\n'.join(cur + [seg])) > max_chars or len(cur) + 1 > max_lines):
                    pieces.append('\n'.join(cur))
                    cur = []
                cur.append(seg)
        if cur:
            pieces.append('\n'.join(cur))

    def size(group):
        joined = '\n\n'.join(group)
        return len(joined), joined.count('\n') + 1

    messages, cur = [], []
    for piece in pieces:
        chars, lines = size(cur + [piece])
        if cur and (chars > max_chars or lines > max_lines):
            carry = []
            if len(cur) > 1 and HEADING_RE.match(cur[-1]) and size([cur[-1], piece])[0] <= max_chars \
                    and size([cur[-1], piece])[1] <= max_lines:
                carry = [cur.pop()]
            messages.append('\n\n'.join(cur))
            cur = carry
        cur.append(piece)
    if cur:
        messages.append('\n\n'.join(cur))
    return messages


# ─── 发送 ─────────────────────────────────────────────────────────────

def _log(entry):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / 'log.jsonl', 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def _stage_image(path):
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        return None
    if p.is_relative_to(DATA_ROOT) or p.is_relative_to(CODE_ROOT):
        return p
    staged = STATE / 'media' / f'{uuid.uuid4().hex[:8]}-{p.name}'
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, staged)
    return staged


def _import_cron_command_tasks():
    try:
        import cron_command_tasks as cct
        return cct
    except ImportError:
        for cand in (
            CODE_ROOT / 'scripts',
            DATA_ROOT / 'scripts',
            Path(__file__).resolve().parents[2] / 'data_assets' / 'scripts',
        ):
            if (cand / 'cron_command_tasks.py').is_file() and str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
                import cron_command_tasks as cct
                return cct
        raise


def mirror_card(card, label='', config=None, sender=None):
    """把卡片发到配置里的每个目标。不抛异常，返回结果摘要。"""
    config = load_config() if config is None else config
    entry = {'ts': datetime.now().astimezone().isoformat(timespec='seconds'), 'label': label}
    try:
        text, images = card_to_text(card)
        messages = chunk(text)
        media = [m for m in (_stage_image(p) for p in images) if m]
    except Exception as exc:  # noqa: BLE001
        entry.update(status='failed', error=f'渲染失败：{exc}')
        _log(entry)
        print(f'[eve-tracking-mirror] Discord 同步失败（飞书已发送成功，不要重发飞书）：{entry["error"]}',
              file=sys.stderr)
        return entry
    run_dir = STATE / 'receipts' / (datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6])
    entry.update(messages_total=len(messages), media_total=len(media), sent=[])
    try:
        cct = _import_cron_command_tasks()  # 复用 cron 那套 openclaw message send 回执校验
        if DATA_ROOT.is_dir():
            cct.WS = DATA_ROOT
        send = sender or cct.send
        for target in config.get('targets') or []:
            items = [cct.message(target['channel'], target['target'], m) for m in messages]
            items += [cct.message(target['channel'], target['target'], '', str(p)) for p in media]
            for item in items:
                receipt = send(item, run_dir)
                entry['sent'].append({'target': target['target'],
                                      'messageId': (receipt or {}).get('messageId')})
    except Exception as exc:  # noqa: BLE001
        entry.update(status='failed', error=f'{type(exc).__name__}: {exc}', receipts=str(run_dir))
        _log(entry)
        print(f'[eve-tracking-mirror] Discord 同步失败，已发 {len(entry["sent"])} 条'
              f'（飞书已发送成功，不要重发飞书）：{entry["error"]}', file=sys.stderr)
        return entry
    shutil.rmtree(run_dir, ignore_errors=True)
    entry['status'] = 'sent'
    _log(entry)
    print(f'[eve-tracking-mirror] 已同步到 Discord，共 {len(entry["sent"])} 条', file=sys.stderr)
    return entry


def mirror_after_feishu(chat_id, receive_id_type, card, label=''):
    """飞书发送成功后调用；不是 Eve tracking 群就返回 None。不抛异常。"""
    try:
        if not should_mirror(chat_id, receive_id_type):
            return None
        return mirror_card(copy.deepcopy(card), label=label)
    except Exception as exc:  # noqa: BLE001
        print(f'[eve-tracking-mirror] Discord 同步异常（飞书已发送成功，不要重发飞书）：{exc}', file=sys.stderr)
        return {'status': 'failed', 'error': str(exc)}


# ─── CLI ──────────────────────────────────────────────────────────────

def _load_cards(args):
    import feishu_send_card
    if args.card_json_file:
        card = json.loads(Path(args.card_json_file).read_text())
        splitter = getattr(feishu_send_card, 'split_feishu_card_to_chunks', None)
        return splitter(card) if callable(splitter) else [card]
    content = Path(args.file).read_text(encoding='utf-8')
    chunker = getattr(feishu_send_card, 'markdown_to_card_chunks', None)
    if callable(chunker):
        return chunker(markdown=content, title=args.title or '', subtitle=args.subtitle or '')
    return [feishu_send_card.markdown_to_card_elements(markdown=content, title=args.title or '',
                                                       subtitle=args.subtitle or '')]


def _load_card(args):
    return _load_cards(args)[0]


def self_test():
    card = {
        'header': {'title': {'content': '每日公告提醒'}, 'subtitle': {'content': '2026-10-27'},
                   'text_tag_list': [{'text': {'content': '重点 1'}}]},
        'body': {'elements': [
            {'tag': 'markdown', 'content': "**ATAT** <font color='grey'>亚朵</font> [10-27](https://sec.gov/a)"},
            {'tag': 'hr'},
            {'tag': 'collapsible_panel', 'header': {'title': {'content': '**例行 · 2 家**'}},
             'elements': [{'tag': 'markdown', 'content': '| 代码 | 金额 |\n|---|---|\n| META | 1.2 |'}]},
            {'tag': '__img__', 'path': '/nonexistent/x.png'},
        ]},
    }
    text, images = card_to_text(card)
    assert text.startswith('**每日公告提醒**\n-# 2026-10-27 · 重点 1 · ' + SOURCE_NOTE), text
    assert '<font' not in text and '[10-27](<https://sec.gov/a>)' in text, text
    assert '- 代码：META · 金额：1.2' in text and images == ['/nonexistent/x.png'], text
    assert feishu_md_to_discord('见 https://x.com/a/1。下') == '见 <https://x.com/a/1>。下'
    assert feishu_md_to_discord('#### 小节') == '**小节**'

    long_text = '\n\n'.join(f'**第{i}节**\n\n' + '\n'.join(f'- 要点{i}-{k} ' + '字' * 120 for k in range(4))
                            for i in range(8))
    msgs = chunk(long_text)
    assert all(len(m) <= MAX_CHARS and m.count('\n') + 1 <= MAX_LINES for m in msgs), msgs
    assert not any(HEADING_RE.match(m.split('\n\n')[-1]) for m in msgs), '小标题落在末尾'
    assert ''.join(long_text.split()) == ''.join(''.join(msgs).split()), '切条丢字'
    assert all(len(m) <= 300 for m in chunk('。'.join(['字' * 50] * 30), max_chars=300))

    cfg = {'enabled': True, 'source_chat_id': 'oc_x', 'targets': [{'channel': 'discord', 'target': 'user:1'}]}
    assert should_mirror('chat:oc_x', 'chat_id', cfg) and not should_mirror('oc_y', 'chat_id', cfg)
    assert not should_mirror('oc_x', 'open_id', cfg)
    assert not should_mirror('oc_x', 'chat_id', {**cfg, 'enabled': False})

    global STATE
    import tempfile
    saved, STATE = STATE, Path(tempfile.mkdtemp(prefix='eve-mirror-selftest-'))
    try:
        calls = []
        ok = mirror_card(card, 'self-test', cfg, sender=lambda item, d: calls.append(item) or {'messageId': '1'})
        assert ok['status'] == 'sent' and len(calls) == ok['messages_total'] >= 1, ok

        def boom(item, d):
            if calls:
                raise RuntimeError('discord down')
            calls.append(item)
            return {'messageId': '1'}
        calls = []
        bad = mirror_card({'body': {'elements': [{'tag': 'markdown', 'content': '甲\n\n' + '乙' * 3000}]}},
                          'self-test', cfg, sender=boom)
        assert bad['status'] == 'failed' and len(bad['sent']) == 1, bad
    finally:
        shutil.rmtree(STATE, ignore_errors=True)
        STATE = saved
    print('self-test ok')
    return 0


def main():
    ap = argparse.ArgumentParser(description='Eve tracking 群卡片 → Discord 同步')
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('preview', 'send'):
        p = sub.add_parser(name)
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument('--card-json-file')
        src.add_argument('--file', help='Markdown，按 feishu_send_card.py 的规则先转成卡片')
        p.add_argument('--title', default='')
        p.add_argument('--subtitle', default='')
        p.add_argument('--label', default='manual')
    sub.add_parser('self-test')
    args = ap.parse_args()
    if args.cmd == 'self-test':
        return self_test()
    cards = _load_cards(args)
    if args.cmd == 'preview':
        msgs: list[str] = []
        images: list[str] = []
        for card in cards:
            text, card_images = card_to_text(card)
            images.extend(card_images)
            msgs.extend(chunk(text))
        for i, m in enumerate(msgs, 1):
            print(f'===== {i}/{len(msgs)} · {len(m)} 字符 · {m.count(chr(10)) + 1} 行')
            print(m)
        if images:
            print('===== 图片：' + ', '.join(images))
        return 0
    results = [mirror_card(card, label=args.label) for card in cards]
    result = results[0] if len(results) == 1 else {
        'status': 'sent' if all(r.get('status') == 'sent' for r in results) else 'failed',
        'results': results,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('status') == 'sent' else 1


if __name__ == '__main__':
    sys.exit(main())
