"""Workspace file layout and path helpers for the company knowledge layer (PK.1)."""

from __future__ import annotations

from datetime import datetime, timezone

DOC_KEYS: tuple[str, ...] = (
    "company",
    "thesis",
    "debate",
    "drivers",
    "management",
    "lessons",
    "agenda",
    "models",
)

DATA_DIR = "data"
HISTORY_DIR = "history"
PUBLISHED_DIR = "published"
DRAFTS_DIR = "drafts"
MODEL_DIR = "model"
RESEARCH_DIR = "research"
SOURCES_DIR = "sources"

RENDERED: dict[str, str] = {
    "thesis": "thesis.md",
    "debate": "debate.md",
    "drivers": "drivers.md",
    "management": "management.md",
    "lessons": "lessons.md",
    "agenda": "agenda.md",
}

VERDICTS_MD = "verdicts.md"
PUBLISHED_INDEX = "published/INDEX.md"
AGENT_MD = "agent.md"

AGENT_SUMMARY_START = (
    "<!-- dalton:summary:start (由 knowledge_update 维护；手改会在下次更新时被重写) -->"
)
AGENT_SUMMARY_START_PREFIX = "<!-- dalton:summary:start"
AGENT_SUMMARY_END = "<!-- dalton:summary:end -->"

WATCHED_GLOBS: tuple[str, ...] = (
    "data/*.json",
    "published/**",
    *RENDERED.values(),
)


def data_path(doc_key: str) -> str:
    """Return workspace-relative path for a knowledge JSON document."""
    if doc_key not in DOC_KEYS:
        raise ValueError(f"Unknown knowledge doc_key: {doc_key!r}")
    return f"{DATA_DIR}/{doc_key}.json"


def rendered_path(doc_key: str) -> str | None:
    """Return workspace-relative path for a rendered Markdown file, if any."""
    return RENDERED.get(doc_key)


def _format_utc_stamp(at: datetime) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    else:
        at = at.astimezone(timezone.utc)
    return at.strftime("%Y%m%dT%H%M%SZ")


def history_path(doc_key: str, version_no: int, at: datetime) -> str:
    """Return workspace-relative history snapshot path for a knowledge document."""
    if doc_key not in DOC_KEYS:
        raise ValueError(f"Unknown knowledge doc_key: {doc_key!r}")
    stamp = _format_utc_stamp(at)
    return f"{HISTORY_DIR}/{doc_key}/{stamp}-v{version_no:04d}.json"


def history_edited_md_path(doc_key: str, version_no: int, at: datetime) -> str:
    """Return workspace-relative backup path when a rendered .md file was directly edited."""
    if doc_key not in RENDERED:
        raise ValueError(f"Doc key {doc_key!r} does not have a rendered markdown file")
    stamp = _format_utc_stamp(at)
    return f"{HISTORY_DIR}/{doc_key}/{stamp}-v{version_no:04d}-edited.md"


def render_header_comment(doc_key: str, version_no: int, at_iso: str) -> str:
    """Return the fixed first-line HTML comment for rendered Markdown files (PK.1)."""
    return (
        f"<!-- 由 data/{doc_key}.json 渲染（版本 v{version_no} · {at_iso}）；"
        "直接修改会被记录，请用 knowledge_update 合入 -->"
    )
