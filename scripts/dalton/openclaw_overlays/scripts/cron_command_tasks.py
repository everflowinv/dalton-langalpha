#!/usr/bin/env python3
"""Deterministic adapters for cron command tasks without LLM or shell interpolation.

In Dalton runner mode (DALTON_NO_DIRECT_SEND=1 or Dalton runner environment variables),
OpenClaw CLI delivery is disabled unconditionally. For us-topbottom-dm-v23 (OC-1 T26),
Dalton mode invokes us_bottom_monitor_cli.py and us_top_monitor_cli.py directly via
DALTON_PYTHON_BIN (falling back to sys.executable) without run.sh venv/pip bootstrap,
retains per-run chart copies under .dalton-runs/<run_id>/attachments/, updates last-run.json
without touching legacy pending/inflight/receipts files, and writes a preview payload to
DALTON_OUTBOX_FILE in tmpfs.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WS = Path(__file__).resolve().parents[1]
TRACKING = 'oc_397207208fb52a0b09969794ef3cd421'
SEMI = 'oc_a49280092f9c099d429f9412ad038f65'
ASIA = 'chat:oc_a6b59323f034cb246c80e1b3684e71d1'
V_DM = 'user:1134818575211839498'
DEV = 'channel:1481256083589697566'
SIMPLE = {
    'memory-merge-daily': [['bash', 'scripts/merge_daily_memory.sh', str(p / 'memory')]
                           for p in (WS, WS.parent / 'workspace-marco')],
    'weekly-cleanup': [['bash', 'scripts/cleanup_workspace.sh']],
    'openclaw-config-backup': [['bash', 'scripts/openclaw_config_backup.sh'],
                               ['bash', 'scripts/openclaw_config_backup.sh', '--verify']],
    'daily-ops-audit': [['bash', 'scripts/daily_ops_audit.sh']],
    'softbank-nav-monitor': [['python3', 'scripts/softbank_nav_monitor.py', '--feishu', SEMI]],
    'tsmc-adr-premium': [['python3', 'scripts/tsmc_adr_premium.py', '--feishu', SEMI]],
    'skhy-premium-weekdays': [['python3', 'scripts/skhy_premium_monitor.py']],
    'weekly-token-usage': [['python3', 'scripts/token_weekly_usage_tracker.py', '--send', '--to', TRACKING]],
    'us-etf-monthly-flows': [['python3', 'scripts/etf_monthly_flows_tracker.py', '--send', '--to', TRACKING]],
}
SPECIAL = ['ai-chain-daily', 'e-watch-daily', 'pt-watchlist-asia', 'pt-watchlist-us',
           'china-2c-hk-monitor', 'china-2c-us-monitor', 'us-topbottom-dm-v23',
           'vol-surface-daily-snapshot', 'hk-momentum-weekly']
TASKS = sorted([*SIMPLE, *SPECIAL])

DALTON_WRAPPER_ENV_VARS = (
    'DALTON_OUTBOX_FILE',
    'DALTON_RUN_ID',
    'DALTON_RUN_DIR',
    'DALTON_JOB_KEY',
    'DALTON_NOTIFY_DRY_RUN',
    'DALTON_PYTHON_BIN',
)
DALTON_RUNNER_ENV_VARS = ('DALTON_NO_DIRECT_SEND', *DALTON_WRAPPER_ENV_VARS)
US_TOPBOTTOM_TASK = 'us-topbottom-dm-v23'
US_TOPBOTTOM_COLLECTORS: tuple[tuple[str, str], ...] = (
    ('us-bottom-monitor', 'skills/us-bottom-monitor/scripts/us_bottom_monitor_cli.py'),
    ('us-top-monitor', 'skills/us-top-monitor/scripts/us_top_monitor_cli.py'),
)
US_TOPBOTTOM_SKILLS = tuple(skill for skill, _ in US_TOPBOTTOM_COLLECTORS)
ALLOWED_CHART_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.webp', '.gif'})
DEFAULT_CONTAINER_PYTHON_BIN = '/app/.venv/bin/python'
DEFAULT_TMPFS_OUTBOX_DIR = Path('/run/dalton-outbox')
DEFAULT_CONTAINER_SECRETS_DIR = Path('/run/dalton-secrets')


def is_dalton_mode() -> bool:
    """Return True when running under the Dalton data-task wrapper or DALTON_NO_DIRECT_SEND."""
    if os.environ.get('DALTON_NO_DIRECT_SEND', '').strip().lower() in ('1', 'true', 'yes'):
        return True
    return any(bool(os.environ.get(name, '').strip()) for name in DALTON_WRAPPER_ENV_VARS)


def _atomic_write_bytes(target_path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write bytes via a same-directory unique temp file with fsync and cleanup."""
    if target_path.is_symlink() or (target_path.exists() and target_path.is_dir()):
        raise ValueError(f'目标写入路径非法：{target_path}')
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f'.tmp.{target_path.name}.',
        dir=str(target_path.parent),
    )
    tmp_path = Path(tmp_name)
    fd_open = True
    try:
        fh = os.fdopen(fd, 'wb')
        fd_open = False
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target_path)
    except BaseException:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)
        raise


def _atomic_write_json(target_path: Path, value: Any) -> None:
    payload_bytes = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    _atomic_write_bytes(target_path, payload_bytes, mode=0o600)


def json_result(text, context='采集输出'):
    """Accept a pretty-printed object or final JSON object after progress lines."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r'(?m)^\s*\{', text):
        try:
            value, end = decoder.raw_decode(text[match.start():].lstrip())
            if isinstance(value, dict) and not text[match.start():].lstrip()[end:].strip():
                return value
        except ValueError:
            pass
    raise ValueError(f'{context}没有返回完整 JSON')


class DeliveryUncertain(RuntimeError):
    """A send may have happened; do not retry without reconciliation."""


def message_receipt(raw, channel):
    """Allow CLI progress after a proven receipt, never an incomplete JSON object."""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', raw)
    try:
        return json_result(text, '发送回执')
    except ValueError:
        candidates = []
        for match in re.finditer(r'(?m)^\s*\{', text):
            try:
                value, _ = json.JSONDecoder().raw_decode(text[match.start():].lstrip())
            except ValueError:
                continue
            if (isinstance(value, dict) and value.get('action') == 'send'
                    and value.get('channel') == channel and value.get('messageId')):
                candidates.append(value)
        if len(candidates) == 1:
            return candidates[0]
        raise ValueError('发送回执缺失、截断或存在多个结果')


def write_json(path, value):
    _atomic_write_json(Path(path), value)


def message(channel, target, text='', media=None):
    item = {'channel': channel, 'target': target, 'text': text}
    if media:
        p = Path(media).resolve()
        if not p.is_relative_to(WS) or not p.is_file():
            raise ValueError(f'附件不存在或不在 workspace：{p}')
        item['media'] = str(p)
    return item


SEND_TIMEOUT_SECONDS = {'e-watch-daily': 300}


def proven_text_delivery(payload, item):
    """Treat a single-part direct text send with a unique platform message ID as delivered."""
    if item.get('media') or payload.get('channel') != item['channel']:
        return False
    error = payload.get('error') or {}
    msg = error.get('message', '') if isinstance(error, dict) else str(error)
    inner = payload.get('payload') or {}
    receipt = (inner.get('result') or {}).get('receipt') or {}
    ids = receipt.get('platformMessageIds') or []
    parts = receipt.get('parts') or []
    return bool('claim was lost' in msg and inner.get('via') == 'direct'
                and len(ids) == 1 and len(parts) == 1 and parts[0].get('kind') == 'text'
                and ids[0] == payload.get('messageId') == receipt.get('primaryPlatformMessageId')
                and receipt.get('sentAt'))


def send(item, receipt_dir=None, timeout=120):
    if is_dalton_mode():
        raise RuntimeError('Dalton 模式禁止调用 openclaw 发送消息')
    if os.environ.get('PYTEST_CURRENT_TEST'):
        raise RuntimeError('测试环境禁止调用 openclaw 真实发送消息')
    argv = ['openclaw', 'message', 'send', '--channel', item['channel'],
            '--target', item['target'], '--json']
    if item['text']:
        argv += ['--message', item['text']]
    if item.get('media'):
        argv += ['--media', item['media']]
    directory = receipt_dir or WS / 'state/cron-command/delivery-receipts'
    directory.mkdir(parents=True, exist_ok=True)
    stem = directory / (datetime.now().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex)

    def preserve(stdout, stderr, code):
        paths = {}
        for suffix, value in (('stdout', stdout), ('stderr', stderr)):
            path = stem.with_suffix('.' + suffix)
            path.write_bytes(value if isinstance(value, bytes) else (value or '').encode())
            path.chmod(0o600)
            paths[suffix] = str(path)
        write_json(stem.with_suffix('.json'), {'returncode': code, **paths})

    try:
        result = subprocess.run(argv, cwd=WS, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        preserve(exc.stdout, exc.stderr, 'timeout')
        raise DeliveryUncertain(f'发送超时，是否送达待核对；回执 {stem}') from exc
    preserve(result.stdout, result.stderr, result.returncode)
    try:
        payload = message_receipt(result.stdout, item['channel'])
    except ValueError as exc:
        raise DeliveryUncertain(f'发送回执不完整，是否送达待核对；原始输出 {stem}') from exc
    if payload.get('sentBeforeError') or payload.get('deliveryStatus') in ('partial', 'partial_failed', 'unknown'):
        if proven_text_delivery(payload, item):
            return payload
        raise DeliveryUncertain(f'消息可能已部分送达，核对后再续发；回执 {stem}')
    if result.returncode:
        raise RuntimeError('消息发送失败：' + (result.stderr or result.stdout)[-1800:])

    def failed(obj):
        if isinstance(obj, dict):
            if obj.get('ok') is False or obj.get('success') is False or obj.get('status') in ('error', 'failed'):
                return True
            if isinstance(obj.get('code'), int) and obj['code'] != 0:
                return True
            return any(failed(v) for v in obj.values())
        return isinstance(obj, list) and any(failed(v) for v in obj)

    if failed(payload):
        raise RuntimeError('消息服务返回失败：' + json.dumps(payload, ensure_ascii=False)[-1800:])
    return payload


def _resolve_dalton_workspace() -> tuple[Path, Path]:
    raw_ws = (
        os.environ.get('OPENCLAW_WORKSPACE', '').strip()
        or os.environ.get('WORKDIR', '').strip()
    )
    if raw_ws:
        raw_path = Path(raw_ws)
        if raw_path.is_symlink():
            raise ValueError(f'workspace 路径不能为软链接：{raw_path}')
        if not raw_path.exists() or not raw_path.is_dir():
            raise ValueError(f'workspace 目录不存在：{raw_path}')
        return raw_path.resolve(strict=True), raw_path
    if not WS.exists() or not WS.is_dir():
        raise ValueError(f'workspace 目录不存在：{WS}')
    return WS.resolve(strict=True), WS


def _resolve_dalton_run_id() -> str:
    raw = os.environ.get('DALTON_RUN_ID', '').strip()
    if not raw:
        stamp = datetime.now(ZoneInfo('UTC')).strftime('%Y%m%dT%H%M%SZ')
        return f'preview-{stamp}-{uuid.uuid4().hex[:8]}'
    if (
        '\x00' in raw
        or '/' in raw
        or '\\' in raw
        or raw in ('.', '..')
        or Path(raw).is_absolute()
        or Path(raw).parts != (raw,)
    ):
        raise ValueError(f'DALTON_RUN_ID 非法：{raw!r}')
    return raw


def _resolve_noncreating_path(candidate: Path) -> Path:
    """Resolve a possibly non-existent absolute path read-only without creating any directory."""
    missing_tail: list[str] = []
    probe = candidate
    while not probe.exists():
        if probe.is_symlink():
            raise ValueError(f'路径包含悬空软链接：{probe}')
        if probe.parent == probe:
            raise ValueError(f'根路径不存在：{candidate}')
        missing_tail.append(probe.name)
        probe = probe.parent

    if missing_tail and not probe.is_dir():
        raise ValueError(f'父路径不是目录：{probe}')

    missing_tail.reverse()
    resolved_base = probe.resolve(strict=True)
    return resolved_base.joinpath(*missing_tail) if missing_tail else resolved_base


def _assert_no_ancestor_symlinks(
    candidate: Path,
    *,
    stop_at: set[Path] | None = None,
) -> None:
    """Reject any symlink component on candidate or its ancestors down to stop_at."""
    anc = candidate
    while stop_at is None or anc not in stop_at:
        if anc.is_symlink():
            raise ValueError(f'路径包含软链接：{anc}')
        if anc.parent == anc:
            break
        anc = anc.parent


def _ensure_safe_workspace_subpath(
    ws_root: Path,
    rel_parts: tuple[str, ...],
    *,
    create_parents: bool = False,
) -> Path:
    if not rel_parts:
        raise ValueError('子路径不能为空')
    cur = ws_root
    for idx, part in enumerate(rel_parts):
        if not part or part in ('.', '..') or '/' in part or '\\' in part or '\x00' in part:
            raise ValueError(f'子路径片段非法：{part!r}')
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f'拒绝软链接路径：{cur}')
        is_last = idx == len(rel_parts) - 1
        if cur.exists():
            if not is_last and not cur.is_dir():
                raise ValueError(f'父路径不是目录：{cur}')
            if not cur.resolve(strict=True).is_relative_to(ws_root):
                raise ValueError(f'路径越出 workspace：{cur}')

    if create_parents:
        parent = ws_root
        for part in rel_parts[:-1]:
            parent = parent / part
            if parent.is_symlink():
                raise ValueError(f'拒绝软链接路径：{parent}')
            parent.mkdir(parents=False, exist_ok=True)
            if not parent.resolve(strict=True).is_relative_to(ws_root):
                raise ValueError(f'路径越出 workspace：{parent}')
    return cur


def _resolve_dalton_run_dir(
    ws_root: Path,
    run_id: str,
    *,
    raw_ws: Path | None = None,
) -> Path:
    raw_run_dir = os.environ.get('DALTON_RUN_DIR', '').strip()
    if raw_run_dir:
        candidate = Path(raw_run_dir)
        if '\x00' in raw_run_dir or '..' in candidate.parts or candidate.is_symlink():
            raise ValueError(f'DALTON_RUN_DIR 非法：{raw_run_dir!r}')
        if not candidate.is_absolute():
            rel_parts = tuple(p for p in candidate.parts if p not in ('', '.'))
            if not rel_parts:
                raise ValueError(f'DALTON_RUN_DIR 必须位于 workspace 内：{raw_run_dir}')
            run_dir = _ensure_safe_workspace_subpath(ws_root, rel_parts, create_parents=True)
        else:
            resolved_candidate = _resolve_noncreating_path(candidate)
            if not resolved_candidate.is_relative_to(ws_root) or resolved_candidate == ws_root:
                raise ValueError(f'DALTON_RUN_DIR 必须位于 workspace 内：{raw_run_dir}')
            ws_ancestors = {ws_root, *ws_root.parents}
            if raw_ws is not None:
                ws_ancestors.update({raw_ws, *raw_ws.parents})
            _assert_no_ancestor_symlinks(candidate, stop_at=ws_ancestors)
            rel_parts = tuple(resolved_candidate.relative_to(ws_root).parts)
            run_dir = _ensure_safe_workspace_subpath(ws_root, rel_parts, create_parents=True)
    else:
        run_dir = _ensure_safe_workspace_subpath(
            ws_root,
            ('.dalton-runs', run_id),
            create_parents=True,
        )

    if run_dir.is_symlink() or (run_dir.exists() and not run_dir.is_dir()):
        raise ValueError(f'运行目录非法：{run_dir}')
    run_dir.mkdir(parents=True, exist_ok=True)
    resolved_run_dir = run_dir.resolve(strict=True)
    if not resolved_run_dir.is_relative_to(ws_root) or resolved_run_dir == ws_root:
        raise ValueError(f'运行目录越出 workspace：{run_dir}')
    return resolved_run_dir


def _resolve_staging_outbox_path(run_dir: Path, run_id: str) -> Path:
    raw_outbox = os.environ.get('DALTON_OUTBOX_FILE', '').strip()
    if raw_outbox:
        outbox_path = Path(raw_outbox)
        if '\x00' in raw_outbox or '..' in outbox_path.parts:
            raise ValueError(f'DALTON_OUTBOX_FILE 非法：{raw_outbox!r}')
    elif DEFAULT_TMPFS_OUTBOX_DIR.is_dir() and os.access(DEFAULT_TMPFS_OUTBOX_DIR, os.W_OK):
        outbox_path = DEFAULT_TMPFS_OUTBOX_DIR / 'outbox.json'
    else:
        outbox_path = Path(tempfile.gettempdir()) / f'dalton-outbox-{run_id}' / 'outbox.json'

    abs_outbox = outbox_path if outbox_path.is_absolute() else (Path.cwd() / outbox_path)
    if (
        outbox_path.is_symlink()
        or abs_outbox.is_symlink()
        or abs_outbox.parent.is_symlink()
        or (abs_outbox.exists() and abs_outbox.is_dir())
    ):
        raise ValueError(f'DALTON_OUTBOX_FILE 目标非法：{outbox_path}')

    sys_temp_root = Path(tempfile.gettempdir())
    allowed_roots = {
        DEFAULT_TMPFS_OUTBOX_DIR,
        *DEFAULT_TMPFS_OUTBOX_DIR.parents,
        sys_temp_root,
        *sys_temp_root.parents,
        Path('/tmp'),
        Path('/var/tmp'),
        Path('/var'),
        *run_dir.parents[1:],
    }
    _assert_no_ancestor_symlinks(abs_outbox, stop_at=allowed_roots)
    resolved_outbox = _resolve_noncreating_path(abs_outbox)
    persisted_outbox = run_dir.resolve(strict=True) / 'outbox.json'
    if resolved_outbox == persisted_outbox:
        raise ValueError('DALTON_OUTBOX_FILE 不能直接指向持久化运行目录 outbox.json')

    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    return outbox_path


def _assert_no_symlinks_under_base(base: Path, rel_parts: tuple[str, ...]) -> None:
    cur = base
    for part in rel_parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f'附件路径包含软链接：{cur}')


def _validate_workspace_chart(chart_raw: Any, ws_root: Path, raw_ws: Path) -> Path:
    if not isinstance(chart_raw, str) or not chart_raw.strip() or '\x00' in chart_raw:
        raise ValueError('采集输出缺少有效 chart')
    raw_p = Path(chart_raw.strip())
    if '..' in raw_p.parts:
        raise ValueError(f'附件路径包含越界片段：{chart_raw}')

    if not raw_p.is_absolute():
        rel_parts = tuple(p for p in raw_p.parts if p not in ('', '.'))
        if not rel_parts:
            raise ValueError(f'附件不存在或不在 workspace：{chart_raw}')
        _assert_no_symlinks_under_base(ws_root, rel_parts)
        candidate = ws_root.joinpath(*rel_parts)
    else:
        if raw_p.is_symlink():
            raise ValueError(f'附件不能为软链接：{raw_p}')
        if raw_p.is_relative_to(raw_ws):
            _assert_no_symlinks_under_base(raw_ws, tuple(raw_p.relative_to(raw_ws).parts))
        if raw_p.is_relative_to(ws_root):
            _assert_no_symlinks_under_base(ws_root, tuple(raw_p.relative_to(ws_root).parts))
        candidate = raw_p

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f'附件不存在或不在 workspace：{candidate}') from exc

    if not resolved.is_relative_to(ws_root) or resolved == ws_root:
        raise ValueError(f'附件不存在或不在 workspace：{resolved}')
    _assert_no_symlinks_under_base(ws_root, tuple(resolved.relative_to(ws_root).parts))

    suffix = resolved.suffix.lower()
    if suffix not in ALLOWED_CHART_SUFFIXES:
        raise ValueError(f'附件格式不支持：{resolved}')

    open_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(resolved, open_flags)
    except OSError as exc:
        raise ValueError(f'附件无法读取：{resolved}') from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f'附件不存在或不在 workspace：{resolved}')
        if st.st_size <= 0:
            raise ValueError(f'附件为空文件：{resolved}')
    finally:
        os.close(fd)
    return resolved


def _atomic_copy_chart(source_path: Path, dest_path: Path) -> tuple[int, str]:
    open_flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(source_path, open_flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            raise ValueError(f'附件非法或为空：{source_path}')
        chunks: list[bytes] = []
        hasher = hashlib.sha256()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            hasher.update(chunk)
        data = b''.join(chunks)
    finally:
        os.close(fd)
    if not data:
        raise ValueError(f'附件为空文件：{source_path}')
    _atomic_write_bytes(dest_path, data, mode=0o600)
    return len(data), hasher.hexdigest()


def _load_mounted_secret_patterns() -> tuple[str, ...]:
    """Load mounted container secret values when readable so workspace state never persists them."""
    raw_secrets_dir = os.environ.get('DALTON_SECRETS_DIR', '').strip()
    secrets_dir = Path(raw_secrets_dir) if raw_secrets_dir else DEFAULT_CONTAINER_SECRETS_DIR
    if not secrets_dir.is_dir():
        return ()
    values: set[str] = set()
    try:
        entries = sorted(secrets_dir.iterdir())
    except OSError:
        return ()
    for entry in entries:
        if not entry.is_file() or entry.name.startswith('.'):
            continue
        try:
            raw = entry.read_text(encoding='utf-8')
        except OSError:
            continue
        for candidate in (raw, raw.rstrip('\r\n'), raw.strip()):
            if len(candidate) >= 4:
                values.add(candidate)
    return tuple(sorted(values, key=len, reverse=True))


def _redact_obj(obj: Any, patterns: tuple[str, ...]) -> Any:
    if not patterns:
        return obj
    if isinstance(obj, str):
        out = obj
        for pat in patterns:
            if pat in out:
                out = out.replace(pat, '[REDACTED]')
        return out
    if isinstance(obj, dict):
        return {_redact_obj(k, patterns): _redact_obj(v, patterns) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v, patterns) for v in obj]
    return obj


def _resolve_dalton_python_bin() -> str:
    """Prefer DALTON_PYTHON_BIN when valid; fall back to sys.executable on local hosts without /app/.venv."""
    configured = os.environ.get('DALTON_PYTHON_BIN', '').strip()
    if configured:
        if '\x00' in configured:
            raise ValueError('DALTON_PYTHON_BIN 包含非法空字符')
        if '/' in configured or '\\' in configured:
            cand = Path(configured)
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
            if configured != DEFAULT_CONTAINER_PYTHON_BIN:
                return configured
        else:
            found = shutil.which(configured)
            return found or configured
    return sys.executable or 'python3'


def _collect_dalton_skill_output(
    cli_rel_path: str,
    ws_root: Path,
    raw_ws: Path,
) -> tuple[str, Path]:
    cli_parts = tuple(p for p in Path(cli_rel_path).parts if p not in ('', '.'))
    cli_file = _ensure_safe_workspace_subpath(ws_root, cli_parts, create_parents=False)
    if not cli_file.is_file():
        raise RuntimeError(f'{cli_rel_path} 不存在：{cli_file}')

    python_bin = _resolve_dalton_python_bin()
    argv = [python_bin, cli_rel_path, '--json', 'run']
    try:
        result = subprocess.run(argv, cwd=ws_root, capture_output=True, text=True, timeout=540)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f'{cli_rel_path} 超时（timeout 540s）') from exc
    except OSError as exc:
        raise RuntimeError(f'{cli_rel_path} 启动失败：{exc}') from exc

    raw = result.stdout or ''
    stderr = result.stderr or ''
    if result.returncode != 0 or 'feishu card send returned' in stderr:
        raise RuntimeError(
            f'{cli_rel_path} 失败（exit {result.returncode}）：\n' + (raw + '\n' + stderr)[-2400:]
        )

    value = json_result(raw)
    if value.get('ok') is False or value.get('status') == 'error':
        raise RuntimeError(str(value.get('error') or value.get('hint') or value))

    msg_text = value.get('feishu_message')
    if not isinstance(msg_text, str) or not msg_text.strip():
        raise ValueError('采集输出缺少有效 feishu_message')

    chart_path = _validate_workspace_chart(value.get('chart'), ws_root, raw_ws)
    return msg_text, chart_path


def run_dalton_us_topbottom_preview() -> int:
    """Run fresh us-bottom-monitor and us-top-monitor CLIs directly and emit Dalton preview artifacts."""
    ws_root, raw_ws = _resolve_dalton_workspace()
    run_id = _resolve_dalton_run_id()
    run_dir = _resolve_dalton_run_dir(ws_root, run_id, raw_ws=raw_ws)
    staging_outbox_path = _resolve_staging_outbox_path(run_dir, run_id)

    state_rel = ('state', 'cron-command', US_TOPBOTTOM_TASK)
    state_dir = _ensure_safe_workspace_subpath(ws_root, state_rel, create_parents=True)
    if state_dir.is_symlink() or (state_dir.exists() and not state_dir.is_dir()):
        raise ValueError(f'状态目录非法：{state_dir}')
    state_dir.mkdir(parents=True, exist_ok=True)
    if not state_dir.resolve(strict=True).is_relative_to(ws_root):
        raise ValueError(f'状态目录越出 workspace：{state_dir}')

    last_run_path = _ensure_safe_workspace_subpath(ws_root, (*state_rel, 'last-run.json'))
    lock_path = _ensure_safe_workspace_subpath(ws_root, (*state_rel, 'dalton-preview.lock'))
    if lock_path.is_symlink():
        raise ValueError(f'锁文件不能为软链接：{lock_path}')

    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    try:
        lock_fd = os.open(lock_path, lock_flags, 0o600)
    except OSError as exc:
        raise ValueError(f'锁文件打开失败：{lock_path}') from exc

    with os.fdopen(lock_fd, 'a+', encoding='utf-8') as lock_fh:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f'{US_TOPBOTTOM_TASK} 预览任务已在运行中') from exc

        run_rel_parts = tuple(run_dir.relative_to(ws_root).parts)
        attachments_dir = _ensure_safe_workspace_subpath(
            ws_root,
            (*run_rel_parts, 'attachments'),
            create_parents=True,
        )
        attachments_dir.mkdir(parents=True, exist_ok=True)

        collected: list[dict[str, Any]] = []
        errors: list[str] = []
        for skill, cli_rel_path in US_TOPBOTTOM_COLLECTORS:
            try:
                msg_text, src_chart = _collect_dalton_skill_output(
                    cli_rel_path,
                    ws_root,
                    raw_ws,
                )
                idx = len(collected)
                suffix = src_chart.suffix.lower()
                retained_chart = _ensure_safe_workspace_subpath(
                    ws_root,
                    (*run_rel_parts, 'attachments', f'{idx}{suffix}'),
                )
                chart_bytes, chart_sha256 = _atomic_copy_chart(src_chart, retained_chart)
                collected.append({
                    'index': idx,
                    'skill': skill,
                    'channel': 'discord',
                    'target': V_DM,
                    'text': msg_text,
                    'retained_chart': retained_chart,
                    'media_rel_run': f'attachments/{idx}{suffix}',
                    'media_rel_ws': retained_chart.relative_to(ws_root).as_posix(),
                    'chart_bytes': chart_bytes,
                    'chart_sha256': chart_sha256,
                })
            except (RuntimeError, ValueError, OSError) as exc:
                errors.append(f'{skill}：{exc}')

        error = '\n'.join(errors)
        plan_items: list[dict[str, Any]] = []
        outbox_items: list[dict[str, Any]] = []

        if collected:
            for pos, entry in enumerate(collected):
                item_text = entry['text']
                if error and pos == len(collected) - 1:
                    item_text = item_text + '\n⚠️ 另一份未取得：' + error
                plan_items.append({
                    'channel': entry['channel'],
                    'target': entry['target'],
                    'text': item_text,
                    'media': str(entry['retained_chart']),
                })
                outbox_item: dict[str, Any] = {
                    'index': pos,
                    'skill': entry['skill'],
                    'channel': entry['channel'],
                    'target': entry['target'],
                    'text': item_text,
                    'media': entry['media_rel_run'],
                    'workspace_media': entry['media_rel_ws'],
                    'chart_sha256': entry['chart_sha256'],
                    'chart_bytes': entry['chart_bytes'],
                    'delivered': False,
                }
                if item_text != entry['text']:
                    outbox_item['original_text'] = entry['text']
                outbox_items.append(outbox_item)
        else:
            total_fail_text = '顶部和底部监控均失败：\n' + error
            plan_items.append({
                'channel': 'discord',
                'target': V_DM,
                'text': total_fail_text,
            })
            outbox_items.append({
                'index': 0,
                'skill': None,
                'channel': 'discord',
                'target': V_DM,
                'text': total_fail_text,
                'media': None,
                'delivered': False,
            })

        plan = {
            'items': plan_items,
            'text': 'NO_REPLY',
            'error': error,
            'after': None,
        }
        last_run_payload = _redact_obj(
            {
                'mode': 'preview',
                'ok': not bool(error),
                'delivered': False,
                'delivery_status': 'failed' if error else 'preview_only',
                'run_id': run_id,
                'plan': plan,
            },
            _load_mounted_secret_patterns(),
        )
        outbox_payload = {
            'schema_version': 1,
            'task_code': 'T26',
            'job_key': US_TOPBOTTOM_TASK,
            'run_id': run_id,
            'mode': 'preview',
            'ok': not bool(error),
            'delivered': False,
            'delivery_status': 'failed' if error else 'preview_only',
            'channel': 'discord',
            'target': V_DM,
            'text': '\n\n'.join(item['text'] for item in outbox_items if item.get('text')),
            'error': error,
            'items': outbox_items,
        }

        _atomic_write_json(last_run_path, last_run_payload)
        _atomic_write_json(staging_outbox_path, outbox_payload)
        if error:
            print(error, file=sys.stderr)
        print(plan['text'], end='' if plan['text'].endswith('\n') else '\n')
        return 1 if error else 0


class Runner:
    def __init__(self, name, root=None):
        self.name = name
        self.directory = (root or WS / 'state/cron-command') / name
        self.path = self.directory / 'pending.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'commands': {}, 'next': 0}

    def save(self):
        write_json(self.path, self.state)

    def run(self, argv, accepted=(0,)):
        key = json.dumps(argv)
        if key in self.state['commands']:
            return self.state['commands'][key]
        result = subprocess.run(argv, cwd=WS, capture_output=True, text=True, timeout=540)
        raw = result.stdout
        if result.returncode not in accepted or 'feishu card send returned' in result.stderr:
            raise RuntimeError(f'{argv[1]} 失败（exit {result.returncode}）：\n' + (raw + '\n' + result.stderr)[-2400:])
        self.state['commands'][key] = raw
        self.save()
        return raw

    def obj(self, argv):
        raw = self.run(argv)
        try:
            value = json_result(raw)
        except ValueError:
            self.state['commands'].pop(json.dumps(argv), None)
            self.save()
            raise
        if value.get('ok') is False or value.get('status') == 'error':
            self.state['commands'].pop(json.dumps(argv), None)
            self.save()
            raise RuntimeError(str(value.get('error') or value.get('hint') or value))
        return value

    def plan(self):
        name = self.name
        items, text, error, after = [], 'NO_REPLY', '', None
        if name in SIMPLE:
            outputs = [self.run(c) for c in SIMPLE[name]]
            raw = '\n'.join(outputs)
            if name == 'memory-merge-daily':
                for output in outputs:
                    if json_result(output).get('status') not in ('ok', 'skip'):
                        raise RuntimeError(output)
            elif name == 'weekly-cleanup':
                counts = re.findall(r'(?:cleaned|removed):\s*(\d+)', raw)
                text = 'NO_REPLY' if counts and all(int(n) == 0 for n in counts) else raw
            elif name == 'openclaw-config-backup':
                if 'VERIFY_OK' not in outputs[-1] or re.search(r'\b(?:FAIL|WARN)\b', raw):
                    self.state['commands'] = {}
                    self.save()
                    raise RuntimeError(raw)
            elif name == 'daily-ops-audit' and raw.strip():
                text = '运维巡检已完成自动修复：\n' + raw
            elif name == 'skhy-premium-weekdays':
                text = outputs[0]
            elif name in ('weekly-token-usage', 'us-etf-monthly-flows'):
                if '发送成功' not in raw:
                    raise RuntimeError('脚本未确认卡片发送成功：' + raw[-1800:])
                text = '已生成报告并推送至 Eve tracking 群。'
        elif name.startswith('pt-watchlist-'):
            obj = self.obj(['python3', 'scripts/pt_watchlist_monitor.py'])
            if obj['feishu_target'] != ASIA:
                raise ValueError('投递目标与原任务不一致，请检查脚本配置')
            items = [message('feishu', ASIA, obj['main_message'])]
            if obj.get('jepq_message'):
                items.append(message('feishu', ASIA, obj['jepq_message']))
        elif name.startswith('china-2c-'):
            market = 'hk' if '-hk-' in name else 'us'
            raw = self.run(['python3', 'scripts/china_2c_monitor.py', '--market', market], accepted=(0, 1))
            if not raw.strip():
                raise ValueError('消费监控输出为空')
            items = [message('feishu', ASIA, raw)]
        elif name == 'e-watch-daily':
            obj = self.obj(['python3', 'scripts/e_watch_monitor.py'])
            items = [message('discord', V_DM, m) for m in obj['messages']]
            items.append(message('discord', V_DM, '📎 e-watch 全文（含未触发清单与口径）', obj['report_file']))
        elif name == 'ai-chain-daily':
            self.run(['bash', 'skills/ai-chain/run.sh', '--json', 'daily-run'])
            path = WS / 'skills/ai-chain/state/daily_push.json'
            obj = json.loads(path.read_text())
            if not obj.get('sent') and (not obj.get('quiet') or '🟡' in obj.get('summary', '')):
                if obj['target'] != DEV:
                    raise ValueError('ai-chain 投递目标与原任务不一致')
                items = [message('discord', DEV, obj['summary'])]
                items += [message('discord', DEV, media=p) for p in obj['high_cards']]
                after = {'path': str(path), 'value': {**obj, 'sent': True}}
        elif name == 'hk-momentum-weekly':
            obj = self.obj(['python3', 'scripts/hk_momentum_index/render_chart.py'])
            asof = datetime.fromisoformat(obj['asof']).date()
            today = datetime.now(ZoneInfo('Asia/Hong_Kong')).date()
            if (today - asof).days > 7 or asof > today:
                raise ValueError('港股动量数据日期异常：' + obj['asof'])
            items = [message('feishu', TRACKING, media=WS / 'temp/hk_momentum_1y.png')]
        elif name == 'us-topbottom-dm-v23':
            results, errors = [], []
            for skill in ('us-bottom-monitor', 'us-top-monitor'):
                try:
                    obj = self.obj(['bash', f'skills/{skill}/run.sh', '--json', 'run'])
                    results.append(message('discord', V_DM, obj['feishu_message'], obj['chart']))
                except (RuntimeError, ValueError) as exc:
                    errors.append(f'{skill}：{exc}')
            if errors:
                error = '\n'.join(errors)
                if results:
                    results[-1]['text'] += '\n⚠️ 另一份未取得：' + error
                else:
                    results = [message('discord', V_DM, '顶部和底部监控均失败：\n' + error)]
            items = results
        elif name == 'vol-surface-daily-snapshot':
            results, gaps = [], []
            try:
                obj = json_result(self.run(['bash', 'skills/hedging-analysis/run.sh', 'timemachine', '--json', 'snapshot', '--tickers', '^SPX,QQQ'], accepted=(0, 1)))
                for row in obj['results']:
                    if not row.get('ok'):
                        gaps.append(f"{row['ticker']}：{row.get('error', '采集失败')}")
                        continue
                    iv = row.get('atm_iv', {})
                    results.append(f"{row['ticker']}：数据截止 {row.get('timestamp', row.get('date', '未取得'))}；spot {row.get('spot', '未取得')}；期权数 {row.get('total_options', '未取得')}；ATM IV " + ' / '.join(f"{d} {str(iv[d]) + '%' if iv.get(d) is not None else '未取得'}" for d in ('7D', '30D', '60D', '90D')) + f"；30D skew {str(row['skew_30d_pp']) + 'pp' if row.get('skew_30d_pp') is not None else '未取得'}")
            except (RuntimeError, ValueError) as exc:
                gaps.append(str(exc))
            try:
                obj = self.obj(['bash', 'skills/hedging-analysis/run.sh', 'timemachine', '--json', 'backfill-vix'])
                results.append('VIX 历史库已更新，数据截止 ' + str(obj['regime_range'][-1]))
            except (RuntimeError, ValueError) as exc:
                gaps.append(str(exc))
            error = '\n'.join(gaps)
            text = '\n'.join(['每日波动率曲面快照', *results, '数据缺口：' + (error or '无')])
        return {'items': items, 'text': text, 'error': error, 'after': after}

    def execute(self):
        if 'plan' not in self.state:
            self.state['plan'] = self.plan()
            for index, item in enumerate(self.state['plan']['items']):
                if item.get('media'):
                    source = Path(item['media'])
                    copy = self.directory / 'attachments' / f'{index}{source.suffix}'
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, copy)
                    item['media'] = str(copy)
            self.save()
        plan = self.state['plan']
        if self.state.get('inflight') is not None:
            raise DeliveryUncertain('上次发送结果未知，禁止自动重发；先核对 pending.json 的 inflight 与 receipts')
        for index in range(self.state['next'], len(plan['items'])):
            self.state['inflight'] = index
            self.save()
            try:
                extra = {'timeout': SEND_TIMEOUT_SECONDS[self.name]} if self.name in SEND_TIMEOUT_SECONDS else {}
                receipt = send(plan['items'][index], receipt_dir=self.directory / 'receipts', **extra)
            except DeliveryUncertain:
                raise
            except RuntimeError:
                self.state.pop('inflight', None)
                self.save()
                raise
            self.state.setdefault('receipts', {})[str(index)] = receipt
            self.state['next'] = index + 1
            self.state.pop('inflight', None)
            self.save()
        if plan['after']:
            write_json(Path(plan['after']['path']), plan['after']['value'])
        print(plan['text'], end='' if plan['text'].endswith('\n') else '\n')
        self.path.replace(self.directory / 'last-run.json')
        return 1 if plan['error'] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('task', choices=TASKS, nargs='?')
    parser.add_argument('--describe', action='store_true', help='List tasks without running commands or sending')
    args = parser.parse_args()
    if args.describe:
        print(json.dumps({'tasks': TASKS, 'llm': False, 'state': 'state/cron-command/<task>/pending.json'}, ensure_ascii=False))
        return 0
    if not args.task:
        parser.error('task is required')
    if args.task == US_TOPBOTTOM_TASK and is_dalton_mode():
        try:
            return run_dalton_us_topbottom_preview()
        except Exception as exc:
            print(f'{args.task} 执行失败：{exc}', file=sys.stderr)
            return 1
    runner = Runner(args.task)
    runner.directory.mkdir(parents=True, exist_ok=True)
    with (runner.directory / 'run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('NO_REPLY')
            return 0
        try:
            runner = Runner(args.task)
            return runner.execute()
        except Exception as exc:
            error = f'{args.task} 执行失败：{exc}'
            if args.task == 'openclaw-config-backup':
                dest = Path.home() / 'Library/CloudStorage/GoogleDrive-richard.lu.everflowinv@gmail.com/我的云端硬盘/OpenClawBackup'
                yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
                exists = (dest / f'openclaw-config-{yesterday}.tar.gz.enc').exists()
                error += '\n昨日备份文件：' + ('存在，尚未验证可还原' if exists else '未找到')
            print(error)
            if 'plan' not in runner.state:
                route = {'e-watch-daily': ('discord', V_DM), 'ai-chain-daily': ('discord', DEV),
                         'hk-momentum-weekly': ('feishu', TRACKING)}.get(args.task)
                if route and not is_dalton_mode() and not os.environ.get('PYTEST_CURRENT_TEST'):
                    try:
                        send(message(*route, error))
                    except Exception as send_error:
                        print('失败通知也未发出：' + str(send_error))
            return 1


if __name__ == '__main__':
    sys.exit(main())
