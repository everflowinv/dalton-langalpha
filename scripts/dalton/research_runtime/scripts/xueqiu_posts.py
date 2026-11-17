#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""雪球帖子读取 — 个股讨论流、帖子全文、评论、关键词搜帖、用户时间线。

为什么不用 agent-reach 的 XueqiuChannel：它只走 xueqiu.com 主站，帖子类路径
被阿里云 WAF 拦成滑块验证页（2026-08-25 实测）。api.xueqiu.com 走的是另一套
网关，同样的登录 Cookie 下不过 WAF，帖子接口直接返回 JSON。行情和热股榜继续
用 agent-reach，本脚本只补帖子这一路。

Cookie 复用 agent-reach 的 ~/.agent-reach/config.yaml 里的 xueqiu_cookie
（当前只存 xq_a_token）。没有它 api.xueqiu.com 一律 400，所以过期时要重新签发：
  agent-reach configure --from-browser chrome --platform xueqiu
配置里的 token 过期（400013）时，会先用主站 /hq 页签发的访客 Cookie 自动重试一次；
访客身份可读的能力须逐项验证；讨论流/全文可读时，评论仍可能要求登录。

用法：
  python3 scripts/xueqiu_posts.py posts 06181 --pages 2
  python3 scripts/xueqiu_posts.py post https://xueqiu.com/1234567/406698595
  python3 scripts/xueqiu_posts.py comments 406698595
  python3 scripts/xueqiu_posts.py search 老铺黄金 --pages 2 --json
  python3 scripts/xueqiu_posts.py user https://xueqiu.com/u/1955602780
  python3 scripts/xueqiu_posts.py self-test
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

API = "https://api.xueqiu.com"
HOME = "https://xueqiu.com"
CONFIG_PATH = os.path.expanduser("~/.agent-reach/config.yaml")
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 20
CST = timezone(timedelta(hours=8))

COOKIE_HINT = (
    "雪球 Cookie 缺失或已过期。重新签发：\n"
    "  agent-reach configure --from-browser chrome --platform xueqiu\n"
    "（登录窗口若不在默认 Chrome profile，要指向当前配置的 Cookies 库）"
)


class XueqiuError(RuntimeError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------- #

def _read_cookie() -> str:
    """从 agent-reach 配置里取 xueqiu_cookie；没有 PyYAML 时退回逐行解析。"""
    if not os.path.exists(CONFIG_PATH):
        return ""
    raw = open(CONFIG_PATH, encoding="utf-8").read()
    try:
        import yaml  # type: ignore

        return (yaml.safe_load(raw) or {}).get("xueqiu_cookie") or ""
    except ImportError:
        m = re.search(r"^xueqiu_cookie:\s*(.+?)\s*$", raw, re.M)
        return m.group(1).strip().strip("'\"") if m else ""


def _build_opener(visitor: bool = False) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    for pair in ("" if visitor else _read_cookie()).split(";"):
        if "=" not in pair:
            continue
        name, _, value = pair.strip().partition("=")
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0, name=name.strip(), value=value.strip(), port=None,
                port_specified=False, domain=".xueqiu.com", domain_specified=True,
                domain_initial_dot=True, path="/", path_specified=True, secure=True,
                expires=None, discard=True, comment=None, comment_url=None, rest={},
            )
        )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    # 首页会补一个 acw_tc 反爬 Cookie，缺了它部分接口直接拒。首页只给 acw_tc，
    # /hq 行情页还会签发整套访客 token（xq_a_token 等）。
    home = HOME + "/hq" if visitor else HOME
    opener.open(urllib.request.Request(home, headers={"User-Agent": UA}), timeout=TIMEOUT).read()
    return opener


_OPENER: urllib.request.OpenerDirector | None = None
_VISITOR = False

# 雪球把「登录态失效」和「帖子不存在」都返回 400，只能靠 error_code 分开。
_AUTH_ERROR_CODES = {"400016", "400017", "400018"}
# timeout_access_token：配置里的 token 过期，换访客 token 重试。
_EXPIRED_TOKEN_CODES = {"400013"}


def _http_error(e: urllib.error.HTTPError) -> XueqiuError:
    try:
        payload = json.loads(e.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - 错误体本身不可解析时按登录失效处理
        payload = {}
    code = str(payload.get("error_code") or "")
    desc = payload.get("error_description") or ""
    if code in _AUTH_ERROR_CODES or "登录" in desc or not desc:
        return XueqiuError(f"HTTP {e.code} {desc or ''} — {COOKIE_HINT}".strip(), code)
    return XueqiuError(f"雪球拒绝请求（{code}）：{desc}", code)


def _get(path: str, **params) -> dict:
    global _OPENER, _VISITOR
    if _OPENER is None:
        _OPENER = _build_opener()
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Referer": HOME + "/",
            "Accept": "application/json, text/plain, */*",
        },
    )
    last = None
    for attempt in range(3):
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
            if "aliyun_waf" in body[:800]:
                raise XueqiuError(f"命中阿里云 WAF 验证页：{path}（该路径不能走主站）")
            data = json.loads(body)
            if isinstance(data, dict) and data.get("success") is False:
                raise XueqiuError(f"雪球返回错误 {data.get('code')}：{data.get('message')}")
            return data
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                err = _http_error(e)
                if err.code in _EXPIRED_TOKEN_CODES and not _VISITOR:
                    _OPENER, _VISITOR = _build_opener(visitor=True), True
                    # 旧请求对象上已挂着过期 Cookie，CookieProcessor 不会覆盖，要重建。
                    req = urllib.request.Request(url, headers=dict(req.headers))
                    continue
                raise err from None
            last = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise XueqiuError(f"{path} 连续三次失败：{type(last).__name__} {last}")


# --------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------- #

def strip_html(text: str | None) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def ts_to_str(ms) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d %H:%M")


def normalize_symbol(raw: str) -> str:
    """600519→SH600519、000858→SZ000858；港股 5 位与美股代码原样返回。"""
    s = raw.strip().upper()
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", s):
        return s
    if re.fullmatch(r"\d{6}", s):
        if s[0] in "69":
            return "SH" + s
        if s[0] in "03":
            return "SZ" + s
        if s[0] in "48":
            return "BJ" + s
    return s


def parse_status_id(raw: str) -> str:
    """接受帖子 URL 或纯 id。"""
    s = raw.strip()
    if s.isdigit():
        return s
    m = re.search(r"xueqiu\.com/(?:\d+)/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,})/?$", s)
    if m:
        return m.group(1)
    raise XueqiuError(f"无法从 {raw!r} 解析帖子 id；给 URL 或纯数字 id")


def parse_user_id(raw: str) -> str:
    s = raw.strip()
    if s.isdigit():
        return s
    m = re.search(r"xueqiu\.com/u/(\d+)", s)
    if m:
        return m.group(1)
    m = re.search(r"xueqiu\.com/(\d+)", s)
    if m:
        return m.group(1)
    raise XueqiuError(f"无法从 {raw!r} 解析用户 id；给主页 URL 或纯数字 id")


def flatten_post(item: dict) -> dict:
    user = item.get("user") or {}
    body = item.get("text") or item.get("description") or ""
    return {
        "id": item.get("id"),
        "url": f"{HOME}{item.get('target')}" if item.get("target") else
               (f"{HOME}/{user.get('id')}/{item.get('id')}" if user.get("id") else ""),
        "created_at": ts_to_str(item.get("created_at")),
        "created_at_ms": item.get("created_at"),
        "author": user.get("screen_name", ""),
        "author_id": user.get("id"),
        "followers": user.get("followers_count"),
        "title": strip_html(item.get("title")),
        "text": strip_html(body),
        "reply_count": item.get("reply_count"),
        "like_count": item.get("like_count"),
        "retweet_count": item.get("retweet_count"),
        "view_count": item.get("view_count"),
        "truncated": bool(item.get("truncated")),
    }


def _cut(ms_cutoff, posts):
    if ms_cutoff is None:
        return posts
    return [p for p in posts if (p.get("created_at_ms") or 0) >= ms_cutoff]


def _since_ms(since: str | None):
    if not since:
        return None
    return int(datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=CST).timestamp() * 1000)


# --------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------- #

def cmd_posts(args) -> list:
    """个股讨论流，按时间倒序（--sort hot 取热门/精华）。"""
    symbol = normalize_symbol(args.symbol)
    cutoff = _since_ms(args.since)
    out: list = []
    for page in range(1, args.pages + 1):
        data = _get(
            "/query/v1/symbol/search/status.json",
            count=args.count, comment=0, symbol=symbol, hl=0,
            source="user", sort="alpha" if args.sort == "hot" else "time",
            page=page, type=11, q="",
        )
        items = data.get("list") or []
        if not items:
            break
        out.extend(flatten_post(i) for i in items)
        if cutoff is not None and (items[-1].get("created_at") or 0) < cutoff:
            break
        if page < args.pages:
            time.sleep(0.4)
    return _cut(cutoff, out)


def cmd_post(args) -> dict:
    """单帖全文。列表接口的正文会被截断，落到具体一帖必须走这里。"""
    data = _get("/statuses/show.json", id=parse_status_id(args.target))
    post = flatten_post(data)
    post["source_app"] = data.get("source")
    return post


def cmd_comments(args) -> list:
    sid = parse_status_id(args.target)
    out: list = []
    for page in range(1, args.pages + 1):
        data = _get(
            "/statuses/comments.json", id=sid, count=args.count, page=page,
            reply="true", asc="false", type="status", split="true",
        )
        items = data.get("comments") or []
        if not items:
            break
        for c in items:
            user = c.get("user") or {}
            out.append({
                "id": c.get("id"),
                "created_at": ts_to_str(c.get("created_at")),
                "author": user.get("screen_name", ""),
                "text": strip_html(c.get("text")),
                "like_count": c.get("like_count"),
            })
        if page >= (data.get("maxPage") or 1):
            break
        if page < args.pages:
            time.sleep(0.4)
    return out


def cmd_search(args) -> list:
    """全站关键词搜帖。sortId=2 按时间，sortId=1 按相关度。"""
    cutoff = _since_ms(args.since)
    out: list = []
    for page in range(1, args.pages + 1):
        data = _get(
            "/query/v1/search/status.json",
            sortId=1 if args.sort == "hot" else 2,
            q=args.query, count=args.count, page=page,
        )
        items = data.get("list") or []
        if not items:
            break
        out.extend(flatten_post(i) for i in items)
        if page < args.pages:
            time.sleep(0.4)
    return _cut(cutoff, out)


def cmd_user(args) -> list:
    uid = parse_user_id(args.target)
    cutoff = _since_ms(args.since)
    out: list = []
    for page in range(1, args.pages + 1):
        data = _get("/v4/statuses/user_timeline.json", user_id=uid, page=page)
        items = data.get("statuses") or []
        if not items:
            break
        out.extend(flatten_post(i) for i in items)
        if page >= (data.get("maxPage") or 1):
            break
        if page < args.pages:
            time.sleep(0.4)
    # 用户时间线第一条可能是置顶旧帖，统一按时间倒序，--since 才好用
    out.sort(key=lambda p: p.get("created_at_ms") or 0, reverse=True)
    return _cut(cutoff, out)


def cmd_self_test(args) -> dict:
    checks: list = []

    def run(name, fn):
        try:
            n = fn()
            checks.append({"check": name, "status": "ok", "detail": n})
        except Exception as e:  # noqa: BLE001 - self-test 要报出全部失败项
            checks.append({"check": name, "status": "fail", "detail": f"{type(e).__name__}: {e}"})

    run("cookie 已配置", lambda: "xq_a_token" if "xq_a_token" in _read_cookie() else
        (_ for _ in ()).throw(XueqiuError(COOKIE_HINT)))
    run("个股讨论流 A股", lambda: f"{len(cmd_posts(argparse.Namespace(symbol='600519', pages=1, count=5, sort='time', since=None)))} 帖")
    run("个股讨论流 港股", lambda: f"{len(cmd_posts(argparse.Namespace(symbol='06181', pages=1, count=5, sort='time', since=None)))} 帖")
    run("关键词搜帖", lambda: f"{len(cmd_search(argparse.Namespace(query='老铺黄金', pages=1, count=5, sort='time', since=None)))} 帖")

    probe = None
    try:
        probe = cmd_posts(argparse.Namespace(symbol='06181', pages=1, count=20, sort='time', since=None))
        probe = max(probe, key=lambda p: p.get("reply_count") or 0)
    except Exception:
        probe = None
    if probe:
        run("单帖全文", lambda: f"id={probe['id']} 正文 {len(cmd_post(argparse.Namespace(target=str(probe['id'])))['text'])} 字")
        run("帖子评论", lambda: f"{len(cmd_comments(argparse.Namespace(target=str(probe['id']), pages=1, count=10)))} 条")
    else:
        checks.append({"check": "单帖全文/评论", "status": "skip", "detail": "没取到探针帖子"})

    run("用户时间线", lambda: f"{len(cmd_user(argparse.Namespace(target='1955602780', pages=1, since=None)))} 帖")

    failed = [c for c in checks if c["status"] == "fail"]
    return {"ok": not failed, "checks": checks}


# --------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------- #

def render(result, args) -> str:
    if args.json:
        return json.dumps(result, ensure_ascii=False, indent=2)
    if args.cmd == "self-test":
        icon = {"ok": "✅", "fail": "❌", "skip": "⏭️"}
        lines = [f"{icon[c['status']]} {c['check']}：{c['detail']}" for c in result["checks"]]
        lines.append("全部通过" if result["ok"] else "有失败项，见上")
        return "\n".join(lines)
    if args.cmd == "post":
        p = result
        head = f"[{p['created_at']}] {p['author']}（粉丝 {p.get('followers')}）"
        meta = f"赞 {p['like_count']} / 评论 {p['reply_count']} / 阅读 {p.get('view_count')}"
        return "\n".join(x for x in [head, p["title"], "", p["text"], "", meta, p["url"]] if x is not None)
    if args.cmd == "comments":
        if not result:
            return "无评论"
        return "\n".join(f"[{c['created_at']}] {c['author']}（赞 {c['like_count']}）\n  {c['text']}"
                         for c in result)
    if not result:
        return "无结果"
    lines = []
    for p in result:
        lines.append(
            f"[{p['created_at']}] {p['author']} | 赞 {p['like_count']} 评 {p['reply_count']} 阅 {p.get('view_count')}\n"
            f"  {p['text'][:200]}{'…' if len(p['text']) > 200 else ''}\n"
            f"  {p['url']}"
        )
    lines.append(f"— 共 {len(result)} 帖")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="雪球帖子读取（api.xueqiu.com 网关）")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    # --json 放子命令前后都认，省得记顺序
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True, parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))

    p = sub.add_parser("posts", help="个股讨论流")
    p.add_argument("symbol", help="600519 / SH600519 / 06181 / AAPL")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--count", type=int, default=20, help="每页条数，上限 20")
    p.add_argument("--sort", choices=["time", "hot"], default="time")
    p.add_argument("--since", help="只保留该日期（含）之后的帖，YYYY-MM-DD")

    p = sub.add_parser("post", help="单帖全文")
    p.add_argument("target", help="帖子 URL 或 id")

    p = sub.add_parser("comments", help="帖子评论")
    p.add_argument("target", help="帖子 URL 或 id")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--count", type=int, default=20)

    p = sub.add_parser("search", help="全站关键词搜帖")
    p.add_argument("query")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--sort", choices=["time", "hot"], default="time")
    p.add_argument("--since", help="YYYY-MM-DD")

    p = sub.add_parser("user", help="用户时间线")
    p.add_argument("target", help="用户主页 URL 或 id")
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--since", help="YYYY-MM-DD")

    sub.add_parser("self-test", help="逐条验证接口可用性")

    args = ap.parse_args()
    handler = {
        "posts": cmd_posts, "post": cmd_post, "comments": cmd_comments,
        "search": cmd_search, "user": cmd_user, "self-test": cmd_self_test,
    }[args.cmd]
    try:
        result = handler(args)
    except XueqiuError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    print(render(result, args))
    return 0 if (args.cmd != "self-test" or result["ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
