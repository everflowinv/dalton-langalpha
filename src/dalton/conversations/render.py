"""Per-company and industry-topic Markdown renderer for Dalton's conversation archive.

Rules:
- For each involved company `ticker` (that has a Dalton company workspace):
  writes `<workspace>/conversations/<date_et>.md`.
- For industry topics (cards with no ticker or multiple tickers):
  writes `research/topics/conversations/<date_et>.md` in the user's Home workspace.
- Private companies (`OpenAI`, `Anthropic`, etc.) stay in `entities[]` and never create a company workspace.
- Never uses the term "wiki" in rendered files; explicitly labels source type as `conversation`.
"""

from __future__ import annotations

import hashlib
import logging
import shlex
from datetime import date
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from src.config.env import LOCAL_DEV_USER_ID
from src.dalton.conversations.search import format_conversation_locator
from src.server.database.pool import get_db_connection
from src.server.database.workspace_file import bulk_upsert_files, delete_file_rows

logger = logging.getLogger(__name__)


def archive_owner_user_id() -> str:
    """Return the owner user ID for conversation archive billing and workspace rendering.

    With Supabase sign-in on, `DALTON_PM_USER_IDS` names the owner's account;
    falls back to `LOCAL_DEV_USER_ID` when unconfigured.
    """
    from src.dalton.settings import get_dalton_config

    pm_ids = [u for u in (get_dalton_config().pm_user_ids or []) if str(u).strip()]
    return str(pm_ids[0]).strip() if pm_ids else LOCAL_DEV_USER_ID


def company_conversation_path(date_et: str | date) -> str:
    """Return workspace-relative path `conversations/<date_et>.md` for a company workspace."""
    d_str = date_et.isoformat() if isinstance(date_et, date) else str(date_et)[:10]
    return f"conversations/{d_str}.md"


def topic_conversation_path(date_et: str | date) -> str:
    """Return Home-relative path `research/topics/conversations/<date_et>.md` for industry topics."""
    d_str = date_et.isoformat() if isinstance(date_et, date) else str(date_et)[:10]
    return f"research/topics/conversations/{d_str}.md"


def _card_display_seq(card: dict[str, Any]) -> int:
    seq_range = card.get("seq_range")
    if isinstance(seq_range, list) and seq_range:
        try:
            return max(int(x) for x in seq_range)
        except Exception:
            return 0
    return 0


def render_conversation_digest_md(
    cards: list[dict[str, Any]],
    *,
    date_et: str,
    ticker: str | None = None,
) -> str:
    """Render a chronological Markdown digest containing only cards, verbatim quotes, and `openclaw://` provenance."""
    title_target = f"{ticker.upper()} · {date_et}" if ticker else f"行业与跨公司讨论 · {date_et}"
    lines = [
        f"# 对话历史纪要（{title_target}）",
        "",
        "- **来源类型**：`conversation`（OpenClaw 对话历史库）",
        f"- **美东日期**：`{date_et}`",
        f"- **卡片数量**：{len(cards)}",
        "",
    ]

    ordered = sorted(
        cards,
        key=lambda c: (str(c.get("ts") or ""), _card_display_seq(c), str(c.get("card_id") or "")),
    )

    for idx, card in enumerate(ordered, start=1):
        c_type = str(card.get("type") or "opinion")
        claim = str(card.get("claim") or "").strip()
        topic = str(card.get("topic_label") or "").strip()
        cid = str(card.get("card_id") or "")
        speaker = card.get("speaker") if isinstance(card.get("speaker"), dict) else {}
        sp_name = str(speaker.get("name") or "Participant")
        sp_role = str(speaker.get("role") or card.get("speaker_role") or "participant")
        locator = format_conversation_locator(card)
        ts_str = str(card.get("ts") or "")
        quote_str = str(card.get("quote") or "").strip()
        conf = float(card.get("confidence", 0.8))

        header_suffix = f"（{topic}）" if topic else ""
        lines.append(f"## {idx}. [{c_type}] {claim}{header_suffix}")
        lines.append("")
        lines.append(f"- **卡片 ID**：`{cid}` · **来源类型**：`conversation` · **置信度**：`{conf:.2f}`")
        lines.append(f"- **说话人**：{sp_name}（`{sp_role}`）· **时间**：`{ts_str}`")

        tickers = [str(t).upper() for t in (card.get("tickers") or []) if str(t).strip()]
        entities = [str(e) for e in (card.get("entities") or []) if str(e).strip()]
        sectors = [str(s) for s in (card.get("sectors") or []) if str(s).strip()]
        meta_parts: list[str] = []
        if tickers:
            meta_parts.append(f"Tickers: `{', '.join(tickers)}`")
        if entities:
            meta_parts.append(f"Entities: `{', '.join(entities)}`")
        if sectors:
            meta_parts.append(f"Sectors: `{', '.join(sectors)}`")
        if meta_parts:
            lines.append("- **关联标的/实体**：" + " | ".join(meta_parts))

        all_figs = [f for f in (card.get("figures") or []) if isinstance(f, dict)]
        figs = [f for f in all_figs if f.get("metric") != "_verification"]
        flags = [f for f in all_figs if f.get("metric") == "_verification"]
        if flags:
            nums = "；".join(str(f.get("value") or "").removeprefix("unsupported:") for f in flags)
            lines.append(f"- **⚠ 核验**：卡片结论里有数字未被原话支持（{nums}），使用前回原话核对。")
        if figs:
            fig_strs = [
                f"`{f.get('metric')}` = `{f.get('value')}` {f.get('unit') or ''} ({f.get('period') or ''})".strip()
                for f in figs
            ]
            lines.append("- **关键数值**：" + "；".join(fig_strs))

        cited = [str(s) for s in (card.get("cited_sources") or []) if str(s).strip()]
        if cited:
            lines.append("- **引用资料**：" + "；".join(f"`{s}`" for s in cited))

        if card.get("supersedes"):
            lines.append(f"- **更正旧卡 (`supersedes`)**：`{card['supersedes']}`")

        lines.append(f"- **出处**：`{locator}`")
        lines.append("")
        lines.append(f"> 「{quote_str}」")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


async def _resolve_workspace_backend(
    workspace_id: str,
    *,
    user_id: str | None = None,
) -> Any:
    """Resolve a pinned `SandboxBackend` for `workspace_id` via `WorkspaceManager`.

    Lazily initializes `WorkspaceManager` with SDK config when invoked from standalone CLI.
    """
    from ptc_agent.agent.backends.sandbox import SandboxBackend
    from ptc_agent.core.paths import SandboxLayout
    from src.server.database.workspace import get_workspace
    from src.server.services.workspace_manager import WorkspaceManager

    try:
        workspace_manager = WorkspaceManager.get_instance()
    except ValueError:
        from ptc_agent.config import ConfigContext, load_from_files

        agent_config = await load_from_files(context=ConfigContext.SDK)
        workspace_manager = WorkspaceManager.get_instance(config=agent_config)

    session = await workspace_manager.get_session_for_workspace(
        str(workspace_id), user_id=user_id
    )
    if not hasattr(session, "sandbox") or session.sandbox is None:
        raise RuntimeError(f"Sandbox session unavailable for workspace {workspace_id}")
    row = await get_workspace(str(workspace_id)) or {}
    dir_name = row.get("dir_name")
    layout = SandboxLayout(session.sandbox.working_dir).for_workspace(dir_name)
    return SandboxBackend(session.sandbox, layout.workspace)


async def write_conversation_file_to_backend(
    workspace_id: str | None,
    rel_path: str,
    md_text: str,
    *,
    backend: Any = None,
    user_id: str | None = None,
    conn: Any = None,
) -> str:
    """Write a rendered conversation Markdown file to `backend` and mirror to `workspace_files`,
    following `write_version_files_to_backend` in `src/dalton/knowledge/service.py`.

    Both the backend write and `workspace_files` upsert must succeed; failures raise so the
    caller can retry on the next run.
    """
    eff_backend = backend
    if eff_backend is None and workspace_id:
        eff_backend = await _resolve_workspace_backend(str(workspace_id), user_id=user_id)
    if eff_backend is None:
        raise RuntimeError(
            f"Cannot write conversation digest {rel_path}: no filesystem backend for workspace_id={workspace_id}"
        )

    if hasattr(eff_backend, "awrite_text"):
        wrote = await eff_backend.awrite_text(rel_path, md_text)
        if wrote is False:
            raise RuntimeError(f"Backend awrite_text failed for {rel_path}")
    elif hasattr(eff_backend, "awrite"):
        await eff_backend.awrite(rel_path, md_text)
    elif hasattr(eff_backend, "write"):
        eff_backend.write(rel_path, md_text)
    else:
        raise RuntimeError(
            f"Filesystem backend {type(eff_backend).__name__} does not support writing {rel_path}"
        )

    if workspace_id:
        md_bytes = md_text.encode("utf-8")
        md_sha = hashlib.sha256(md_bytes).hexdigest()
        file_name = Path(rel_path).name
        await bulk_upsert_files(
            str(workspace_id),
            [
                {
                    "file_path": rel_path,
                    "file_name": file_name,
                    "file_size": len(md_bytes),
                    "content_hash": md_sha,
                    "content_text": md_text,
                    "mime_type": "text/markdown",
                    "is_binary": False,
                    "kind": "file",
                }
            ],
            conn=conn,
        )

    return rel_path


async def remove_conversation_file_from_backend(
    workspace_id: str | None,
    rel_path: str,
    *,
    backend: Any = None,
    user_id: str | None = None,
    conn: Any = None,
) -> str:
    """Remove a stale conversation Markdown file from `backend` and delete its row in `workspace_files`."""
    eff_backend = backend
    if eff_backend is None and workspace_id:
        eff_backend = await _resolve_workspace_backend(str(workspace_id), user_id=user_id)
    if eff_backend is None:
        raise RuntimeError(
            f"Cannot remove conversation digest {rel_path}: no filesystem backend for workspace_id={workspace_id}"
        )

    if hasattr(eff_backend, "aexecute_bash"):
        target_path = rel_path
        if hasattr(eff_backend, "normalize_path"):
            norm = eff_backend.normalize_path(rel_path)
            if isinstance(norm, str) and norm:
                target_path = norm
        res = await eff_backend.aexecute_bash(f"rm -f {shlex.quote(target_path)}")
        if isinstance(res, dict) and int(res.get("exit_code") or 0) != 0:
            raise RuntimeError(f"Backend rm -f failed for {rel_path}")
    elif hasattr(eff_backend, "awrite_text"):
        wrote = await eff_backend.awrite_text(rel_path, "")
        if wrote is False:
            raise RuntimeError(f"Backend awrite_text failed while clearing {rel_path}")

    if workspace_id:
        await delete_file_rows(str(workspace_id), [rel_path], conn=conn)

    return rel_path


async def _resolve_workspace_map(
    user_id: str | None = None,
    conn: Any = None,
) -> tuple[dict[str, str], str | None]:
    """Return `(ticker_to_workspace_id, home_workspace_id)` from existing workspaces.

    Propagates DB query errors rather than swallowing them so rendering never marks dates
    complete on a broken DB lookup.
    """
    ticker_map: dict[str, str] = {}
    home_ws_id: str | None = None
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            if user_id is not None:
                await cur.execute(
                    """
                    SELECT workspace_id, name, layout_origin
                    FROM workspaces
                    WHERE status <> 'deleted' AND user_id = %s
                    """,
                    (user_id,),
                )
            else:
                await cur.execute(
                    """
                    SELECT workspace_id, name, layout_origin
                    FROM workspaces
                    WHERE status <> 'deleted'
                    """
                )
            rows = await cur.fetchall()
            for r in rows or []:
                ws_id = str(r["workspace_id"])
                name = str(r.get("name") or "").strip()
                if str(r.get("layout_origin") or "") == "home" or name.lower() == "home":
                    home_ws_id = ws_id
                elif name:
                    ticker_map[name.upper()] = ws_id

    if home_ws_id is None and user_id:
        from src.server.database.home_workspace import get_home_workspace_id

        home_ws_id = str(get_home_workspace_id(user_id))

    return ticker_map, home_ws_id


async def render_and_write_conversation_digests(
    cards: list[dict[str, Any]],
    *,
    target_dates: list[str | date] | None = None,
    user_id: str | None = None,
    backend_by_ticker: dict[str, Any] | None = None,
    backend_by_workspace_id: dict[str, Any] | None = None,
    home_backend: Any = None,
    ticker_to_workspace_id: dict[str, str] | None = None,
    home_workspace_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Group `cards` by `(ticker, date_et)` and industry topics, render Markdown, and write to workspaces.

    - Cards with `ticker` in `card.tickers` that have a company workspace ->
      `<ticker_workspace>/conversations/<date_et>.md`.
    - Cards whose `ticker` has no company workspace -> recorded in `unmapped_tickers` ("无工作区可写")
      and routed into Home workspace `research/topics/conversations/<date_et>.md` so no card is
      silently dropped.
    - Industry topic cards (`len(tickers) == 0` or `len(tickers) > 1`) ->
      Home workspace `research/topics/conversations/<date_et>.md`.
    - Records the actual written locations (`company` or `home`) on each card (`rendered_locations`),
      rewrites all active locations for a date,diffs historical `(workspace_id, path)` locations against
      the new active targets to clean up stale files in their actual historical workspaces, and only
      marks a date rendered after all its relevant locations have been updated.
    """
    eff_user_id = (
        str(user_id).strip()
        if user_id and str(user_id).strip()
        else archive_owner_user_id()
    )
    if ticker_to_workspace_id is None or home_workspace_id is None:
        db_ticker_map, db_home_id = await _resolve_workspace_map(user_id=eff_user_id, conn=conn)
        if ticker_to_workspace_id is None:
            ticker_to_workspace_id = db_ticker_map
        if home_workspace_id is None:
            home_workspace_id = db_home_id

    active_cards = [c for c in cards if str(c.get("status") or "active") == "active"]
    inactive_cards = [c for c in cards if str(c.get("status") or "active") != "active"]

    by_company_date: dict[tuple[str, str], list[dict[str, Any]]] = {}
    by_topic_date: dict[str, list[dict[str, Any]]] = {}
    topic_seen_card_keys: dict[str, set[str]] = {}
    unmapped_tickers: list[dict[str, Any]] = []
    unmapped_seen: set[tuple[str, str]] = set()
    planned_card_locations: dict[str, list[dict[str, Any]]] = {}
    cards_by_date: dict[str, list[dict[str, Any]]] = {}

    def _add_topic_card(d_et: str, card_obj: dict[str, Any]) -> None:
        ckey = str(card_obj.get("card_id") or card_obj.get("content_hash") or id(card_obj))
        seen = topic_seen_card_keys.setdefault(d_et, set())
        if ckey not in seen:
            seen.add(ckey)
            by_topic_date.setdefault(d_et, []).append(card_obj)

    def _loc_key(ws_id: str | None, path: str, target: str, ticker: str | None) -> tuple[str, str]:
        if ws_id:
            return (str(ws_id), str(path))
        fallback_ws = f"__ticker__:{ticker}" if ticker else f"__{target or 'home'}__"
        return (fallback_ws, str(path))

    for c in cards:
        d_et = str(c.get("date_et") or "")[:10]
        if d_et:
            cards_by_date.setdefault(d_et, []).append(c)

    for c in active_cards:
        date_et = str(c.get("date_et") or "")[:10]
        if not date_et:
            continue
        cid = str(c.get("card_id") or "")
        tickers = [str(t).strip().upper() for t in (c.get("tickers") or []) if str(t).strip()]
        has_unmapped = False
        locs_for_card: list[dict[str, Any]] = []
        for t in tickers:
            ws_id = (ticker_to_workspace_id or {}).get(t)
            backend = (backend_by_ticker or {}).get(t)
            if ws_id is None and backend is None:
                has_unmapped = True
                if (t, date_et) not in unmapped_seen:
                    unmapped_seen.add((t, date_et))
                    unmapped_tickers.append(
                        {
                            "ticker": t,
                            "date_et": date_et,
                            "status": "no_company_workspace",
                            "routed_to": topic_conversation_path(date_et),
                        }
                    )
            else:
                by_company_date.setdefault((t, date_et), []).append(c)
                locs_for_card.append(
                    {
                        "target": "company",
                        "ticker": t,
                        "workspace_id": ws_id,
                        "path": company_conversation_path(date_et),
                    }
                )
        if len(tickers) != 1 or has_unmapped:
            _add_topic_card(date_et, c)
            locs_for_card.append(
                {
                    "target": "home",
                    "ticker": None,
                    "workspace_id": home_workspace_id,
                    "path": topic_conversation_path(date_et),
                }
            )
        if cid:
            planned_card_locations[cid] = locs_for_card

    for c in inactive_cards:
        cid = str(c.get("card_id") or "")
        if cid:
            planned_card_locations[cid] = []

    all_dates: set[str] = set()
    if target_dates:
        for d in target_dates:
            d_str = d.isoformat() if isinstance(d, date) else str(d)[:10]
            if d_str:
                all_dates.add(d_str)
    for c in cards:
        d_str = str(c.get("date_et") or "")[:10]
        if d_str:
            all_dates.add(d_str)

    company_files_written: list[dict[str, Any]] = []
    topic_files_written: list[dict[str, Any]] = []
    removed_files: list[dict[str, Any]] = []
    rendered_dates: list[str] = []
    card_locations: dict[str, list[dict[str, Any]]] = {}

    # Process date by date so a date is only marked rendered after all its active and stale
    # locations (company workspaces and Home) have been rewritten or cleaned up.
    for date_et in sorted(all_dates):
        new_locations_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        date_company_tickers = sorted(
            t for (t, d_et) in by_company_date if d_et == date_et
        )
        for ticker in date_company_tickers:
            group = by_company_date[(ticker, date_et)]
            ws_id = (ticker_to_workspace_id or {}).get(ticker)
            backend = (
                (backend_by_workspace_id or {}).get(ws_id or "")
                or (backend_by_ticker or {}).get(ticker)
            )
            rel_path = company_conversation_path(date_et)
            md_text = render_conversation_digest_md(group, date_et=date_et, ticker=ticker)
            await write_conversation_file_to_backend(
                ws_id,
                rel_path,
                md_text,
                backend=backend,
                user_id=eff_user_id,
                conn=conn,
            )
            new_locations_by_key[_loc_key(ws_id, rel_path, "company", ticker)] = {
                "target": "company",
                "ticker": ticker,
                "workspace_id": ws_id,
                "path": rel_path,
            }
            company_files_written.append(
                {
                    "ticker": ticker,
                    "workspace_id": ws_id,
                    "date_et": date_et,
                    "path": rel_path,
                    "cards_count": len(group),
                }
            )

        if date_et in by_topic_date:
            group = by_topic_date[date_et]
            eff_home_backend = (
                (backend_by_workspace_id or {}).get(home_workspace_id or "")
                or home_backend
            )
            if home_workspace_id is None and eff_home_backend is None:
                raise RuntimeError(
                    f"Cannot write topic conversation digest for {date_et}: Home workspace/backend unavailable"
                )
            rel_path = topic_conversation_path(date_et)
            md_text = render_conversation_digest_md(group, date_et=date_et, ticker=None)
            await write_conversation_file_to_backend(
                home_workspace_id,
                rel_path,
                md_text,
                backend=eff_home_backend,
                user_id=eff_user_id,
                conn=conn,
            )
            new_locations_by_key[_loc_key(home_workspace_id, rel_path, "home", None)] = {
                "target": "home",
                "ticker": None,
                "workspace_id": home_workspace_id,
                "path": rel_path,
            }
            topic_files_written.append(
                {
                    "workspace_id": home_workspace_id,
                    "date_et": date_et,
                    "path": rel_path,
                    "cards_count": len(group),
                }
            )

        # Collect historical `(workspace_id, path)` locations for `date_et` from `rendered_locations`
        # (and fallback locations for legacy inactive cards without `rendered_locations`) and diff against `new_locations_by_key`.
        prev_locations_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for c in cards_by_date.get(date_et, []):
            prev_locs = c.get("rendered_locations")
            has_recorded_locs = False
            if isinstance(prev_locs, list):
                for loc in prev_locs:
                    if not isinstance(loc, dict):
                        continue
                    has_recorded_locs = True
                    raw_path = str(loc.get("path") or "").strip()
                    raw_target = str(loc.get("target") or "").strip()
                    loc_ticker = str(loc.get("ticker") or "").strip().upper() or None
                    is_home_loc = (
                        raw_target == "home"
                        or raw_path.startswith("research/topics/conversations/")
                    )
                    eff_target = "home" if is_home_loc else "company"
                    eff_path = raw_path or (
                        topic_conversation_path(date_et)
                        if is_home_loc
                        else company_conversation_path(date_et)
                    )
                    raw_ws = str(loc.get("workspace_id") or "").strip() or None
                    if raw_ws is None:
                        raw_ws = (
                            home_workspace_id
                            if is_home_loc
                            else (ticker_to_workspace_id or {}).get(loc_ticker or "")
                        )
                    if is_home_loc and raw_ws is None and home_backend is None:
                        continue
                    if not is_home_loc and raw_ws is None and (backend_by_ticker or {}).get(loc_ticker or "") is None:
                        continue
                    k = _loc_key(raw_ws, eff_path, eff_target, loc_ticker)
                    prev_locations_by_key.setdefault(
                        k,
                        {
                            "target": eff_target,
                            "ticker": loc_ticker,
                            "workspace_id": raw_ws,
                            "path": eff_path,
                        },
                    )

            if not has_recorded_locs and str(c.get("status") or "active") != "active":
                c_tickers = [str(t).strip().upper() for t in (c.get("tickers") or []) if str(t).strip()]
                c_has_unmapped = False
                for t in c_tickers:
                    ws_id = (ticker_to_workspace_id or {}).get(t)
                    backend = (backend_by_ticker or {}).get(t)
                    if ws_id is None and backend is None:
                        c_has_unmapped = True
                    else:
                        p = company_conversation_path(date_et)
                        k = _loc_key(ws_id, p, "company", t)
                        prev_locations_by_key.setdefault(
                            k,
                            {
                                "target": "company",
                                "ticker": t,
                                "workspace_id": ws_id,
                                "path": p,
                            },
                        )
                if len(c_tickers) != 1 or c_has_unmapped:
                    p = topic_conversation_path(date_et)
                    k = _loc_key(home_workspace_id, p, "home", None)
                    prev_locations_by_key.setdefault(
                        k,
                        {
                            "target": "home",
                            "ticker": None,
                            "workspace_id": home_workspace_id,
                            "path": p,
                        },
                    )

        stale_keys = [k for k in prev_locations_by_key if k not in new_locations_by_key]
        for k in stale_keys:
            stale_loc = prev_locations_by_key[k]
            stale_ws_id = stale_loc["workspace_id"]
            stale_path = stale_loc["path"]
            stale_ticker = stale_loc["ticker"]
            stale_target = stale_loc["target"]

            stale_backend = None
            if stale_ws_id and backend_by_workspace_id and stale_ws_id in backend_by_workspace_id:
                stale_backend = backend_by_workspace_id[stale_ws_id]
            elif stale_target == "home" and (stale_ws_id is None or stale_ws_id == home_workspace_id):
                stale_backend = home_backend
            elif stale_ws_id and backend_by_ticker and stale_ws_id in backend_by_ticker:
                stale_backend = backend_by_ticker[stale_ws_id]
            elif stale_ticker and backend_by_ticker and stale_ticker in backend_by_ticker:
                mapped_ws = (ticker_to_workspace_id or {}).get(stale_ticker)
                if stale_ws_id is None or mapped_ws == stale_ws_id or mapped_ws is None:
                    stale_backend = backend_by_ticker[stale_ticker]

            if stale_target == "home" and stale_ws_id is None and stale_backend is None:
                raise RuntimeError(
                    f"Cannot remove stale topic conversation digest for {date_et}: Home workspace/backend unavailable"
                )

            await remove_conversation_file_from_backend(
                stale_ws_id,
                stale_path,
                backend=stale_backend,
                user_id=eff_user_id,
                conn=conn,
            )
            rem_entry: dict[str, Any] = {
                "workspace_id": stale_ws_id,
                "date_et": date_et,
                "path": stale_path,
            }
            if stale_ticker:
                rem_entry["ticker"] = stale_ticker
            removed_files.append(rem_entry)

        for c in cards_by_date.get(date_et, []):
            cid = str(c.get("card_id") or "")
            locs = planned_card_locations.get(cid, [])
            c["rendered_locations"] = list(locs)
            if cid:
                card_locations[cid] = list(locs)

        rendered_dates.append(date_et)

    return {
        "company_files": company_files_written,
        "topic_files": topic_files_written,
        "removed_files": removed_files,
        "unmapped_tickers": unmapped_tickers,
        "rendered_dates": sorted(rendered_dates),
        "card_locations": card_locations,
    }
