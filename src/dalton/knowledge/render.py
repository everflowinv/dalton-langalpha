"""Chinese Markdown rendering for company knowledge documents and agent.md block (PK.1, PK.4, PK.6)."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from src.dalton.knowledge.paths import (
    AGENT_SUMMARY_END,
    AGENT_SUMMARY_START,
    AGENT_SUMMARY_START_PREFIX,
    RENDERED,
    render_header_comment,
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

VERDICT_ZH: dict[str, str] = {
    "NO_CHANGE": "无变化",
    "THESIS_STRENGTHENED": "强化",
    "THESIS_WEAKENED": "削弱",
    "THESIS_BROKEN": "证伪",
    "NEW_THESIS": "新论点",
}

STAGE_ZH: dict[str, str] = {
    "onboarding": "入职迁移",
    "initial": "初步覆盖",
    "full": "完整覆盖中",
    "maintenance": "日常跟踪",
    "paused": "暂停覆盖",
}

MAX_AGENT_BLOCK_CHARS = 2500


@lru_cache(maxsize=1)
def _jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def _format_iso(at: datetime | str | None) -> str:
    if isinstance(at, str) and at:
        return at
    if isinstance(at, datetime):
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        else:
            at = at.astimezone(timezone.utc)
        return at.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _trunc(text: str | None, max_len: int = 80) -> str:
    if not text:
        return ""
    s = " ".join(str(text).split())
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def render_document_md(
    doc_key: str,
    doc: dict[str, Any],
    *,
    version_no: int = 1,
    at: datetime | str | None = None,
    ticker: str = "",
) -> str | None:
    """Render ``data/<doc_key>.json`` into its human-readable ``<doc_key>.md``."""
    if doc_key not in RENDERED:
        return None
    at_iso = _format_iso(at or doc.get("as_of"))
    header = render_header_comment(doc_key, version_no, at_iso)
    tmpl = _jinja_env().get_template(f"{doc_key}.md.j2")
    eff_ticker = ticker or str(doc.get("ticker") or "").strip() or doc_key.upper()
    rendered = tmpl.render(
        header_comment=header,
        ticker=eff_ticker,
        doc=doc,
        verdict_zh=VERDICT_ZH,
    )
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


def render_verdicts_md(
    verdicts: list[dict[str, Any]],
    *,
    ticker: str = "",
    rendered_at: datetime | str | None = None,
) -> str:
    """Render ``verdicts.md`` for the most recent 60 verdicts."""
    recent = verdicts[:60]
    normalized: list[dict[str, Any]] = []
    for row in recent:
        if "payload" in row and isinstance(row["payload"], dict):
            item = dict(row["payload"])
            item.setdefault("id", row.get("verdict_id"))
            item.setdefault("verdict", row.get("word"))
            item.setdefault("at", _format_iso(row.get("verdict_at")))
            normalized.append(item)
        else:
            normalized.append(row)
    tmpl = _jinja_env().get_template("verdicts.md.j2")
    out = tmpl.render(
        ticker=ticker or "COMPANY",
        verdicts=normalized,
        verdict_zh=VERDICT_ZH,
        rendered_at=_format_iso(rendered_at),
    )
    return out if out.endswith("\n") else out + "\n"


def render_published_index_md(
    publications: list[dict[str, Any]],
    *,
    ticker: str = "",
    rendered_at: datetime | str | None = None,
) -> str:
    """Render ``published/INDEX.md`` from ``dalton_publications`` rows."""
    tmpl = _jinja_env().get_template("published_index.md.j2")
    out = tmpl.render(
        ticker=ticker or "COMPANY",
        publications=publications,
        rendered_at=_format_iso(rendered_at),
    )
    return out if out.endswith("\n") else out + "\n"


def render_agent_block(
    *,
    ticker: str,
    company_doc: dict[str, Any] | None = None,
    thesis_doc: dict[str, Any] | None = None,
    thesis_version_no: int | None = None,
    debate_doc: dict[str, Any] | None = None,
    agenda_doc: dict[str, Any] | None = None,
    models_doc: dict[str, Any] | None = None,
    last_verdict: dict[str, Any] | None = None,
    alerts: list[str] | None = None,
) -> str:
    """Render the managed block inside ``agent.md`` (guaranteed <= 2,500 chars, PK.6)."""
    c = company_doc or {}
    t = thesis_doc or {}
    d = debate_doc or {}
    a = agenda_doc or {}
    m = models_doc or {}

    eff_ticker = str(c.get("ticker") or ticker or "COMPANY").upper()
    name = c.get("name") or eff_ticker
    tier = str(c.get("coverage_tier") or "core").capitalize()
    stage = STAGE_ZH.get(str(c.get("coverage_stage") or ""), c.get("coverage_stage") or "覆盖中")
    header_title = f"{eff_ticker}（{name} · {tier}，{stage}）"

    one_liner = _trunc(t.get("one_liner") or c.get("one_liner") or "待初始化", 110)
    hv = t.get("house_view") or {}
    hv_stance = hv.get("stance") or "no_view"
    hv_conv = hv.get("conviction") or "none"
    v_str = f"v{thesis_version_no}" if thesis_version_no else "v1"
    as_of_date = str(t.get("as_of") or "")[:10]
    ver_meta = f"{v_str}，{as_of_date}" if as_of_date else v_str
    one_liner_line = f"{one_liner}；house view：{hv_stance} / {hv_conv}（{ver_meta}）。"

    official = t.get("official")
    if isinstance(official, dict) and official.get("version_no"):
        off_date = str(official.get("approved_at") or "")[:10]
        approved_by = official.get("approved_by") or "PM"
        pub_id = str(official.get("publication_id") or "")[:8]
        date_prefix = f"{off_date} " if off_date else ""
        pub_suffix = f"（publication {pub_id}）" if pub_id else ""
        official_line = f"thesis v{official['version_no']}，{date_prefix}{approved_by} 批准{pub_suffix}"
    else:
        official_line = "尚未发布正式版本（迁移观点或研究草稿，待复核）"

    agg_status = t.get("aggregate_status") or "untested"
    pillar_parts = [f"aggregate {agg_status}"]
    for p in (t.get("pillars") or [])[:5]:
        if isinstance(p, dict):
            p_tokens = [str(x) for x in (p.get("id") or "?", p.get("status")) if x]
            if p_tokens:
                pillar_parts.append(" ".join(p_tokens))
    thesis_status_line = "｜".join(pillar_parts)

    debate_parts: list[str] = []
    for db_item in (d.get("debates") or [])[:3]:
        if isinstance(db_item, dict):
            mv = db_item.get("my_view") or {}
            d_head = " ".join(
                str(x)
                for x in (db_item.get("id") or "D?", _trunc(db_item.get("variable"), 28))
                if x
            )
            view_tokens = [str(x) for x in (mv.get("side"), mv.get("confidence")) if x]
            view_suffix = f"（我方：{'/'.join(view_tokens)}）" if view_tokens else ""
            debate_parts.append(f"{d_head}{view_suffix}")
    debates_line = "｜".join(debate_parts) if debate_parts else "暂无"

    pending_vals = [
        v
        for v in (a.get("next_validations") or [])
        if isinstance(v, dict) and v.get("status") == "pending"
    ]
    next_validation: dict[str, str] | None = None
    if pending_vals:
        v0 = pending_vals[0]
        v_id = str(v0.get("id") or "V?")
        v_due = str(v0.get("due") or "").strip()
        v_what = _trunc(v0.get("what"), 45)
        v_exp = _trunc(v0.get("expected"), 45)
        next_validation = {
            "id": v_id,
            "due": v_due,
            "what": v_what,
            "expected": v_exp,
        }
        head_parts = [x for x in (v_id, v_due, v_what) if x]
        next_validation_line = " ".join(head_parts) + (f"：{v_exp}" if v_exp else "")
    else:
        next_validation_line = "暂无待验证项"

    lv = last_verdict or a.get("last_verdict")
    last_verdict_item: dict[str, str] | None = None
    if isinstance(lv, dict) and (lv.get("verdict_id") or lv.get("id")):
        vid = str(lv.get("verdict_id") or lv.get("id"))
        word = str(lv.get("word") or lv.get("verdict") or "NO_CHANGE")
        w_zh = VERDICT_ZH.get(word, word) or ""
        ev_text = _trunc(lv.get("event") or "", 45)
        last_verdict_item = {
            "id": vid,
            "word_zh": w_zh,
            "event": ev_text,
        }
        last_verdict_line = (
            " ".join(x for x in (vid, w_zh) if x)
            + (f"（{ev_text}）" if ev_text else "")
        )
    else:
        last_verdict_line = "暂无"

    waiting_handoffs = [
        h
        for h in (a.get("pm_handoffs") or [])
        if isinstance(h, dict) and h.get("status") == "waiting"
    ]
    if waiting_handoffs:
        waiting_pm_line = "｜".join(
            " ".join(x for x in (str(h.get("id") or "H?"), _trunc(h.get("what"), 40)) if x)
            for h in waiting_handoffs[:3]
        )
    else:
        waiting_pm_line = ""

    model_current = (
        f"当前 {m['current']}" if isinstance(m, dict) and m.get("current") else "暂无正式模型"
    )
    alert_lines = "；".join(str(x) for x in alerts[:3] if x) if alerts else ""

    tmpl = _jinja_env().get_template("agent_block.md.j2")
    rendered = tmpl.render(
        header_title=header_title,
        one_liner_line=one_liner_line,
        official_line=official_line,
        thesis_status_line=thesis_status_line,
        debates_line=debates_line,
        next_validation=next_validation,
        next_validation_line=next_validation_line,
        last_verdict_item=last_verdict_item,
        last_verdict_line=last_verdict_line,
        waiting_pm_line=waiting_pm_line,
        model_current=model_current,
        alert_lines=alert_lines,
    ).strip()

    if len(rendered) > MAX_AGENT_BLOCK_CHARS:
        # Ensure hard cap <= 2,500 chars while keeping closing marker intact
        body = rendered[: MAX_AGENT_BLOCK_CHARS - len(AGENT_SUMMARY_END) - 10].rstrip()
        rendered = f"{body}\n{AGENT_SUMMARY_END}"

    return rendered


_BLOCK_PATTERN = re.compile(
    r"<!--\s*dalton:summary:start.*?-->.*?<!--\s*dalton:summary:end\s*-->",
    re.DOTALL,
)


def update_agent_md_block(
    existing_agent_md: str | None,
    rendered_block: str,
) -> str:
    """Replace only the managed block inside ``agent.md``, preserving all outside text."""
    block = rendered_block.strip()
    if not block.startswith(AGENT_SUMMARY_START_PREFIX):
        block = f"{AGENT_SUMMARY_START}\n{block}"
    if not block.endswith(AGENT_SUMMARY_END):
        block = f"{block}\n{AGENT_SUMMARY_END}"

    if not existing_agent_md or not existing_agent_md.strip():
        return block + "\n"

    if _BLOCK_PATTERN.search(existing_agent_md):
        updated = _BLOCK_PATTERN.sub(lambda _: block, existing_agent_md, count=1)
        return updated if updated.endswith("\n") else updated + "\n"

    return f"{block}\n\n{existing_agent_md.lstrip()}"
