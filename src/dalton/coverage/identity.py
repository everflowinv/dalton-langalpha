"""The single workspace coverage-identity resolver (sector layer P0, round 2).

Auto-tracking (`tracking._resolve_workspace_ticker`), the director board
(`autonomy/board.py`) and the director probe (`autonomy/probes/director.py`)
all ask THIS module — and only this module — whether a workspace is a company
and what its ticker is. One rule, one result, no per-call-site re-derivation.

Hard resolution order (`resolve_workspace_coverage_identity`):

1. The owner's Home → `home`. Never a company, whatever its name looks like
   and whatever is registered against it.
2. While `coverage_units.enabled` is on, the active `dalton_coverage_units`
   row bound to the workspace → `registered_unit`:
   - `sector` → never a company and never a ticker, unconditionally — neither
     a legacy company doc nor a ticker-shaped name can override it;
   - `company` → company; the ticker is the unit's ticker, falling back to the
     compatible rules below when the unit carries none.
3. Compatible rules (the whole rule set while the flag is off), in tracking's
   historical order — any hit makes the workspace a company with that ticker:
   workspace metadata ticker → company-doc ticker (thesis ticker when the
   company doc has none) → workspace name that is EXACTLY a ticker (the
   bootstrap folder-name convention; a leading-token match is never enough) →
   this workspace's existing `dalton-<t>-*` automation rows.
   Nothing hits → `unclassified`.

Note on the metadata step: today's `workspaces` table has no `metadata`/
`ticker` column and `get_workspace()`'s projection returns neither, so that
step is currently unreachable in production. It stays because it is tracking's
documented historical order, not because any row can still carry it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from src.dalton.settings import is_coverage_units_enabled

KIND_COMPANY = "company"
KIND_SECTOR = "sector"
KIND_HOME = "home"
KIND_UNCLASSIFIED = "unclassified"

SOURCE_REGISTERED = "registered_unit"
SOURCE_COMPAT = "compat"
SOURCE_HOME = "home"
SOURCE_UNCLASSIFIED = "unclassified"

TICKER_SOURCE_UNIT = "unit"
TICKER_SOURCE_METADATA = "workspace_metadata"
TICKER_SOURCE_COMPANY_DOC = "company_doc"
TICKER_SOURCE_THESIS_DOC = "thesis_doc"
TICKER_SOURCE_WORKSPACE_NAME = "workspace_name"
TICKER_SOURCE_AUTOMATION = "automation"

# Ticker shapes accepted everywhere (tracking's historical convention):
# 1-10 alnum chars plus an optional 1-4 letter market suffix (.HK, .SS, ...).
_CLEAN_TICKER_RE = re.compile(r"^[A-Za-z0-9]{1,10}(?:\.[A-Za-z]{1,4})?$")
# Sector automations (`dalton-sector-…`, P3) must never yield a ticker here —
# including a slug like `earn-ai` whose task name would otherwise satisfy the
# `earn-*` suffix and read the literal "sector" out as one. The lookahead
# can't touch company rows: a company task name is `dalton-<ticker>-…` and no
# ticker is the literal `sector-…` prefix (codex r2 #8).
_TICKER_FROM_AUTO_NAME_RE = re.compile(
    r"^dalton-(?!sector-)([a-z0-9.]+)-(?:watch|delta|move-up|move-down|earn-.*)$"
)


def _extract_ticker_from_text(raw: str | None) -> str | None:
    """Return the ticker when the WHOLE string is one (e.g. "NVDA", "0700.HK"), else None.

    Full-string only (sector layer P0): the old leading-token match turned a
    topic workspace named "AI Infrastructure" into ticker "AI". A workspace
    whose name is exactly a ticker shape — the bootstrap convention, folder
    name = ticker — still resolves; anything with spaces, underscores or a
    "home" name never yields a ticker.
    """
    cleaned = str(raw or "").strip()
    if not cleaned or cleaned.lower() == "home" or "_" in cleaned:
        return None
    if _CLEAN_TICKER_RE.match(cleaned):
        return cleaned.upper()
    return None


def ticker_from_automation_rows(
    workspace_id: str,
    rows: list[Mapping[str, Any]] | None,
) -> str | None:
    """Ticker from this workspace's existing `dalton-<t>-*` automations.

    Tracking's historical compat fallback: a row belongs to the workspace when
    its `workspace_id` matches or is empty (legacy rows), and supplies the
    ticker through `metadata.ticker` or its `dalton-<t>-*` name. Paused rows
    count too — pausing is a user state, not an identity change. A row that
    declares itself a sector subject (`metadata.subject_kind == "sector"` or a
    `dalton-sector-` name, P3) never supplies a ticker: sectors have none, and
    the name's second segment is the literal "sector" (codex r2 #8).
    """
    ws_str = str(workspace_id or "").strip()
    for r in rows or []:
        row_ws = str(r.get("workspace_id") or "").strip()
        if row_ws and row_ws != ws_str:
            continue
        r_meta = r.get("metadata") if isinstance(r.get("metadata"), Mapping) else {}
        if str(r_meta.get("subject_kind") or "").strip().lower() == "sector":
            continue
        if str(r.get("name") or "").strip().lower().startswith("dalton-sector-"):
            continue
        if r_meta.get("ticker"):
            return str(r_meta["ticker"]).strip().upper()
        m = _TICKER_FROM_AUTO_NAME_RE.match(str(r.get("name") or "").strip().lower())
        if m:
            return m.group(1).upper()
    return None


@dataclass(frozen=True)
class WorkspaceCoverageIdentity:
    """The coverage subject a workspace resolves to.

    `ticker` is set only for companies; a sector never carries one, whatever a
    legacy company doc inside the workspace says. `ticker_source` names which
    rule produced the ticker ("unit", "workspace_metadata", "company_doc",
    "thesis_doc", "workspace_name", "automation"; empty when there is none).
    """

    kind: str
    source: str
    ticker: str | None = None
    unit: Mapping[str, Any] | None = field(default=None)
    ticker_source: str = ""

    @property
    def is_company(self) -> bool:
        return self.kind == KIND_COMPANY

    @property
    def is_sector(self) -> bool:
        return self.kind == KIND_SECTOR

    @property
    def is_home(self) -> bool:
        return self.kind == KIND_HOME


def _doc_ticker(content: Any) -> str | None:
    if not isinstance(content, Mapping):
        return None
    raw = str(content.get("ticker") or "").strip().upper()
    return raw or None


def _metadata_ticker(workspace: Mapping[str, Any]) -> str | None:
    meta = workspace.get("metadata") if isinstance(workspace.get("metadata"), Mapping) else {}
    return _extract_ticker_from_text(meta.get("ticker") or workspace.get("ticker"))


def _compat_ticker(
    workspace: Mapping[str, Any],
    *,
    company_content: Any,
    thesis_content: Any,
    automations: list[Mapping[str, Any]] | None,
) -> tuple[str | None, str]:
    """The compatible-rules ticker, in tracking's historical order.

    Returns `(ticker, ticker_source)`; `(None, "")` when nothing hits.
    """
    meta_ticker = _metadata_ticker(workspace)
    if meta_ticker:
        return meta_ticker, TICKER_SOURCE_METADATA

    company_ticker = _doc_ticker(company_content)
    if company_ticker:
        cleaned = _extract_ticker_from_text(company_ticker)
        if cleaned:
            return cleaned, TICKER_SOURCE_COMPANY_DOC
    thesis_ticker = _doc_ticker(thesis_content)
    if thesis_ticker:
        cleaned = _extract_ticker_from_text(thesis_ticker)
        if cleaned:
            return cleaned, TICKER_SOURCE_THESIS_DOC

    name_ticker = _extract_ticker_from_text(workspace.get("name"))
    if name_ticker:
        return name_ticker, TICKER_SOURCE_WORKSPACE_NAME

    auto_ticker = ticker_from_automation_rows(
        str(workspace.get("workspace_id") or ""), automations
    )
    if auto_ticker:
        return auto_ticker, TICKER_SOURCE_AUTOMATION

    return None, ""


def classify_workspace_coverage(
    workspace: Mapping[str, Any] | None,
    *,
    company_content: Any = None,
    thesis_content: Any = None,
    automations: list[Mapping[str, Any]] | None = None,
) -> WorkspaceCoverageIdentity:
    """Pure compatible-rules classification — no DB, no flag, no re-derivation.

    Home first (a Home is never a company whatever its name), then tracking's
    historical ticker order; a workspace is a company exactly when one of the
    compat rules yields a ticker. A company doc that exists but carries no
    usable ticker does NOT by itself make the workspace a company — the ticker
    must come from one of the four rules.
    """
    if workspace is None:
        return WorkspaceCoverageIdentity(KIND_UNCLASSIFIED, SOURCE_UNCLASSIFIED)

    from src.server.database.home_workspace import is_home_row

    if is_home_row(workspace):
        return WorkspaceCoverageIdentity(KIND_HOME, SOURCE_HOME)

    ticker, ticker_source = _compat_ticker(
        workspace,
        company_content=company_content,
        thesis_content=thesis_content,
        automations=automations,
    )
    if ticker:
        return WorkspaceCoverageIdentity(
            KIND_COMPANY, SOURCE_COMPAT, ticker, ticker_source=ticker_source
        )
    return WorkspaceCoverageIdentity(KIND_UNCLASSIFIED, SOURCE_UNCLASSIFIED)


_NOT_LOADED = object()


async def _load_doc_content(workspace_id: str, doc_key: str) -> Any:
    from src.dalton.database import knowledge as db_knowledge

    try:
        row = await db_knowledge.latest_valid_version(workspace_id, doc_key)
    except Exception:
        return None
    if not row:
        return None
    content = row.get("content")
    return content if isinstance(content, Mapping) else {}


async def resolve_workspace_coverage_identity(
    workspace: Mapping[str, Any] | None,
    *,
    company_content: Any = _NOT_LOADED,
    thesis_content: Any = _NOT_LOADED,
    registered_units: Mapping[str, Mapping[str, Any]] | None = None,
    automations: list[Mapping[str, Any]] | None = None,
    conn: Any = None,
) -> WorkspaceCoverageIdentity:
    """The one resolver used by tracking, the board and the director probe.

    `registered_units` is a caller-supplied `{workspace_id: unit_row}` map
    (board-style batch load) and is trusted as-is: the caller already decided
    to consult the registry. When omitted, the flag decides — off means the
    registry is never read; on means the active unit is looked up per
    workspace. `automations` are this workspace's candidate `dalton-*` rows
    (the compat fallback); pass None to skip that fallback. Pass preloaded doc
    contents to avoid re-querying.
    """
    if workspace is None:
        return WorkspaceCoverageIdentity(KIND_UNCLASSIFIED, SOURCE_UNCLASSIFIED)
    ws_id = str(workspace.get("workspace_id") or "").strip()

    from src.server.database.home_workspace import is_home_row

    if is_home_row(workspace):
        return WorkspaceCoverageIdentity(KIND_HOME, SOURCE_HOME)

    if ws_id and (registered_units is not None or is_coverage_units_enabled()):
        unit = registered_units.get(ws_id) if registered_units is not None else None
        if unit is None and registered_units is None:
            from src.dalton.database import coverage_units as cu_db

            unit = await cu_db.get_active_unit_by_workspace(ws_id, conn=conn)
        if unit is not None:
            kind = str(unit.get("kind") or "").strip().lower()
            if kind == KIND_SECTOR:
                return WorkspaceCoverageIdentity(
                    KIND_SECTOR, SOURCE_REGISTERED, ticker=None, unit=unit
                )
            if kind == KIND_COMPANY:
                unit_ticker = str(unit.get("ticker") or "").strip().upper() or None
                if unit_ticker:
                    return WorkspaceCoverageIdentity(
                        KIND_COMPANY,
                        SOURCE_REGISTERED,
                        unit_ticker,
                        unit=unit,
                        ticker_source=TICKER_SOURCE_UNIT,
                    )
                # Company unit without a ticker: the company identity stands,
                # the ticker falls through to the compatible rules.
                if company_content is _NOT_LOADED and ws_id:
                    company_content = await _load_doc_content(ws_id, "company")
                if thesis_content is _NOT_LOADED and ws_id:
                    thesis_content = await _load_doc_content(ws_id, "thesis")
                compat_ticker, compat_source = _compat_ticker(
                    workspace,
                    company_content=None if company_content is _NOT_LOADED else company_content,
                    thesis_content=None if thesis_content is _NOT_LOADED else thesis_content,
                    automations=automations,
                )
                return WorkspaceCoverageIdentity(
                    KIND_COMPANY,
                    SOURCE_REGISTERED,
                    compat_ticker,
                    unit=unit,
                    ticker_source=compat_source,
                )

    if company_content is _NOT_LOADED and ws_id:
        company_content = await _load_doc_content(ws_id, "company")
    if thesis_content is _NOT_LOADED and ws_id:
        thesis_content = await _load_doc_content(ws_id, "thesis")
    return classify_workspace_coverage(
        workspace,
        company_content=None if company_content is _NOT_LOADED else company_content,
        thesis_content=None if thesis_content is _NOT_LOADED else thesis_content,
        automations=automations,
    )
