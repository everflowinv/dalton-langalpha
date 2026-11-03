"""公司级研究路由到目标公司自己的工作区（生产线程 ada64139 复盘，第 2 轮）。

行业工作区的分析师给成员公司跑公司级 workflow 时，把产物用相对路径写进
那家公司的公司工作区目录——执行身份仍是行业工作区，于是自动跟踪
``forbidden_workspace``、``dalton_check_done`` 按本工作区找文件全部落空。
修复在启动和核对两个入口比对三者：参数里的目标公司 ticker、当前执行的
工作区、产物根目录（``workspace_dir``）。

路由按两步解析，身份一律走唯一解析器 ``coverage.identity``（owner Home →
覆盖单元注册表 → 兼容规则），不在调用点重推导：

1. **当前工作区**（不论属主——同事共享的工作区也是获授权的执行地）：
   是这家公司的公司工作区 → 放行。
2. **调用者自己的工作区**里找这家公司的**公司工作区**：找到 → 拒绝并指
   名；没有 → 放行，回执附"产物会留在当前工作区"提示（行业工作区研究
   尚未建工作区的成员公司是允许的）。别人的工作区一律不查。

行业工作区永不作为某家公司的"自己的工作区"（第 2 轮 agy 1）：行业工作
区研究成员公司时会写下这家公司的 company 文档，兼容规则随之把它解析成
公司工作区——候选必须先过行业判定。行业标识取系统里已有的三个标记，
任一命中即行业：active 的 ``kind='sector'`` 覆盖单元（行业层 P0 的注册
身份）、有效的 ``sector`` 知识文档（company 档不接受该文档，见
``knowledge/profiles._SECTOR_ONLY_DOCS``）、挂在本工作区上的
``dalton-sector-*`` 自动化行（P3 行业任务命名，见 ``autonomy/preflight``）。

数据库读不到时放行并记日志：这是路由提示，不是权限边界，短暂的读失败
不该拦掉整轮覆盖（``dalton_check_done`` 稍后会再核对一次路由）。
"""

from __future__ import annotations

import logging
import posixpath
from typing import Any

from src.dalton.autonomy.probes.cnhk import (
    classify_cnhk_ticker,
    normalize_cn_ticker,
    normalize_hk_ticker,
)
from src.dalton.coverage.identity import resolve_workspace_coverage_identity
from src.dalton.settings import is_coverage_units_enabled

logger = logging.getLogger(__name__)

# 公司级 workflow：契约 params_required 里带 ticker 的那四个（topic-research
# 按专题、weekly-coverage-review 在 Home 跑，都不是公司级）。名单由
# tests/unit/dalton/test_company_routing.py 对照全部契约钉住。
COMPANY_WORKFLOWS = frozenset(
    {"initial-coverage", "full-coverage", "bullbear-doc", "earnings-deep-update"}
)

# 身份解析只读文档的 ticker 字段，枚举就只投影这一列（第 2 轮 agy 6）。
_DOC_TICKER_PROJECTION = "jsonb_build_object('ticker', content->>'ticker') AS content"


class CompanyRoutingRefused(ValueError):
    """启动必须就地拒绝：目标公司属于另一个工作区（或产物根目录越出当前
    工作区）。启动路径把它转成拒绝回执，不留任何 run。"""


def _param_ticker(params: dict[str, Any]) -> str | None:
    """``params.ticker`` 规范化成大写；漏传/非字符串/空白返回 None（契约自
    己的缺参检查会兜住，路由不做第二套参数校验）。"""
    raw = params.get("ticker")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().upper()
    return cleaned or None


def _ticker_key(ticker: str | None) -> str | None:
    """比较用规范键，复用 cn/hk 探针的现成归一（第 2 轮审查 3）：
    A 股统一 ``DDDDDD.{SS|SZ|BJ}``（裸 6 位码按代码段推断交易所，.SH 折成
    .SS），港股统一 5 位数字（``700`` / ``0700.HK`` / ``00700`` 同键），其
    余市场原样大写。"""
    cleaned = str(ticker or "").strip().upper()
    if not cleaned:
        return None
    kind, _ = classify_cnhk_ticker(cleaned)
    if kind == "cn":
        return normalize_cn_ticker(cleaned)
    if kind == "hk":
        return normalize_hk_ticker(cleaned)
    return cleaned


def _workspace_dir_escapes(workspace_dir: Any, root: Any = None) -> bool:
    """产物根目录是否越出当前工作区。

    有锚定根（backend 的 ``workspace_dir``，与 ``check_done_tools`` 的
    ``_workspace_relative`` 同一口径）时相对路径拼到根下、绝对路径直接判：
    normpath 后必须在根下。无锚定根时只做相对路径的词法逃逸检查，绝对路
    径无从判定、维持放行。符号链接不解析——那要进沙箱跑 realpath，启动
    路径不该为它多一次往返（越界路径极少同时再叠加符号链接）。"""
    if not isinstance(workspace_dir, str):
        return False
    cleaned = workspace_dir.strip()
    if not cleaned:
        return False
    anchor = str(root).rstrip("/") if isinstance(root, str) and str(root).strip() else ""
    if anchor:
        absolute = cleaned if cleaned.startswith("/") else posixpath.join(anchor, cleaned)
        normalized = posixpath.normpath(absolute)
        return not (normalized == anchor or normalized.startswith(anchor + "/"))
    if cleaned.startswith("/"):
        return False
    normalized = posixpath.normpath(cleaned)
    return normalized == ".." or normalized.startswith("../")


def _is_sector_workspace(
    *,
    unit: dict[str, Any] | None,
    doc_keys: set[str],
    automations: list[dict[str, Any]],
    workspace_id: str,
) -> bool:
    """行业工作区判定：单元 / sector 文档 / 本工作区的 dalton-sector 任务，
    任一命中即行业（依据见模块 docstring）。自动化行只认 workspace_id 严格
    相等的——legacy 空工作区行可能属于任何工作区，宽松匹配会把每个工作区
    都判成行业。"""
    if unit is not None and str(unit.get("kind") or "").strip().lower() == "sector":
        return True
    if "sector" in doc_keys:
        return True
    ws_id = str(workspace_id)
    return any(
        str(row.get("workspace_id") or "").strip() == ws_id
        and str(row.get("name") or "").strip().lower().startswith("dalton-sector-")
        for row in automations
    )


def _doc_ticker_content(docs: dict[str, dict[str, Any]], key: str) -> Any:
    """解析器要的文档内容形态：投影后的 ``{'ticker': …}``（或无该文档时
    None——"已读、没有"，区别于未读）。"""
    content = (docs.get(key) or {}).get("content")
    return content if isinstance(content, dict) else None


async def _load_workspace_docs(
    cur: Any, ids: list[str]
) -> dict[str, dict[str, Any]]:
    """每个工作区最新一版 company/thesis/sector 文档，只投影 ticker。"""
    await cur.execute(
        f"""
        SELECT DISTINCT ON (workspace_id, doc_key)
               workspace_id, doc_key, {_DOC_TICKER_PROJECTION}
        FROM dalton_knowledge_versions
        WHERE schema_valid = TRUE
          AND doc_key IN ('company', 'thesis', 'sector')
          AND workspace_id = ANY(%s::uuid[])
        ORDER BY workspace_id, doc_key, version_no DESC
        """,
        (ids,),
    )
    docs_by_ws: dict[str, dict[str, Any]] = {}
    for row in await cur.fetchall():
        docs_by_ws.setdefault(str(row["workspace_id"]), {})[str(row["doc_key"])] = dict(row)
    return docs_by_ws


async def _resolve_workspace_company(
    workspace_id: str, *, conn: Any = None
) -> tuple[Any, bool]:
    """一个工作区的公司身份与行业判定（当前工作区专用：不论属主）。

    返回 ``(identity, is_sector)``；工作区不存在时 ``(None, False)``。身份
    仍走共享解析器，单元与文档预读后传入，解析器自身不再查库。
    """
    from src.server.database.workspace import get_workspace

    ws = await get_workspace(workspace_id, conn=conn)
    if ws is None:
        return None, False
    ws_id = str(ws.get("workspace_id") or "").strip()

    units_by_ws: dict[str, dict[str, Any]] = {}
    if ws_id and is_coverage_units_enabled():
        from src.dalton.database import coverage_units as cu_db

        unit = await cu_db.get_active_unit_by_workspace(ws_id, conn=conn)
        if unit is not None:
            units_by_ws[ws_id] = unit

    from psycopg.rows import dict_row

    from src.dalton.autonomy.units_view import load_dalton_automations_by_user
    from src.server.database.pool import get_db_connection

    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            automations = await load_dalton_automations_by_user(
                cur, str(ws.get("user_id") or "")
            )
            docs_by_ws = await _load_workspace_docs(cur, [ws_id] if ws_id else [])

    owner_autos = automations.get(str(ws.get("user_id") or ""), [])
    docs = docs_by_ws.get(ws_id) or {}
    identity = await resolve_workspace_coverage_identity(
        ws,
        company_content=_doc_ticker_content(docs, "company"),
        thesis_content=_doc_ticker_content(docs, "thesis"),
        registered_units=units_by_ws,
        automations=owner_autos,
    )
    is_sector = _is_sector_workspace(
        unit=units_by_ws.get(ws_id),
        doc_keys=set(docs),
        automations=owner_autos,
        workspace_id=ws_id,
    )
    return identity, is_sector


async def find_company_workspaces(
    user_id: str, ticker: str, *, conn: Any = None
) -> list[dict[str, Any]]:
    """当前用户自己的、覆盖身份解析为 ``ticker`` 的**公司**工作区行。

    与覆盖看板同一条枚举路径：本人未删除工作区一次取出，覆盖单元、本人
    ``dalton-*`` 自动化、company/thesis/sector 文档（只投影 ticker）各一次
    批量读，身份逐个走共享解析器——不在任何调用点重推导规则。行业工作区
    （三个标记任一命中）不进结果。查询按 ``user_id = %s`` 过滤，别的用户
    的工作区无论何种状态都不会出现在结果里。
    """
    normalized = str(ticker or "").strip().upper()
    uid = str(user_id or "").strip()
    if not normalized or not uid:
        return []

    from psycopg.rows import dict_row

    from src.dalton.autonomy.units_view import load_dalton_automations_by_user
    from src.dalton.database.coverage_units import load_active_units_by_workspace
    from src.server.database.pool import get_db_connection

    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id, user_id, name, status
                FROM workspaces
                WHERE status <> 'deleted' AND user_id = %s
                ORDER BY name ASC
                """,
                (uid,),
            )
            ws_rows = [dict(r) for r in await cur.fetchall()]
            if not ws_rows:
                return []
            ids = [str(r["workspace_id"]) for r in ws_rows]

            # 开关关闭时传空 dict（"调用方已决定：无单元"），解析器即不读注册表。
            units_by_ws: dict[str, dict[str, Any]] = {}
            if is_coverage_units_enabled():
                units_by_ws = await load_active_units_by_workspace(ids, conn=c)

            autos_by_user = await load_dalton_automations_by_user(cur, uid)
            docs_by_ws = await _load_workspace_docs(cur, ids)

    wanted = _ticker_key(normalized)
    matches: list[dict[str, Any]] = []
    for ws in ws_rows:
        ws_id = str(ws["workspace_id"])
        docs = docs_by_ws.get(ws_id) or {}
        owner_autos = autos_by_user.get(str(ws.get("user_id") or ""), [])
        if _is_sector_workspace(
            unit=units_by_ws.get(ws_id),
            doc_keys=set(docs),
            automations=owner_autos,
            workspace_id=ws_id,
        ):
            continue
        identity = await resolve_workspace_coverage_identity(
            ws,
            company_content=_doc_ticker_content(docs, "company"),
            thesis_content=_doc_ticker_content(docs, "thesis"),
            registered_units=units_by_ws,
            automations=owner_autos,
        )
        if identity.is_company and _ticker_key(identity.ticker) == wanted:
            matches.append(ws)
    return matches


def _refuse_message(ticker: str, workspace: dict[str, Any]) -> str:
    name = str(workspace.get("name") or workspace.get("workspace_id") or "")
    return (
        f"本工作区不是 {ticker} 的工作区，不能从这里启动公司级研究："
        f"{ticker} 的研究应该在「{name}」里做。"
        "请让用户切换到那个工作区后再运行，或从研究总监（Home）把任务交给"
        "那个工作区的分析师；不要用相对路径把产物写到其他工作区的目录"
        "（自动跟踪与完成核对都只认当前工作区）。"
    )


def _guard_workspace_dir(params: dict[str, Any], ticker: str, root: Any) -> None:
    """产物根目录守卫：越出当前工作区就地拒绝（同一个 ada64139 错误——
    产物写进核对与跟踪都看不见的目录）。"""
    if _workspace_dir_escapes(params.get("workspace_dir"), root):
        raise CompanyRoutingRefused(
            f"产物根目录 `workspace_dir`（{params.get('workspace_dir')!r}）越出了当前工作区："
            "不要用相对或跨区路径把产物写到其他工作区的目录（自动跟踪与完成核对都只认当前工作区）。"
            "留在当前工作区运行时请传 `workspace_dir=\".\"`；若这项研究属于另一个工作区，"
            "请让用户切换到那个工作区后再运行，或从研究总监（Home）把任务交给那个工作区的分析师。"
        )


async def resolve_launch_company_routing(
    workflow: str,
    params: Any,
    configurable: dict[str, Any] | None,
    *,
    workspace_root: Any = None,
) -> str | None:
    """公司级 workflow 启动前的路由检查；返回值是附在启动回执后的提示
    （None 表示照常、无提示），路由不符抛 ``CompanyRoutingRefused``。

    两步解析（第 2 轮审查 2）：先看当前工作区（不论属主——同事共享的工
    作区也是获授权的执行地），就是这家公司的公司工作区则直接放行；再在
    调用者自己的工作区里找候选，找到即拒绝并指名。非公司级 workflow、
    无 ticker、无用户身份的调用原样放行。数据库读失败同样放行（记警
    告）——路由是提示性检查，不是权限边界。
    """
    if workflow not in COMPANY_WORKFLOWS or not isinstance(params, dict):
        return None
    ticker = _param_ticker(params)
    if ticker is None:
        return None
    cfg = configurable or {}
    user_id = str(cfg.get("user_id") or "").strip()
    if not user_id:
        return None
    current_ws = str(cfg.get("workspace_id") or "").strip()
    wanted = _ticker_key(ticker)

    try:
        if current_ws:
            identity, is_sector = await _resolve_workspace_company(current_ws)
            if (
                identity is not None
                and identity.is_company
                and not is_sector
                and _ticker_key(identity.ticker) == wanted
            ):
                # 当前工作区就是这家公司的工作区（哪怕属主是共享它的同事）。
                _guard_workspace_dir(params, ticker, workspace_root)
                return None
        matches = [
            ws
            for ws in await find_company_workspaces(user_id, ticker)
            if str(ws.get("workspace_id") or "") != current_ws
        ]
    except CompanyRoutingRefused:
        raise
    except Exception as error:
        logger.warning(
            "company routing lookup failed; launch proceeds unchecked "
            "(ticker=%s, workflow=%s)",
            ticker,
            workflow,
            exc_info=error,
        )
        return None

    if matches:
        raise CompanyRoutingRefused(_refuse_message(ticker, matches[0]))
    _guard_workspace_dir(params, ticker, workspace_root)
    return f"注意：{ticker} 还没有自己的工作区，本次产物会留在当前工作区。"


async def routing_hint_for_check(
    params: Any,
    user_id: str | None,
    workspace_id: str | None,
    *,
    workflow: str | None = None,
    workspace_root: Any = None,
) -> str | None:
    """``dalton_check_done`` 的路由指引：只对公司级流程生效（与启动检查同
    一份名单，第 2 轮 agy 5——topic-research 多传一个 ticker 不该被误判成
    未完成）。两种情况给出 missing 指引：

    - 参数 ticker 属于调用者自己的另一个工作区（当前工作区不是这家公司
      的公司工作区）——按本工作区找文件必然缺项，先指路；
    - 产物根目录 ``workspace_dir`` 越出当前工作区——产物写进了核对与跟踪
      都看不见的目录。

    手动降级路径（workflow 关闭、按 SKILL 手动执行）没有启动拦截可依，
    这里是它的路由信号；workflow 路径被拒后跑到别的线程核对时同样适用。
    ticker 就是当前工作区、或公司尚无工作区（行业工作区研究成员公司是
    允许的）时返回 None。"""
    if workflow not in COMPANY_WORKFLOWS or not isinstance(params, dict):
        return None
    ticker = _param_ticker(params)
    if ticker is None:
        return None
    if not user_id:
        # 无用户身份仍可判目录越界——它不依赖任何查询。
        return _dir_hint(params, workspace_root)
    current_ws = str(workspace_id or "").strip()
    wanted = _ticker_key(ticker)

    try:
        if current_ws:
            identity, is_sector = await _resolve_workspace_company(current_ws)
            if (
                identity is not None
                and identity.is_company
                and not is_sector
                and _ticker_key(identity.ticker) == wanted
            ):
                return _dir_hint(params, workspace_root)
        matches = [
            ws
            for ws in await find_company_workspaces(user_id, ticker)
            if str(ws.get("workspace_id") or "") != current_ws
        ]
    except Exception as error:
        logger.warning(
            "company routing lookup failed; check proceeds without routing "
            "hint (ticker=%s)",
            ticker,
            exc_info=error,
        )
        return None

    if matches:
        name = str(matches[0].get("name") or matches[0].get("workspace_id") or "")
        return (
            f"路由不符：本工作区不是 {ticker} 的工作区，按本工作区找文件必然缺项——"
            f"{ticker} 的研究应该在「{name}」里做。请让用户切换到那个工作区重跑，"
            "或从研究总监（Home）把任务交给那个工作区的分析师；"
            "不要用相对路径把产物写到其他工作区的目录（自动跟踪与完成核对都只认当前工作区）。"
        )
    return _dir_hint(params, workspace_root)


def _dir_hint(params: dict[str, Any], workspace_root: Any) -> str | None:
    if _workspace_dir_escapes(params.get("workspace_dir"), workspace_root):
        return (
            f"产物根目录 `workspace_dir`（{params.get('workspace_dir')!r}）越出了当前"
            "工作区（自动跟踪与完成核对都只认当前工作区，越界目录里的产物无法通过核对）。"
            "留在当前工作区运行时请传 `workspace_dir=\".\"`；若这项研究属于目标公司自己的工作区，"
            "请让用户切换到那个工作区重跑，或从研究总监（Home）把任务交给那个工作区的分析师。"
        )
    return None
