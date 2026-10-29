"""Language review of formal deliverables: flag issues, never rewrite (check_language).

A formal deliverable is any agent-written Markdown left in the workspace for
people to read — ``is_reviewable_deliverable`` below is the one owner of that
rule. Every deliverable owes two rounds: round 1 after it is written (for
reviewed drafts, before the first independent review), round 2 after its
content is final — for reviewed drafts, before the last review, the order that
keeps every review's ``draft_sha256`` binding intact; for write-once notes,
right after the round-1 fixes. Each call appends one record to
``<draft>.language.json`` and rewrites the human checklist
``<draft>.language.md``; nothing is written unless the whole review succeeded
(model down / timeout = error, not a pass).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ptc_agent.core.paths import DEFAULT_LAYOUT, resolve_agent_path
from src.dalton.auth import verify_workspace_access
from src.dalton.llm.assist import assist_json
from src.dalton.llm.model_prefs import (
    resolve_dalton_audit_timeout_s,
    resolve_dalton_language_model,
)
from src.dalton.settings import get_dalton_config

logger = logging.getLogger(__name__)

LANGUAGE_TASK = "language.review"

# The rules ARE the review, so they are read from the shipped skill rather than
# duplicated here; a missing file is a loud error, never a silent no-rules pass.
SKILL_RULES_PATH = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-language"
    / "SKILL.md"
)

# A chunk is one heading section where possible; the model only sees whole
# chunks, so every quote it returns is quotable verbatim from the draft.
_CHUNK_CHAR_BUDGET = 6000
_CONCURRENCY = 4

_HEADING_RE = re.compile(r"^#{1,3} ", re.MULTILINE)

# The closed set of issue types, mirroring dalton-language SKILL's flag list.
# The addendum's two new families (negation_defensive, translationese) live
# here, in the output schema and in the review prompt — one list, three uses.
ISSUE_TYPES: tuple[str, ...] = (
    "filler_opening",
    "meta_transitional",
    "redundancy",
    "wordiness",
    "credentialing",
    "negation_defensive",
    "translationese",
)

_ISSUES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    # No enum here on purpose: assist_json schema-validates the
                    # whole response, so an enum would turn one stray issue_type
                    # into a failed review. The enum is enforced per item in
                    # ``_collect_issues`` (dropped and counted).
                    "quote": {"type": "string"},
                    "issue_type": {"type": "string"},
                    "why": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["section", "quote", "issue_type", "why", "suggestion"],
            },
        }
    },
    "required": ["issues"],
}

_rules_cache: str | None = None


def _load_skill_rules() -> str:
    global _rules_cache
    if _rules_cache is None:
        _rules_cache = SKILL_RULES_PATH.read_text(encoding="utf-8")
    return _rules_cache


def _utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


# ---- What counts as a formal deliverable (user's 2026-10-24 scope) --------
# Every agent-written Markdown left in the workspace for people to read is
# reviewable; raw/machine files, scraped-data directories, knowledge-layer
# renders (knowledge/paths.py serves them from the workspace ROOT, so the
# drafts/research allowlist already excludes them), machine-rendered
# conversation digests and verification sidecars are not. ``published/`` is
# deliberately out: publish_report writes the archive AFTER the review with a
# banner prepended (different hash) and never edits it again — its language
# review is the draft's, which the two rounds already cover. This function is
# the ONE owner of the rule — workflows, SKILLs and completion contracts all
# defer to it, none keeps its own list.
_REVIEWABLE_ROOTS = ("drafts", "research")
_EXCLUDED_SEGMENTS = frozenset({"bg_data", "data", "lenses_work", "model_dump"})
_MACHINE_RENDERED_PREFIXES = ("research/topics/conversations/",)
# Public: completion-contract glob counting excludes these too — a
# ``cluster-*.md`` pattern would otherwise count ``cluster-a.md.language.md``.
SIDECAR_SUFFIXES = (".audit.md", ".annotated.md", ".language.md", ".language.json")
_SIDECAR_SUFFIXES = SIDECAR_SUFFIXES


def _normalize_relpath(path: str, workspace_dir: str | None = None) -> str:
    """One canonical workspace-relative form (also strips evidence entries'
    ``|sha256`` tails); see check_done's ``_workspace_relative`` for the twin."""
    cleaned = str(path or "").split("|", 1)[0].strip()
    root = str(workspace_dir).rstrip("/") if workspace_dir else ""
    if not root:
        cleaned = posixpath.normpath(cleaned)
    else:
        absolute = cleaned if cleaned.startswith("/") else posixpath.join(root, cleaned)
        cleaned = posixpath.normpath(absolute)
        if cleaned.startswith(root + "/"):
            cleaned = cleaned[len(root) + 1 :]
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def is_reviewable_deliverable(path: str, workspace_dir: str | None = None) -> bool:
    """Whether ``path`` is a formal deliverable owed two language-review
    rounds: a human-readable ``.md`` under ``drafts/`` or ``research/`` that
    is not machine data, a knowledge render, a machine-rendered conversation
    digest or a verification sidecar."""
    rel = _normalize_relpath(path, workspace_dir)
    parts = [p for p in rel.split("/") if p]
    if not parts or parts[0] not in _REVIEWABLE_ROOTS:
        return False
    if not rel.endswith(".md") or rel.endswith(_SIDECAR_SUFFIXES):
        return False
    if any(rel.startswith(prefix) for prefix in _MACHINE_RENDERED_PREFIXES):
        return False
    return not any(seg in _EXCLUDED_SEGMENTS for seg in parts)


def language_sidecar_paths(draft_path: str) -> tuple[str, str]:
    """``(<draft>.language.json, <draft>.language.md)`` — the full draft path is
    kept (``x.md.language.json``), unlike check_draft's stem swap, so the sidecar
    never collides with another ``x-language`` draft stem."""
    return f"{draft_path}.language.json", f"{draft_path}.language.md"


def split_sections(text: str, budget: int = _CHUNK_CHAR_BUDGET) -> list[tuple[str, str]]:
    """``[(section_name, chunk), …]`` split on markdown headings, then packed to
    ``budget`` chars at paragraph boundaries. A section longer than the budget
    becomes several chunks under the same name."""
    matches = list(_HEADING_RE.finditer(text))
    sections: list[tuple[str, str]] = []
    if not matches:
        sections.append((Path("draft").stem, text))
    else:
        if matches[0].start() > 0:
            sections.append(("（开头）", text[: matches[0].start()]))
        for i, m in enumerate(matches):
            line_end = text.find("\n", m.start())
            name = text[m.start() : line_end if line_end != -1 else len(text)].lstrip("# ").strip()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            sections.append((name or "（无标题）", text[m.start() : end]))

    chunks: list[tuple[str, str]] = []
    for name, body in sections:
        if not body.strip():
            continue
        if len(body) <= budget:
            chunks.append((name, body))
            continue
        paragraphs = body.split("\n\n")
        current = ""
        for para in paragraphs:
            candidate = f"{current}\n\n{para}" if current else para
            if len(candidate) > budget and current:
                chunks.append((name, current))
                current = para
            else:
                current = candidate
        if current:
            chunks.append((name, current))
    return chunks or [("（空稿）", "")]


def _review_instructions(round_no: int) -> str:
    return (
        "你执行 Dalton 的语言审查（只标问题模式）。规则全文如下，严格遵守其中"
        "「保住什么」「标什么问题」「投研写作豁免」与「只标问题的输出约定」：\n\n"
        f"{_load_skill_rules()}\n\n"
        f"现在审查草稿的一个节选（第 {round_no} 轮）。只报节选内的问题；每条 issue 给出 "
        "section（节选的章节名）、quote（逐字摘自节选的原句片段，不改任何字符、标点与空格）、"
        f"issue_type（只能从枚举里选：{' / '.join(ISSUE_TYPES)}）、"
        "why（由原文支持的具体理由）、suggestion（修改方向，不是改写文本）。"
        "重点覆盖否定式/防御式句式（negation_defensive：先否定再肯定的套路、预防式免责与情态堆叠；"
        "承载事实或判断的否定必须保留）与英文直译腔（translationese：'3Q 打印''落地在 X''beat 了预期'"
        "'指引的上修'这类句式与动词直译；Dalton 约定保留的英文术语不报）。"
        "没有明确问题就返回空 issues，不凑条数；不输出改写稿、评分或“AI 含量”，"
        "也不判断文章是不是 AI 写的。"
    )


def _collect_issues(res: Any, section: str) -> tuple[list[dict[str, Any]], int]:
    """(issues, dropped_for_type): items with a quote and an enum-valid
    ``issue_type``; anything else from the model is dropped and counted."""
    data = res.data if isinstance(getattr(res, "data", None), dict) else {}
    issues: list[dict[str, Any]] = []
    dropped = 0
    for item in data.get("issues") or []:
        if not isinstance(item, dict):
            continue
        quote = item.get("quote")
        issue_type = str(item.get("issue_type") or "").strip()
        if not isinstance(quote, str) or not quote.strip():
            continue
        if issue_type not in ISSUE_TYPES:
            dropped += 1
            continue
        issues.append(
            {
                "section": str(item.get("section") or section),
                "quote": quote,
                "issue_type": issue_type,
                "why": str(item.get("why") or ""),
                "suggestion": str(item.get("suggestion") or ""),
            }
        )
    return issues, dropped


def _render_language_md(doc: dict[str, Any], draft_path: str) -> str:
    lines = [f"# 语言审查清单：{draft_path}", ""]
    for record in doc.get("rounds") or []:
        lines.append(
            f"## 第 {record.get('round')} 轮 · {record.get('generated_at', '')} · "
            f"{len(record.get('issues') or [])} 条问题"
            f"（丢弃 {record.get('dropped_quotes', 0)} 条无法逐字定位的引用）"
        )
        lines.append("")
        lines.append(f"- 审查模型：{record.get('model', '')}")
        lines.append(f"- 草稿哈希：`{str(record.get('draft_sha256', ''))[:16]}…`")
        if record.get("unchanged_since_round_1"):
            lines.append("- ⚠️ 第 1 轮之后未修改：本轮仍照常审查，但请确认第 1 轮清单已处理。")
        issues = record.get("issues") or []
        if not issues:
            lines.append("")
            lines.append("无问题。")
        for i, issue in enumerate(issues, 1):
            lines.append("")
            lines.append(f"{i}. **[{issue['section']}] {issue['issue_type']}**")
            lines.append(f"   - 原句：{issue['quote']}")
            lines.append(f"   - 理由：{issue['why']}")
            lines.append(f"   - 建议：{issue['suggestion']}")
        lines.append("")
    return "\n".join(lines)


# In-process per-draft review locks keyed by (workspace_id, resolved_draft_path).
# Cleaned up in finally when the active review finishes so the dict never leaks.
_DRAFT_REVIEW_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _resolve_lock_path(backend: Any, draft_path: str) -> str:
    """Resolve ``draft_path`` using the backend's own path normalizer
    (``SandboxBackend.normalize_path`` / ``resolve_agent_path``) so ``drafts/a.md``,
    ``/drafts/a.md``, and the workspace absolute path share one lock key."""
    normalize = getattr(backend, "normalize_path", None)
    if callable(normalize):
        resolved = normalize(draft_path)
        if isinstance(resolved, str) and resolved:
            return resolved
    workspace = getattr(backend, "workspace_dir", None)
    if not isinstance(workspace, str) or not workspace:
        workspace = DEFAULT_LAYOUT.root
    root = getattr(backend, "computer_root", None)
    if not isinstance(root, str) or not root:
        root = DEFAULT_LAYOUT.root
    return resolve_agent_path(
        draft_path,
        workspace=workspace,
        root=root,
        allowed=(workspace, root, *DEFAULT_LAYOUT.allowed_directories),
    )


async def _load_sidecar_doc(
    backend: Any, json_path: str, draft_path: str
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        existing = await backend.aread_text(json_path)
    except Exception as e:
        logger.error("check_language sidecar read failed (%s): %s", json_path, e)
        return None, (
            f"ERROR: 语言审查记录读取失败（{json_path}：{e}），不会覆盖；请稍后重试。"
        )
    doc: dict[str, Any] = {"draft": draft_path, "rounds": []}
    if existing is not None and existing.strip():
        try:
            loaded = json.loads(existing)
        except ValueError:
            return None, (
                f"ERROR: 语言审查记录不可解析（{json_path}），不会覆盖；"
                "请修复或删除该文件后重试。"
            )
        if not isinstance(loaded, dict) or not isinstance(loaded.get("rounds"), list):
            return None, (
                f"ERROR: 语言审查记录结构异常（{json_path}），不会覆盖；"
                "请修复或删除该文件后重试。"
            )
        doc = loaded
    return doc, None


async def _write_sidecar_text(backend: Any, path: str, text: str) -> bool:
    try:
        res = await backend.awrite_text(path, text)
    except Exception as e:
        logger.error("check_language sidecar write failed (%s): %s", path, e)
        return False
    return res is not False


async def run_check_language(
    *,
    workspace_id: str,
    draft_path: str,
    round: int,
    backend: Any,
    user_id: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Execute ``check_language``: review one draft per the dalton-language rules,
    append the round record to the sidecar pair, and never write on failure."""
    if not user_id or not str(user_id).strip():
        raise ValueError("user_id is required")
    user_id = str(user_id).strip()
    if round not in (1, 2):
        return {
            "ok": False,
            "message": "ERROR: round 只能是 1 或 2：第 1 轮在初稿写完、第一次独立审稿之前；第 2 轮在终稿语言修改完成后、最后一次独立审稿之前。",
        }
    await verify_workspace_access(workspace_id, user_id)
    if backend is None:
        return {"ok": False, "message": "ERROR: filesystem backend is unavailable."}

    lock_key = (str(workspace_id).strip(), _resolve_lock_path(backend, draft_path))
    lock = _DRAFT_REVIEW_LOCKS.get(lock_key)
    if lock is not None and lock.locked():
        logger.warning(
            "Concurrent check_language rejected (workspace_id=%s, draft_path=%s, round=%s)",
            workspace_id,
            draft_path,
            round,
        )
        return {
            "ok": False,
            "message": (
                f"ERROR: 另一轮审查进行中（{draft_path}），请等待当前语言审查完成后再调用 check_language。"
            ),
        }
    if lock is None:
        lock = asyncio.Lock()
        _DRAFT_REVIEW_LOCKS[lock_key] = lock
    await lock.acquire()
    try:
        return await _run_check_language_locked(
            workspace_id=workspace_id,
            draft_path=draft_path,
            round=round,
            backend=backend,
            user_id=user_id,
            thread_id=thread_id,
        )
    finally:
        lock.release()
        if not lock.locked() and _DRAFT_REVIEW_LOCKS.get(lock_key) is lock:
            _DRAFT_REVIEW_LOCKS.pop(lock_key, None)


async def _run_check_language_locked(
    *,
    workspace_id: str,
    draft_path: str,
    round: int,
    backend: Any,
    user_id: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    try:
        draft_text = await backend.aread_text(draft_path)
    except Exception as e:
        logger.error("check_language draft read failed (%s): %s", draft_path, e)
        return {"ok": False, "message": f"ERROR: 草稿读取失败（{draft_path}：{e}），请稍后重试。"}
    if draft_text is None:
        return {"ok": False, "message": f"ERROR: Draft file not found at {draft_path}"}

    json_path, md_path = language_sidecar_paths(draft_path)
    doc, load_err = await _load_sidecar_doc(backend, json_path, draft_path)
    if load_err is not None or doc is None:
        return {"ok": False, "message": load_err or f"ERROR: 语言审查记录读取失败（{json_path}）"}

    draft_sha = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
    round1_records = [r for r in doc["rounds"] if isinstance(r, dict) and r.get("round") == 1]
    if round == 2 and not round1_records:
        return {
            "ok": False,
            "message": (
                "ERROR: 第 2 轮必须在第 1 轮之后：先调用 check_language(draft_path=…, round=1)，"
                "按清单修改后再做第 2 轮。"
            ),
        }
    unchanged_since_round_1 = bool(
        round == 2
        and round1_records
        and str(round1_records[-1].get("draft_sha256") or "") == draft_sha
    )

    try:
        _load_skill_rules()
    except Exception as e:
        logger.error("dalton-language skill rules unreadable: %s", e)
        return {
            "ok": False,
            "message": (
                f"ERROR: 语言审查规则文件缺失或不可读（{SKILL_RULES_PATH}），本次审查不执行。"
            ),
        }

    model = await resolve_dalton_language_model(user_id)
    if not model:
        return {
            "ok": False,
            "message": (
                "ERROR: 语言审查未配置模型：请在 设置 → 模型 → 高级 → 语言审查 指定，"
                "或在 dalton.yaml 的 publish.language_model（留空沿用 publish.audit_model）配置。"
            ),
        }
    timeout_s = await resolve_dalton_audit_timeout_s(user_id)
    deadline = time.monotonic() + get_dalton_config().publish.audit_total_timeout_s

    chunks = split_sections(draft_text)
    instructions = _review_instructions(round)
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    dropped_issue_types = [0]

    async def _review_chunk(section: str, chunk: str) -> list[dict[str, Any]]:
        async with semaphore:
            res = await assist_json(
                task=LANGUAGE_TASK,
                instructions=instructions,
                payload={
                    "draft_path": draft_path,
                    "round": round,
                    "section": section,
                    "excerpt": chunk,
                },
                schema=_ISSUES_SCHEMA,
                user_id=user_id,
                role="audit",
                timeout_s=timeout_s,
                model=model,
                workspace_id=workspace_id,
                thread_id=thread_id,
                deadline=deadline,
            )
        if not res.ok:
            raise RuntimeError(f"第 {round} 轮语言审查调用失败（{res.error or res.status}）")
        chunk_issues, chunk_dropped = _collect_issues(res, section)
        dropped_issue_types[0] += chunk_dropped
        return chunk_issues

    try:
        results = await asyncio.gather(*(
            _review_chunk(section, chunk) for section, chunk in chunks
        ))
    except Exception as e:
        return {
            "ok": False,
            "message": (
                f"ERROR: 语言审查第 {round} 轮未完成（{e}）。未写侧车，不算通过；"
                "请稍后重试或检查语言审查模型配置。"
            ),
        }

    # Re-read the draft before touching the sidecar: if the draft changed while
    # the review was in flight, discard this round so the sidecar never records
    # a stale sha256 as reviewed.
    try:
        current_draft_text = await backend.aread_text(draft_path)
    except Exception as e:
        logger.error("check_language draft re-read failed (%s): %s", draft_path, e)
        return {
            "ok": False,
            "message": f"ERROR: 草稿重读失败（{draft_path}：{e}），本轮未写入记录；请稍后重试。",
        }
    current_sha = (
        hashlib.sha256(current_draft_text.encode("utf-8")).hexdigest()
        if current_draft_text is not None
        else None
    )
    if current_sha != draft_sha:
        logger.warning(
            "Stale check_language discarded: draft modified during review "
            "(workspace_id=%s, draft_path=%s, round=%s, start_sha256=%s, current_sha256=%s)",
            workspace_id,
            draft_path,
            round,
            draft_sha,
            current_sha,
        )
        return {
            "ok": False,
            "stale": True,
            "message": (
                f"ERROR: 审查期间稿件已改动，请对当前稿件重新调用 check_language"
                f"（{draft_path}，起始哈希 {draft_sha[:12]}…，当前哈希 {(current_sha or 'missing')[:12]}…；"
                "本轮作废，未写入通过记录）。"
            ),
        }

    # Re-read the sidecar under the per-draft lock before appending so any
    # external write during the LLM call is preserved rather than overwritten.
    latest_doc, latest_err = await _load_sidecar_doc(backend, json_path, draft_path)
    if latest_err is not None or latest_doc is None:
        return {"ok": False, "message": latest_err or f"ERROR: 语言审查记录读取失败（{json_path}）"}
    if doc["rounds"] and not latest_doc.get("rounds"):
        return {
            "ok": False,
            "message": (
                f"ERROR: 语言审查记录重读失败（{json_path}），不会覆盖已有轮次记录；请稍后重试。"
            ),
        }
    if latest_doc.get("rounds") != doc.get("rounds"):
        doc = latest_doc
        if round == 2:
            latest_round1 = [
                r for r in doc["rounds"] if isinstance(r, dict) and r.get("round") == 1
            ]
            if latest_round1:
                unchanged_since_round_1 = (
                    str(latest_round1[-1].get("draft_sha256") or "") == draft_sha
                )

    raw_issues = [issue for chunk_issues in results for issue in chunk_issues]
    issues = [issue for issue in raw_issues if issue["quote"] in draft_text]
    dropped = len(raw_issues) - len(issues)

    record = {
        "round": round,
        "draft_sha256": draft_sha,
        "model": model,
        "generated_at": _utc_now_iso(),
        "issues": issues,
        "dropped_quotes": dropped,
    }
    if dropped_issue_types[0]:
        record["dropped_issue_types"] = dropped_issue_types[0]
    if unchanged_since_round_1:
        record["unchanged_since_round_1"] = True
    doc["rounds"].append(record)

    # The checklist first, the record last: the contract reads the json, so a
    # half-written pair can never be "json without checklist". Either write
    # reporting failure (a falsy return or exception) fails the whole round — no
    # sidecar pair, not a pass.
    language_md = _render_language_md(doc, draft_path)
    if not await _write_sidecar_text(backend, md_path, language_md):
        return {
            "ok": False,
            "message": (
                f"ERROR: 语言审查清单写入失败（{md_path}），本轮不算通过，未写记录；请重试。"
            ),
        }
    language_json = json.dumps(doc, ensure_ascii=False, indent=2)
    if not await _write_sidecar_text(backend, json_path, language_json):
        return {
            "ok": False,
            "message": (
                f"ERROR: 语言审查记录写入失败（{json_path}），本轮不算通过；请重试"
                "（清单已写，记录缺失）。"
            ),
        }

    note = (
        "；⚠️ 第 1 轮之后未修改（本轮仍照常审查，请确认第 1 轮清单已处理）"
        if unchanged_since_round_1
        else ""
    )
    type_note = (
        f"（另丢弃 {dropped_issue_types[0]} 条 issue_type 非法的条目）"
        if dropped_issue_types[0]
        else ""
    )
    summary = (
        f"语言审查第 {round} 轮完成：{len(issues)} 条问题"
        f"（丢弃 {dropped} 条无法逐字定位的引用{type_note}），清单 {md_path}{note}。"
    )
    return {
        "ok": True,
        "round": round,
        "draft_sha256": draft_sha,
        "model": model,
        "issues": issues,
        "dropped_quotes": dropped,
        "dropped_issue_types": dropped_issue_types[0],
        "unchanged_since_round_1": unchanged_since_round_1,
        "json_path": json_path,
        "md_path": md_path,
        "message": summary,
    }
