#!/usr/bin/env python3
"""Host-side read-only OpenClaw conversation exporter (`export_openclaw.py`).

Responsibilities:
1. Snapshot `~/.openclaw/agents/<agent>/agent/openclaw-agent.sqlite` via SQLite Online Backup API
   into a temporary directory (with timeout; never locks the live gateway and never writes to `~/.openclaw/`).
2. Verify SQLite schema (`transcript_events`, `session_windows`, and optional `schema_meta`,
   `transcript_event_identities`, `transcript_rewrite_watermarks`) before reading; abort on mismatch.
3. Filter to human sessions (`Discord`, `Feishu`, `agent:main:main`, `chem`/`marco` human sessions);
   exclude `cron`, `heartbeat`, subagent, and `probe`/`acceptance` test sessions; strip pasted full
   transcripts from `codex-desktop-*` while preserving surrounding human discussion.
4. Extract text-only blocks from `message` events (`user` and `assistant`, skipping `thinking`/`toolCall`/`toolResult`).
5. Apply deterministic rule sanitization before any LLM call: redact secrets/tokens/pairing codes/auth codes
   (regex + Shannon entropy), skip operational dialogue (auth/approval/cron/model/config) and <30-char chitchat.
6. Incrementally export by cursor `(agent, session_id, last_seq, generation)` with automatic full rescan
   when `transcript_rewrite_watermarks.generation` changes, writing `var/dalton/conversations/inbox/<run_id>.jsonl`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger("dalton.export_openclaw")

ET_TZ = ZoneInfo("America/New_York")

DEFAULT_AGENTS: tuple[str, ...] = ("main", "chem", "marco")
DEFAULT_OCW_AGENTS_ROOT = Path(os.path.expanduser("~/.openclaw/agents"))
DEFAULT_INBOX_DIR = Path("var/dalton/conversations/inbox")
DEFAULT_CURSORS_FILE = Path("var/dalton/conversations/cursors.json")
DEFAULT_EXPORT_CURSORS_FILE = Path("var/dalton/conversations/export_cursors.json")


class OpenClawSchemaError(RuntimeError):
    """Raised when the OpenClaw SQLite database schema does not match expected tables/columns."""


@dataclass
class SessionCursor:
    """Host-side per-session high-water mark cursor for scan optimization."""

    agent: str
    session_id: str
    session_key: str = ""
    last_seq: int = -1
    generation: str = ""
    updated_at: str = ""


@dataclass
class ExportRunResult:
    """Summary of a host-side export run (never contains raw message text)."""

    run_id: str
    inbox_path: Path
    events_scanned: int = 0
    events_exported: int = 0
    events_skipped_ops: int = 0
    events_skipped_chitchat: int = 0
    events_skipped_transcript: int = 0
    events_parse_failed: int = 0
    secrets_redacted: int = 0
    sessions_rescanned: int = 0
    updated_cursors: dict[tuple[str, str], SessionCursor] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Rule-based Secret Sanitizer (Regex + Shannon Entropy)
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # OpenAI / Anthropic / Google / GitHub / Slack / XAI / JWT tokens
    re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bxai-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    # HTTP Authorization / Bearer / API-Key headers (independent of length/entropy)
    re.compile(
        r"(?i)\b(?:Authorization\s*:\s*(?:Bearer|Basic|Token)\s+|"
        r"X-API-Key\s*:\s*|X-Auth-Token\s*:\s*|X-Dalton-Bridge-Token\s*:\s*)"
        r"(?:\"((?:\\.|[^\"\\\r\n])+)\"|'((?:\\.|[^'\\\r\n])+)'|`((?:\\.|[^`\\\r\n])+)`|([^\s\"'`,;，。；！）\]}\r\n]+))"
    ),
    re.compile(
        r"(?i)\bBearer\s+(?:\"((?:\\.|[^\"\\\r\n])+)\"|'((?:\\.|[^'\\\r\n])+)'|`((?:\\.|[^`\\\r\n])+)`|([A-Za-z0-9._~+/=!@#$%^&*-]{3,}))"
    ),
    # JSON string fields with credential key names (independent of length/entropy)
    re.compile(
        r"(?i)[\"'](?:[a-z0-9_.-]*[_-])?(?:api[_-]?key|apikey|secret(?:[_-]?key)?|"
        r"app[_-]?secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|"
        r"auth[_-]?token|bot[_-]?token|bridge[_-]?token|broker[_-]?token|"
        r"gateway[_-]?token|private[_-]?key|password|passwd|bearer[_-]?token|credential|token)"
        r"[\"']\s*:\s*(?:\"((?:\\.|[^\"\\\r\n])+)\"|'((?:\\.|[^'\\\r\n])+)'|`((?:\\.|[^`\\\r\n])+)`)"
    ),
    # Key-value secret assignments (YAML/env/prose, independent of length/entropy)
    re.compile(
        r"(?i)\b(?:[a-z0-9_.-]*[_-])?(?:api[_-]?key|apikey|secret(?:[_-]?key)?|"
        r"app[_-]?secret|client[_-]?secret|access[_-]?token|refresh[_-]?token|"
        r"auth[_-]?token|bot[_-]?token|bridge[_-]?token|broker[_-]?token|"
        r"gateway[_-]?token|private[_-]?key|password|passwd|credential)\s*[:=]\s*"
        r"(?:\"((?:\\.|[^\"\\\r\n])+)\"|'((?:\\.|[^'\\\r\n])+)'|`((?:\\.|[^`\\\r\n])+)`|([^\s\"'`,;，。；！）\]}\r\n]+))"
    ),
    # Uppercase ENV variable assignments (e.g. KEY=..., OPENAI_API_KEY=..., TOKEN=...)
    re.compile(
        r"\b(?:export\s+)?(?:[A-Z0-9_]+_)?(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)\s*=\s*"
        r"(?:\"((?:\\.|[^\"\\\r\n])+)\"|'((?:\\.|[^'\\\r\n])+)'|`((?:\\.|[^`\\\r\n])+)`|([^\s\"'`,;，。；！）\]}\r\n]+))"
    ),
    # Pairing codes, authorization codes, verification codes in Chinese or English context
    re.compile(
        r"(?i)(?:配对码|授权码|验证码|设备码|一次性密码|pairing\s*code|auth(?:orization)?\s*code|"
        r"verification\s*code|device\s*code|one[- ]time\s*code|otp)\s*(?:是|为|:|：|=|\bis\b)?\s*"
        r"['\"`]?([A-Z0-9]{4,12}(?:-[A-Z0-9]{3,12})*)['\"`]?"
    ),
    # Standalone hyphenated pairing codes mixing uppercase letters and digits (e.g. ABCD-1234 or A1B2-C3D4)
    re.compile(r"\b(?=[A-Z0-9-]{9,19}\b)(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]{4}-[A-Z0-9]{4}(?:-[A-Z0-9]{4})?\b"),
    # Long hex digests / tokens (32+ hex chars)
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
)

_CANDIDATE_TOKEN_RE = re.compile(r"(?<![/\w.-])[A-Za-z0-9+/=_-]{22,}(?![/\w.-])")


def _shannon_entropy(token: str) -> float:
    if not token:
        return 0.0
    length = len(token)
    counts: dict[str, int] = {}
    for ch in token:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def _looks_like_high_entropy_secret(token: str) -> bool:
    if len(token) < 22 or token.startswith("REDACTED"):
        return False
    has_upper = any(c.isupper() for c in token)
    has_lower = any(c.islower() for c in token)
    has_digit = any(c.isdigit() for c in token)
    classes = sum((has_upper, has_lower, has_digit))
    if classes < 2:
        return False
    # Exclude Pure camelCase identifiers without digits unless very high entropy
    if not has_digit and len(token) < 32:
        return False
    entropy = _shannon_entropy(token)
    threshold = 3.85 if has_digit else 4.25
    return entropy >= threshold


def sanitize_text(text: str) -> tuple[str, int]:
    """Redact secrets, tokens, pairing codes, and high-entropy credentials from `text`.

    Returns `(cleaned_text, redaction_count)`.
    """
    if not text:
        return "", 0

    redactions = 0
    cleaned = text

    for pat in _SECRET_PATTERNS:
        def _repl(match: re.Match[str]) -> str:
            nonlocal redactions
            if match.lastindex and match.lastindex >= 1:
                full = match.group(0)
                grp_idx = next(
                    (idx for idx in range(1, match.lastindex + 1) if match.group(idx) is not None),
                    None,
                )
                if grp_idx is not None:
                    secret_part = match.group(grp_idx)
                    if secret_part == "[REDACTED]":
                        return full
                    if "REDACTED" not in secret_part:
                        redactions += 1
                    rel_start = match.start(grp_idx) - match.start(0)
                    rel_end = match.end(grp_idx) - match.start(0)
                    return full[:rel_start] + "[REDACTED]" + full[rel_end:]
            if "REDACTED" in match.group(0):
                return match.group(0)
            redactions += 1
            return "[REDACTED]"

        cleaned = pat.sub(_repl, cleaned)

    def _entropy_repl(match: re.Match[str]) -> str:
        nonlocal redactions
        tok = match.group(0)
        if _looks_like_high_entropy_secret(tok):
            redactions += 1
            return "[REDACTED]"
        return tok

    cleaned = _CANDIDATE_TOKEN_RE.sub(_entropy_repl, cleaned)
    return cleaned, redactions


# ---------------------------------------------------------------------------
# Operational Dialogue & Chitchat Filtering
# ---------------------------------------------------------------------------

_OPS_COMMAND_RE = re.compile(
    r"^\s*(?:/approve|/auth|/pair|/model|/models|/config|/cron|/restart|/status|/token|/login)\b",
    re.IGNORECASE,
)

_OPS_KEYWORDS_RE = re.compile(
    r"(?i)(?:配对码|授权码|批准执行|授权通过|网关重启|切换模型|模型配置|定时任务配置|"
    r"修改配置|配置\s*cron|crontab|launchctl|systemctl|docker\s+compose|openclaw\s+gateway|"
    r"oauth\s*登录|token\s*刷新|重启\s*bridge|重启网关|帮我配一下|把模型切成|切换成\s*gemini|"
    r"切换成\s*claude|切换成\s*gpt|approve\s+pairing|pairing\s+request|cron\s+schedule)"
)

_PRIVATE_KEYWORDS_RE = re.compile(
    r"(?i)(?:家庭住址|身份证号|银行卡号|私人行程|看病挂号|医院体检|家人住院|老婆孩子|"
    r"纯私人|个人隐私|不要入库|private\s+matter|personal\s+medical|bank\s+account\s+number)"
)

_RESEARCH_SIGNAL_RE = re.compile(
    r"(?:(?-i:\b(?!(?:CRON|AUTH|PAIR|TOKEN|OAUTH|MODEL|CONFIG|ADMIN|HTTP|HTTPS)\b)[A-Z]{2,5}\b)|"
    r"(?i:营收|收入|毛利率|净利率|利润|估值|指引|财报|业绩|订单|出货量|"
    r"份额|增速|同比|环比|良率|算力|资本开支|capex|revenue|margin|ebitda|eps|"
    r"guidance|backlog|arr|gmv|arpu|dau|mau|云业务|广告|电商|芯片|大模型|推理|训练))"
)

_CHITCHAT_RE = re.compile(
    r"^\s*(?:你好|早安|早上好|晚上好|下午好|谢谢|多谢|辛苦了|收到|好的|明白|ok|okay|"
    r"哈哈|嗯嗯|好的谢谢|没问题|测试|test|ping|在吗|先这样|回头聊|拜拜)[!！。.,，～~\s]*$",
    re.IGNORECASE,
)


def is_operational_or_chitchat(text: str) -> tuple[bool, str]:
    """Return `(should_skip, reason)` for operational/private messages or <30-char chitchat."""
    stripped = (text or "").strip()
    if not stripped:
        return True, "empty"

    if _OPS_COMMAND_RE.search(stripped):
        return True, "ops"

    if _OPS_KEYWORDS_RE.search(stripped) and not _RESEARCH_SIGNAL_RE.search(stripped):
        return True, "ops"

    if _PRIVATE_KEYWORDS_RE.search(stripped):
        return True, "private"

    # Normalize whitespace and remove redaction markers for length check
    compact = re.sub(r"\s+", " ", stripped.replace("[REDACTED]", "")).strip()
    if not compact:
        return True, "ops"

    if _CHITCHAT_RE.match(compact):
        return True, "chitchat"

    if len(compact) < 30:
        # Skip <30 char chitchat / short non-substantive remarks
        if not _RESEARCH_SIGNAL_RE.search(compact):
            return True, "chitchat"
        # Even if a short token matched, <30 chars without a claim or question is chitchat
        if len(compact) < 15:
            return True, "chitchat"

    return False, ""


# ---------------------------------------------------------------------------
# Codex-Desktop Long Pasted User Message & Assistant Verbatim Echo Filter
# ---------------------------------------------------------------------------

CODEX_MAX_USER_MESSAGE_CHARS = 2000


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_codex_assistant_verbatim_repeat(
    assistant_text: str,
    discarded_user_texts: list[str],
) -> bool:
    """Return True if an assistant message in a codex session verbatim repeats a discarded user message."""
    norm_asst = _normalize_ws(assistant_text)
    if not norm_asst or not discarded_user_texts:
        return False

    asst_paras = [
        _normalize_ws(p)
        for p in re.split(r"\n+", assistant_text)
        if len(_normalize_ws(p)) >= 60
    ]

    for raw_user in discarded_user_texts:
        norm_user = _normalize_ws(raw_user)
        if not norm_user:
            continue
        if len(norm_asst) >= 40 and (norm_asst in norm_user or norm_user in norm_asst):
            return True
        for para in asst_paras:
            if para in norm_user:
                return True
        # Check sliding 120-char window overlap for verbatim echo without newlines
        if len(norm_asst) >= 120 and len(norm_user) >= 120:
            step = max(20, len(norm_asst) // 10)
            for idx in range(0, len(norm_asst) - 119, step):
                chunk = norm_asst[idx : idx + 120]
                if chunk in norm_user:
                    return True
    return False


def strip_codex_desktop_transcript(
    text: str,
    *,
    is_codex_desktop: bool,
    role: str = "user",
    discarded_user_texts: list[str] | None = None,
) -> tuple[str, bool]:
    """Filter codex session messages: discard >2000-char user messages and assistant verbatim repetitions.

    Returns `(remaining_text, was_discarded)`.
    """
    if not is_codex_desktop or not text:
        return text, False

    raw = text.strip()
    if role == "user":
        if len(raw) > CODEX_MAX_USER_MESSAGE_CHARS:
            if discarded_user_texts is not None:
                discarded_user_texts.append(raw)
            return "", True
        return text, False

    if role == "assistant" and discarded_user_texts:
        if is_codex_assistant_verbatim_repeat(raw, discarded_user_texts):
            return "", True

    return text, False


# ---------------------------------------------------------------------------
# Session Classification & SQLite Schema Verification
# ---------------------------------------------------------------------------

def is_human_session(
    agent: str,
    session_key: str,
    channel: str = "",
    chat_type: str = "",
) -> bool:
    """Return True if the session belongs to an included human conversation channel.

    For all agents (`main`, `chem`, `marco`, etc.), the session source must be one of:
    - Discord (`:discord:` or `channel == 'discord'`)
    - Feishu (`:feishu:` / `:lark:` or `channel in ('feishu', 'lark')`)
    - Main (`agent:<agent>:main` or `main` or `channel == 'main'`)
    - Codex (`codex` / `codex-desktop-*` or `channel in ('codex', 'codex-desktop')`)

    Excluded:
    - `cron`, `heartbeat`, subagent (`:subagent:`, `:sub:`), `probe` / `acceptance` / test sessions
    """
    sk = (session_key or "").strip().lower()
    ch = (channel or "").strip().lower()
    ct = (chat_type or "").strip().lower()
    ag = (agent or "").strip().lower()

    # Hard exclusions first
    excluded_tokens = (
        ":cron:",
        "cron:",
        "heartbeat",
        ":subagent:",
        "subagent:",
        ":sub:",
        ":probe:",
        "probe-",
        "probe_",
        "acceptance",
        ":test:",
        "test-session",
        "smoke-test",
    )
    for tok in excluded_tokens:
        if tok in sk or tok in ch or tok in ct:
            return False
    if ch in ("cron", "heartbeat", "subagent", "probe", "test") or ct in (
        "cron",
        "heartbeat",
        "subagent",
        "probe",
        "test",
    ):
        return False

    # Inclusions (must match discord, feishu/lark, main, or codex for any agent)
    if ":discord:" in sk or ch == "discord":
        return True
    if ":feishu:" in sk or ":lark:" in sk or ch in ("feishu", "lark"):
        return True
    if sk in ("agent:main:main", "main", f"agent:{ag}:main") or (
        ag and sk.startswith(f"agent:{ag}:") and sk.endswith(":main")
    ) or ch == "main":
        return True
    if (
        sk.startswith("codex")
        or ":codex:" in sk
        or ":codex-desktop:" in sk
        or "codex-desktop" in sk
        or ch in ("codex", "codex-desktop")
    ):
        return True

    return False


def verify_openclaw_schema(conn: sqlite3.Connection) -> None:
    """Verify required tables and columns in an OpenClaw SQLite snapshot before reading."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required_tables = {"transcript_events", "session_windows"}
    missing_tables = required_tables - tables
    if missing_tables:
        raise OpenClawSchemaError(
            f"OpenClaw SQLite missing required table(s): {sorted(missing_tables)}"
        )

    te_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(transcript_events)").fetchall()
    }
    for req_col in ("session_id", "seq", "created_at"):
        if req_col not in te_cols:
            raise OpenClawSchemaError(
                f"transcript_events missing required column '{req_col}' (found: {sorted(te_cols)})"
            )
    if "event_json" not in te_cols and "event_zstd" not in te_cols:
        raise OpenClawSchemaError(
            "transcript_events must contain 'event_json' or 'event_zstd'"
        )

    sw_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(session_windows)").fetchall()
    }
    for req_col in ("session_id", "session_key", "channel", "chat_type"):
        if req_col not in sw_cols:
            raise OpenClawSchemaError(
                f"session_windows missing required column '{req_col}' (found: {sorted(sw_cols)})"
            )

    if "schema_meta" in tables:
        sm_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(schema_meta)").fetchall()
        }
        if not sm_cols:
            raise OpenClawSchemaError("schema_meta table has no columns")


def snapshot_sqlite_readonly(
    src_db_path: Path,
    dst_db_path: Path,
    *,
    timeout_s: float = 60.0,
) -> None:
    """Copy `src_db_path` to `dst_db_path` using SQLite Online Backup API without locking writers."""
    if not src_db_path.is_file():
        raise FileNotFoundError(f"OpenClaw SQLite file not found: {src_db_path}")

    uri = f"{src_db_path.resolve().as_uri()}?mode=ro"
    t0 = time.monotonic()
    src_conn = sqlite3.connect(uri, uri=True, timeout=min(timeout_s, 15.0))
    try:
        src_conn.execute("PRAGMA query_only=ON")
        src_conn.execute(f"PRAGMA busy_timeout={int(min(timeout_s, 15.0) * 1000)}")
        dst_conn = sqlite3.connect(str(dst_db_path), timeout=timeout_s)
        try:
            def _progress(_status: int, remaining: int, _total: int) -> None:
                if remaining > 0 and (time.monotonic() - t0) > timeout_s:
                    raise TimeoutError(
                        f"SQLite online backup exceeded timeout ({timeout_s}s)"
                    )

            src_conn.backup(dst_conn, pages=2000, sleep=0.005, progress=_progress)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _decompress_zstd(blob: bytes, expected_bytes: int | None = None) -> str:
    """Decompress a zstd-compressed `event_zstd` blob into UTF-8 JSON text.

    Prefers the project's `zstandard` dependency (Python 3.13), falling back to
    `compression.zstd` (Python 3.14+) or the `zstd` CLI binary.
    """
    try:
        import zstandard as zstd_mod  # type: ignore[import-not-found]

        dctx = zstd_mod.ZstdDecompressor()
        max_out = max(int(expected_bytes or 0), len(blob) * 20, 1024 * 1024)
        return dctx.decompress(blob, max_output_size=max_out).decode("utf-8", errors="replace")
    except ImportError:
        pass
    try:
        from compression import zstd as stdlib_zstd  # type: ignore[import-not-found]

        return stdlib_zstd.decompress(blob).decode("utf-8", errors="replace")
    except Exception:
        raw = subprocess.check_output(["zstd", "-d", "-q", "-c"], input=blob, timeout=10)
        return raw.decode("utf-8", errors="replace")


def _extract_message_text_only(content: Any) -> str:
    """Extract only `text` blocks from a message content payload (ignoring `thinking`, `toolCall`, etc.)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    txt = item["text"].strip()
                    if txt:
                        parts.append(txt)
        return "\n".join(parts).strip()
    return ""


def _infer_channel_from_session_key(session_key: str) -> str:
    sk = (session_key or "").strip().lower()
    if ":discord:" in sk:
        return "discord"
    if ":feishu:" in sk or ":lark:" in sk:
        return "feishu"
    if "codex-desktop" in sk or sk.startswith("codex") or ":codex:" in sk:
        return "codex-desktop"
    if sk in ("main", "agent:main:main") or sk.endswith(":main"):
        return "main"
    return ""


# ---------------------------------------------------------------------------
# Local JSON Cursor Mirror Helpers
# ---------------------------------------------------------------------------

def load_cursors_file(path: Path = DEFAULT_CURSORS_FILE) -> dict[tuple[str, str], SessionCursor]:
    """Load `(agent, session_id) -> SessionCursor` from local JSON mirror.

    Propagates read/parse errors when `path` exists so callers never overwrite a
    corrupted cursor file with empty cursors.
    """
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("cursors", []) if isinstance(raw, dict) else raw
    out: dict[tuple[str, str], SessionCursor] = {}
    if isinstance(items, list):
        for entry in items:
            if not isinstance(entry, dict):
                continue
            ag = str(entry.get("agent") or "").strip()
            sid = str(entry.get("session_id") or "").strip()
            if ag and sid:
                raw_gen = entry.get("generation")
                out[(ag, sid)] = SessionCursor(
                    agent=ag,
                    session_id=sid,
                    session_key=str(entry.get("session_key") or ""),
                    last_seq=int(entry.get("last_seq", -1)),
                    generation="" if raw_gen is None else str(raw_gen),
                    updated_at=str(entry.get("updated_at") or ""),
                )
    return out


def save_cursors_file(
    cursors: dict[tuple[str, str], SessionCursor],
    path: Path = DEFAULT_CURSORS_FILE,
) -> None:
    """Atomically write `(agent, session_id) -> SessionCursor` to local JSON mirror."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "cursors": [
            {
                "agent": c.agent,
                "session_id": c.session_id,
                "session_key": c.session_key,
                "last_seq": c.last_seq,
                "generation": c.generation,
                "updated_at": c.updated_at or datetime.now(timezone.utc).isoformat(),
            }
            for _, c in sorted(cursors.items())
        ],
    }
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


# ---------------------------------------------------------------------------
# Main Agent DB Export Pipeline
# ---------------------------------------------------------------------------

def export_agent_sqlite(
    agent: str,
    sqlite_path: Path,
    *,
    cursors: dict[tuple[str, str], SessionCursor],
    since_date: date | None = None,
    until_date: date | None = None,
    ignore_cursors: bool = False,
    backup_timeout_s: float = 60.0,
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], SessionCursor], dict[str, int]]:
    """Snapshot and export sanitized human conversation events from one agent's SQLite DB."""
    stats = {
        "events_scanned": 0,
        "events_exported": 0,
        "events_skipped_ops": 0,
        "events_skipped_chitchat": 0,
        "events_skipped_transcript": 0,
        "events_parse_failed": 0,
        "secrets_redacted": 0,
        "sessions_rescanned": 0,
    }
    exported_events: list[dict[str, Any]] = []
    new_cursors: dict[tuple[str, str], SessionCursor] = dict(cursors)

    with tempfile.TemporaryDirectory(prefix=f"dalton_ocw_{agent}_") as tmp_dir:
        snap_path = Path(tmp_dir) / "snapshot.sqlite"
        snapshot_sqlite_readonly(sqlite_path, snap_path, timeout_s=backup_timeout_s)

        snap_uri = f"{snap_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(snap_uri, uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            verify_openclaw_schema(conn)

            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

            # 1. Load session_windows mapping
            session_map: dict[str, dict[str, Any]] = {}
            for row in conn.execute("SELECT * FROM session_windows").fetchall():
                d = dict(row)
                sk = str(d.get("session_key") or "")
                sid = str(d.get("session_id") or d.get("primary_conversation_id") or sk)
                if sid:
                    session_map[sid] = d
                if sk:
                    session_map[sk] = d

            # 2. Load rewrite watermarks if table exists
            watermarks: dict[str, str] = {}
            if "transcript_rewrite_watermarks" in tables:
                wm_cols = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(transcript_rewrite_watermarks)"
                    ).fetchall()
                }
                id_col = "session_id" if "session_id" in wm_cols else (
                    "session_key" if "session_key" in wm_cols else None
                )
                if id_col and "generation" in wm_cols:
                    for r in conn.execute(
                        f"SELECT {id_col} AS sid, generation FROM transcript_rewrite_watermarks"
                    ).fetchall():
                        raw_wm_gen = r["generation"]
                        watermarks[str(r["sid"])] = "" if raw_wm_gen is None else str(raw_wm_gen)

            # 3. Load stable event identities if table exists
            identities: dict[tuple[str, int], str] = {}
            if "transcript_event_identities" in tables:
                id_cols = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(transcript_event_identities)"
                    ).fetchall()
                }
                if {"event_id", "seq"}.issubset(id_cols):
                    if "session_id" in id_cols:
                        for r in conn.execute(
                            "SELECT session_id, seq, event_id FROM transcript_event_identities"
                        ).fetchall():
                            identities[(str(r["session_id"]), int(r["seq"]))] = str(r["event_id"])
                    else:
                        for r in conn.execute(
                            "SELECT seq, event_id FROM transcript_event_identities"
                        ).fetchall():
                            identities[("*", int(r["seq"]))] = str(r["event_id"])

            # 4. Discover distinct session_ids in transcript_events
            te_cols = {
                r[1] for r in conn.execute("PRAGMA table_info(transcript_events)").fetchall()
            }
            has_zstd = "event_zstd" in te_cols
            has_utf8_bytes = "event_utf8_bytes" in te_cols
            has_nav = "navigation_json" in te_cols

            session_rows = conn.execute(
                "SELECT DISTINCT session_id FROM transcript_events ORDER BY session_id"
            ).fetchall()

            for srow in session_rows:
                session_id = str(srow["session_id"])
                sw = session_map.get(session_id, {})
                session_key = str(sw.get("session_key") or session_id)
                channel = str(sw.get("channel") or _infer_channel_from_session_key(session_key))
                chat_type = str(sw.get("chat_type") or "direct")

                if not is_human_session(agent, session_key, channel, chat_type):
                    continue

                current_gen = str(watermarks.get(session_id, watermarks.get(session_key, "")))
                prev_cursor = cursors.get((agent, session_id))
                gen_changed = (
                    not ignore_cursors
                    and prev_cursor is not None
                    and str(prev_cursor.generation) != str(current_gen)
                )
                if ignore_cursors:
                    last_seq = -1
                elif prev_cursor is not None:
                    if gen_changed:
                        last_seq = -1
                        stats["sessions_rescanned"] += 1
                    else:
                        last_seq = prev_cursor.last_seq
                else:
                    last_seq = -1

                select_cols = ["session_id", "seq", "created_at"]
                if "event_json" in te_cols:
                    select_cols.append("event_json")
                if has_zstd:
                    select_cols.append("event_zstd")
                if has_utf8_bytes:
                    select_cols.append("event_utf8_bytes")
                if has_nav:
                    select_cols.append("navigation_json")

                query = (
                    f"SELECT {', '.join(select_cols)} FROM transcript_events "
                    "WHERE session_id = ? AND seq > ? ORDER BY seq ASC"
                )
                ev_rows = conn.execute(query, (session_id, last_seq)).fetchall()
                baseline_seq = (
                    prev_cursor.last_seq
                    if (prev_cursor is not None and not gen_changed)
                    else -1
                )
                max_seq_seen = baseline_seq
                is_codex = "codex" in session_key.lower() or "codex" in channel.lower()
                discarded_codex_texts: list[str] = []

                # When scanning incrementally (`last_seq >= 0`) on a codex session, preload
                # prior >2000-char user messages so cross-export assistant verbatim echoes are caught.
                if is_codex and last_seq >= 0:
                    hist_rows = conn.execute(
                        f"SELECT {', '.join(select_cols)} FROM transcript_events "
                        "WHERE session_id = ? AND seq <= ? ORDER BY seq ASC",
                        (session_id, last_seq),
                    ).fetchall()
                    for hrow in hist_rows:
                        hd = dict(hrow)
                        h_raw: str | None = hd.get("event_json")
                        if not h_raw and has_zstd and hd.get("event_zstd"):
                            h_blob = hd["event_zstd"]
                            if isinstance(h_blob, memoryview):
                                h_blob = h_blob.tobytes()
                            try:
                                h_raw = _decompress_zstd(
                                    h_blob, expected_bytes=hd.get("event_utf8_bytes")
                                )
                            except Exception:
                                h_raw = None
                        if not h_raw:
                            continue
                        try:
                            h_obj = json.loads(h_raw)
                        except Exception:
                            continue
                        if not isinstance(h_obj, dict) or str(h_obj.get("type") or "") != "message":
                            continue
                        h_msg = h_obj.get("message")
                        if isinstance(h_msg, dict) and str(h_msg.get("role") or "") == "user":
                            h_txt = _extract_message_text_only(h_msg.get("content"))
                            if h_txt and len(h_txt.strip()) > CODEX_MAX_USER_MESSAGE_CHARS:
                                discarded_codex_texts.append(h_txt.strip())

                # When generation changed during incremental `run` (`not ignore_cursors`), ignore high-water mark
                # and `since_date` so the rewritten session is re-exported; `backfill` (`ignore_cursors=True`)
                # always respects its explicit `[since_date, until_date]` window.
                effective_since_date = None if (gen_changed and not ignore_cursors) else since_date
                skipped_after_until = False
                session_parse_failed = False

                for erow in ev_rows:
                    ed = dict(erow)
                    seq = int(ed["seq"])
                    stats["events_scanned"] += 1

                    created_ms = int(ed.get("created_at") or 0)
                    ts_dt = (
                        datetime.fromtimestamp(created_ms / 1000.0, tz=timezone.utc)
                        if created_ms > 0
                        else datetime.now(timezone.utc)
                    )
                    date_et = ts_dt.astimezone(ET_TZ).date()

                    # Out-of-window events must NEVER advance the high-water mark
                    if effective_since_date is not None and date_et < effective_since_date:
                        continue
                    if until_date is not None and date_et > until_date:
                        skipped_after_until = True
                        continue

                    raw_json_str: str | None = ed.get("event_json")
                    if not raw_json_str and has_zstd and ed.get("event_zstd"):
                        blob = ed["event_zstd"]
                        if isinstance(blob, memoryview):
                            blob = blob.tobytes()
                        try:
                            raw_json_str = _decompress_zstd(
                                blob, expected_bytes=ed.get("event_utf8_bytes")
                            )
                        except Exception:
                            raw_json_str = None
                    if not raw_json_str:
                        stats["events_parse_failed"] += 1
                        session_parse_failed = True
                        logger.warning(
                            "Unreadable event payload for agent=%s session_id=%s seq=%d; stopping session scan before seq=%d",
                            agent,
                            session_id,
                            seq,
                            seq,
                        )
                        break

                    try:
                        event_obj = json.loads(raw_json_str)
                    except Exception:
                        stats["events_parse_failed"] += 1
                        session_parse_failed = True
                        logger.warning(
                            "Malformed event JSON for agent=%s session_id=%s seq=%d; stopping session scan before seq=%d",
                            agent,
                            session_id,
                            seq,
                            seq,
                        )
                        break
                    if not isinstance(event_obj, dict):
                        stats["events_parse_failed"] += 1
                        session_parse_failed = True
                        logger.warning(
                            "Non-object event JSON for agent=%s session_id=%s seq=%d; stopping session scan before seq=%d",
                            agent,
                            session_id,
                            seq,
                            seq,
                        )
                        break

                    if seq > max_seq_seen:
                        max_seq_seen = seq

                    if str(event_obj.get("type") or "") != "message":
                        continue

                    msg = event_obj.get("message")
                    if not isinstance(msg, dict):
                        continue

                    role = str(msg.get("role") or "")
                    if role not in ("user", "assistant"):
                        continue

                    text_content = _extract_message_text_only(msg.get("content"))
                    if not text_content:
                        continue

                    if is_codex:
                        text_content, stripped_tr = strip_codex_desktop_transcript(
                            text_content,
                            is_codex_desktop=True,
                            role=role,
                            discarded_user_texts=discarded_codex_texts,
                        )
                        if stripped_tr:
                            stats["events_skipped_transcript"] += 1
                        if not text_content:
                            continue

                    sanitized_text, redaction_cnt = sanitize_text(text_content)
                    stats["secrets_redacted"] += redaction_cnt

                    skip, skip_reason = is_operational_or_chitchat(sanitized_text)
                    if skip:
                        if skip_reason in ("ops", "private"):
                            stats["events_skipped_ops"] += 1
                        else:
                            stats["events_skipped_chitchat"] += 1
                        continue

                    event_id = (
                        identities.get((session_id, seq))
                        or identities.get(("*", seq))
                        or str(event_obj.get("id") or f"{session_id}:{seq}")
                    )

                    if role == "user":
                        ocw = msg.get("__openclaw") or event_obj.get("__openclaw") or {}
                        if not isinstance(ocw, dict):
                            ocw = {}
                        raw_sender_id = str(ocw.get("senderId") or "owner")
                        sender_id_hash = hashlib.sha256(
                            raw_sender_id.encode("utf-8")
                        ).hexdigest()[:16]
                        is_owner = bool(ocw.get("senderIsOwner", True if not ocw else False))
                        sender_name = str(
                            ocw.get("senderName") or ("Owner" if is_owner else "Participant")
                        )
                        speaker_role = "owner" if is_owner else "participant"
                        transport = ocw.get("transport") if isinstance(ocw.get("transport"), dict) else {}
                        ev_channel = str(transport.get("channel") or channel)
                    else:
                        sender_id_hash = f"agent:{agent}"
                        is_owner = False
                        sender_name = "Eve" if agent == "main" else agent
                        speaker_role = "agent"
                        ev_channel = channel

                    ev_record: dict[str, Any] = {
                        "agent": agent,
                        "session_id": session_id,
                        "session_key": session_key,
                        "channel": ev_channel,
                        "chat_type": chat_type,
                        "event_id": event_id,
                        "seq": seq,
                        "generation": current_gen,
                        "created_at_ms": created_ms,
                        "ts": ts_dt.isoformat(),
                        "date_et": date_et.isoformat(),
                        "message_role": role,
                        "speaker": {
                            "sender_id_hash": sender_id_hash,
                            "name": sender_name,
                            "is_owner": is_owner,
                            "role": speaker_role,
                        },
                        "text": sanitized_text,
                    }
                    exported_events.append(ev_record)
                    stats["events_exported"] += 1

                if gen_changed and (skipped_after_until or session_parse_failed):
                    # Do not update the export cursor to the new generation until all events of the
                    # rewritten session have been exported without parse failures.
                    pass
                elif max_seq_seen > baseline_seq or (gen_changed and not session_parse_failed):
                    new_cursors[(agent, session_id)] = SessionCursor(
                        agent=agent,
                        session_id=session_id,
                        session_key=session_key,
                        last_seq=max_seq_seen,
                        generation=current_gen,
                        updated_at=datetime.now(timezone.utc).isoformat(),
                    )
        finally:
            conn.close()

    return exported_events, new_cursors, stats


def run_export(
    *,
    agents: list[str] | tuple[str, ...] = DEFAULT_AGENTS,
    ocw_agents_root: Path = DEFAULT_OCW_AGENTS_ROOT,
    inbox_dir: Path = DEFAULT_INBOX_DIR,
    cursors_file: Path = DEFAULT_EXPORT_CURSORS_FILE,
    run_id: str | None = None,
    since_date: date | None = None,
    until_date: date | None = None,
    ignore_cursors: bool = False,
    persist_cursors: bool = True,
    sqlite_paths_by_agent: dict[str, Path] | None = None,
) -> ExportRunResult:
    """Export new sanitized human conversation events across `agents` to `inbox/<run_id>.jsonl`."""
    eff_run_id = run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    inbox_path = inbox_dir / f"{eff_run_id}.jsonl"

    cursors = load_cursors_file(cursors_file)
    result = ExportRunResult(run_id=eff_run_id, inbox_path=inbox_path, updated_cursors=dict(cursors))
    all_events: list[dict[str, Any]] = []

    for agent in agents:
        db_path = (
            sqlite_paths_by_agent[agent]
            if sqlite_paths_by_agent and agent in sqlite_paths_by_agent
            else ocw_agents_root / agent / "agent" / "openclaw-agent.sqlite"
        )
        if not db_path.is_file():
            logger.info("Skipping agent=%s (SQLite not found at %s)", agent, db_path)
            continue

        events, updated_cursors, st = export_agent_sqlite(
            agent,
            db_path,
            cursors=result.updated_cursors,
            since_date=since_date,
            until_date=until_date,
            ignore_cursors=ignore_cursors,
        )
        all_events.extend(events)
        result.updated_cursors = updated_cursors
        result.events_scanned += st["events_scanned"]
        result.events_exported += st["events_exported"]
        result.events_skipped_ops += st["events_skipped_ops"]
        result.events_skipped_chitchat += st["events_skipped_chitchat"]
        result.events_skipped_transcript += st["events_skipped_transcript"]
        result.events_parse_failed += st["events_parse_failed"]
        result.secrets_redacted += st["secrets_redacted"]
        result.sessions_rescanned += st["sessions_rescanned"]

    tmp_inbox_path = inbox_dir / f".{eff_run_id}.jsonl.tmp"
    with tmp_inbox_path.open("w", encoding="utf-8") as fh:
        for ev in all_events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    tmp_inbox_path.replace(inbox_path)

    if persist_cursors and not ignore_cursors:
        save_cursors_file(result.updated_cursors, cursors_file)

    logger.info(
        "Export run_id=%s scanned=%d exported=%d skipped_ops=%d skipped_chitchat=%d "
        "skipped_transcript=%d parse_failed=%d secrets_redacted=%d rescanned=%d",
        eff_run_id,
        result.events_scanned,
        result.events_exported,
        result.events_skipped_ops,
        result.events_skipped_chitchat,
        result.events_skipped_transcript,
        result.events_parse_failed,
        result.secrets_redacted,
        result.sessions_rescanned,
    )
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] export_openclaw: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Read-only OpenClaw conversation exporter")
    parser.add_argument("--agents", default=",".join(DEFAULT_AGENTS), help="Comma-separated agent names")
    parser.add_argument("--ocw-agents-root", type=Path, default=DEFAULT_OCW_AGENTS_ROOT)
    parser.add_argument("--inbox-dir", type=Path, default=DEFAULT_INBOX_DIR)
    parser.add_argument("--cursors-file", type=Path, default=DEFAULT_EXPORT_CURSORS_FILE)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--since", default=None, help="Filter events since YYYY-MM-DD (ET)")
    parser.add_argument("--until", default=None, help="Filter events until YYYY-MM-DD (ET)")
    parser.add_argument("--ignore-cursors", action="store_true", help="Re-read window ignoring high-water mark")
    parser.add_argument("--no-save-cursors", action="store_true", help="Do not update export_cursors.json on disk")
    args = parser.parse_args(argv)

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    since_d = date.fromisoformat(args.since) if args.since else None
    until_d = date.fromisoformat(args.until) if args.until else None

    res = run_export(
        agents=agents,
        ocw_agents_root=args.ocw_agents_root,
        inbox_dir=args.inbox_dir,
        cursors_file=args.cursors_file,
        run_id=args.run_id,
        since_date=since_d,
        until_date=until_d,
        ignore_cursors=bool(args.ignore_cursors),
        persist_cursors=not args.no_save_cursors,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "run_id": res.run_id,
                "inbox_path": str(res.inbox_path),
                "events_scanned": res.events_scanned,
                "events_exported": res.events_exported,
                "events_skipped_ops": res.events_skipped_ops,
                "events_skipped_chitchat": res.events_skipped_chitchat,
                "events_skipped_transcript": res.events_skipped_transcript,
                "events_parse_failed": res.events_parse_failed,
                "secrets_redacted": res.secrets_redacted,
                "sessions_rescanned": res.sessions_rescanned,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
