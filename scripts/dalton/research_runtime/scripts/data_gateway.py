#!/usr/bin/env python3
"""Read-only research data directory and CLI/local-MCP dispatcher.

No LLM, daemon, credentials store, implicit fan-out, or provider substitution.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

_DEFAULT_CODE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_research_roots(default_code_root: Path = _DEFAULT_CODE_ROOT) -> tuple[Path, Path]:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    # Legacy compat: when new vars are unset and DALTON_OCW_ROOT is set, keep old single-root layout.
    if raw_ocw and not raw_code and not raw_home:
        ocw = Path(raw_ocw).expanduser().resolve()
        return ocw, ocw
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code_root.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return code_root, home_root


ROOT, HOME_ROOT = _resolve_research_roots()
REGISTRY = ROOT / "config/data_sources.json"
NATIVE_SOURCES = {
    "alphaengine": "http://127.0.0.1:8950/mcp",
    "guidepoint": os.environ.get("DALTON_GUIDEPOINT_MCP_URL", "").strip() or "http://127.0.0.1:8943/mcp",
}


def _read_bridge_env_fallback(key: str) -> str:
    env_file = Path(
        os.environ.get("DALTON_BRIDGE_ENV_FILE", "").strip()
        or (Path.home() / ".config" / "dalton" / "researchdata-bridge.env")
    ).expanduser()
    if not env_file.is_file():
        return ""
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            k, v = line.split("=", 1)
            if k.strip() == key:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                return v
    except OSError:
        return ""
    return ""


def _resolve_guidepoint_mcp_url() -> str:
    return (
        os.environ.get("DALTON_GUIDEPOINT_MCP_URL", "").strip()
        or _read_bridge_env_fallback("DALTON_GUIDEPOINT_MCP_URL")
        or NATIVE_SOURCES["guidepoint"]
    )


def _resolve_research_host_token() -> str:
    return (
        os.environ.get("DALTON_RESEARCH_HOST_TOKEN", "").strip()
        or _read_bridge_env_fallback("DALTON_RESEARCH_HOST_TOKEN")
    )
LIBRARY_SOURCES = {"company-wiki", "sales-notes", "tmtb", "funda-ai", "all"}
CN_HK_SINGLE_DAY_DEFAULT_MAX_PAGES = 5
CN_HK_MAX_PAGES_LIMIT = 20
CN_HK_MAX_START_PAGE = 200
CN_HK_MAX_DEADLINE_S = 600.0
# AlphaEngine 业绩会 ASR 稿类型；roic.ai 自 2026-08 整站 403，已停用。
TRANSCRIPT_TYPE_ID = "IND_SECOND_5FA442AE"
CALL_TITLE = re.compile(r"(?:\b[1-4]Q|\bQ[1-4])\s*'?(?:FY)?\s*\d{2,4}|\bFY\s?'?\d{2,4}|\bearnings\b|业绩.{0,6}会|电话会|说明会", re.I)
CALLBACK_TITLE = re.compile(r"call\s?back|回访|闭门会", re.I)


def _resolve_route_skill_path(route):
    skill = route.get("skill")
    if route.get("id") == "agent-reach":
        override = os.environ.get("DALTON_AGENT_REACH_SKILL_MD", "").strip()
        if override:
            return Path(override).expanduser()
    if not skill:
        return None
    p = Path(skill).expanduser()
    return p if p.is_absolute() else (ROOT / p)


def catalog(include_hidden=False):
    registry=json.loads(REGISTRY.read_text())
    if not include_hidden:
        registry['routes']=[r for r in registry['routes'] if not r.get('hidden',False)]
    override = os.environ.get("DALTON_AGENT_REACH_SKILL_MD", "").strip()
    if override:
        for r in registry.get("routes", []):
            if isinstance(r, dict) and r.get("id") == "agent-reach":
                r["skill"] = str(Path(override).expanduser())
    return registry


def require(args, *names):
    for name in names:
        if not getattr(args, name, None):
            raise ValueError(f"需要 --{name.replace('_', '-')}")


def latest_today():
    """Today in the latest timezone the sources date by.

    AlphaEngine and the internal library stamp Beijing dates, which run a day
    ahead of this machine's for part of every day; a date that is already today
    there is not in the future.
    """
    from zoneinfo import ZoneInfo

    return max(date.today(), datetime.now(ZoneInfo("Asia/Shanghai")).date())


def date_arg(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or parsed > latest_today():
        raise argparse.ArgumentTypeError("日期须为 YYYY-MM-DD，且不能晚于今天")
    return value


def build_command(route, args):
    rid = route["id"]
    if getattr(args, "params_json", None) and rid not in set(NATIVE_SOURCES) | LIBRARY_SOURCES | {"sales-mail", "sec", "estimates", "cn-hk"}:
        raise ValueError("--params-json 仅用于研究来源的只读参数")
    accepted = {
        "sec": {"ticker", "query", "concept", "item", "document", "as_of", "form"},
        "fred": {"series", "start", "end", "vintage"},
        "treasury": {"dataset", "start", "end"},
        "cn-hk": {"ticker", "query", "start", "end"},
        "sales-mail": {"query", "start", "end", "document"},
        "transcript": {"ticker", "query", "start", "end"},
        "estimates": {"ticker"},
        "alphaengine": {"query", "start", "end"},
        "guidepoint": {"query", "start", "end"},
    }
    accepted.update({rid: {"query", "start", "end", "document"} for rid in LIBRARY_SOURCES})
    if rid in accepted:
        for key in ("ticker", "query", "series", "concept", "item", "document", "form", "dataset", "start", "end", "as_of", "vintage"):
            if getattr(args, key, None) and key not in accepted[rid]:
                raise ValueError(f"{rid} 不支持 --{key.replace('_', '-')}；参数不会被静默忽略")
    operation = args.operation or (route["operations"][0] if route["operations"] else None)
    if operation and operation not in route["operations"]:
        raise ValueError(f"{rid} 不支持 {operation}；可用：{route['operations']}")
    if args.as_of and rid != "sec":
        raise ValueError("--as-of 只支持 SEC；FRED 用 --vintage，其他源未声明历史可见版本")
    if args.vintage and rid != "fred":
        raise ValueError("--vintage 只支持 FRED/ALFRED")
    if rid in LIBRARY_SOURCES or rid == "sales-mail":
        from research_library import validate_request
        params = json.loads(args.params_json or "{}")
        if not isinstance(params, dict):
            raise ValueError("params-json 须为对象")
        target = ("sales-notes" if operation == "read" else "all") if rid == "sales-mail" else rid
        if rid == "sales-mail" and operation == "search":
            params={"sources":["sales-notes","tmtb"],"layer":"all","media":"text",**params}
        validate_request(target, operation, args.query, args.document, params)
        if operation == "read" and (args.start or args.end):
            raise ValueError("read 不接受日期筛选")
        cmd = ["bash", str(ROOT / "scripts/research_library.sh"), target,
               "--operation=" + operation, "--limit=" + str(args.limit),
               "--params-json=" + json.dumps(params, ensure_ascii=False)]
        for field in ("query", "document", "start", "end"):
            if getattr(args, field, None):
                cmd.append("--" + field + "=" + getattr(args, field))
        return cmd
    fin = ["bash", str(ROOT / "skills/findata-analyst/run.sh"), "--json"]
    if rid == "sec":
        require(args, "ticker")
        for field, operations in {"query": {"smart-facts", "search-concepts", "search-text"},
                                  "concept": {"facts"}, "item": {"read-item"}, "document": {"attachments"}}.items():
            if getattr(args, field, None) and operation not in operations:
                raise ValueError(f"SEC {operation} 不接受 {field}；参数不会被静默忽略")
        extra = json.loads(args.params_json or "{}")
        flags = {
            "attachments": {"accession": "--accession", "document_type": "--document-type", "max_chars": "--max-chars"},
            "financials": {"statement": "--statement"},
            "read-item": {"max_chars": "--max_chars"},
            "search-text": {"window": "--window", "max_matches": "--max_matches"},
        }.get(operation, {})
        if not isinstance(extra, dict) or set(extra) - set(flags):
            raise ValueError(f"SEC {operation} params 仅支持 {list(flags)}")
        for key, value in extra.items():
            if key in ("max_chars", "window", "max_matches"):
                if type(value) is not int or not 1 <= value <= (100 if key == "max_matches" else 500000):
                    raise ValueError(f"{key} 须为范围内的正整数")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} 须为非空字符串")
        if "accession" in extra and not re.fullmatch(r"\d{10}-\d{2}-\d{6}", extra["accession"]):
            raise ValueError("accession 须为 SEC 完整编号 XXXXXXXXXX-XX-XXXXXX")
        if extra.get("statement", "all") not in ("all", "income", "balance", "cash"):
            raise ValueError("statement 须为 all|income|balance|cash")
        if args.document and extra.get("document_type"):
            raise ValueError("document 与 document_type 不能同时使用")
        cmd = fin + [operation, "--ticker", args.ticker]
        if operation not in ("eight-k", "insider", "search-concepts"):
            cmd += ["--form", args.form or "10-Q"]
        if operation not in ("read-item", "attachments", "search-concepts"):
            cmd += ["--limit", str(args.limit)]
        if operation in ("smart-facts", "search-concepts", "search-text"):
            require(args, "query"); cmd += ["--keyword", args.query]
        if operation == "facts":
            require(args, "concept"); cmd += ["--concept", args.concept]
        if operation == "read-item":
            require(args, "item"); cmd += ["--item", args.item]
        if operation == "attachments" and args.document:
            cmd += ["--document", args.document]
        if args.as_of:
            cmd += ["--as-of", args.as_of]
        for key, value in extra.items():
            cmd.append(flags[key] + "=" + str(value))
        return cmd
    if rid == "fred":
        require(args, "series", "start", "end")
        return fin + ["macro", "--series", args.series, "--start", args.start, "--end", args.end] + (["--vintage", args.vintage] if args.vintage else [])
    if rid == "treasury":
        require(args, "dataset", "start", "end")
        return fin + ["treasury", "--dataset", args.dataset, "--start", args.start, "--end", args.end]
    if rid == "cn-hk":
        extra = json.loads(args.params_json or "{}")
        if not isinstance(extra, dict):
            raise ValueError("params-json 须为对象")
        cmd = ["bash", str(ROOT / "skills/cn-hk-findata/run.sh"), "--json"]
        if operation == "filings":
            require(args, "ticker", "start", "end")
            if args.query:
                raise ValueError("cn-hk filings 不接受 query；使用 ticker/start/end")
            allowed_filings_params = {"max_pages", "start_page", "deadline_s"}
            if set(extra) - allowed_filings_params:
                raise ValueError("cn-hk filings params 仅支持 ['max_pages', 'start_page', 'deadline_s']")
            for key, max_val in (("max_pages", CN_HK_MAX_PAGES_LIMIT), ("start_page", CN_HK_MAX_START_PAGE)):
                if key in extra and extra[key] is not None:
                    val = extra[key]
                    if type(val) is not int or not 1 <= val <= max_val:
                        raise ValueError(f"{key} 须为范围内的正整数")
            deadline_s = extra.get("deadline_s")
            if deadline_s is not None:
                import math
                if (
                    isinstance(deadline_s, bool)
                    or not isinstance(deadline_s, (int, float))
                    or not math.isfinite(float(deadline_s))
                    or not 0 < float(deadline_s) <= CN_HK_MAX_DEADLINE_S
                ):
                    raise ValueError("deadline_s 须为范围内的正数")
            max_pages = extra.get("max_pages")
            if max_pages is None:
                if args.start == args.end:
                    max_pages = CN_HK_SINGLE_DAY_DEFAULT_MAX_PAGES
                else:
                    max_pages = max(1, (int(args.limit) + 29) // 30)
            start_page = extra.get("start_page") or 1
            filings_cmd = cmd + [
                "--ticker", args.ticker,
                "--list-filings",
                "--date-from", args.start,
                "--date-to", args.end,
                "--limit", str(args.limit),
                "--max-pages", str(max_pages),
            ]
            if start_page > 1:
                filings_cmd += ["--start-page", str(start_page)]
            if deadline_s is not None:
                eff_deadline = float(deadline_s)
                if getattr(args, "timeout", None):
                    eff_deadline = min(eff_deadline, float(args.timeout))
                filings_cmd += ["--deadline-s", f"{eff_deadline:g}"]
            return filings_cmd
        if any(v is not None for v in extra.values()):
            raise ValueError(f"cn-hk {operation} params 仅支持 []")
        if operation == "quote":
            require(args, "ticker")
            if args.query or args.start or args.end:
                raise ValueError("cn-hk quote 只接受 ticker；当前报价不接受历史日期")
            if not re.fullmatch(r"\d{5,6}", args.ticker):
                raise ValueError("cn-hk quote 使用5位港股或6位A股代码")
            return cmd + ["--fallback-quote", args.ticker]
        if args.ticker or args.start or args.end:
            raise ValueError("cn-hk ask 的公司和日期请写入 query；结构化 ticker/start/end 用 filings，当前报价用 quote+ticker")
        require(args, "query")
        return cmd + ["--backend", "akshare", "--ask", args.query, "--max-rows", str(args.limit)]
    if rid == "transcript":
        require(args, "ticker")
        return None
    if rid == "estimates":
        require(args, "ticker")
        extra = json.loads(args.params_json or "{}")
        if not isinstance(extra, dict) or set(extra) - {"shares_bn", "tax"}:
            raise ValueError("estimates params 仅支持 shares_bn 和 tax")
        import math
        for key, value in extra.items():
            if type(value) not in (int, float) or not math.isfinite(value) or not (value > 0 if key == "shares_bn" else 0 <= value < 1):
                raise ValueError("shares_bn 须为正数，tax 须为 0≤税率<1")
        return ["bash", str(ROOT / "scripts/consensus_estimates.sh"), "--ticker", args.ticker] + [
            "--" + key.replace("_", "-") + "=" + str(value) for key, value in extra.items()]
    return None


def native_request(route, args, execute_request=False):
    """Forward only registered read-only operations to existing loopback MCPs."""
    rid = route["id"]
    operation = args.operation or "search_library"
    params = json.loads(args.params_json or "{}")
    if not isinstance(params, dict):
        raise ValueError("params-json 必须是 JSON 对象")
    if operation not in route["operations"]:
        raise ValueError("未登记的只读操作")
    if operation == "search_library":
        if args.query:
            if "query" in params and params["query"] != args.query:
                raise ValueError("query 与 params-json.query 冲突")
            params["query"] = args.query
        if rid == "alphaengine":
            has_query = bool(str(params.get("query") or "").strip())
            if has_query and params.get("sort", "relevance") != "relevance":
                raise ValueError("主题检索须使用 sort=relevance")
            params.setdefault("sort", "relevance" if has_query else "newest")
            params.setdefault("limit", args.limit)
        else:
            params.setdefault("size", min(args.limit, 30))
            if type(params["size"]) is not int or not 1 <= params["size"] <= 30:
                raise ValueError("Guidepoint search_library 的 size 须为 1–30 的整数（以实时 tools/list 为准）")
            query = params.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 1000:
                raise ValueError("Guidepoint search_library 需要 1–1000 字符的 query；keywords 不能替代 query")
            bias = params.get("recency_bias")
            if bias is not None and (type(bias) is not int or not 1 <= bias <= 100):
                raise ValueError("Guidepoint recency_bias 须为 1–100 的整数或 null")
            keywords = params.get("keywords")
            if keywords is not None and (not isinstance(keywords, list) or not 1 <= len(keywords) <= 10
                                         or any(not isinstance(k, str) or not k.strip() for k in keywords)):
                raise ValueError("Guidepoint keywords 须为 1–10 个非空字符串；无筛选时省略或传 null")
        for name, field in (("start", "date_from" if rid == "alphaengine" else "start_date"),
                            ("end", "date_to" if rid == "alphaengine" else "end_date")):
            value = getattr(args, name)
            if value:
                if field in params and params[field] != value:
                    raise ValueError("日期参数冲突")
                params[field] = value
    elif rid == "alphaengine" and operation == "list_recording_tasks":
        for name, field in (("query", "query"), ("start", "date_from"), ("end", "date_to")):
            value = getattr(args, name)
            if value:
                if field in params and params[field] != value:
                    raise ValueError(f"{name} 与 params-json.{field} 冲突")
                params[field] = value
        params.setdefault("limit", args.limit)
    elif any((args.query, args.start, args.end)):
        raise ValueError("非 search_library 操作的参数请通过 params-json 完整传入")
    result = {"operation": operation, "parameters": params}
    if not execute_request:
        return result
    target_url = _resolve_guidepoint_mcp_url() if rid == "guidepoint" else NATIVE_SOURCES[rid]
    req_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if rid == "guidepoint":
        host_token = _resolve_research_host_token()
        if host_token:
            req_headers["Authorization"] = f"Bearer {host_token}"
    request = urllib.request.Request(target_url, data=json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": operation, "arguments": params},
    }).encode(), headers=req_headers)
    try:
        # Local services must not be sent through an outbound HTTP proxy.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=args.timeout) as response:
            raw = response.read().decode()
        message = json.loads(raw) if raw.lstrip().startswith("{") else json.loads(
            [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")][-1])
        if "error" in message:
            return {"ok": False, "status": "error", "error": message["error"], **result}
        payload = message["result"]
        ok = not payload.get("isError", False)
        return {"ok": ok, "status": "ok" if ok else "error", "data": payload, **result}
    except (OSError, ValueError, KeyError, IndexError) as exc:
        return {"ok": False, "status": "error", "error": type(exc).__name__,
                "hint": "检查原有本地 MCP 服务及登录状态；本入口未切换数据源。", **result}


def _name_variants(name):
    """公司全称 → 去掉法律后缀的名称和核心名（Amazon.com, Inc. → Amazon.com / Amazon）。"""
    legal = re.sub(r"(?:[,.]?\s+(?:Corporation|Corp|Incorporated|Inc|Ltd|Limited|Co|Company|PLC|S\.?A|N\.?V|AG|SE)\.?)+$",
                   "", name, flags=re.I).strip(" ,.")
    core = re.sub(r"(?:\.com|\s+(?:Holdings?|Group|Platforms|Technologies|Technology)\b.*)$", "", legal, flags=re.I).strip()
    return [n for n in dict.fromkeys((legal, core)) if n]


def _ticker_company_name(ticker):
    """ticker → 公司名：先 yfinance（12 秒上限），再 13F 名称表；都没有返回 None。"""
    from concurrent.futures import ThreadPoolExecutor
    def lookup():
        import yfinance as yf
        info = yf.Ticker(ticker).get_info()
        return info.get("longName") or info.get("shortName")
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        name = pool.submit(lookup).result(timeout=12)
        if name:
            return name
    except Exception:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    try:
        table = json.loads((ROOT / "skills/13f-tracker/scripts/name_to_ticker.json").read_text())
        hits = [k for k, v in table.items() if v == ticker.upper()]
        return min(hits, key=len).title() if hits else None
    except (OSError, ValueError):
        return None


def _page_error_text(raw):
    """Flatten a JSON error body into plain text so the bridge sees `status_code=429` and decoded Chinese."""
    if isinstance(raw, dict):
        obj = raw
    else:
        try:
            obj = json.loads(raw) if isinstance(raw, str) and raw.strip().startswith("{") else None
        except ValueError:
            obj = None
    if not isinstance(obj, dict):
        return raw
    inner = obj.get("error") if isinstance(obj.get("error"), dict) else {}
    parts = []
    for src in (obj, inner):
        for key in ("status_code", "status", "error_code", "code"):
            val = src.get(key)
            if isinstance(val, (str, int)) and not isinstance(val, bool) and str(val):
                parts.append(f"{key}={val}")
        for key in ("error", "message", "detail"):
            val = src.get(key)
            if isinstance(val, str) and val:
                parts.append(val)
    return " ".join(parts)[:300] or json.dumps(obj, ensure_ascii=False)[:300]


def fetch_transcripts(reg, args, fallback_cmd):
    """只用 AlphaEngine 业绩会稿（roic.ai 已停用）；只返回清单，正文按 doc_id 另行读取。"""
    ae_route = next(r for r in reg["routes"] if r["id"] == "alphaengine")
    query_names = [n.strip() for n in re.split(r"[|,，/]", args.query or "") if n.strip()]
    names = [args.ticker] + query_names
    # 稿件标题多写公司英文名（GOOGL 的稿叫 Alphabet Q2 2026，腾讯的叫 Tencent Q2 2026），
    # 只按 ticker 或中文名比对标题会漏掉，所以总是补上解析出的公司名。
    resolved = _ticker_company_name(args.ticker)
    if resolved:
        names += [n for n in _name_variants(resolved) if n.upper() not in {x.upper() for x in names}]
    # 不给窗口时默认近 400 天：按相关性排序会把旧季度排在前面（GOOGL 曾只返回 2024 年的稿）。
    end = args.end or latest_today().isoformat()
    start = args.start or date.fromordinal(date.fromisoformat(end).toordinal() - 400).isoformat()
    ae_args = argparse.Namespace(operation="search_library", query=" ".join(query_names) or names[-1],
                                 start=start, end=end, limit=50, timeout=args.timeout,
                                 params_json=json.dumps({"type_ids": [TRANSCRIPT_TYPE_ID], "include_snippets": False}))
    items, cursors, warnings, page_errors = [], set(), [], []
    for page in range(10):
        primary = native_request(ae_route, ae_args, True)
        if not primary.get("ok"):
            page_err = primary.get("error") or "".join(
                c.get("text", "") for c in (primary.get("data") or {}).get("content", [])
            )
            if not page_err and isinstance(primary.get("data"), dict):
                sc_err = primary["data"].get("structuredContent")
                if isinstance(sc_err, dict):
                    page_err = str(
                        sc_err.get("error") or sc_err.get("message") or sc_err.get("status") or ""
                    )[:300]
            # Parse the full body first; truncating before json.loads breaks long escaped errors.
            page_err = _page_error_text(page_err)
            if isinstance(page_err, str):
                page_err = page_err[:300]
            page_errors.append(
                {
                    "page": page + 1,
                    "error": str(page_err or primary.get("status") or "alphaengine_error"),
                }
            )
            if items:
                warnings.append("后续检索页失败，当前结果不完整")
            break
        payload = (primary.get("data") or {}).get("structuredContent") or {}
        items.extend(payload.get("results") or [])
        if not payload.get("has_more"):
            break
        cursor = payload.get("cursor")
        if not cursor or cursor in cursors or page == 9:
            warnings.append("检索尚有后续页；请缩小日期窗口或通过 alphaengine cursor 继续检索")
            break
        cursors.add(cursor)
        params = json.loads(ae_args.params_json)
        params["cursor"] = cursor
        ae_args.params_json = json.dumps(params)
    coverage = {"pages_scanned": page + 1, "records_scanned": len(items),
                "complete": not warnings and bool(primary.get("ok")), "warnings": warnings}
    if page_errors:
        coverage["page_errors"] = page_errors
    if warnings:
        continuation = json.loads(ae_args.params_json)
        if primary.get("ok") and payload.get("cursor"):
            continuation["cursor"] = payload["cursor"]
        coverage["continuation_params"] = {**continuation, "query": ae_args.query,
                                            "date_from": start, "date_to": end, "sort": "relevance", "limit": 50}
    # 标题须含名称；只看 companies 标签会把错标的他家电话会（如 SEA 标成 Meta）带进来。
    # 名称后紧跟季度/年份/业绩字样算紧匹配；有紧匹配就丢掉宽松匹配（Apple Hospitality REIT、Tencent Music）。
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    loose = re.compile(r"(?<![A-Za-z])(?:" + alt + r")(?![A-Za-z])", re.I)
    tight = re.compile(r"(?<![A-Za-z])(?:" + alt + r")\s*[-–:,(（]?\s*(?:[1-4]Q|Q[1-4]|FY|H[12]\b|20\d\d|\d{2}年|第|业绩|earnings)", re.I)
    candidates, seen = [], set()
    for item in items:
        title = item.get("title") or ""
        key = (title, (item.get("publish_time") or "")[:10])
        if key in seen or not CALL_TITLE.search(title) or not loose.search(title):
            continue
        seen.add(key)
        candidates.append({"doc_id": item["doc_id"], "title": title, "publish_time": item.get("publish_time"),
                           "kind": "broker_callback" if CALLBACK_TITLE.search(title) else "company_call",
                           "companies": item.get("companies"),
                           "source_attribution": (item.get("source_attribution") or {}).get("markdown"),
                           "_tight": bool(tight.search(title))})
    if any(c["_tight"] for c in candidates):
        candidates = [c for c in candidates if c["_tight"]]
    for c in candidates:
        del c["_tight"]
    candidates.sort(key=lambda x: x.get("publish_time") or "", reverse=True)
    if candidates:
        data_out = {
            "ok": True,
            "source": "alphaengine",
            "type_id": TRANSCRIPT_TYPE_ID,
            "names_matched": names,
            "coverage": coverage,
            "transcripts": candidates[:args.limit],
            "read_with": 'research(route="alphaengine", operation="get_document", params={"doc_id": "<doc_id>"})',
            "note": "ASR 稿，引用时标注转写误差，截掉末尾重复段；broker_callback 是券商回访，不是公司电话会。全文额度有限，只读需要的期次。",
        }
        if page_errors:
            data_out["page_errors"] = page_errors
        out = {"ok": True, "status": "partial" if warnings else "ok", "data": data_out}
        if page_errors:
            out["page_errors"] = page_errors
        return out
    error_text = None if primary.get("ok") else primary.get("error") or "".join(
        c.get("text", "") for c in (primary.get("data") or {}).get("content", []))[:300] or None
    # roic.ai 自 2026-08 整站 403，已停用兜底：AlphaEngine 无结果就如实返回。
    reason = "alphaengine_no_match" if primary.get("ok") else "alphaengine_error"
    return {"ok": False, "status": "no_match" if primary.get("ok") else "error", "data": {
        "ok": False, "source": "alphaengine", "reason": reason, "alphaengine_error": error_text,
        "names_tried": names, "coverage": coverage}, "hint": "AlphaEngine 无匹配：query 传公司全称或中文名（可用 | 分隔多个）重试，扩大 start/end，或改查 IR webcast / 8-K Ex-99。"}


def execute(cmd, timeout):
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
        return {"ok": False, "status": "timeout", "error": f"超过 {timeout} 秒，已停止取数进程组", "hint": "缩小窗口或按底层 skill 的 self-test 排查。"}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        # Do not print unparsed stderr: underlying tools may emit credential material.
        return {"ok": False, "status": "invalid_output", "exit_code": proc.returncode,
                "error": "底层工具未返回单一有效 JSON", "hint": "按底层 skill 文档排查；本入口未把日志当作数据。"}
    if not isinstance(payload, dict):
        return {"ok": False, "status": "invalid_output", "error": "底层 JSON 缺少结果对象", "hint": "直接核对底层工具输出协议。"}
    if proc.returncode or payload.get("ok") is False:
        return {"ok": False, "status": "error", "data": payload, "exit_code": proc.returncode}
    status = payload.get("status", "ok")
    out = {"ok": True, "status": status, "data": payload}
    if "pagination_complete" in payload:
        if status == "ok" and (
            payload.get("pagination_complete") is False
            or payload.get("has_more") is True
            or payload.get("truncated") is True
        ):
            out["status"] = "partial"
        for key in (
            "pagination_complete",
            "has_more",
            "truncated",
            "next_page",
            "start_page",
            "last_page",
            "pages_fetched",
            "max_pages",
            "page_size",
            "total_count",
            "page_error",
            "deadline_reached",
        ):
            if key in payload:
                out[key] = payload[key]
    return out


class _ArgumentError(ValueError):
    pass


class _JsonArgumentParser(argparse.ArgumentParser):
    """Report a bad argument as the gateway's JSON, not as usage on stderr.

    argparse exits 2 with nothing on stdout, which callers could only read as
    "no valid JSON" and filed as the upstream being down.
    """

    def error(self, message):
        raise _ArgumentError(message)


def main(argv=None):
    p = _JsonArgumentParser(description="统一研究取数入口：分类目录、路由与只读取数")
    p.add_argument("--json", action="store_true", help="兼容标志；本入口默认始终输出 JSON")
    sub = p.add_subparsers(dest="command", required=True)
    for action in ("catalog", "plan", "fetch", "self-test"):
        a = sub.add_parser(action)
        if action == "catalog":
            a.add_argument("--category")
        if action in ("plan", "fetch"):
            a.add_argument("route")
            for field in ("operation", "ticker", "query", "series", "concept", "item", "document"):
                a.add_argument("--" + field)
            a.add_argument("--form")
            a.add_argument("--params-json", help="研究来源参数；内部库支持 company/sector/tag/sender/layer/sources/search_mode/offset/max_chars")
            a.add_argument("--dataset", choices=("tga", "debt"))
            for field in ("start", "end", "as-of", "vintage"):
                a.add_argument("--" + field, type=date_arg)
            a.add_argument("--limit", type=int, default=3)
            a.add_argument("--timeout", type=int, default=180)
    try:
        args = p.parse_args(argv)
    except _ArgumentError as exc:
        print(json.dumps({"ok": False, "status": "invalid_arguments", "error_category": "参数错误",
                          "error": f"[参数错误] {exc}", "hint": "运行 catalog 或 plan --help 核对路由与参数。"},
                         ensure_ascii=False))
        return 1
    try:
        reg = catalog(include_hidden=args.command in ('plan','fetch','self-test'))
        if args.command == "catalog":
            if args.category and args.category not in reg["categories"]:
                raise ValueError(f"分类必须为 {list(reg['categories'])}")
            result = {"ok": True, **reg, "routes": [r for r in reg["routes"] if not args.category or r["category"] == args.category]}
        elif args.command == "self-test":
            ids = [r["id"] for r in reg["routes"]]
            checks = [{"name": "unique_route_ids", "ok": len(ids) == len(set(ids))}]
            for route in reg["routes"]:
                skill_path = _resolve_route_skill_path(route)
                valid = route["category"] in reg["categories"] and (skill_path is None or skill_path.is_file())
                checks.append({"name": route["id"], "ok": valid})
            result = {"ok": all(c["ok"] for c in checks), "scope": "local_registry_only; not provider health", "checks": checks}
        else:
            route = next((r for r in reg["routes"] if r["id"] == args.route), None)
            if route is None:
                raise ValueError("未知路由；先运行 catalog")
            if not 1 <= args.limit <= 100 or not 1 <= args.timeout <= 600:
                raise ValueError("limit 须在 1–100；timeout 须在 1–600 秒")
            if args.start and args.end and args.start > args.end:
                raise ValueError("start 不能晚于 end")
            cmd = build_command(route, args)
            result = {"ok": True, "status": "planned", "executed": False, "route": route, "argv": cmd,
                      "request": {k: v for k, v in vars(args).items() if k not in ("json", "command") and v is not None}}
            if route.get('hidden'):
                result['migration']={'deprecated_route':route['id'],'engine':'research_library',
                    'result_format':'document_v1','replacement':'sales-notes / tmtb / all',
                    'note':'旧名称只作转发；返回 document_id/snippet，用 read 读取正文，不再返回旧分块 JSON。'}
            if route["id"] == "transcript":
                result["primary"] = {"source": "alphaengine", "operation": "search_library",
                                     "type_ids": [TRANSCRIPT_TYPE_ID],
                                     "query": args.query or f"{args.ticker}（检索前先解析成公司名）"}
            if args.command == "fetch" and route["id"] == "transcript":
                result.update(fetch_transcripts(reg, args, cmd), executed=True)
            elif args.command == "fetch" and cmd:
                result.update(execute(cmd, args.timeout), executed=True)
            elif route["id"] in NATIVE_SOURCES:
                result.update(native_request(route, args, args.command == "fetch"), executed=args.command == "fetch")
            elif not cmd and route["id"] != "transcript":
                result.update(status="handoff_required", hint="读取指定 skill，或由当前 agent 发现并调用列出的原生工具；尚未取回数据。")
            result["gateway_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    except (ValueError, OSError) as exc:
        print(json.dumps({"ok": False, "status": "error", "error": str(exc), "hint": "运行 catalog 或 plan --help 核对路由与参数。"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
