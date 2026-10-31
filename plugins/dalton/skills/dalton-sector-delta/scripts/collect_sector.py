#!/usr/bin/env python3
"""
collect_sector.py — 行业覆盖单元的资料增量采集脚本（sector layer P3）。

纯确定性代码，不调用大语言模型。与公司增量脚本
`.agents/skills/dalton-thesis-delta/scripts/collect.py` 共用同一条
researchdata 调用路与响应解析（import 复用，不复制传输层），
但采集面按行业边界收敛：

  - 检索词来自 data/sector.json：行业名（name / name_zh）与成员公司名
    （members[].name），默认上限 --max-terms（6）个；
  - 只跑关键词检索路 sales-notes / tmtb / funda-ai 与问答检索路
    guidepoint；
  - 设计性不采（packet 的 skipped 有说明）：SEC filing、港 A 公告、
    财报窗口、一致预期、股价——成员公司的证券类事件由公司侧发现一次，
    经 P2 跨单元联动进入行业，不重复购买/采集；alphaengine 的覆盖边界
    窗口模型按单一公司查询词维护游标状态，行业多检索词无法复用，首期不扫。

子命令：
  1. collect --workspace-dir <DIR> [--since YYYY-MM-DD] [--max-terms N] [--terms a,b,c]
     - 读取 <workspace_dir>/data/sector.json 构造检索词
     - 逐词检索四个内部来源（不在 seen.json 中的为新文档）
     - 写入 <workspace_dir>/research/tracker/<run_id>/packet.json 与 packet.md
     - collect 阶段绝不修改 seen.json
  2. commit --workspace-dir <DIR> --run-id <RUN_ID> --material yes|no --summary "..."
     - 将本轮新文档 id 并入 seen.json，更新 last_run.json，追加 updates.log
  3. status --workspace-dir <DIR>
  4. self-test
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

SKILL_DIR = Path(__file__).resolve().parent.parent
# 传输层与解析层复用公司增量脚本（同仓发布的兄弟技能目录）；
# 它的 argparse 只在 __main__ 下运行，import 无副作用。
THESIS_DELTA_COLLECT = (
    SKILL_DIR.parent / "dalton-thesis-delta" / "scripts" / "collect.py"
)

SECTOR_ROUTES = ("sales-notes", "tmtb", "funda-ai", "guidepoint")
SKIPPED_SOURCES = [
    {"source": "sec", "reason": "行业不采集证券类事件：公司侧发现一次，经跨单元联动进入行业"},
    {"source": "cnhk", "reason": "行业不采集证券类事件：公司侧发现一次，经跨单元联动进入行业"},
    {"source": "consensus", "reason": "行业无一致预期主体"},
    {"source": "price", "reason": "行业无股价探针（按需订阅属公司侧）"},
    {"source": "alphaengine", "reason": "覆盖边界窗口模型按单一公司查询词维护游标，行业多检索词无法复用，首期不扫"},
]

DEFAULT_MAX_TERMS = 6
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _load_base_module() -> Any:
    """Import the thesis-delta collector as a module (transport + parsing reuse)."""
    if not THESIS_DELTA_COLLECT.exists():
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "base_collector_missing",
                    "error": f"未找到公司增量采集脚本 {THESIS_DELTA_COLLECT}；sector 采集依赖它的传输层，请确认技能目录完整",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise SystemExit(2)
    spec = importlib.util.spec_from_file_location(
        "dalton_thesis_delta_collect", THESIS_DELTA_COLLECT
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {THESIS_DELTA_COLLECT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_base = _load_base_module()

call_bridge_jsonrpc = _base.call_bridge_jsonrpc
resolve_researchdata_transport = _base.resolve_researchdata_transport
RESEARCHDATA_TRANSPORT_UNCONFIGURED = _base.RESEARCHDATA_TRANSPORT_UNCONFIGURED
load_json = _base.load_json
dump_json = _base.dump_json
build_internal_route_request = _base.build_internal_route_request
_extract_route_items_with_page_info = _base._extract_route_items_with_page_info
_extract_route_warnings_and_status = _base._extract_route_warnings_and_status
_is_source_status_incomplete = _base._is_source_status_incomplete
_normalize_guidepoint_doc_id = _base._normalize_guidepoint_doc_id


def sector_tracker_paths(workspace_dir: Path) -> dict[str, Path]:
    ws = workspace_dir.resolve()
    tr = ws / "research" / "tracker"
    return {
        "workspace": ws,
        "tracker": tr,
        "seen": tr / "seen.json",
        "last_run": tr / "last_run.json",
        "log": tr / "updates.log",
        "sector_json": ws / "data" / "sector.json",
    }


def build_sector_terms(
    sector_doc: dict[str, Any] | None,
    *,
    max_terms: int = DEFAULT_MAX_TERMS,
    explicit_terms: list[str] | None = None,
) -> list[str]:
    """检索词：行业名（name / name_zh）+ 成员公司名，去重保序，封顶 max_terms。

    成员 ticker 不进检索词：证券类检索（公告/财报）不在行业采集面内，
    ticker 只会引来公司级材料。"""
    if explicit_terms:
        seen: list[str] = []
        for t in explicit_terms:
            s = str(t or "").strip()
            if s and s not in seen:
                seen.append(s)
        return seen[: max(1, max_terms)]

    doc = sector_doc if isinstance(sector_doc, dict) else {}
    terms: list[str] = []
    for key in ("name", "name_zh"):
        val = str(doc.get(key) or "").strip()
        if val and val not in terms:
            terms.append(val)
    for m in doc.get("members") or []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        if name and name not in terms:
            terms.append(name)
    return terms[: max(1, int(max_terms))]


def _term_meta(term: str) -> dict[str, Any]:
    """Per-term meta for ``build_internal_route_request``: one keyword per call.

    公司采集把全部别名拼成一个查询串（一个主体一次检索）；行业检索词是
    多个不同主体/关键词，必须逐词检索，否则一个查询串什么也匹配不到。"""
    return {"ticker": "", "name": term, "aliases": [term]}


def collect_sector_sources(
    terms: list[str],
    since: str,
    until: str,
    seen: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """逐词检索四个内部来源，返回 (packet 片段, 状态)。

    新文档按来源文档 id 去重（guidepoint 归一化为 guidepoint:<id>），
    并记录命中的检索词；同一文档被多个检索词命中只保留一条，guidepoint
    同一访谈的每条问答都并进同一文档的 qa 列表（codex r2 #3）。
    单个来源失败记入状态、不中断其余来源。

    完整性（codex r2 #2）：`ok=true` 不等于完整——`status=partial`、
    `has_more=true` 等未扫完信号让该次查询记 `incomplete`，来源聚合
    `ok=false`，最终 `all_sources_ok=false`、不允许短路成"无新增"。
    """
    new_docs: dict[str, dict[str, Any]] = {}
    status: dict[str, dict[str, Any]] = {}
    # (doc_id, qa 键)：同一访谈被第二个检索词再次返回时，问答只留一份
    gp_qa_seen: set[tuple[str, str]] = set()

    for term in terms:
        meta = _term_meta(term)
        for route in SECTOR_ROUTES:
            tool_name, req_args = build_internal_route_request(route, meta, since, until)
            st_key = f"{route}:{term}"
            res = call_bridge_jsonrpc(tool_name, req_args)
            data = res.get("data") if isinstance(res.get("data"), dict) else {}
            items, page_info = _extract_route_items_with_page_info(data)
            ok = bool(res.get("ok", False))
            err_msg = res.get("error") or data.get("error")
            status_val, warnings = _extract_route_warnings_and_status(res, data)
            incomplete = bool(
                _is_source_status_incomplete(
                    {
                        "status": status_val,
                        "has_more": bool(page_info.get("has_more")),
                    }
                )
            )
            status[st_key] = {
                "ok": ok,
                "returned": len(items),
                "status": status_val,
                "incomplete": incomplete,
                **({"has_more": True} if page_info.get("has_more") else {}),
                **({"warnings": warnings} if warnings else {}),
                **({"error": err_msg} if err_msg else {}),
            }
            if not ok:
                continue
            for r in items:
                if route == "guidepoint":
                    did = _normalize_guidepoint_doc_id(r)
                    if not did or did in seen:
                        continue
                    item_date = str(
                        r.get("date")
                        or r.get("published_at")
                        or r.get("publish_date")
                        or r.get("created_at")
                        or ""
                    ).strip()
                    date_prefix = item_date[:10]
                    if _DATE_RE.match(date_prefix) and (
                        date_prefix < since or date_prefix > until
                    ):
                        continue
                    qa_pair: dict[str, Any] = {
                        k: r[k] for k in ("question", "answer", "context") if r.get(k) not in (None, "")
                    }
                    # 同一访谈的多条问答并进同一文档；不同检索词命中同一
                    # 文档也只保留一条、合并 matched_terms，重复返回的问答
                    # 按（文档 + 问答键）去重（codex r2 #3）。
                    entry = new_docs.get(did)
                    if entry is None:
                        entry = new_docs[did] = {
                            "route": "guidepoint",
                            "term": term,
                            "matched_terms": [term],
                            "title": r.get("transcript_name") or r.get("title") or r.get("headline"),
                            "date": date_prefix or item_date or None,
                            "from": r.get("original_source") or r.get("source") or "Guidepoint",
                            "snippet": str(r.get("snippet") or r.get("summary") or "")[:600],
                            "qa": [],
                        }
                    else:
                        if term not in entry["matched_terms"]:
                            entry["matched_terms"].append(term)
                    if qa_pair:
                        raw_id = str(r.get("id") or r.get("transcript_id") or r.get("document_id") or "").strip()
                        qa_key = raw_id or json.dumps(qa_pair, ensure_ascii=False, sort_keys=True)
                        if (did, qa_key) not in gp_qa_seen:
                            gp_qa_seen.add((did, qa_key))
                            entry["qa"].append(qa_pair)
                else:
                    did = r.get("document_id")
                    if not did or str(did).startswith("image:") or did in seen or did in new_docs:
                        continue
                    new_docs[did] = {
                        "route": route,
                        "term": term,
                        "matched_terms": [term],
                        "title": r.get("title"),
                        "date": r.get("date"),
                        "from": r.get("original_source"),
                        "snippet": (r.get("snippet") or "")[:600],
                    }

    per_route_status: dict[str, Any] = {}
    for st_key, st in status.items():
        route = st_key.split(":", 1)[0]
        agg = per_route_status.setdefault(
            route,
            {
                "ok": True,
                "complete": True,
                "queries": 0,
                "failed": 0,
                "incomplete_queries": 0,
                "returned": 0,
                "detail": {},
            },
        )
        agg["queries"] += 1
        agg["returned"] += int(st.get("returned") or 0)
        agg["detail"][st_key] = st
        if not st.get("ok", False):
            agg["ok"] = False
            agg["failed"] += 1
        if st.get("incomplete"):
            agg["ok"] = False
            agg["complete"] = False
            agg["incomplete_queries"] += 1

    packet_frag = {
        "new": new_docs,
        "status": per_route_status,
    }
    return packet_frag, status


def summarize_sector_failures(per_route_status: dict[str, Any]) -> list[dict[str, str]]:
    """汇总 ok=false 或未完成的来源（含检索词级明细），供短路门禁使用。

    未完成（partial / has_more / incomplete）与取数失败同级：都不能当成
    "没有新文档"（codex r2 #2）。
    """
    failures: list[dict[str, str]] = []
    for route, agg in per_route_status.items():
        if agg.get("ok", False):
            continue
        detail = agg.get("detail") or {}
        failed_terms = [k for k, st in detail.items() if not st.get("ok", False) and not st.get("incomplete")]
        incomplete_terms = [k for k, st in detail.items() if st.get("incomplete")]
        reasons: list[str] = []
        if failed_terms:
            first_err = ""
            for k in failed_terms:
                first_err = str((detail[k].get("error")) or (detail[k].get("status")) or "")
                if first_err:
                    break
            reasons.append(f"{len(failed_terms)}/{agg.get('queries')} 个检索词失败：{first_err}")
        if incomplete_terms:
            reasons.append(f"{len(incomplete_terms)}/{agg.get('queries')} 个检索词未扫完（partial/has_more）")
        failures.append({"source": route, "error": "；".join(reasons) or "未完成"})
    return failures


def render_packet_md(packet: dict[str, Any]) -> str:
    lines = [
        f"# 行业资料增量包 · {packet.get('sector') or packet.get('run_id')}",
        "",
        f"- 主体：{packet.get('sector')}（sector unit `{packet.get('unit_id')}`）",
        f"- 窗口：{packet.get('since')} → {packet.get('until')}（run `{packet.get('run_id')}`）",
        f"- 检索词：{', '.join(packet.get('terms') or [])}",
        f"- 新文档：{len((packet.get('internal') or {}).get('new') or {})} 篇",
    ]
    skipped = packet.get("skipped") or []
    if skipped:
        lines.append("- 设计性不采（行业边界，非取数失败）：")
        for s in skipped:
            lines.append(f"  - {s['source']}：{s['reason']}")
    failures = packet.get("failed_sources") or []
    if failures:
        lines.append("- 取数失败/未完成（不能当作无新文档）：")
        for f in failures:
            lines.append(f"  - {f['source']}：{f['error']}")
    new_docs = (packet.get("internal") or {}).get("new") or {}
    if new_docs:
        lines += ["", "## 新文档", ""]
        for did, d in new_docs.items():
            hit = "/".join(d.get("matched_terms") or [])
            lines.append(
                f"- `{did}` [{d.get('route')}]（检索词 {hit}）{d.get('title') or ''}"
                f"{(' · ' + str(d.get('date'))) if d.get('date') else ''}"
            )
            if d.get("snippet"):
                lines.append(f"  > {str(d['snippet'])[:200]}")
            qa_list = d.get("qa") or []
            if qa_list:
                lines.append(f"  - 问答（{len(qa_list)} 条，必读再判断增量）：")
                for qa in qa_list[:3]:
                    q_text = str(qa.get("question") or "").strip()
                    a_text = str(qa.get("answer") or "").strip()
                    if q_text:
                        lines.append(f"    - Q: {q_text[:200]}")
                    if a_text:
                        lines.append(f"      A: {a_text[:300]}")
                if len(qa_list) > 3:
                    lines.append(f"    - ……其余 {len(qa_list) - 3} 条见 packet.json")
    else:
        lines += ["", "本轮无新文档。"]
    return "\n".join(lines) + "\n"


def cmd_collect(args: argparse.Namespace) -> int:
    ws_dir = Path(args.workspace_dir).resolve()
    P = sector_tracker_paths(ws_dir)

    transport = resolve_researchdata_transport()
    if transport["mode"] is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": RESEARCHDATA_TRANSPORT_UNCONFIGURED,
                    "subject_kind": "sector",
                    "error": transport["error"],
                    "hint": (
                        "沙箱内应经 PTC 包装模块（tools.researchdata）调用，或设 "
                        "DALTON_RESEARCHDATA_URL 走 HTTP 桥；两者皆不可用，本轮未采集任何来源，"
                        "按取数失败如实汇报，不要当成“本轮无新文档”"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    sector_doc = load_json(P["sector_json"], None)
    if not isinstance(sector_doc, dict) or not sector_doc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "sector_doc_missing",
                    "subject_kind": "sector",
                    "error": f"未找到行业画像 {P['sector_json']}；先经 knowledge_update 初始化 sector 文档再采集",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    explicit = [t for t in (args.terms or "").split(",") if t.strip()] or None
    terms = build_sector_terms(
        sector_doc, max_terms=args.max_terms, explicit_terms=explicit
    )
    if not terms:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "no_search_terms",
                    "subject_kind": "sector",
                    "error": "sector 文档里没有可用的行业名或成员名作为检索词",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    last_run = load_json(P["last_run"], {})
    seen = set(load_json(P["seen"], []))

    now = datetime.now()
    until = now.date().isoformat()
    if args.since:
        since = args.since
    elif last_run.get("until"):
        since = (date.fromisoformat(str(last_run["until"])) - timedelta(days=1)).isoformat()
    else:
        since = (now.date() - timedelta(days=7)).isoformat()

    run_id = args.run_id or now.strftime("%Y%m%d-%H%M")
    run_dir = P["tracker"] / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    packet_frag, _ = collect_sector_sources(terms, since, until, seen)
    failed_sources = summarize_sector_failures(packet_frag["status"])
    all_sources_ok = len(failed_sources) == 0
    short_circuit_eligible = all_sources_ok and len(packet_frag["new"]) == 0

    sector_name = str(sector_doc.get("name_zh") or sector_doc.get("name") or "").strip()
    packet = {
        "subject_kind": "sector",
        "sector": sector_name,
        "unit_id": sector_doc.get("unit_id"),
        "run_id": run_id,
        "since": since,
        "until": until,
        "terms": terms,
        "all_sources_ok": all_sources_ok,
        "failed_sources": failed_sources,
        "short_circuit_eligible": short_circuit_eligible,
        "internal_status": packet_frag["status"],
        "internal": packet_frag,
        "skipped": SKIPPED_SOURCES,
    }

    pkt_json_path = run_dir / "packet.json"
    pkt_md_path = run_dir / "packet.md"
    dump_json(pkt_json_path, packet)
    pkt_md_path.write_text(render_packet_md(packet), encoding="utf-8")

    summary = {
        "ok": all_sources_ok,
        "all_sources_ok": all_sources_ok,
        "subject_kind": "sector",
        "sector": sector_name,
        "run_id": run_id,
        "since": since,
        "until": until,
        "terms": terms,
        "failed_sources": failed_sources,
        "short_circuit_eligible": short_circuit_eligible,
        "packet_json": str(pkt_json_path.relative_to(ws_dir)),
        "packet_md": str(pkt_md_path.relative_to(ws_dir)),
        "internal_status": packet_frag["status"],
        "counts": {"internal": len(packet_frag["new"])},
        "skipped": [s["source"] for s in SKIPPED_SOURCES],
        "next": (
            f"python3 .agents/skills/dalton-sector-delta/scripts/collect_sector.py commit "
            f"--workspace-dir {ws_dir} --run-id {run_id} --material yes|no --summary '...'"
        ),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    ws_dir = Path(args.workspace_dir).resolve()
    P = sector_tracker_paths(ws_dir)
    pkt_path = P["tracker"] / args.run_id / "packet.json"
    pk = load_json(pkt_path, None)
    if not isinstance(pk, dict):
        print(json.dumps({"ok": False, "error": f"packet 不存在：{pkt_path}"}, ensure_ascii=False))
        return 2

    new_ids = list(((pk.get("internal") or {}).get("new") or {}).keys())
    seen = set(load_json(P["seen"], []))
    seen.update(new_ids)
    dump_json(P["seen"], sorted(seen))

    # 窗口纪律（codex r2 #2 / r3 #4）：采集不完整（有来源失败或未扫完）时
    # 绝不推进 until——未扫到的区间留在下一轮窗口里，seen 去重兜底。首次
    # 采集（尚无 last_run.until）即不完整时，until 记为本轮 since：窗口
    # 起点之前视为已完整覆盖（与公司 collect 的 ae_covered_until 边界语
    # 义一致），下一轮 since = until - 1 天，从本轮窗口起点重扫整个窗口。
    prev_last_run = load_json(P["last_run"], {})
    packet_complete = bool(pk.get("all_sources_ok"))
    if packet_complete:
        next_until = str(pk.get("until") or "")
    else:
        prev_until = str(prev_last_run.get("until") or "").strip()
        if prev_until:
            next_until = prev_until
        else:
            next_until = str(pk.get("since") or "")

    now_iso = datetime.now().isoformat(timespec="seconds")
    dump_json(
        P["last_run"],
        {
            "subject_kind": "sector",
            "sector": pk.get("sector"),
            "unit_id": pk.get("unit_id"),
            "until": next_until,
            "window_advanced": packet_complete,
            "committed_at": now_iso,
            "run_id": args.run_id,
            "material": args.material,
            "summary": args.summary.strip(),
            **({} if packet_complete else {"incomplete_run_id": args.run_id}),
        },
    )
    with P["log"].open("a", encoding="utf-8") as f:
        window_note = "" if packet_complete else "（采集不完整，未推进检索窗口）"
        f.write(
            f"{now_iso} | {args.run_id} | material={args.material} | {args.summary.strip()}{window_note}\n"
        )
    print(
        json.dumps(
            {
                "ok": True,
                "committed": args.run_id,
                "material": args.material,
                "new_count": len(new_ids),
                "window_advanced": packet_complete,
                "until": next_until,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ws_dir = Path(args.workspace_dir).resolve()
    P = sector_tracker_paths(ws_dir)
    last_run = load_json(P["last_run"], {})
    seen = load_json(P["seen"], [])
    sector_doc = load_json(P["sector_json"], {})
    print(
        json.dumps(
            {
                "subject_kind": "sector",
                "sector": (sector_doc or {}).get("name_zh") or (sector_doc or {}).get("name"),
                "last_run": last_run,
                "seen_count": len(seen) if isinstance(seen, list) else 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_self_test() -> int:
    """不触网的自检：检索词构造与去重、packet 渲染的最小形状。"""
    doc = {
        "name": "AI Infrastructure",
        "name_zh": "AI 基础设施",
        "members": [
            {"id": "M1", "name": "Mega Tech"},
            {"id": "M2", "name": "AI 基础设施"},  # 与行业名重复 → 去重
            {"id": "M3", "name": ""},
        ],
    }
    terms = build_sector_terms(doc, max_terms=6)
    assert terms == ["AI Infrastructure", "AI 基础设施", "Mega Tech"], terms
    capped = build_sector_terms(doc, max_terms=2)
    assert capped == ["AI Infrastructure", "AI 基础设施"], capped
    explicit = build_sector_terms(
        doc,
        max_terms=6,
        explicit_terms=[t for t in "政策, 半导体".split(",") if t.strip()],
    )
    assert explicit == ["政策", "半导体"], explicit
    md = render_packet_md(
        {
            "sector": "AI 基础设施",
            "unit_id": "u-1",
            "run_id": "r1",
            "since": "2026-10-01",
            "until": "2026-10-07",
            "terms": ["AI Infrastructure"],
            "skipped": SKIPPED_SOURCES,
            "failed_sources": [],
            "internal": {"new": {}},
        }
    )
    assert "行业资料增量包" in md and "设计性不采" in md
    print(json.dumps({"ok": True, "self_test": "passed"}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="行业覆盖单元的资料增量采集（P3）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_col = sub.add_parser("collect", help="按行业关键词与成员公司名采集内部资料增量")
    p_col.add_argument("--workspace-dir", default=".", help="行业工作区目录路径（默认当前目录）")
    p_col.add_argument("--since", help="检索窗口起始日期 YYYY-MM-DD（默认上次 until - 1 天，首次回看 7 天）")
    p_col.add_argument("--run-id", help="指定 run_id（默认按当前时间 YYYYMMDD-HHMM 生成）")
    p_col.add_argument("--max-terms", type=int, default=DEFAULT_MAX_TERMS, help=f"检索词上限（默认 {DEFAULT_MAX_TERMS}）")
    p_col.add_argument("--terms", help="逗号分隔的检索词覆盖（默认取 sector 文档的行业名与成员名）")

    p_com = sub.add_parser("commit", help="提交本轮增量结果，更新 seen.json、last_run.json 与 updates.log")
    p_com.add_argument("--workspace-dir", default=".", help="行业工作区目录路径（默认当前目录）")
    p_com.add_argument("--run-id", required=True, help="要提交的 run_id")
    p_com.add_argument("--material", choices=["yes", "no"], required=True, help="本轮是否有重要变化")
    p_com.add_argument("--summary", required=True, help="本轮一句话结论摘要")

    p_st = sub.add_parser("status", help="查看行业工作区增量跟踪状态")
    p_st.add_argument("--workspace-dir", default=".", help="行业工作区目录路径（默认当前目录）")

    sub.add_parser("self-test", help="检索词构造与 packet 渲染的离线自检")

    args = parser.parse_args()
    if args.cmd == "collect":
        return cmd_collect(args)
    if args.cmd == "commit":
        return cmd_commit(args)
    if args.cmd == "status":
        return cmd_status(args)
    return cmd_self_test()


if __name__ == "__main__":
    sys.exit(main())
