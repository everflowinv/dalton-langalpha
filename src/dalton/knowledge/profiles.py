"""Knowledge-layer schema profiles (sector layer P1).

One workspace, one profile. The profile decides WHICH knowledge documents a
workspace's knowledge layer accepts — nothing here relaxes validation for
company workspaces: with the flag off (the default) or without a registered
sector unit, the profile is ``company`` and the accepted doc set is exactly the
historic eight, byte for byte.

The profile follows P0's single identity resolver (``coverage.identity``): a
workspace with an ACTIVE registered ``sector`` unit is a sector workspace;
everything else — flag off, no unit, a company unit — is a company workspace,
i.e. today's behavior. The resolver itself needs the workspace row; the
knowledge layer only ever names non-Home workspaces (tools reject Home), so the
decisive step is the active-unit lookup, which is what this module performs.

Doc sets (paths.ALL_DOC_KEYS is the path-validity union):

- company: ``company thesis debate drivers management lessons agenda models``
  — the historic ``DOC_KEYS``.
- sector:   ``sector thesis debate drivers lessons agenda models`` — the shared
  docs keep their own schemas and invariants verbatim; ``management`` and
  ``company`` are company-only, ``sector`` is sector-only.
"""

from __future__ import annotations

from typing import Any

from src.dalton.knowledge.paths import DOC_KEYS, SECTOR_DOC_KEYS
from src.dalton.settings import is_coverage_units_enabled

PROFILE_COMPANY = "company"
PROFILE_SECTOR = "sector"

# Docs only one profile accepts — their handling (write admission, reconcile
# scan, rendering) needs a resolved identity; with the identity unknown they
# are skipped/refused rather than guessed (codex r2 #1).
PROFILE_SPECIFIC_DOCS: tuple[str, ...] = ("company", "management", "sector")

# Docs both profiles accept: identical schema and invariants either way, so
# detection and pending writes can proceed even while the identity is unknown.
SHARED_DOC_KEYS: tuple[str, ...] = tuple(
    k for k in DOC_KEYS if k in SECTOR_DOC_KEYS
)

_SECTOR_ONLY_DOCS = ("sector",)
_COMPANY_ONLY_DOCS = ("company", "management")

_COMPANY_REFUSAL = (
    "本工作区按公司覆盖处理（未绑定行业覆盖单元，或 coverage_units.enabled 关闭），"
    "不接受行业文档 sector。公司工作区可用文档：{docs}。"
)
_SECTOR_REFUSAL = (
    "本工作区绑定了行业覆盖单元（sector profile），{doc} 是公司专属文档，行业工作区不创建。"
    "行业工作区可用文档：{docs}；行业观点写到 thesis（不强制公司目标价等公司专属字段），"
    "成员公司在 sector 文档的 members 清单里维护。"
)


def doc_keys_for_profile(profile: str) -> tuple[str, ...]:
    """The doc keys a workspace with ``profile`` accepts."""
    return SECTOR_DOC_KEYS if profile == PROFILE_SECTOR else DOC_KEYS


def is_doc_allowed(doc_key: str, profile: str) -> bool:
    return doc_key in doc_keys_for_profile(profile)


def doc_not_allowed_message(doc_key: str, profile: str) -> str:
    """The refusal a company-only / sector-only doc gets in the other profile."""
    if profile == PROFILE_SECTOR:
        return _SECTOR_REFUSAL.format(doc=doc_key, docs=", ".join(SECTOR_DOC_KEYS))
    return _COMPANY_REFUSAL.format(docs=", ".join(DOC_KEYS))


async def resolve_workspace_profile(
    workspace_id: str,
    *,
    conn: Any = None,
) -> str:
    """The workspace's knowledge profile.

    Flag off → ``company`` without reading the registry at all (today's
    behavior). Flag on → the ACTIVE unit bound to the workspace decides; a
    missing table, a missing unit or a company unit all mean ``company``.
    """
    wid = str(workspace_id or "").strip()
    if not wid or not is_coverage_units_enabled():
        return PROFILE_COMPANY
    from src.dalton.database import coverage_units as cu_db

    unit = await cu_db.get_active_unit_by_workspace(wid, conn=conn)
    if unit is not None and str(unit.get("kind") or "").strip().lower() == "sector":
        return PROFILE_SECTOR
    return PROFILE_COMPANY
