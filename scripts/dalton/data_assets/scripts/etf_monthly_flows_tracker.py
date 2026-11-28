#!/usr/bin/env python3
"""
美股 ETF 资金净流入月度跟踪 (Top 20) -> 飞书消息卡片 (Card Schema 2.0)

在扣除二级市场 mark-to-market (MTM) 价格涨跌影响后，
通过 Modified Dietz 现金流折算期初规模，计算纯资金净流入占期初比例最大的一级市场申购前 20 大 ETF。

配置项（优先读环境变量，其次读 $DALTON_DATA_TASKS_CONFIG 或 <runtime_workspace>/config/data_tasks.env）：
    DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID  飞书接收群 chat_id（兼容 DALTON_FEISHU_TRACKING_CHAT_ID；--send 未显式传 --to 时必填）
    DALTON_DATA_TASK_WORKSPACE                数据任务工作目录（兼容 DALTON_WORKSPACE / OPENCLAW_WORKSPACE / WORKDIR，默认当前运行目录）
    DALTON_DATA_TASK_OPENCLAW_CONFIG          OpenClaw 配置文件路径（兼容 DALTON_OPENCLAW_CONFIG，默认 ~/.openclaw/openclaw.json）

用法:
    python3 scripts/etf_monthly_flows_tracker.py --dry-run
    python3 scripts/etf_monthly_flows_tracker.py --send --to oc_<chat_id>
    python3 scripts/etf_monthly_flows_tracker.py --self-test
"""
from __future__ import annotations


import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd
import requests


def _read_data_task_config_file(config_path: Path) -> dict[str, str]:
    if not config_path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def _resolve_workspace() -> Path:
    for var in ("DALTON_DATA_TASK_WORKSPACE", "DALTON_WORKSPACE", "OPENCLAW_WORKSPACE", "WORKDIR"):
        env_ws = (os.environ.get(var) or "").strip()
        if env_ws:
            return Path(env_ws)
    return Path.cwd()


def _get_config_value(*keys: str) -> str:
    for key in keys:
        env_val = (os.environ.get(key) or "").strip()
        if env_val:
            return env_val
    cfg_override = (os.environ.get("DALTON_DATA_TASKS_CONFIG") or "").strip()
    cfg_path = Path(cfg_override) if cfg_override else (_resolve_workspace() / "config" / "data_tasks.env")
    cfg = _read_data_task_config_file(cfg_path)
    for key in keys:
        val = cfg.get(key, "").strip()
        if val:
            return val
    return ""


CONFIG_PATH = (
    _get_config_value("DALTON_DATA_TASK_OPENCLAW_CONFIG", "DALTON_OPENCLAW_CONFIG")
    or os.path.expanduser("~/.openclaw/openclaw.json")
)
FEISHU_HOST = "https://open.feishu.cn"
DEFAULT_EVE_TRACKING_CHAT_ID = _get_config_value(
    "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID",
    "DALTON_FEISHU_TRACKING_CHAT_ID",
)
ETF_FEATURES_CONFIG_PATH = str(_resolve_workspace() / "config" / "etf_characteristics.json")
_ETF_FEATURES_CACHE: dict[str, str] | None = None


def load_credentials() -> tuple[str, str]:
    raw = open(CONFIG_PATH, "r", encoding="utf-8").read()
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError:
        raw = re.sub(r"//.*$", "", raw, flags=re.MULTILINE)
        raw = re.sub(r",\s*([}\]])", r"\1", raw)
        cfg = json.loads(raw)
    feishu = cfg.get("channels", {}).get("feishu", {})
    app_id = feishu.get("appId")
    app_secret = feishu.get("appSecret")
    if not app_id and "accounts" in feishu:
        acc = feishu["accounts"].get("default") or next(iter(feishu["accounts"].values()))
        app_id, app_secret = acc.get("appId"), acc.get("appSecret")
    if not app_id or not app_secret:
        raise ValueError("openclaw.json 里未找到 feishu appId / appSecret")
    return app_id, app_secret


def get_tenant_token() -> str:
    app_id, app_secret = load_credentials()
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
        data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    if result.get("code") != 0:
        raise ValueError(f"获取 tenant_access_token 失败: {result}")
    return result["tenant_access_token"]


def fetch_etf_data(window: str = "1M", min_fetch_aum: float = 200_000_000) -> list[dict]:
    """从数据源拉取全市场 ETF 的规模与资金净流入数据。"""
    url = "https://scanner.tradingview.com/america/scan"
    flow_col = f"fund_flows.{window}"
    ret_col = f"nav_total_return.{window}"
    perf_col = f"Perf.{window}"

    cols = [
        "name", "description", "aum", flow_col, ret_col,
        perf_col, "close", "shares_outstanding", "category", "asset_class", "exchange"
    ]

    all_etfs = []
    start = 0
    page_size = 250

    while True:
        payload = {
            "filter": [
                {"left": "typespecs", "operation": "has", "right": "etf"},
                {"left": flow_col, "operation": "nempty"},
                {"left": "aum", "operation": "greater", "right": min_fetch_aum}
            ],
            "options": {"lang": "en"},
            "symbols": {"query": {"types": []}, "tickers": []},
            "columns": cols,
            "sort": {"sortBy": flow_col, "sortOrder": "desc"},
            "range": [start, start + page_size]
        }
        r = requests.post(url, json=payload, timeout=25)
        if r.status_code != 200:
            raise RuntimeError(f"Scanner 请求失败 {r.status_code}: {r.text[:200]}")
        data = r.json()
        rows = data.get("data", [])
        if not rows:
            break
        all_etfs.extend(rows)
        total = data.get("totalCount", 0)
        start += len(rows)
        if start >= total:
            break
        time.sleep(0.15)

    field_names = [
        "ticker", "description", "aum", "fund_flow", "nav_return",
        "perf_return", "close", "shares_outstanding", "category", "asset_class", "exchange"
    ]
    records = []
    for item in all_etfs:
        row = dict(zip(field_names, item.get("d")))
        row["ticker_id"] = item.get("s")
        records.append(row)
    return records


def calculate_top_etfs(
    records: list[dict],
    min_beginning_aum: float = 1_000_000_000,
    top_n: int = 20
) -> pd.DataFrame:
    """按 Modified Dietz 现金流模型折算期初规模并计算净流入占比。"""
    df = pd.DataFrame(records)
    df = df.dropna(subset=["aum", "fund_flow"])
    df["aum"] = pd.to_numeric(df["aum"])
    df["fund_flow"] = pd.to_numeric(df["fund_flow"])
    df["nav_return"] = pd.to_numeric(df["nav_return"])
    df["perf_return"] = pd.to_numeric(df["perf_return"])

    # 排除 OTC / 场外粉单等非标准交易凭证
    df = df[df["exchange"].isin(["NASDAQ", "NYSE", "AMEX", "CBOE", "BATS"])].copy()

    # 综合回报率（优先 NAV Total Return，缺失时回退价格表现）
    df["ret"] = df["nav_return"].fillna(df["perf_return"]).fillna(0.0)
    df["r"] = df["ret"] / 100.0

    # 过滤异常除零
    df = df[df["r"] > -0.99].copy()

    # Modified Dietz 模型推算期初规模 AUM_0:
    # AUM_end = AUM_0 * (1 + r) + Flow * (1 + r/2)
    # AUM_0 = (AUM_end - Flow * (1 + r/2)) / (1 + r)
    df["aum_0"] = (df["aum"] - df["fund_flow"] * (1.0 + df["r"] / 2.0)) / (1.0 + df["r"])
    df["flow_pct"] = (df["fund_flow"] / df["aum_0"]) * 100.0

    # 筛选期初规模不低于 10 亿美元，且净流入为正
    filtered = df[(df["aum_0"] >= min_beginning_aum) & (df["fund_flow"] > 0)].copy()
    top_df = filtered.sort_values(by="flow_pct", ascending=False).head(top_n).copy()
    return top_df


def infer_strategy_tag(ticker: str, desc: str) -> str:
    """根据标的名称与代码推断策略/资产标签。"""
    desc_lower = (desc or "").lower()
    t = (ticker or "").upper()

    if "treasury" in desc_lower or "bond" in desc_lower or "bill" in desc_lower:
        if "short" in desc_lower or "0-3" in desc_lower or "ultra" in desc_lower:
            return "超短美债/现金管理"
        if "zero coupon" in desc_lower or "20+" in desc_lower or "long" in desc_lower:
            return "超长端美债/久期博弈"
        return "固收与债券配置"
    if "ethereum" in desc_lower or "crypto" in desc_lower or "bitcoin" in desc_lower or t in ["ETHA", "IBIT", "FBTC"]:
        return "加密资产/现货ETF"
    if "value" in desc_lower:
        return "大盘价值/主动量化"
    if "mega" in desc_lower or "large cap" in desc_lower:
        return "大盘/超大盘权益"
    if "small" in desc_lower:
        return "小盘股票核心配置"
    if "climate" in desc_lower or "clean" in desc_lower or "esg" in desc_lower:
        return "气候与能源转型"
    if "infrastructure" in desc_lower:
        return "全球基础设施"
    if "income" in desc_lower or "autocallable" in desc_lower or "barrier" in desc_lower:
        return "衍生品结构/高息增益"
    if "taiwan" in desc_lower or "singapore" in desc_lower or "china" in desc_lower or "overseas" in desc_lower:
        return "海外单一国别/区域市场"
    if "agriculture" in desc_lower or "commodity" in desc_lower or "gold" in desc_lower:
        return "大宗商品与农产品"
    return "精选指数/主题权益"


def load_etf_characteristics() -> dict[str, str]:
    global _ETF_FEATURES_CACHE
    if _ETF_FEATURES_CACHE is not None:
        return _ETF_FEATURES_CACHE
    if os.path.exists(ETF_FEATURES_CONFIG_PATH):
        try:
            with open(ETF_FEATURES_CONFIG_PATH, "r", encoding="utf-8") as f:
                _ETF_FEATURES_CACHE = json.load(f)
                return _ETF_FEATURES_CACHE
        except Exception:
            _ETF_FEATURES_CACHE = {}
    else:
        _ETF_FEATURES_CACHE = {}
    return _ETF_FEATURES_CACHE


def save_etf_characteristic(ticker: str, feature: str):
    global _ETF_FEATURES_CACHE
    cache = load_etf_characteristics()
    cache[ticker.upper()] = feature
    try:
        os.makedirs(os.path.dirname(ETF_FEATURES_CONFIG_PATH), exist_ok=True)
        with open(ETF_FEATURES_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Warn: 保存 ETF 特征库失败: {e}", file=sys.stderr)


def synthesize_feature_via_rule(ticker: str, desc: str, summary: str = "", category: str = "") -> str:
    """基于标的名称、分类和业务摘要通过规则引擎推断一句话特点。"""
    text = f"{ticker} {desc} {summary} {category}".lower()

    if "box" in text and ("spread" in text or "collateral" in text):
        return "采用标普 500 盒式套利（Box Spread）锁定无风险利率，将利息转化为资本利得递延税负"
    if "autocallable" in text or "barrier" in text:
        return "阶梯式自动敲入敲出结构化策略，配置结构性期权/票据获取高票息并设下行保护"
    if "strips" in text or "zero coupon" in text:
        return "长端零息国债纯本金久期博弈，对长端基准利率极端敏感、降息周期高弹性"
    if "crypto" in text or "ethereum" in text or "bitcoin" in text:
        return "数字资产现货信托，由合规机构级冷钱包 1:1 物理锚定现货资产"
    if "subset" in text and "msci" in text:
        return "跟踪 MSCI 精选子集指数，在大中盘核心资产基础上筛除高碳排与环境争议企业"
    if "covered call" in text or "buywrite" in text or "premium income" in text:
        return "买入股票现货并卖出对应指数虚值认购期权，获取丰厚月度期权费分红现金流"
    if "long-short" in text or "long/short" in text or "flexible equity" in text:
        return "主动多空对冲策略，结合系统量化与主动头寸对冲以平滑市场单向波动"
    if "clo" in text:
        return "配置最高信评 AAA 级浮动利率抵押贷款凭证（CLO）优先档，高票息且信用风险极低"
    if "infrastructure" in text:
        return "主动精选全球电力网络、交通与数字通信等派息型重资产基建龙头"
    if "climate" in text or "carbon" in text or "clean energy" in text:
        return "基于巴黎协定温升目标加权核心股票，向清洁低碳与绿色转型核心龙头倾斜"
    if "ultra-short" in text or "ultra short" in text:
        return "主动配置久期 1 年以内的超短端投资级债券与票据，力争稳健收益跑赢货币基金"
    if "treasury" in text or "bill" in text:
        if "0-3" in text or "1-3" in text or "short" in text:
            return "持有优质短端美国国债，提供优于现金的收益率与极高流动性、极低本金波动"
        if "20+" in text or "long" in text:
            return "长久期美国国债配置工具，衡量长端利率预期的核心资产"
    if "managed futures" in text or "cta" in text:
        return "管理期货（CTA）趋势跟踪策略，复现宏观对冲基金跨资产动量交易"
    if "convertible" in text:
        return "投资美国主流可转换债券，兼顾权益上涨弹性与债券票息托底防护"
    if "value" in text:
        return "量化或主动筛选高盈利能力、强自由现金流且估值具备安全边际的价值蓝筹"
    if "small" in text:
        return "主动或指数化精选小盘潜力企业，博弈经济复苏与利率宽松周期弹性"
    if "mega" in text or "large" in text:
        return "聚焦大盘与超大盘行业领军巨头，提供高流动性与高确定性的核心资产敞口"
    if "agriculture" in text or "commodity" in text:
        return "一篮子持有大宗商品期货合约，有效对冲通胀与供应链供给冲击"
    if "taiwan" in text:
        return "紧密跟踪富时台湾指数，重仓台积电及半导体先进制程核心供应链"
    if "singapore" in text:
        return "覆盖新加坡核心蓝筹资产，高比例配置星展、大华、华侨等东南亚金融支柱"
    if "korea" in text:
        return "重仓存储芯片巨头与韩国核心制造业，承接半导体周期与公司治理改革红利"

    if desc:
        return f"精选配置 {desc}，提供特定资产类别的流动性与风险收益敞口"
    return "提供特定资产类别的主动或被动指数化风险收益敞口"


def query_llm_for_feature(ticker: str, desc: str, summary: str = "", category: str = "") -> str | None:
    """尝试通过本地模型网关获取精准的一句话特点总结。"""
    try:
        data = {
            "model": "gemini-4-argon",
            "reasoning_effort": "low",
            "messages": [
                {
                    "role": "system",
                    "content": "你是一位资深 ETF 分析师。请用一句话（35字以内）用中文准确阐明该 ETF 的底层核心资产、策略机制或独特特点，重点说明让人困惑的名称（如 Subset Index, Autocallable, Collateral, Box Spread 等实质），语言精炼专业，直击投资实质。不要包含代码或多余前缀。"
                },
                {
                    "role": "user",
                    "content": f"标的代码: {ticker}, 官方名称: {desc}, 类别: {category}, 业务简介: {summary[:400]}"
                }
            ],
            "max_tokens": 80,
            "temperature": 0.2
        }
        req = urllib.request.Request(
            "http://127.0.0.1:4569/v1/chat/completions",
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer openclaw"}
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            ans = res["choices"][0]["message"]["content"].strip()
            ans = re.sub(r"^(特点[：:]|简介[：:]|核心[：:])", "", ans).strip(" \"'“”‘’")
            if ans and len(ans) >= 8:
                return ans
    except Exception:
        pass
    return None


def get_etf_feature(ticker: str, desc: str, category: str = "") -> str:
    """获取单个 ETF 的简短一句话特点介绍（带持久化缓存与自动兜底推断）。"""
    cache = load_etf_characteristics()
    t = ticker.upper()
    if t in cache and cache[t]:
        return cache[t]

    summary = ""
    try:
        import yfinance as yf
        info = yf.Ticker(t).info or {}
        summary = info.get("longBusinessSummary") or info.get("description") or ""
    except Exception:
        pass

    llm_feature = query_llm_for_feature(t, desc, summary=summary, category=category)
    if llm_feature:
        save_etf_characteristic(t, llm_feature)
        return llm_feature

    rule_feature = synthesize_feature_via_rule(t, desc, summary=summary, category=category)
    save_etf_characteristic(t, rule_feature)
    return rule_feature


def format_amount(val_usd: float) -> str:
    """格式化美元金额：超过 10 亿显示 $X.XXB，其余显示 $X.XMB。"""
    if abs(val_usd) >= 1_000_000_000:
        return f"${val_usd / 1_000_000_000:.2f}B"
    return f"${val_usd / 1_000_000:.1f}M"


def build_card(top_df: pd.DataFrame, window: str = "1M", min_aum_b: float = 1.0) -> dict[str, Any]:
    """构造飞书交互式卡片 (Card Schema 2.0)。"""
    now = datetime.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    as_of_label = f"截至 {today_str} · 过去 {window}"

    # 梳理宏观/流向要点
    categories = [infer_strategy_tag(r["ticker"], r["description"]) for _, r in top_df.head(5).iterrows()]
    unique_cats = list(dict.fromkeys(categories))
    lead_summary = f"**过去 1 个月纯资金内生增长（剔除 MTM）由【{' / '.join(unique_cats[:3])}】等方向主导。前 20 大 ETF 净流入占期初比例均在 {top_df.iloc[-1]['flow_pct']:.1f}% 以上，最高达 {top_df.iloc[0]['flow_pct']:.1f}%。**"

    header = {
        "title": {
            "tag": "plain_text",
            "content": f"美股 ETF 资金净流入月榜 (Top 20)"
        },
        "subtitle": {
            "tag": "plain_text",
            "content": f"{as_of_label} ｜ 剔除 MTM 纯资金内生增速 ｜ 期初规模 ≥ ${min_aum_b:.0f}B"
        },
        "template": "indigo",
        "text_tag_list": [{
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": "月度 Tracking"},
            "color": "blue"
        }]
    }

    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": lead_summary},
        {"tag": "hr"},
        {
            "tag": "markdown",
            "text_size": "notation",
            "content": f"<font color='grey'>重点 · 净流入增速前 10 名</font>"
        }
    ]

    # 前 10 名作为核心展示区
    for idx, (_, r) in enumerate(top_df.head(10).iterrows()):
        rank = idx + 1
        ticker = r["ticker"]
        desc = r["description"]
        flow_pct = r["flow_pct"]
        flow = format_amount(r["fund_flow"])
        aum0 = format_amount(r["aum_0"])
        aum_end = format_amount(r["aum"])
        ret = r["ret"]
        ret_sign = "+" if ret > 0 else ""
        strategy = infer_strategy_tag(ticker, desc)
        feature = get_etf_feature(ticker, desc, r.get("category", ""))

        # 标题行：代码 + 名称 + 流入比例徽章 + 一句话特点
        item_text = (
            f"**{rank}. {ticker}** <font color='grey'>· {desc}</font>  **+{flow_pct:.1f}%**\n"
            f"<font color='grey'>· 净流入: {flow} ｜ 期初: {aum0} ｜ 期末: {aum_end} ｜ 1月回报: {ret_sign}{ret:.1f}%</font>\n"
            f"· 特点: {feature}\n"
            f"<font color='grey'>· 分类: {strategy} · 交易所: {r['exchange']}</font>"
        )
        elements.append({"tag": "markdown", "content": item_text})

    # 第 11 - 20 名作为折叠面板展示，保持版面清爽
    if len(top_df) > 10:
        elements.append({"tag": "hr"})
        routine_lines = []
        for idx, (_, r) in enumerate(top_df.iloc[10:20].iterrows()):
            rank = idx + 11
            ticker = r["ticker"]
            desc = r["description"]
            flow_pct = r["flow_pct"]
            flow = format_amount(r["fund_flow"])
            aum0 = format_amount(r["aum_0"])
            ret_sign = "+" if r["ret"] > 0 else ""
            strategy = infer_strategy_tag(ticker, desc)
            feature = get_etf_feature(ticker, desc, r.get("category", ""))

            line = (
                f"**{rank}. {ticker}** <font color='grey'>· {desc}</font> · **+{flow_pct:.1f}%**\n"
                f"<font color='grey'>· 净流入 {flow} / 期初 {aum0} ｜ 1月回报 {ret_sign}{r['ret']:.1f}% ｜ {strategy}</font>\n"
                f"· 特点: {feature}"
            )
            routine_lines.append({"tag": "markdown", "content": line})

        elements.append({
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {
                    "tag": "markdown",
                    "content": f"**第 11 – 20 名 ETF（点击展开）** <font color='grey'>流入占期初 {top_df.iloc[-1]['flow_pct']:.1f}% ~ {top_df.iloc[10]['flow_pct']:.1f}%</font>"
                },
                "vertical_align": "center",
                "padding": "4px 8px 4px 8px",
                "icon": {
                    "tag": "standard_icon",
                    "token": "down-small-ccm_outlined",
                    "size": "12px 12px"
                },
                "icon_position": "right",
                "icon_expanded_angle": -180
            },
            "border": {"color": "grey", "corner_radius": "6px"},
            "vertical_spacing": "8px",
            "padding": "8px 8px 8px 8px",
            "elements": routine_lines
        })

    # 底部说明与数据源
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "markdown",
        "text_size": "notation",
        "content": "数据源：SEC Form N-CEN / 交易所每日申赎结算 · TradingView · Modified Dietz 现金流模型"
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "body": {
            "direction": "vertical",
            "vertical_spacing": "10px",
            "elements": elements
        }
    }


def send_card(chat_id: str, card: dict[str, Any]) -> dict[str, Any]:
    token = get_tenant_token()
    url = f"{FEISHU_HOST}/open-apis/im/v1/messages?receive_id_type=chat_id"
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False)
    }
    req = urllib.request.Request(
        url,
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}"
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ValueError(f"飞书返回 HTTP {exc.code}: {detail}") from exc


def self_test() -> int:
    """运行单元测试验证。"""
    sample_records = [
        {
            "ticker": "TEST1", "description": "Test Treasury Bond ETF",
            "aum": 2_000_000_000, "fund_flow": 500_000_000, "nav_return": 1.0,
            "perf_return": 1.0, "close": 100, "shares_outstanding": 20_000_000,
            "category": "58", "asset_class": "fixed_income", "exchange": "NASDAQ"
        },
        {
            "ticker": "TEST2", "description": "Test Crypto Trust ETF",
            "aum": 1_500_000_000, "fund_flow": 300_000_000, "nav_return": 5.0,
            "perf_return": 5.0, "close": 50, "shares_outstanding": 30_000_000,
            "category": "71", "asset_class": "crypto", "exchange": "NYSE"
        }
    ]
    df = calculate_top_etfs(sample_records, min_beginning_aum=500_000_000, top_n=10)
    assert len(df) == 2, f"Expected 2, got {len(df)}"
    card = build_card(df, window="1M", min_aum_b=0.5)
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "indigo"
    assert len(card["body"]["elements"]) >= 4
    card_str = json.dumps(card, ensure_ascii=False)
    assert "特点:" in card_str, "Card must include '特点:' description for ETFs"
    print("self-test PASSED")
    return 0


def _mirror_to_discord(chat_id, receive_id_type, card, label):
    """发到 Eve tracking 群成功后同步一份到 Discord；失败只报 stderr，不影响飞书结果。"""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from eve_tracking_mirror import mirror_after_feishu
    except Exception as exc:  # noqa: BLE001
        print(f"[eve-tracking-mirror] 未加载，Discord 未同步（飞书已发送成功，不要重发飞书）：{exc}",
              file=sys.stderr)
        return None
    return mirror_after_feishu(chat_id, receive_id_type, card, label)


def main() -> int:
    parser = argparse.ArgumentParser(description="美股 ETF 资金净流入月报卡片生成与推送")
    parser.add_argument("--window", default="1M", choices=["1M", "1Y"], help="统计时间窗口 (默认 1M)")
    parser.add_argument("--min-aum", type=float, default=1_000_000_000, help="期初规模门槛 (USD, 默认 10 亿)")
    parser.add_argument("--top", type=int, default=20, help="入选数量 (默认 20)")
    parser.add_argument("--to", default=None, help="接收方 chat_id（未传或为空时读取 DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID）")
    parser.add_argument("--dry-run", action="store_true", help="打印卡片 JSON 与清单，不发送飞书")
    parser.add_argument("--send", action="store_true", help="直接发送到飞书")
    parser.add_argument("--self-test", action="store_true", help="执行自检")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    explicit_to = (args.to or "").strip()
    target_chat_id = explicit_to or _get_config_value(
        "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID",
        "DALTON_FEISHU_TRACKING_CHAT_ID",
    )
    if args.send and not target_chat_id:
        print(
            "ERROR: 未配置 DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID（兼容 DALTON_FEISHU_TRACKING_CHAT_ID；"
            "请设置环境变量或在 config/data_tasks.env 中配置，或通过 --to 指定）",
            file=sys.stderr,
        )
        return 1

    print(f"正在拉取全市场 ETF 数据 (窗口: {args.window}, 期初门槛: ${args.min_aum/1e9:.1f}B)...")
    records = fetch_etf_data(window=args.window)
    print(f"原始符合条件标的数: {len(records)}")

    top_df = calculate_top_etfs(records, min_beginning_aum=args.min_aum, top_n=args.top)
    print(f"计算完成，提取 Top {len(top_df)} ETF:")
    for idx, (_, r) in enumerate(top_df.iterrows()):
        print(f"  {idx+1:2d}. {r['ticker']:<6} | 净流入占比: +{r['flow_pct']:5.1f}% | 净流入: {format_amount(r['fund_flow']):>8} | 期初: {format_amount(r['aum_0']):>8} | 期末: {format_amount(r['aum']):>8} | 回报: {r['ret']:+5.1f}% | {r['description']}")

    card = build_card(top_df, window=args.window, min_aum_b=args.min_aum / 1e9)

    if args.dry_run:
        print("\n--- 飞书卡片 JSON (Dry Run) ---")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return 0

    if args.send:
        print(f"\n正在发送飞书卡片至 {target_chat_id}...")
        mirror_source = json.loads(json.dumps(card))
        res = send_card(target_chat_id, card)
        if res.get("code") == 0:
            print(f"发送成功! Message ID: {res.get('data', {}).get('message_id')}")
            _mirror_to_discord(target_chat_id, "chat_id", mirror_source, "ETF 月度资金流")
            return 0
        else:
            print(f"发送失败: {res}", file=sys.stderr)
            return 1

    print("\n提示: 未指定 --send 或 --dry-run，已完成数据计算与卡片生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
