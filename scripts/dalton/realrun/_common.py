#!/usr/bin/env python3
"""Shared utilities for Dalton real-run evidence scripts (P0I §2.1–§2.2, L37).

Responsibilities:
1. Build the mandatory v1 evidence header (`evidence_version`, `git_sha`, `script`,
   `started_at`, `ended_at`, `backend`, `test_user_id`, `synthetic`, `journal`).
2. Validate `synthetic` declarations against the four allowed injection categories (L37).
3. Compute SHA-256 of raw evidence JSON under `var/dalton/` and render a credential-free
   Markdown summary under `docs/dalton/dev-guide/trial-runs/realrun-<YYYYMMDD>/G<N>.md`.
4. Provide an HTTP client helper for interacting with the live backend (`http://127.0.0.1:8000`)
   without importing `src.dalton` internal functions.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from scripts.dalton.realrun.journal_dump import dump_journal, format_iso_utc

REPO_ROOT = Path(__file__).resolve().parents[3]

ALLOWED_SYNTHETIC_KINDS = frozenset(
    {
        "settings_api_budget",
        "knowledge_api_agenda",
        "watch_state_unsee_filing",
        "bridge_delay_hook",
        "registered_test_hook",
    }
)

FORBIDDEN_SYNTHETIC_SUBSTRINGS = (
    "direct_signal_insert",
    "direct_journal_insert",
    "direct_budget_insert",
    "preflight_check_direct",
    "enforce_local_admission_direct",
)

_SECRET_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9_\-]{20,}|xai-[A-Za-z0-9_\-]{20,}|gho_[A-Za-z0-9_]{20,}|ya29\.[A-Za-z0-9_\-.]{20,})"
)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_test_user_id(family: str) -> str:
    """Create a fresh, unique test user ID so runs never hit stale cached journal rows."""
    slug = re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    short_uid = uuid.uuid4().hex[:8]
    return f"realrun-{slug}-{stamp}-{short_uid}"


def get_git_sha(short: bool = True) -> str:
    cmd = ["git", "-C", str(REPO_ROOT), "rev-parse"]
    if short:
        cmd.append("--short=8")
    cmd.append("HEAD")
    return subprocess.check_output(cmd, text=True).strip()


def validate_synthetic_declarations(synthetic: list[Any]) -> list[dict[str, Any] | str]:
    """Validate `synthetic` list against P0I §2.1 #4 allowed injection categories."""
    validated: list[dict[str, Any] | str] = []
    for item in synthetic:
        if isinstance(item, str):
            raw_str = item.strip()
            for bad in FORBIDDEN_SYNTHETIC_SUBSTRINGS:
                if bad in raw_str:
                    raise ValueError(f"Forbidden synthetic injection declared: {raw_str}")
            kind_prefix = raw_str.split(":", 1)[0].strip()
            if kind_prefix not in ALLOWED_SYNTHETIC_KINDS:
                raise ValueError(
                    f"Synthetic kind '{kind_prefix}' is not in ALLOWED_SYNTHETIC_KINDS "
                    f"{sorted(ALLOWED_SYNTHETIC_KINDS)}"
                )
            validated.append(raw_str)
        elif isinstance(item, dict):
            kind = str(item.get("kind") or "").strip()
            entry_point = str(item.get("entry_point") or "").strip()
            if kind not in ALLOWED_SYNTHETIC_KINDS:
                raise ValueError(
                    f"Synthetic kind '{kind}' is not in ALLOWED_SYNTHETIC_KINDS "
                    f"{sorted(ALLOWED_SYNTHETIC_KINDS)}"
                )
            if not entry_point:
                raise ValueError(f"Synthetic declaration {item!r} must specify 'entry_point'")
            validated.append(item)
        else:
            raise TypeError(f"Unsupported synthetic item type: {type(item).__name__}")
    return validated


def compute_file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def assert_no_credentials_in_obj(obj: Any) -> None:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    m = _SECRET_PATTERN.search(text)
    if m:
        raise ValueError(f"Refusing to write evidence containing secret token pattern: {m.group(0)[:8]}***")


def build_evidence_envelope(
    *,
    script: str,
    started_at: str | datetime,
    ended_at: str | datetime,
    backend: str,
    test_user_id: str,
    synthetic: list[Any],
    journal: dict[str, Any] | None = None,
    dsn: str | None = None,
    include_null_user_journal: bool = False,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct a P0I §2.1 #2 compliant evidence JSON dictionary with fixed top keys."""
    started_iso = format_iso_utc(started_at) or now_utc_iso()
    ended_iso = format_iso_utc(ended_at) or now_utc_iso()
    validated_synthetic = validate_synthetic_declarations(synthetic)

    if journal is None:
        journal = dump_journal(
            user_id=test_user_id,
            since=started_iso,
            until=ended_iso,
            dsn=dsn,
            include_null_user=include_null_user_journal,
        )

    envelope: dict[str, Any] = {
        "evidence_version": 1,
        "git_sha": get_git_sha(short=True),
        "script": script,
        "started_at": started_iso,
        "ended_at": ended_iso,
        "backend": backend,
        "test_user_id": test_user_id,
        "synthetic": validated_synthetic,
        "journal": journal,
    }
    if extra_fields:
        for k, v in extra_fields.items():
            if k not in envelope:
                envelope[k] = v
    assert_no_credentials_in_obj(envelope)
    return envelope


def render_summary_markdown(
    *,
    family_code: str,
    title: str,
    raw_json_rel_path: str,
    raw_json_sha256: str,
    envelope: dict[str, Any],
    key_findings: list[tuple[str, Any]],
    notes: list[str] | None = None,
) -> str:
    """Render the trial-run Markdown summary required by P0I §2.1 #6."""
    journal = envelope.get("journal") or {}
    totals = journal.get("totals") or {}
    rows = journal.get("rows") or []
    synthetic = envelope.get("synthetic") or []

    lines: list[str] = [
        f"# {family_code} 真实运行验证摘要 — {title}",
        "",
        "## 1. 运行元数据与原始证据校验",
        "",
        "| 字段 | 值 |",
        "|---|---|",
        f"| `evidence_version` | `{envelope.get('evidence_version', 1)}` |",
        f"| `git_sha` | `{envelope.get('git_sha')}` |",
        f"| `script` | `{envelope.get('script')}` |",
        f"| `started_at` | `{envelope.get('started_at')}` |",
        f"| `ended_at` | `{envelope.get('ended_at')}` |",
        f"| `backend` | `{envelope.get('backend')}` |",
        f"| `test_user_id` | `{envelope.get('test_user_id')}` |",
        f"| 原始证据路径 | `{raw_json_rel_path}` |",
        f"| 原始 JSON `sha256` | `{raw_json_sha256}` |",
        "",
        "## 2. 非真实成分声明 (`synthetic`)",
        "",
    ]
    if not synthetic:
        lines.append("- `[]`（全链路无任何注入、模拟或替身）")
    else:
        for item in synthetic:
            if isinstance(item, dict):
                lines.append(
                    f"- **`{item.get('kind')}`**（入口：`{item.get('entry_point')}`）：{item.get('description', '')}"
                )
            else:
                lines.append(f"- `{item}`")

    lines.extend(
        [
            "",
            "## 3. 关键验证字段汇总",
            "",
            "| 检查项 | 实测结果 |",
            "|---|---|",
        ]
    )
    for k, v in key_findings:
        val_str = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        lines.append(f"| {k} | `{val_str}` |")

    lines.extend(
        [
            "",
            "## 4. 调用日志对账 (`dalton_llm_journal`)",
            "",
            f"- **总调用数 (`total_calls`)**：`{totals.get('total_calls', 0)}`",
            f"- **真实新调用 (`fresh_calls`, `replayed=false`)**：`{totals.get('fresh_calls', 0)}`",
            f"- **缓存回放 (`replayed_calls`, `replayed=true`)**：`{totals.get('replayed_calls', 0)}`",
            f"- **状态分布**：`done={totals.get('done_calls', 0)}`, `failed={totals.get('failed_calls', 0)}`, `indeterminate={totals.get('indeterminate_calls', 0)}`",
            f"- **日志现金花费 (`cash_usd`)**：`${totals.get('cash_usd', 0.0):.6f}`",
            f"- **日志名义花费 (`notional_usd`)**：`${totals.get('notional_usd', 0.0):.6f}`",
            f"- **对话轮次 (`conversation_turns`)**：`{totals.get('conversation_turns', 0)}`（对话现金 `${totals.get('conversation_cash_usd', 0.0):.6f}`）",
            f"- **合计现金花费 (`combined_cash_usd`)**：`${totals.get('combined_cash_usd', 0.0):.6f}`",
            "",
        ]
    )

    if rows:
        lines.extend(
            [
                "| `invocation_id` | `purpose` | `model_key` | `state` | `replayed` | `total_tokens` | `cash_usd` | `notional_usd` | `created_at` |",
                "|---|---|---|---|---|---:|---:|---:|---|",
            ]
        )
        for r in rows[:50]:
            inv = str(r.get("invocation_id") or "")
            short_inv = inv[:18] + "…" if len(inv) > 19 else inv
            lines.append(
                f"| `{short_inv}` | `{r.get('purpose')}` | `{r.get('model_key')}` | "
                f"`{r.get('state')}` | `{str(bool(r.get('replayed'))).lower()}` | "
                f"`{r.get('total_tokens', 0)}` | `${float(r.get('cash_usd', 0.0)):.6f}` | "
                f"`${float(r.get('notional_usd', 0.0)):.6f}` | `{r.get('created_at')}` |"
            )
        if len(rows) > 50:
            lines.append(f"| ... | 其余 {len(rows) - 50} 条记录见原始 JSON | | | | | | | |")
    else:
        lines.append("_本窗口内无 `dalton_llm_journal` 记录。_")

    if notes:
        lines.extend(["", "## 5. 补充说明", ""])
        for n in notes:
            lines.append(f"- {n}")

    lines.append("")
    return "\n".join(lines)


def write_evidence_and_summary(
    *,
    family_code: str,
    title: str,
    raw_json_path: str | Path,
    summary_md_path: str | Path,
    envelope: dict[str, Any],
    key_findings: list[tuple[str, Any]],
    notes: list[str] | None = None,
) -> tuple[Path, Path, str]:
    """Write raw JSON evidence to `var/dalton/...` and Markdown summary to `trial-runs/realrun-<date>/G<N>.md`."""
    assert_no_credentials_in_obj(envelope)
    raw_path = Path(raw_json_path)
    if not raw_path.is_absolute():
        raw_path = REPO_ROOT / raw_path
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    sha256_hex = compute_file_sha256(raw_path)
    try:
        rel_raw = raw_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel_raw = raw_path.as_posix()

    md_path = Path(summary_md_path)
    if not md_path.is_absolute():
        md_path = REPO_ROOT / md_path
    md_path.parent.mkdir(parents=True, exist_ok=True)

    md_text = render_summary_markdown(
        family_code=family_code,
        title=title,
        raw_json_rel_path=rel_raw,
        raw_json_sha256=sha256_hex,
        envelope=envelope,
        key_findings=key_findings,
        notes=notes,
    )
    assert_no_credentials_in_obj({"markdown": md_text})
    md_path.write_text(md_text, encoding="utf-8")
    return raw_path, md_path, sha256_hex


class RealRunHttpClient:
    """Thin HTTP client for operating against the live backend (`http://127.0.0.1:8000`)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        *,
        user_id: str = "local-dev-user",
        timeout_s: float = 120.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        headers = {"X-User-Id": user_id}
        if extra_headers:
            headers.update(extra_headers)
        self._client = httpx.Client(base_url=self.base_url, headers=headers, timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RealRunHttpClient:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        resp = self._client.request(
            method.upper(),
            path,
            json=json_body,
            params=params,
            headers=headers,
        )
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text
        return resp.status_code, resp.text
