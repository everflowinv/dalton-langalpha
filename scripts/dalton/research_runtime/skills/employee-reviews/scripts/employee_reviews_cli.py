#!/usr/bin/env python3
"""employee-reviews CLI — Blind / Indeed / Glassdoor 员工评论抓取。

设计原则与 agent-reach 的 channel 一致：每个站点一条主通路 + 明确的失败口径。
- Blind    : 直连 HTTP，解析 Next.js flight payload，可全量翻页
- Indeed   : Firecrawl MCP，markdown 解析，20 条/页
- Glassdoor: Firecrawl MCP，只有首页聚合指标 + 3 条正文（更深需登录态，不绕）

Firecrawl 有 key 就带 key（额度和并发更高），没 key 自动退回无鉴权公开端点。
key 依次从环境变量 FIRECRAWL_API_KEY、~/.config/firecrawl/api_key 读取。

所有输出只作员工侧情绪证据，不作为事实数字的一手来源。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

VERSION = "employee-reviews-1.0.0"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
BLIND_BASE = "https://www.teamblind.com"
FIRECRAWL_MCP = "https://mcp.firecrawl.dev/v2/mcp"
FIRECRAWL_KEY_FILE = os.path.expanduser("~/.config/firecrawl/api_key")

RATING_KEYS = ("overall", "career", "balance", "compensation", "culture", "management")


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def emit_json(payload):
    print(json.dumps(payload, ensure_ascii=False, default=str))


def error_hint(err: Exception) -> str:
    msg = str(err).lower()
    if "404" in msg or "not found" in msg:
        return "公司 slug 可能不对。先跑 blind-resolve 确认 Blind 的 urlAlias。"
    if "403" in msg or "forbidden" in msg:
        return "站点拒绝了直连。Indeed/Glassdoor 必须走 Firecrawl，不要裸 curl。"
    if "429" in msg or "rate" in msg:
        return "触发限流。降低 --pages 或加 --sleep，稍后重试；不要并发重试。"
    if "ssl" in msg or "tls" in msg or "timed out" in msg:
        return "网络或 TLS 失败。检查代理设置后重试。"
    if "firecrawl" in msg or "mcp" in msg:
        if firecrawl_key():
            return ("Firecrawl 报错。先查余额：curl -H \"Authorization: Bearer $(cat "
                    "~/.config/firecrawl/api_key)\" https://api.firecrawl.dev/v2/team/credit-usage")
        return ("没读到 Firecrawl key，正在走无鉴权公开端点，额度和稳定性都更差。"
                "把 key 写进 ~/.config/firecrawl/api_key（chmod 600）后重试。")
    return "检查参数与网络后重试；先跑 self-test 确认三条通路状态。"


def print_error(prefix: str, err: Exception, as_json: bool = False):
    payload = {
        "ok": False,
        "error": str(err),
        "hint": error_hint(err),
        "meta": {"version": VERSION, "ts": datetime.now().isoformat(timespec="seconds")},
    }
    if as_json:
        emit_json(payload)
    else:
        print(f"{prefix}: {payload['error']}", file=sys.stderr)
        print(f"Hint: {payload['hint']}", file=sys.stderr)


def http_get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# --------------------------------------------------------------------------- #
# Blind
# --------------------------------------------------------------------------- #
def blind_payload(html: str) -> str:
    """把 Next.js flight chunk 拼回一整段可搜索的 JSON 文本。"""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    joined = "".join(chunks)
    return joined.encode("utf-8", "ignore").decode("unicode_escape", errors="ignore")


def _slice_json_array(text: str, start_bracket: int):
    depth = 0
    in_str = False
    esc = False
    for j in range(start_bracket, len(text)):
        c = text[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start_bracket : j + 1]
    raise ValueError("Blind flight payload 中的 reviews 数组没有闭合，页面结构可能变了")


def blind_parse_page(html: str):
    payload = blind_payload(html)
    key = '"reviews":{"list":['
    i = payload.find(key)
    if i < 0:
        return [], {}
    raw = _slice_json_array(payload, i + len(key) - 1)
    reviews = json.loads(raw)

    meta = {}
    m = re.search(r'"filteredCount":(\d+),"totalCount":(\d+)', payload)
    if m:
        meta["filtered_count"] = int(m.group(1))
        meta["total_count"] = int(m.group(2))
    return reviews, meta


LOREM_PREFIX = "lorem ipsum dolor sit amet"


def _blind_locked(r: dict) -> bool:
    """Blind 只对最新一页放出 pros/cons 全文，之后用 lorem ipsum 占位。

    被锁的只有正文：六维评分、summary、jobgroup、地点、时间戳仍然是真的。
    """
    return (r.get("pros") or "").strip().lower().startswith(LOREM_PREFIX)


def blind_normalize(r: dict, company: str) -> dict:
    alias = r.get("urlAlias")
    locked = _blind_locked(r)
    return {
        "source": "blind",
        "id": r.get("id"),
        "company": company,
        "url": f"{BLIND_BASE}/company/{urllib.parse.quote(company)}/reviews/{alias}" if alias else None,
        "created_at": r.get("createdAt"),
        "summary": r.get("summary"),
        "pros": None if locked else r.get("pros"),
        "cons": None if locked else r.get("cons"),
        "body_locked": locked,
        "ratings": {k: r.get(k) for k in RATING_KEYS},
        "jobgroup": r.get("jobgroup"),
        "location": r.get("memberLocation"),
        "country": r.get("memberLocationCountryIsoCode"),
        "tenure_start": r.get("memberStart"),
        "tenure_end": r.get("memberEnd"),
        "helpful_count": r.get("helpfulCnt"),
        "is_featured": bool(r.get("isFeatured")),
    }


def blind_fetch(company: str, pages: int, since: str = None, sleep: float = 0.8, log=None):
    out, seen, meta = [], set(), {}
    since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc) if since else None
    stop = False
    for page in range(1, pages + 1):
        url = f"{BLIND_BASE}/company/{urllib.parse.quote(company)}/reviews"
        if page > 1:
            url += f"?page={page}"
        try:
            html = http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError(f"Blind 上没有 '{company}' 这个 slug（HTTP 404）") from e
            raise
        reviews, page_meta = blind_parse_page(html)
        meta.update(page_meta)
        if not reviews:
            break
        for r in reviews:
            if r.get("id") in seen:
                continue
            seen.add(r.get("id"))
            item = blind_normalize(r, company)
            if since_dt and item["created_at"]:
                ts = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
                if ts < since_dt:
                    stop = True
                    continue
            out.append(item)
        if log:
            log(f"[blind] page {page}: +{len(reviews)} 条，累计 {len(out)}")
        if stop or len(reviews) < 30:
            break
        time.sleep(sleep)
    return out, meta


def cmd_blind(args):
    out, meta = blind_fetch(
        args.company,
        pages=args.pages,
        since=args.since,
        sleep=args.sleep,
        log=None if args.quiet else lambda m: print(m, file=sys.stderr),
    )
    locked = sum(1 for r in out if r["body_locked"])
    payload = {
        "ok": True,
        "source": "blind",
        "company": args.company,
        "count": len(out),
        "body_unlocked": len(out) - locked,
        "body_locked": locked,
        "library_total": meta.get("total_count"),
        "reviews": out,
        "notes": [
            "Blind 只对最新一页（30 条）放出 pros/cons 全文，之后 body_locked=true",
            "被锁的条目里六维评分、summary、jobgroup、地点、时间戳仍然真实可用",
        ],
        "meta": {"version": VERSION, "fetched_at": datetime.now(timezone.utc).isoformat()},
    }
    if args.json:
        emit_json(payload)
    else:
        print(f"Blind / {args.company}: 取到 {len(out)} 条（库内共 {meta.get('total_count')} 条）"
              f"；正文可读 {len(out) - locked} 条，其余只有评分和标题")
        for r in out[:20]:
            rr = r["ratings"]
            print(f"  {r['created_at'][:10]}  总{rr['overall']} 成长{rr['career']} 平衡{rr['balance']} "
                  f"薪酬{rr['compensation']} 文化{rr['culture']} 管理{rr['management']}  "
                  f"[{r['jobgroup']}] {r['summary']}")
        if len(out) > 20:
            print(f"  ... 其余 {len(out) - 20} 条见 --json")


def cmd_blind_resolve(args):
    """探测 Blind 的 company urlAlias。Blind 的搜索结果是前端渲染的，只能按候选 slug 试。"""
    q = args.query.strip()
    cands = []
    for c in (q, q.upper(), q.title(), q.replace(" ", ""), q.replace(" ", "-"),
              q.replace(" ", "").title(), q.replace(" ", "").upper()):
        if c not in cands:
            cands.append(c)
    hits = []
    for c in cands:
        url = f"{BLIND_BASE}/company/{urllib.parse.quote(c)}/reviews"
        try:
            html = http_get(url, timeout=20)
        except urllib.error.HTTPError:
            continue
        reviews, meta = blind_parse_page(html)
        if reviews:
            hits.append({"alias": c, "url": url, "total_count": meta.get("total_count"),
                         "sample": reviews[0].get("summary")})
            break
        time.sleep(0.4)
    payload = {"ok": bool(hits), "query": q, "tried": cands, "matches": hits,
               "meta": {"version": VERSION}}
    if not hits:
        payload["hint"] = "候选 slug 都没命中。到 teamblind.com 搜一次，把地址栏里 /company/ 后面那段原样传 --company。"
    if args.json:
        emit_json(payload)
    else:
        if hits:
            h = hits[0]
            print(f"命中 alias: {h['alias']}（库内 {h['total_count']} 条）\n{h['url']}")
        else:
            print(f"没命中。试过：{', '.join(cands)}")
            print(payload["hint"])


# --------------------------------------------------------------------------- #
# Firecrawl（有 key 用 key，没 key 退回无鉴权公开端点）
# --------------------------------------------------------------------------- #
def firecrawl_key() -> str:
    """读 API key：先环境变量，再 key 文件。都没有就返回空串，走公开端点。"""
    key = (os.environ.get("FIRECRAWL_API_KEY") or "").strip()
    if key:
        return key
    try:
        with open(FIRECRAWL_KEY_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _firecrawl_args(url: str, proxy: str, max_age: int):
    args = {"url": url, "formats": ["markdown"], "onlyMainContent": True, "proxy": proxy}
    if max_age is not None:
        args["maxAge"] = max_age
    return args


def _firecrawl_rest(key: str, url: str, timeout: int, proxy: str, max_age: int) -> dict:
    """带 key 时走 REST。比带 key 的 MCP 端点稳，后者会间歇 503 报凭据校验不可用。"""
    body = json.dumps(_firecrawl_args(url, proxy, max_age)).encode()
    req = urllib.request.Request(
        "https://api.firecrawl.dev/v2/scrape", data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    if not payload.get("success"):
        raise RuntimeError(f"Firecrawl 报错: {payload.get('error') or payload}")
    return payload.get("data") or {}


def _firecrawl_mcp(url: str, timeout: int, proxy: str, max_age: int) -> dict:
    """无鉴权公开端点。额度更紧、只有 scrape/search/parse，但不需要 key。"""
    def post(body):
        req = urllib.request.Request(
            FIRECRAWL_MCP, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        for line in raw.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(raw) if raw.strip() else {}

    post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "employee-reviews", "version": VERSION}}})
    res = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "firecrawl_scrape",
                           "arguments": _firecrawl_args(url, proxy, max_age)}})
    if "error" in res:
        raise RuntimeError(f"Firecrawl 报错: {res['error'].get('message', res['error'])}")
    content = res.get("result", {}).get("content") or []
    if not content:
        raise RuntimeError("Firecrawl 返回空内容")
    return json.loads(content[0]["text"])


def firecrawl_scrape(url: str, timeout: int = 120, proxy: str = "auto", max_age: int = None) -> dict:
    key = firecrawl_key()
    if not key:
        return _firecrawl_mcp(url, timeout, proxy, max_age)
    try:
        return _firecrawl_rest(key, url, timeout, proxy, max_age)
    except urllib.error.HTTPError as e:
        # 402 额度耗尽 / 401 key 失效：公开端点也救不了，直接把原因报出来
        if e.code in (401, 402, 403):
            raise RuntimeError(f"Firecrawl REST {e.code}：key 失效或额度耗尽") from e
        sys.stderr.write(f"[employee-reviews] Firecrawl REST {e.code}，回退无鉴权端点\n")
        return _firecrawl_mcp(url, timeout, proxy, max_age)


# --------------------------------------------------------------------------- #
# Indeed
# --------------------------------------------------------------------------- #
INDEED_REVIEW_RE = re.compile(
    r"###\s+\[(?P<title>[^\]]*)\]\((?P<url>https://www\.indeed\.com/cmp/[^)]*?/reviews/[^)]*?)\)"
)


def indeed_parse(md: str, company: str):
    """把 Firecrawl 的 markdown 切成结构化评论。

    页面结构：日期行 → ### [标题](链接) → #### 职位 → 地点 → 正文 → '- Report review'。
    featured review 每页都会置顶重复，标出来供去重。
    """
    reviews = []
    matches = list(INDEED_REVIEW_RE.finditer(md))
    for idx, m in enumerate(matches):
        head = md[: m.start()]
        tail_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md)
        block = md[m.end() : tail_end]

        # featured：标题上方最近的分节标记
        featured = "## Featured review" in head[-400:]

        # 日期：标题上方最近的一行日期
        dates = re.findall(r"^-?\s*([A-Z][a-z]+ \d{1,2}, \d{4})\s*$", head[-600:], re.M)
        date = dates[-1] if dates else None

        lines = [ln.strip() for ln in block.splitlines()]
        job, location, body = None, None, []
        for ln in lines:
            if not ln:
                continue
            if ln.startswith("- Report review") or ln.startswith("- Copy link"):
                break
            if ln.startswith("#### ") and job is None:
                job = ln[5:].strip()
                continue
            if ln.startswith("#") or ln.startswith("["):
                continue
            if job is not None and location is None and len(ln) < 60 and not ln.endswith("."):
                location = ln
                continue
            if job is not None:
                body.append(ln)
        text = " ".join(body).strip()
        truncated = text.endswith("...Show more") or "...Show more" in text
        text = text.replace("...Show more", "").strip()

        rid = None
        qs = urllib.parse.urlparse(m.group("url")).query
        if qs:
            rid = urllib.parse.parse_qs(qs).get("id", [None])[0]

        reviews.append({
            "source": "indeed",
            "id": rid,
            "company": company,
            "url": m.group("url"),
            "date": _norm_date(date),
            "title": m.group("title").strip(),
            "job_title": job,
            "location": location,
            "text": text,
            "text_truncated": truncated,
            "is_featured": featured,
        })
    return reviews


def _norm_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%B %d, %Y").date().isoformat()
    except ValueError:
        return s


def indeed_aggregate(md: str, meta: dict):
    agg = {}
    m = re.search(r"## Overall rating\s*\n+\s*([\d.]+)\s*\n+\s*Based on ([\d,]+) reviews", md)
    if m:
        agg["overall"] = float(m.group(1))
        agg["global_review_count"] = int(m.group(2).replace(",", ""))
    for label, key in (("Work-life balance", "work_life_balance"),
                       ("Pay and benefits", "pay_benefits"),
                       ("Job security and advancement", "job_security"),
                       ("Management", "management"),
                       ("Culture", "culture")):
        mm = re.search(rf"([\d.]+) out of 5 stars for {re.escape(label)}", md)
        if mm:
            agg[key] = float(mm.group(1))
    m = re.search(r"##\s+([\d,]+)\s+reviews", md)
    if m:
        agg["filtered_review_count"] = int(m.group(1).replace(",", ""))
    if meta.get("reviewCount"):
        agg["metadata_review_count"] = int(str(meta["reviewCount"]).replace(",", ""))
    return agg


def cmd_indeed(args):
    all_reviews, seen, agg = [], set(), {}
    for page in range(args.pages):
        start = page * 20
        url = f"https://www.indeed.com/cmp/{args.company}/reviews"
        if start:
            url += f"?start={start}"
        data = firecrawl_scrape(url, proxy=args.proxy, max_age=0 if args.fresh else None)
        md = data.get("markdown") or ""
        meta = data.get("metadata") or {}
        if page == 0:
            agg = indeed_aggregate(md, meta)
        batch = indeed_parse(md, args.company)
        new = 0
        for r in batch:
            key = r["id"] or (r["title"], r["date"])
            if key in seen:
                continue
            seen.add(key)
            all_reviews.append(r)
            new += 1
        if not args.quiet:
            print(f"[indeed] start={start}: 解析 {len(batch)} 条，新增 {new}", file=sys.stderr)
        if new == 0:
            break
        time.sleep(args.sleep)

    payload = {
        "ok": True,
        "source": "indeed",
        "company": args.company,
        "count": len(all_reviews),
        "aggregate": agg,
        "reviews": all_reviews,
        "notes": [
            "featured review 每页置顶重复，已按 id 去重（is_featured 标出）",
            "text_truncated=true 表示正文被 'Show more' 截断，要全文需单独抓该条 url",
            "filtered_review_count 是美国区筛选后的数，global_review_count 才是全球数，别混用",
        ],
        "meta": {"version": VERSION, "fetched_at": datetime.now(timezone.utc).isoformat()},
    }
    if args.json:
        emit_json(payload)
    else:
        print(f"Indeed / {args.company}: {len(all_reviews)} 条")
        if agg:
            print(f"  聚合: 总分 {agg.get('overall')}｜全球 {agg.get('global_review_count')} 条"
                  f"｜筛选后 {agg.get('filtered_review_count')} 条")
            print(f"  平衡 {agg.get('work_life_balance')}｜薪酬 {agg.get('pay_benefits')}"
                  f"｜晋升 {agg.get('job_security')}｜管理 {agg.get('management')}｜文化 {agg.get('culture')}")
        for r in all_reviews[:15]:
            flag = "★" if r["is_featured"] else " "
            print(f" {flag}{r['date']}  [{r['job_title']}] {r['title']}")


# --------------------------------------------------------------------------- #
# Glassdoor
# --------------------------------------------------------------------------- #
GD_DIMENSIONS = ("Work/Life balance", "Culture & values", "Diversity & inclusion",
                 "Career opportunities", "Compensation and benefits", "Senior management")


def glassdoor_aggregate(md: str, meta: dict) -> dict:
    """Glassdoor 首页能拿到的公司级指标。

    注意：评论区里那些逐条六维分属于单条评论，不是公司均值。公司级六维只在
    FAQ 正文里出现，这里按 FAQ 文本抽取，抽不到就留空，不用评论均值冒充。
    """
    agg = {}
    m = re.search(r"employee rating of ([\d.]+) out of 5 stars, based on ([\d,]+) company reviews", md)
    if m:
        agg["overall"] = float(m.group(1))
        agg["review_count"] = int(m.group(2).replace(",", ""))
    else:
        t = meta.get("ogTitle") or meta.get("title") or ""
        mm = re.search(r"Reviews \(([\d,]+)\)", t)
        if mm:
            agg["review_count"] = int(mm.group(1).replace(",", ""))
    for pat, key in ((r"(\d+)% would recommend to a friend", "recommend_pct"),
                     (r"(\d+)% approve of CEO", "ceo_approval_pct"),
                     (r"(\d+)% positive business outlook", "positive_outlook_pct")):
        mm = re.search(pat, md)
        if mm:
            agg[key] = int(mm.group(1))
    mm = re.search(r"industry \(([\d.]+) stars\)", md)
    if mm:
        agg["industry_avg"] = float(mm.group(1))

    faq = {}
    mm = re.search(r"rate their compensation and benefits as ([\d.]+) out of 5", md)
    if mm:
        faq["compensation_and_benefits"] = float(mm.group(1))
    mm = re.search(r"([\d.]+) out of 5 for work life balance, ([\d.]+) for culture and values "
                   r"and ([\d.]+) for career opportunities", md)
    if mm:
        faq["work_life_balance"] = float(mm.group(1))
        faq["culture_and_values"] = float(mm.group(2))
        faq["career_opportunities"] = float(mm.group(3))
    if faq:
        agg["company_dimension_ratings"] = faq

    demo = []
    for val, label, n in re.findall(r"([\d.]+)★\s*\n+\s*([A-Za-z][^\n]*?)(\d+)ratings", md):
        demo.append({"group": label.strip(), "rating": float(val), "n": int(n)})
    if demo:
        agg["demographic_ratings"] = demo
    return agg


def _gd_clean(text: str) -> str:
    """去掉 Glassdoor 正文尾巴上的展开提示和登录墙提示。"""
    for marker in ("Show more", "Sign in to see more insights", "Helpful", "Share", "Report"):
        idx = text.find("\n" + marker)
        if idx >= 0:
            text = text[:idx]
    return text.strip()[:2000]


GD_REVIEW_RE = re.compile(
    r"\[\*\*(?P<title>[^\]]*?)\*\*\]\((?P<url>https://www\.glassdoor\.com/Reviews/Employee-Review-[^)]+)\)"
)


def glassdoor_parse_reviews(md: str):
    """解析首页放出来的少量评论正文（通常 3 条）。"""
    out = []
    ms = list(GD_REVIEW_RE.finditer(md))
    for i, m in enumerate(ms):
        block = md[m.end(): ms[i + 1].start() if i + 1 < len(ms) else len(md)]
        rid = re.search(r"RVW(\d+)\.htm", m.group("url"))
        job = re.search(r"^\[([^\]]+)\]\(https://www\.glassdoor\.com/Reviews/[^)]*KO", block, re.M)
        status = re.search(r"^((?:Current|Former) employee[^\n]*)$", block, re.M)
        date = re.search(r"^([A-Z][a-z]{2} \d{1,2}, \d{4})$", block, re.M)
        overall = re.search(r"^- ([\d.]+)$", block, re.M)
        dims = {}
        for label in GD_DIMENSIONS:
            dm = re.search(rf"([\d.]+)\s*\n+\s*{re.escape(label)}", block)
            if dm:
                dims[label] = float(dm.group(1))
        pros = re.search(r"^Pros\s*\n+([\s\S]*?)\n+Cons\s*$", block, re.M)
        cons = re.search(r"^Cons\s*\n+([\s\S]*?)(?:\n+(?:Helpful|Share|Report)\s*$|\Z)", block, re.M)
        out.append({
            "source": "glassdoor",
            "id": rid.group(1) if rid else None,
            "url": m.group("url"),
            "title": m.group("title").strip(),
            "job_title": job.group(1).strip() if job else None,
            "employment_status": status.group(1).strip() if status else None,
            "date": date.group(1) if date else None,
            "overall": float(overall.group(1)) if overall else None,
            "dimension_ratings": dims,
            "pros": _gd_clean(pros.group(1)) if pros else None,
            "cons": _gd_clean(cons.group(1)) if cons else None,
        })
    return out


def cmd_glassdoor(args):
    data = firecrawl_scrape(args.url, proxy=args.proxy, max_age=0 if args.fresh else None)
    md = data.get("markdown") or ""
    meta = data.get("metadata") or {}
    agg = glassdoor_aggregate(md, meta)
    reviews = glassdoor_parse_reviews(md)
    login_wall = "bot-detection" in json.dumps(meta) or "Sign in to unlock" in md

    payload = {
        "ok": True,
        "source": "glassdoor",
        "url": args.url,
        "aggregate": agg,
        "count": len(reviews),
        "reviews": reviews,
        "login_wall_detected": login_wall,
        "limits": [
            "只有首页可读：_P2.htm 会 302 到 member/profile/login?reason=bot-detection",
            "正文通常只有 3 条；深度评论需登录态，本 skill 不绕登录墙",
            "评论里的六维分是单条评分；公司级六维只从 FAQ 正文抽，抽不到就为空",
        ],
        "meta": {"version": VERSION, "fetched_at": datetime.now(timezone.utc).isoformat()},
    }
    if args.full:
        payload["markdown"] = md
    if args.json:
        emit_json(payload)
    else:
        print(f"Glassdoor: {args.url}")
        print(f"  总分 {agg.get('overall')}｜{agg.get('review_count')} 条｜推荐率 {agg.get('recommend_pct')}%"
              f"｜CEO 认可 {agg.get('ceo_approval_pct')}%｜看好前景 {agg.get('positive_outlook_pct')}%"
              f"｜行业均值 {agg.get('industry_avg')}")
        for k, v in (agg.get("company_dimension_ratings") or {}).items():
            print(f"    {k}: {v}")
        if agg.get("demographic_ratings"):
            print("  人口统计切分：" + "，".join(
                f"{d['group']} {d['rating']}({d['n']})" for d in agg["demographic_ratings"][:8]))
        print(f"\n  首页放出的正文 {len(reviews)} 条：")
        for r in reviews:
            print(f"    {r['date']}｜{r['overall']}｜[{r['job_title']}] {r['title']}")
            if r["pros"]:
                print(f"      + {r['pros'][:120]}")
            if r["cons"]:
                print(f"      - {r['cons'][:120]}")


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def cmd_summary(args):
    reviews, meta = blind_fetch(
        args.company, pages=args.pages, since=args.since, sleep=args.sleep,
        log=None if args.quiet else lambda m: print(m, file=sys.stderr))
    if not reviews:
        raise RuntimeError(f"Blind 上 '{args.company}' 没取到评论")

    buckets = defaultdict(list)
    for r in reviews:
        if not r["created_at"]:
            continue
        buckets[r["created_at"][:7]].append(r["ratings"])
    series = []
    for month in sorted(buckets):
        rows = buckets[month]
        entry = {"month": month, "n": len(rows)}
        for k in RATING_KEYS:
            vals = [x[k] for x in rows if isinstance(x.get(k), (int, float))]
            entry[k] = round(sum(vals) / len(vals), 2) if vals else None
        series.append(entry)

    stop = set("""the a an and or but of to in for on with is are was were be been it its this that
        i my me we our you your they them he she at as if so not no do does did have has had can could
        would should will just very really much more most lot lots get got make makes made work working
        works company job good great bad people team time day days year years thing things""".split())

    unlocked = [r for r in reviews if not r["body_locked"]]

    def top_terms(field, n=15):
        c = Counter()
        for r in unlocked:
            txt = (r.get(field) or "").lower()
            for w in re.findall(r"[a-z][a-z'\-]{2,}", txt):
                if w not in stop:
                    c[w] += 1
        return c.most_common(n)

    jobs = Counter(r["jobgroup"] for r in reviews if r["jobgroup"]).most_common(10)
    locs = Counter(r["location"] for r in reviews if r["location"]).most_common(10)
    overall = [r["ratings"]["overall"] for r in reviews if isinstance(r["ratings"].get("overall"), (int, float))]

    payload = {
        "ok": True,
        "company": args.company,
        "source": "blind",
        "count": len(reviews),
        "body_unlocked": len(unlocked),
        "library_total": meta.get("total_count"),
        "window": {"since": args.since, "pages": args.pages},
        "notes": [
            "六维月度序列基于全部抓到的评论（评分不受正文锁影响）",
            f"pros/cons 词频只统计正文可读的 {len(unlocked)} 条，样本偏最新",
        ],
        "overall_mean": round(sum(overall) / len(overall), 2) if overall else None,
        "monthly_ratings": series,
        "top_pros_terms": top_terms("pros"),
        "top_cons_terms": top_terms("cons"),
        "top_jobgroups": jobs,
        "top_locations": locs,
        "meta": {"version": VERSION, "fetched_at": datetime.now(timezone.utc).isoformat()},
    }
    if args.json:
        emit_json(payload)
    else:
        print(f"{args.company}｜Blind {len(reviews)} 条（库内 {meta.get('total_count')}）"
              f"｜总分均值 {payload['overall_mean']}｜正文可读 {len(unlocked)} 条")
        print("\n月度六维均分：")
        for e in series[-12:]:
            print(f"  {e['month']} n={e['n']:<3} 总{e['overall']} 成长{e['career']} 平衡{e['balance']} "
                  f"薪酬{e['compensation']} 文化{e['culture']} 管理{e['management']}")
        print(f"\n高频优点词（{len(unlocked)} 条正文）：",
              ", ".join(f"{w}({c})" for w, c in payload["top_pros_terms"][:10]))
        print(f"高频缺点词（{len(unlocked)} 条正文）：",
              ", ".join(f"{w}({c})" for w, c in payload["top_cons_terms"][:10]))
        print("岗位分布：", ", ".join(f"{j}({c})" for j, c in jobs[:6]))


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
def cmd_self_test(args):
    checks = []

    def run(name, fn):
        try:
            detail = fn()
            checks.append({"name": name, "ok": True, "detail": detail})
        except Exception as e:
            checks.append({"name": name, "ok": False, "error": str(e), "hint": error_hint(e)})

    def blind_check():
        rs, meta = blind_fetch("NVIDIA", pages=1, sleep=0)
        assert rs, "Blind 返回 0 条"
        assert rs[0]["ratings"]["overall"] is not None, "六维评分缺失"
        assert not rs[0]["body_locked"], "首页正文也被锁了，Blind 的免费额度可能变了"
        return f"{len(rs)} 条 / 库内 {meta.get('total_count')}，首页正文可读"

    def indeed_check():
        data = firecrawl_scrape("https://www.indeed.com/cmp/Nvidia/reviews")
        rs = indeed_parse(data.get("markdown") or "", "Nvidia")
        assert rs, "Indeed 解析出 0 条"
        return f"{len(rs)} 条"

    def glassdoor_check():
        data = firecrawl_scrape("https://www.glassdoor.com/Reviews/NVIDIA-Reviews-E7633.htm")
        md = data.get("markdown") or ""
        assert len(md) > 500, "Glassdoor 返回内容过短"
        return f"markdown {len(md)} 字符（只有首页）"

    def firecrawl_auth_check():
        if not firecrawl_key():
            return "没读到 key，走无鉴权公开端点（额度更紧，功能更少）"
        req = urllib.request.Request(
            "https://api.firecrawl.dev/v2/team/credit-usage",
            headers={"Authorization": f"Bearer {firecrawl_key()}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8", errors="ignore"))
        left = d.get("data", {}).get("remainingCredits")
        assert left is not None, "余额接口返回异常"
        return f"key 可用，剩余 {left} credits"

    run("firecrawl.auth", firecrawl_auth_check)
    run("blind.http", blind_check)
    run("indeed.firecrawl", indeed_check)
    run("glassdoor.firecrawl", glassdoor_check)

    ok = all(c["ok"] for c in checks)
    payload = {"ok": ok, "checks": checks,
               "meta": {"version": VERSION, "ts": datetime.now().isoformat(timespec="seconds")}}
    if args.json:
        emit_json(payload)
    else:
        print(f"Self-test: {'PASS' if ok else 'FAIL'}")
        for c in checks:
            print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c.get('detail') or c.get('error')}")
            if not c["ok"]:
                print(f"         hint: {c.get('hint')}")
    if not ok:
        sys.exit(1)


# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="Blind / Indeed / Glassdoor 员工评论抓取")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--quiet", action="store_true", help="不打进度日志")
    p.add_argument("--version", action="version", version=VERSION)

    # 同一批全局开关在子命令后面也认，省得记参数顺序。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="command", required=True, parser_class=argparse.ArgumentParser)

    b = sub.add_parser("blind", help="Blind 评论，直连全量翻页", parents=[common])
    b.add_argument("--company", required=True, help="Blind urlAlias，如 NVIDIA / Palantir")
    b.add_argument("--pages", type=int, default=3, help="最多翻几页（30 条/页），默认 3")
    b.add_argument("--since", help="只保留该日期之后的评论 YYYY-MM-DD")
    b.add_argument("--sleep", type=float, default=0.8)
    b.set_defaults(func=cmd_blind)

    r = sub.add_parser("blind-resolve", help="探测 Blind 的 company urlAlias", parents=[common])
    r.add_argument("--query", required=True)
    r.set_defaults(func=cmd_blind_resolve)

    i = sub.add_parser("indeed", help="Indeed 评论，走 Firecrawl", parents=[common])
    i.add_argument("--company", required=True, help="Indeed cmp slug，如 Nvidia / Palantir-Technologies")
    i.add_argument("--pages", type=int, default=2, help="最多翻几页（20 条/页），默认 2")
    i.add_argument("--proxy", default="auto", choices=["basic", "stealth", "auto"])
    i.add_argument("--fresh", action="store_true", help="绕过 Firecrawl 缓存")
    i.add_argument("--sleep", type=float, default=1.5)
    i.set_defaults(func=cmd_indeed)

    g = sub.add_parser("glassdoor", help="Glassdoor 首页聚合指标（只有首页）", parents=[common])
    g.add_argument("--url", required=True, help="完整 Reviews URL，如 .../Reviews/NVIDIA-Reviews-E7633.htm")
    g.add_argument("--proxy", default="auto", choices=["basic", "stealth", "auto"])
    g.add_argument("--fresh", action="store_true")
    g.add_argument("--full", action="store_true", help="JSON 里返回完整 markdown")
    g.set_defaults(func=cmd_glassdoor)

    s = sub.add_parser("summary", help="Blind 六维月度序列 + pros/cons 词频", parents=[common])
    s.add_argument("--company", required=True)
    s.add_argument("--pages", type=int, default=10)
    s.add_argument("--since")
    s.add_argument("--sleep", type=float, default=0.8)
    s.set_defaults(func=cmd_summary)

    t = sub.add_parser("self-test", help="三条通路逐条验活", parents=[common])
    t.set_defaults(func=cmd_self_test)

    args = p.parse_args()
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        print_error("Unhandled error", e, as_json=getattr(args, "json", False))
        sys.exit(1)


if __name__ == "__main__":
    main()
