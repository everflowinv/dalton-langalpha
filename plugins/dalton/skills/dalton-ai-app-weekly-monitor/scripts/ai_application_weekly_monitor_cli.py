#!/usr/bin/env python3
"""AI Application weekly monitor helper.

两个引擎：
  discovery  —— 本周新增的 AI 应用变化（新变现、新产品、使用趋势）
  screen     —— 在累积事实库上跑增速筛选，算 YoY / QoQ / 增速变化并分档

事实库是 JSONL，一行一条「公司 x 指标 x 期间 x 来源」，只增不改。
筛选完全由 JSONL 决定，脚本不联网取基本面，不推算未披露数字。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

VERSION = "1.6.0"
TZ = ZoneInfo("Asia/Hong_Kong")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_app_db as db  # noqa: E402
import card_io  # noqa: E402

SKILL_DIR = Path(__file__).resolve().parents[1]
UNIVERSE_PATH = SKILL_DIR / "references" / "universe.json"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FEISHU_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")


def store_path():
    """旧的平铺指标库。migrate-db 之后只读，正式读写走 state/ai_app_db/ 分片。"""
    return db.legacy_metrics_path()


def fundamentals_path():
    return db.legacy_fundamentals_path()


def _checked(path):
    return db.workspace_checked(path)


def _write_workspace_text(path, text):
    card_io.write_bytes_exclusive(_checked(path), text.encode("utf-8"))


def _take_workspace_dir(argv):
    """抽出 --workspace-dir，使它放在子命令前或后都能用。"""
    if argv is None:
        argv = sys.argv[1:]
    rest = []
    value = None
    seen = False
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--workspace-dir":
            if seen:
                raise ValueError("repeated --workspace-dir")
            if index + 1 >= len(argv) or str(argv[index + 1]).startswith("--"):
                raise ValueError("missing --workspace-dir")
            seen = True
            value = argv[index + 1]
            index += 2
            continue
        if item.startswith("--workspace-dir="):
            if seen:
                raise ValueError("repeated --workspace-dir")
            seen = True
            value = item.split("=", 1)[1]
            if not value:
                raise ValueError("missing --workspace-dir")
            index += 1
            continue
        rest.append(item)
        index += 1
    return value, rest


def _reject_symlink_parents(path):
    """工作区每一级父目录都不能是符号链接。不调用 resolve。"""
    cursor = path
    while True:
        parent = cursor.parent
        if parent == cursor:
            return
        if parent.is_symlink():
            raise ValueError("refusing symlink in workspace path")
        cursor = parent


def bind_workspace(value):
    if not value:
        raise ValueError("missing --workspace-dir")
    path = Path(value)
    if path.is_symlink():
        raise ValueError("path must stay inside the workspace")
    if not path.is_dir():
        raise ValueError("workspace not found")
    path = path.absolute()
    _reject_symlink_parents(path)
    db.set_workspace_root(path)
    return path

# ---------------------------------------------------------------- 指标口径
# 三张表的名字。周报正文、快照、导出全部引用这里，改名只改一处。
TABLE_NAMES = {
    "ai_monetization": "表一 AI Monetization · AI 分项变现",
    "ai_monetization_share": "表二 AI Monetization as % of Headline · AI 变现占比",
    "headline_monetization": "表三 Headline Monetization · 公司级整体收入增速",
    "usage": "表四 Usage · 个性化用量",
}

# 钱的口径，可以直接按增速排名次。
FINANCIAL_METRICS = {
    "ai_revenue": "AI 收入（季度或半年）",
    "ai_arr": "AI 年化经常性收入",
    "ai_bookings": "AI 新签合同额 / 订单额",
    "ai_acv": "AI 年度合同价值",
}

# 比率口径。它们是财务指标，但同比排名次不成立：占比从 2% 到 4% 会显示「同比 100%」，
# 跟一家 AI 收入真的翻倍的公司并列，读起来是同一个数，实际不是同一件事。
# 所以单独走表二，按占比的绝对水平排，不按增速排。
# 占比 20% 这件事本身就是重要信息，跟它今年涨了多少无关。
RATIO_METRICS = {
    "ai_revenue_share": "AI 收入占总收入比重（%）",
    "ai_gross_margin": "AI 业务毛利率（%）",
}

USAGE_METRICS = {
    "ai_mau": "AI 产品月活（百万）",
    "ai_dau": "AI 产品日活（百万）",
    "ai_paying_users": "AI 付费用户数（百万）",
    "ai_penetration": "付费用户中开通 AI 的比例（%）",
    "ai_attach_rate": "AI 模块在存量客户中的附加率（%）",
    "api_calls_daily": "日均 API 调用（亿次）",
    "tokens_daily": "日均 token 处理量（万亿）",
    "ai_customers": "付费 AI 企业客户数（家）",
    "ai_seats": "AI 席位数（万）",
    "ai_deals_1m": "百万美元级 AI 订单数（个）",
    # 下面四个是内嵌型公司的可跟踪 AI 量。它们不是收入，进不了主榜，
    # 但对不单列 AI 分项的公司，这是唯一能连续跟踪的 AI 读数。
    "agent_traffic_share": "非人类 / agent 流量占比（%）",
    "ai_crawler_requests": "AI 爬虫与推理请求量（亿次/日）",
    "ai_developers": "平台开发者数（万）",
    "ai_workload_share": "AI 工作负载占平台用量比重（%）",
}

ALL_METRICS = {**FINANCIAL_METRICS, **RATIO_METRICS, **USAGE_METRICS}

# 入库时仍然算财务口径（走同一套规模闸门和证据分级），只是排序时分流到表二。
DISCLOSED_FINANCIAL_METRICS = {**FINANCIAL_METRICS, **RATIO_METRICS}

# 内嵌型公司的代理量，附在通道二结果上作旁证，不参与任何增速分档。
EMBEDDED_PROXY_METRICS = ("agent_traffic_share", "ai_crawler_requests",
                          "ai_developers", "ai_workload_share")

# 规模闸门：小基数上的高增速不入主表。任一条满足即通过。
SCALE_GATES = {
    "value_usd_mn": 25.0,       # 单季 AI 收入 ≥ 2500 万美元
    "arr_usd_mn": 100.0,        # AI ARR ≥ 1 亿美元
    "revenue_share_pct": 5.0,   # AI 收入占总收入 ≥ 5%
    "ai_mau": 10.0,             # AI 月活 ≥ 1000 万
    "ai_paying_users": 0.5,     # AI 付费用户 ≥ 50 万
    "ai_customers": 300.0,      # 付费 AI 企业客户 ≥ 300 家
    "api_calls_daily": 1.0,     # 日调用 ≥ 1 亿次
}

# ARR 是年化口径，只该判 arr_usd_mn 那条 1 亿美元。补齐币种口径之后每条记录都带
# value_usd_mn，ai_arr 记录会顺手够到单季收入那条 2500 万——5000 万的 ARR 摊到单季
# 不足 1300 万，本不该进主表。所以按 metric 分流：ai_arr 不判单季那条闸门；入库没
# 单独给 arr_usd_mn 的，拿这条记录自己的美元值当 ARR 判，它本来就是 ARR。
ARR_METRICS = ("ai_arr",)

# ---------------------------------------------------------------- 币种口径
# 按规模排名之前必须先同币种。入库历史上有两种写法：一种在 add-metric 时手工给了
# --value-usd-mn，一种只留原币 value + unit（如 CNY_mn）。后者以前在榜上直接拿原币
# 数字参与排序，1127.9 CNY_mn 会排在 800 USD_mn 前面——那不是规模差别，是币种差别。
#
# 现在的规则只有一条：排序、规模闸门、榜上的「规模」列一律只认 value_usd_mn；
# 原币金额单独出一列，只用来显示和回溯，永远不参与比大小。
#
# 汇率取固定值，与全量回填口径一致（CNY 7.10），HKD 用联系汇率中枢 7.80。
# 固定汇率只为让规模可比，不还原期间汇率；要做汇率敏感度分析得回原始披露。
FX_TO_USD = {"USD": 1.0, "CNY": 7.10, "HKD": 7.80}
FX_NOTE = "固定汇率折算（CNY 7.10、HKD 7.80），只用于规模可比与闸门，不还原期间汇率"

# unit 里的币种前缀。CNY_mn / usd_mn / USD_mn_run_rate / usd_mn_ttm 都能认出来；
# mn_users、tokens_daily 这类非金额口径认不出来，也不该被折算。
_CURRENCY_UNIT = re.compile(r"^([A-Za-z]{3})_mn(?:_[A-Za-z0-9_]+)?$")

USD_BASIS = {
    "native": "原始披露即美元",
    "disclosed": "入库时已给美元折算值",
    "converted": f"本层按{FX_NOTE}",
    "unconvertible": "币种不在汇率表内，未折算；不参与美元口径排序",
    "none": "该指标不是金额口径",
}


def unit_currency(unit):
    """从 unit 读币种，认不出来返回 None。"""
    m = _CURRENCY_UNIT.match((unit or "").strip())
    return m.group(1).upper() if m else None


def to_usd_mn(value, currency):
    rate = FX_TO_USD.get(currency)
    if not isinstance(value, (int, float)) or rate is None:
        return None
    return float(value) if rate == 1.0 else round(float(value) / rate, 3)


def currency_view(rec):
    """把一条记录的金额拆成「美元可比值」和「原币值」两栏。

    入库已经给过 value_usd_mn 的照用，不覆盖；没给的按 unit 里的币种折算。
    认不出币种就留空——宁可这一行没有规模，也不拿原币数字冒充美元。
    """
    value = rec.get("value")
    currency = unit_currency(rec.get("unit"))
    usd = rec.get("value_usd_mn")
    if isinstance(usd, (int, float)):
        basis = "native" if currency == "USD" else "disclosed"
    elif currency is None:
        usd, basis = None, "none"
    elif currency == "USD":
        usd, basis = to_usd_mn(value, currency), "native"
    else:
        usd = to_usd_mn(value, currency)
        basis = "converted" if usd is not None else "unconvertible"
    return {
        "value_usd_mn": usd,
        "currency": currency,
        "currency_value_mn": value if (currency and currency != "USD") else None,
        "usd_basis": basis,
        "usd_basis_label": USD_BASIS[basis],
        "fx_rate": FX_TO_USD.get(currency) if basis == "converted" else None,
    }


# 通道二的公司级收入入库时只留了折算后的美元值，原币金额写在 note 里。
# 这几条模式按「原文明确写了什么」的顺序取，取不到就留空标 unknown，不按上市地猜币种。
_FUND_ORIG = re.compile(r"原始([A-Z]{3})\s*([\d,]+\.?\d*)\s*百万")
_FUND_RATE = re.compile(r"固定汇率\s*([\d.]+)\s*(CNY|HKD)\s*/\s*USD")
_FUND_USD = re.compile(r"以美元列报|美元列报")


def fundamental_currency_view(rec):
    """公司级收入的原币金额。exact 是 note 里写死的原币数，derived 是按 note 里
    写明的固定汇率回推的，unknown 是 note 没交代——三种要分开标，不能混着看。"""
    usd = rec.get("total_revenue_usd_mn")
    note = rec.get("note") or ""
    if rec.get("reporting_currency"):
        ccy = rec["reporting_currency"]
        orig = rec.get("total_revenue_orig_mn")
        if orig is None and ccy == "USD":
            orig = usd
        return {"reporting_currency": ccy, "total_revenue_orig_mn": orig,
                "revenue_orig_basis": "recorded"}
    m = _FUND_ORIG.search(note)
    if m:
        try:
            return {"reporting_currency": m.group(1),
                    "total_revenue_orig_mn": float(m.group(2).replace(",", "")),
                    "revenue_orig_basis": "note_exact"}
        except ValueError:
            pass
    m = _FUND_RATE.search(note)
    if m and isinstance(usd, (int, float)):
        return {"reporting_currency": m.group(2),
                "total_revenue_orig_mn": round(usd * float(m.group(1)), 3),
                "revenue_orig_basis": "note_rate_derived"}
    if _FUND_USD.search(note):
        return {"reporting_currency": "USD", "total_revenue_orig_mn": usd,
                "revenue_orig_basis": "note_exact"}
    return {"reporting_currency": None, "total_revenue_orig_mn": None,
            "revenue_orig_basis": "unknown"}

# 分档阈值。用户原始设想是 YoY>300% + QoQ>200%；QoQ 200% 年化 81 倍，
# 实际只在从 0 到 1 的第一年出现，那时必然卡在小基数闸门上。
# 所以把 QoQ 主门槛降到 30%（年化 2.9 倍），200% 保留为「垂直起飞」附加标记。
TIERS = {
    "breakout": {"emoji": "🔥", "label": "爆发档", "rule": "YoY ≥ 300% 且规模达标"},
    "high_growth": {"emoji": "🟢", "label": "高增档", "rule": "YoY ≥ 100% 或 QoQ ≥ 30%，且规模达标"},
    "pre_financial": {"emoji": "🟡", "label": "拐点观察", "rule": "财务未验证，非财务指标 YoY ≥ 200%"},
    "small_base": {"emoji": "🔬", "label": "小基数", "rule": "增速达标但规模未达闸门，不进主表"},
    "caveat": {"emoji": "⚠️", "label": "口径存疑", "rule": "口径变更 / 关联方 / 一次性项目制"},
    "decelerating": {"emoji": "🔻", "label": "降速", "rule": "QoQ 连续两季下滑"},
    "watch": {"emoji": "⚪", "label": "暂不入档", "rule": "条件不成组"},
}

# ------------------------------------------------- 通道二：内嵌型 AI 驱动
# 主榜只收「公司愿意单列 AI 分项」的公司，这是一个结构性偏差：
# Cloudflare 这类把 AI 织进主产品、靠 agent 流量推高主业用量的公司永远进不了主榜，
# 但它恰恰是 AI 应用最重要的受益方。通道二不依赖 AI 分项披露，
# 改看公司级同比增速的变化 + 管理层归因 + 可跟踪的 AI 代理量。

EMBEDDED_GATES = {
    "quarterly_revenue_usd_mn": 100.0,  # 季度总收入闸门，挡掉小票噪音
    "min_revenue_yoy_pct": 20.0,        # 总收入增速下限，低增速公司谈不上被 AI 推动
    "yoy_chg_vs_year_ago_min_pts": 3.0, # 同比增速较去年同期抬升的百分点门槛
    "yoy_chg_vs_prev_min_pts": 2.0,     # 同比增速较上期抬升的门槛（配合前瞻指标才算数）
    "yoy_chg_erosion_pts": -5.0,        # 反向：同比增速较去年同期塌陷的百分点门槛
    "standout_yoy_pct": 50.0,           # 绝对增速本身就值得看的门槛，不要求抬升
}

# 业务纯度：公司交付的核心产品有多依赖 AI，与是否披露 AI 收入无关。
# 它只调节归因举证的松紧，不替代归因，也不放松规模和增速变化闸门。
AI_PURITY_LEVELS = {
    "native": "产品即 AI 交付，没有 AI 就没有这门生意",
    "embedded": "AI 已织进主产品并影响用量或定价，主业仍有独立价值",
    "adjacent": "AI 只是众多驱动之一，公司级加速大概率与 AI 无关",
}

# 归因原话的有效期，单位是期。adjacent 只认最近两期，
# embedded 放宽到四期（这类公司不会每期都重复讲 AI），
# native 同样四期，且允许在完全没有原话时凭业务纯度进独立档。
ATTRIBUTION_WINDOW = {"native": 4, "embedded": 4, "adjacent": 2}

EMBEDDED_TIERS = {
    "accelerating": {"emoji": "⚡", "label": "加速档",
                     "rule": "总收入同比增速较去年同期抬升 + 管理层 AI 归因 + 规模达标"},
    "native_accel": {"emoji": "🧬", "label": "AI 原生加速档",
                     "rule": "业务本身即 AI 交付（ai_purity=native），增速抬升与规模达标但暂无管理层归因原话"},
    "high_absolute": {"emoji": "🚀", "label": "高增档",
                      "rule": "总收入同比 ≥ 50% + AI 归因 + 规模达标；增速没抬升，但绝对水平本身就是关注理由"},
    "high_plateau": {"emoji": "🌀", "label": "高位平台",
                     "rule": "增速高但未加速，AI 归因成立，等下一期确认"},
    "eroding": {"emoji": "🩸", "label": "AI 侵蚀",
                "rule": "增速塌陷且归因指向 AI 替代，反向信号"},
    "insufficient": {"emoji": "🕓", "label": "序列不足",
                     "rule": "可比期数不够，算不出同比增速的变化"},
    # 2026-08-31 新增：as-reported 同比跨了重大并表 / 剥离，跟上年不是同一家公司。
    # 这一档不是「没过门槛」，是「这个数不能用来判有没有提速」，必须跟 watch 分开。
    "basis_unclear": {"emoji": "🧩", "label": "口径不可比",
                      "rule": "本期或对比基期跨了重大并表/剥离，公司未给同口径增速；不判加速档，等下一期同口径确认"},
    "watch": {"emoji": "⚪", "label": "暂不入档", "rule": "条件不成组"},
}

ATTRIBUTION_TYPES = {
    "demand_driver": "AI / agent 需求推高主业用量",
    "new_monetization": "AI 原生新变现，尚未单列收入",
    "product_embed": "AI 织进主产品，不单列口径",
    "erosion": "AI 侵蚀原有收入（反向信号）",
    # 非收入归因的 AI 点评（2026-08-18 新增）：公司对 AI 有实际表态/使用，
    # 但未把增长归到 AI 上。这类记录算「AI 进展」不算「收入归因」，
    # 不参与加速档判定，只在表二/详情里展示公司口径。
    "efficiency_cost": "AI 用于提效/降本，未归因收入",
    "explicit_non_ai": "管理层明确：增长非因 AI",
    "ai_usage": "AI 已投入使用/布局，未归因收入",
}

# 口径硬标记的人话说明。榜单每条都要显示，否则读者只看见一个增速数字，
# 不知道这个「AI 收入」到底在数什么、能不能跟上一期比。
FLAG_LABELS = {
    "definition_changed": "公司改过口径，两期不可直接比",
    "related_party": "含关联方交易",
    "one_off": "含一次性项目制收入",
}

# ------------------------------------------------------- AI 归属依据
# evidence_grade 回答「这个数字有多硬」，回答不了「这笔钱凭什么算 AI 收入」。
# 两者经常反向：医渡的「大数据平台及解决方案收入」是年报分部数字，证据 A 级，
# 但公司从没说过它是 AI 收入——记成 AI 收入是本系统的判断；反过来蓝色光标的
# 「AI 驱动收入」是公司自己起的名字，边界也由公司自己划，里面有多少真由 AI 完成
# 外部无法验证。所以口径拆成两个轴：数字强度看 evidence_grade，AI 归属看这里。
# judged_by=system 的两档，读者要知道 AI 这个标签是我们贴的，不是公司贴的。
AI_BASIS = {
    "company_ai_line": {
        "label": "公司口径即 AI",
        "judged_by": "company",
        "desc": "公司在披露里把这条收入直接命名为 AI 业务或 AI 分项，边界由公司划定",
    },
    "company_ai_related": {
        "label": "公司称 AI 相关",
        "judged_by": "company",
        "desc": "公司用的是「AI 相关 / AI 驱动 / AI+X / 与他项合并」这类相关性措辞，"
                "边界由公司主观划定，分子里可能含非 AI 部分",
    },
    "analyst_ai_product": {
        "label": "具名 AI 产品线，本系统归入",
        "judged_by": "system",
        "desc": "公司披露的是一条具名产品线且该产品本身即 AI 交付，但公司未称其为 AI 收入，"
                "计入 AI 是本系统的判断",
    },
    "analyst_related": {
        "label": "本系统主观判断相关性后挂载",
        "judged_by": "system",
        "desc": "公司只披露业务分部或泛口径、或仅表示该业务与 AI 有关，"
                "相关性由本系统主观判断后挂载到 AI 收入，公司未作此归类",
    },
}

AI_BASIS_LABELS = {k: v["label"] for k, v in AI_BASIS.items()}
# 这两档的分子由本系统认定，不是公司认定。榜单和看板必须显式标出来。
SYSTEM_JUDGED_BASIS = tuple(k for k, v in AI_BASIS.items() if v["judged_by"] == "system")


def basis_note(basis):
    """给一条记录的 AI 归属依据配一句人话，数字旁边直接印。"""
    meta = AI_BASIS.get(basis or "")
    if not meta:
        return "AI 归属依据未标注"
    return meta["label"]


REQUIRED_HEADINGS = [
    "## 本周结论",
    "## 🔄 本周变化",
    "## 🔥 AI Monetization",
    "## ⚡ Headline Monetization",
    "## 📊 Usage",
    "## 💰 新变现方式",
    "## 🚀 新产品与新形态",
    "## 📈 使用与渗透趋势",
    "## 🎙️ 专家访谈与研报",
    "## ⚠️ 反向信号与证伪",
    "## 📅 下周催化剂",
    "## 📝 口径与来源",
]

VAGUE_PHRASES = ["大幅增长", "显著提升", "爆发式增长", "快速放量", "远超预期", "行业拐点"]


def emit(payload, as_json):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if payload.get("ok"):
        print(payload.get("message") or json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("ERROR: " + str(payload.get("error")), file=sys.stderr)
        print("HINT: " + str(payload.get("hint")), file=sys.stderr)


# ---------------------------------------------------------------- 期间

PERIOD_RE = re.compile(r"^(\d{4})(Q[1-4]|H[12]|M(0[1-9]|1[0-2])|FY)$")


def parse_period(p):
    m = PERIOD_RE.match(p or "")
    if not m:
        return None
    year, tag = int(m.group(1)), m.group(2)
    if tag.startswith("Q"):
        return {"kind": "Q", "year": year, "index": int(tag[1]), "per_year": 4}
    if tag.startswith("H"):
        return {"kind": "H", "year": year, "index": int(tag[1]), "per_year": 2}
    if tag.startswith("M"):
        return {"kind": "M", "year": year, "index": int(tag[1:]), "per_year": 12}
    return {"kind": "FY", "year": year, "index": 1, "per_year": 1}


def period_key(p):
    d = parse_period(p)
    return (d["year"], d["index"]) if d else (0, 0)


def prev_period(p, steps=1):
    d = parse_period(p)
    if not d or d["kind"] == "FY":
        return None
    total = d["year"] * d["per_year"] + (d["index"] - 1) - steps
    year, idx = divmod(total, d["per_year"])
    idx += 1
    if d["kind"] == "Q":
        return f"{year}Q{idx}"
    if d["kind"] == "H":
        return f"{year}H{idx}"
    return f"{year}M{idx:02d}"


def pct_change(new, old):
    if new is None or old is None:
        return None
    if old == 0:
        return None
    return round((new - old) / abs(old) * 100.0, 1)


def period_kind(p):
    d = parse_period(p)
    return d["kind"] if d else "?"


# 同一次披露在库里可能落成不同的期间标签：半年制公司的年度业绩会，
# fundamentals 记 H2，抽出来的管理层原话可能记成 FY 或 Q4；中期同理（H1 ≡ Q2）。
PERIOD_ALIAS_GROUPS = (("FY", "H2", "Q4"), ("H1", "Q2"))


def period_aliases(p):
    """归因窗口比对用的期间同义组。

    只给窗口比对用：三个标签指的是同一次披露，不认就会出现「有 A 级归因行、
    仍判 has_attribution=False」。序列键不能用这套别名——那里期间类型必须严
    格区分，否则年度会和半年混进同一条序列。
    """
    d = parse_period(p)
    if not d:
        return set()
    tag = d["kind"] if d["kind"] == "FY" else f"{d['kind']}{d['index']}"
    out = {p}
    for group in PERIOD_ALIAS_GROUPS:
        if tag in group:
            out |= {f"{d['year']}{t}" for t in group}
    return out


PERIOD_KIND_LABEL = {"Q": "单季", "H": "半年", "M": "月度", "FY": "年度", "?": "未知"}


def series_id_of(rec):
    """一条记录属于哪条序列。同一公司同一指标下的不同产品口径必须显式分开。"""
    return (rec.get("series_id") or "default").strip() or "default"


def series_key(rec):
    """序列键 = 公司 + 指标 + 期间类型 + 口径序列。

    期间类型必须进键：FY / H1 / Q1 的 period index 都是 1，混在一条序列里
    会让「最新一期」由写盘顺序决定，环比变成年度减半年。
    口径序列必须进键：同一季度同一指标下的不同产品口径（例如百度的
    AI Cloud Infra / AI Applications）是三门生意，不是重复记录。
    """
    return (rec.get("ticker"), rec.get("metric"), period_kind(rec.get("period")), series_id_of(rec))


# 两端含下限或近似值时，多小的差异算披露精度噪声。取值与环比主门槛一致：
# 低于这个幅度的「变化」分不清是真实变化还是公司换了个说法。
BOUND_NOISE_PCT = 30.0
# 下限推算的增速可以判方向，但两期增速之差必须大到超出「超过 / 约」本身的措辞误差。
# 20pp 是经验阈值：Airbnb「约三分之一 → 超 40% → 近 45%」的 -7.5pp 落在阈值内，仍不判。
BOUND_SECOND_DERIV_PCT = 15.0


def growth_between(latest, base):
    """算两期之间的增速，同时判两端是否可比。

    返回 (增速, 状态, 说明)。状态取值：
      computed        两端口径一致且是精确值
      computed_bound  两端含下限或近似值，但差异大到超出精度噪声，只能读作量级变化
      unavailable     不给数，说明写在第三项
    公司自己披露的 yoy_pct / qoq_pct 不走这里，因为那是公司按同口径重算过的。
    """
    if latest is None or base is None:
        return None, "unavailable", "缺对比期"
    for tag, rec in (("最新期", latest), ("对比期", base)):
        if rec.get("definition_changed"):
            return None, "unavailable", f"{tag}口径变更，不可比"
    g = pct_change(latest.get("value"), base.get("value"))
    if g is None:
        return None, "unavailable", "对比期无数值"
    if latest.get("value_is_bound") or base.get("value_is_bound"):
        if abs(g) < BOUND_NOISE_PCT:
            return None, "unavailable", f"两端含下限或近似值，{g}% 在披露精度噪声内"
        return g, "computed_bound", "两端含下限或近似值，只能读作量级变化"
    return g, "computed", ""


# ---------------------------------------------------------------- 事实库

def load_universe():
    data = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return data


def load_store(tickers=None):
    """AI 分项与用量指标。迁移后读分片，迁移前回落到旧的平铺文件。

    读出来就补齐币种口径：原币值留在 value/unit，美元值统一落到 value_usd_mn。
    补在这一层，是为了让筛选、闸门、排行榜、周报共用同一个折算结果，
    不会出现「榜上按美元、闸门按原币」这种一份数据两个口径的事。
    """
    rows = db.read_all("metrics", tickers=tickers)
    for r in rows:
        r.update(currency_view(r))
    return rows


def scale_check(rec):
    """规模闸门。返回 (是否通过, 说明)。"""
    reasons = []
    metric = rec.get("metric")
    for field, floor in SCALE_GATES.items():
        val = rec.get(field)
        if field == "value_usd_mn" and metric in ARR_METRICS:
            continue
        if field == "arr_usd_mn" and metric in ARR_METRICS \
                and not isinstance(val, (int, float)):
            val = rec.get("value_usd_mn")
        if field == "ai_mau" and rec.get("metric") != "ai_mau":
            continue
        if field == "ai_paying_users" and rec.get("metric") != "ai_paying_users":
            continue
        if field == "ai_customers" and rec.get("metric") != "ai_customers":
            continue
        if field == "api_calls_daily" and rec.get("metric") != "api_calls_daily":
            continue
        if field in ("ai_mau", "ai_paying_users", "ai_customers", "api_calls_daily"):
            val = rec.get("value")
        if isinstance(val, (int, float)) and val >= floor:
            reasons.append(f"{field}={val} ≥ {floor}")
    if reasons:
        return True, "; ".join(reasons)
    if rec.get("usd_basis") == "unconvertible":
        return False, (f"原币 {rec.get('unit')} 不在汇率表内，折不出美元规模，闸门不判；"
                       "补 --value-usd-mn 或在 FX_TO_USD 里加这个币种")
    if metric in ARR_METRICS:
        return False, ("ARR 低于 1 亿美元的年化闸门；ARR 不借单季收入那条 2500 万过闸")
    return False, "未提供 value_usd_mn / arr_usd_mn / revenue_share_pct，或均低于闸门"


def classify(latest, yoy, qoq, qoq_prev, scale_ok, flags):
    """分档。硬标记优先，降速其次，再看增速是否达标，最后才分爆发 / 高增 / 拐点观察。"""
    is_financial = latest.get("metric") in DISCLOSED_FINANCIAL_METRICS
    # A/B 级财务数字才能进主表；C 级估算和非财务代理指标最高只能到拐点观察。
    # proxy_only 是人工判定的第三种降级：数字本身是财务口径，但定义决定了它
    # 不能当作 AI 增量变现（例如把存量订阅按「含 AI 层级」整笔计入）。
    main_table_eligible = (is_financial and latest.get("evidence_grade") in ("A", "B")
                           and not latest.get("proxy_only"))

    breakout_ok = yoy is not None and yoy >= 300
    growth_ok = (yoy is not None and yoy >= 100) or (qoq is not None and qoq >= 30)
    proxy_ok = yoy is not None and yoy >= 200
    qualifies = growth_ok if main_table_eligible else proxy_ok

    if flags:
        tier = "caveat"
    elif qoq is not None and qoq_prev is not None and qoq < 0 and qoq_prev < 0:
        tier = "decelerating"
    elif not qualifies:
        tier = "watch"
    elif not scale_ok:
        tier = "small_base"
    elif not main_table_eligible:
        tier = "pre_financial"
    elif breakout_ok:
        tier = "breakout"
    else:
        tier = "high_growth"

    if qoq is None or qoq_prev is None:
        second = "unknown"
    elif qoq - qoq_prev >= 5:
        second = "accelerating"
    elif qoq_prev - qoq >= 5:
        second = "decelerating"
    else:
        second = "flat"
    return tier, second


def build_series(rows):
    """按 (ticker, metric, 期间类型, 口径序列) 聚合成时间序列。

    同一序列同一期间出现多条时取最高证据等级、最新记录。
    """
    grade_rank = {"A": 3, "B": 2, "C": 1, "D": 0}
    buckets = {}
    for r in rows:
        key = series_key(r)
        per = r.get("period")
        if not key[0] or not key[1] or not parse_period(per):
            continue
        cur = buckets.setdefault(key, {}).get(per)
        if cur is None:
            buckets[key][per] = r
            continue
        new_rank = (grade_rank.get(r.get("evidence_grade"), 0), r.get("recorded_at") or "")
        old_rank = (grade_rank.get(cur.get("evidence_grade"), 0), cur.get("recorded_at") or "")
        if new_rank > old_rank:
            buckets[key][per] = r
    return buckets


def mark_primary(results):
    """同一公司同一指标下有多条序列时，只让一条进主表，其余作附注。

    取披露时间最新的那条；披露日期相同再比期间、比点数。这样同一门生意不会
    因为公司换了口径或换了期间类型被算两次。
    """
    groups = {}
    for r in results:
        groups.setdefault((r["ticker"], r["metric"]), []).append(r)
    for group in groups.values():
        if len(group) == 1:
            group[0]["primary"] = True
            group[0]["sibling_series"] = 0
            continue
        ranked = sorted(
            group,
            key=lambda r: (r.get("primary_hint", False), r.get("source_date") or "",
                           period_key(r["period"]), len(r.get("history") or []), r["series_id"]),
            reverse=True,
        )
        for i, r in enumerate(ranked):
            r["primary"] = i == 0
            r["sibling_series"] = len(group) - 1
            if i:
                r["note_only_reason"] = (
                    f"同一指标下另有 {ranked[0]['series_id']} / {ranked[0]['period_kind_label']} 序列"
                    f"披露更新（{ranked[0]['period']}），本条只作附注"
                )


def screen(args):
    try:
        rows = load_store()
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": "修正 state/ai_app_metrics.jsonl 里的坏行后重跑。"}
    uni = {c["ticker"]: c for c in load_universe()["universe"]}
    buckets = build_series(rows)

    results = []
    for (ticker, metric, kind, sid), by_period in buckets.items():
        periods = sorted(by_period, key=period_key)
        latest_p = periods[-1]
        latest = by_period[latest_p]
        d = parse_period(latest_p)

        prev_p = prev_period(latest_p, 1)
        prev2_p = prev_period(latest_p, 2)
        yoy_p = prev_period(latest_p, d["per_year"]) if d and d["kind"] != "FY" else f"{d['year']-1}FY"

        val = latest.get("value")
        blocked = []
        qoq = latest.get("qoq_pct")
        qoq_src = "disclosed"
        if qoq is None:
            qoq, qoq_src, why = growth_between(latest, by_period.get(prev_p))
            if qoq_src == "unavailable" and by_period.get(prev_p) is not None:
                blocked.append(f"环比不给数：{why}")
        yoy = latest.get("yoy_pct")
        yoy_src = "disclosed"
        if yoy is None:
            yoy, yoy_src, why = growth_between(latest, by_period.get(yoy_p))
            if yoy_src == "unavailable" and by_period.get(yoy_p) is not None:
                blocked.append(f"同比不给数：{why}")

        qoq_prev = (by_period.get(prev_p) or {}).get("qoq_pct")
        qoq_prev_src = "disclosed" if qoq_prev is not None else None
        if qoq_prev is None:
            qoq_prev, qoq_prev_src, _ = growth_between(by_period.get(prev_p), by_period.get(prev2_p))

        bound_derived = "computed_bound" in (qoq_src, yoy_src, qoq_prev_src or "")
        scale_ok, scale_reason = scale_check(latest)
        # 硬标记：口径不可比或收入性质存疑。C 级估算不算硬标记，由分档函数降级处理。
        flags = [f for f in ("definition_changed", "related_party", "one_off") if latest.get(f)]
        tier, second = classify(latest, yoy, qoq, qoq_prev, scale_ok, flags)
        if bound_derived and second in ("accelerating", "decelerating"):
            # Airbnb「约三分之一 → 超 40% → 近 45%」被判减速，是把 about a third
            # 当成 33.3% 造出来的精度幻觉。下限推算的增速差只有大到超出措辞误差
            # 才判方向，且在报告里单独标「下限推算」，不和精确序列混读。
            gap = (abs(qoq - qoq_prev)
                   if qoq is not None and qoq_prev is not None else None)
            if gap is not None and gap >= BOUND_SECOND_DERIV_PCT:
                blocked.append(
                    f"增速变化由下限或近似值推算（两期增速差 {round(gap, 1)}pp），只读方向")
            else:
                second = "unknown"
                blocked.append("增速变化不判：增速由下限或近似值推算，差异在措辞误差内")

        info = uni.get(ticker, {})
        results.append({
            "company": latest.get("company") or info.get("name") or ticker,
            "ticker": ticker,
            "track": latest.get("track") or info.get("track"),
            "market": info.get("market"),
            "metric": metric,
            "metric_label": ALL_METRICS.get(metric, metric),
            "series_id": sid,
            "period_kind": kind,
            "period_kind_label": PERIOD_KIND_LABEL.get(kind, kind),
            "period": latest_p,
            "value": val,
            "unit": latest.get("unit"),
            "value_usd_mn": latest.get("value_usd_mn"),
            "arr_usd_mn": latest.get("arr_usd_mn"),
            "currency": latest.get("currency"),
            "currency_value_mn": latest.get("currency_value_mn"),
            "usd_basis": latest.get("usd_basis"),
            "usd_basis_label": latest.get("usd_basis_label"),
            "fx_rate": latest.get("fx_rate"),
            "revenue_share_pct": latest.get("revenue_share_pct"),
            "yoy_pct": yoy,
            "yoy_source": yoy_src,
            "qoq_pct": qoq,
            "qoq_source": qoq_src,
            "qoq_prev_pct": qoq_prev,
            "growth_blocked": blocked,
            "bound_derived": bound_derived,
            "value_is_bound": bool(latest.get("value_is_bound")),
            "proxy_only": bool(latest.get("proxy_only")),
            "proxy_only_reason": latest.get("proxy_only_reason") or "",
            "primary_hint": bool(latest.get("primary_series")),
            "second_derivative": second,
            "vertical_takeoff": bool(qoq is not None and qoq >= 200),
            "scale_ok": scale_ok,
            "scale_reason": scale_reason,
            "flags": flags,
            "tier": tier,
            "tier_emoji": TIERS[tier]["emoji"],
            "tier_label": TIERS[tier]["label"],
            "definition": latest.get("definition"),
            "ai_basis": latest.get("ai_basis") or "",
            "ai_basis_label": basis_note(latest.get("ai_basis")),
            "ai_basis_desc": AI_BASIS.get(latest.get("ai_basis") or "", {}).get("desc", ""),
            "ai_basis_system_judged": (latest.get("ai_basis") or "") in SYSTEM_JUDGED_BASIS,
            "evidence_grade": latest.get("evidence_grade"),
            "source_type": latest.get("source_type"),
            "source_url": latest.get("source_url"),
            "source_date": latest.get("source_date"),
            "history": [{"period": p, "value": by_period[p].get("value")} for p in periods[-6:]],
        })

    mark_primary(results)

    order = ["breakout", "high_growth", "pre_financial", "caveat", "small_base", "decelerating", "watch"]
    results.sort(key=lambda r: (order.index(r["tier"]), -(r["yoy_pct"] or -9999)))

    if args.tier:
        results = [r for r in results if r["tier"] in args.tier]
    if args.track:
        results = [r for r in results if r["track"] == args.track]
    if getattr(args, "primary_only", False):
        results = [r for r in results if r.get("primary")]

    if args.with_price:
        attach_prices(results)

    counts = {}
    for r in results:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1
    return {
        "ok": True,
        "version": VERSION,
        "store": str(db.db_root()) if db.is_migrated() else str(store_path()),
        "records": len(rows),
        "series": len(buckets),
        "counts": counts,
        "thresholds": {"tiers": TIERS, "scale_gates": SCALE_GATES},
        "results": results,
        "message": f"screened {len(results)} series from {len(rows)} records",
    }


def _no_growth_kind(ticker, period, basis):
    """「算不出增速」分两种：查过确无同口径披露（终态），还是根本没去查（活没干完）。

    判据是对比期那一格有没有 metrics 缺口记录。有 not_disclosed，说明有人翻过
    一手件、公司当期就是没这个口径；没有记录，就是这条序列还欠一次补数。
    """
    target = year_ago_period(period) if basis == "yoy" else prev_period(period, 1)
    if not target:
        return {"no_growth_kind": "unresolved", "compare_period": None}
    gap = db.read_gaps().get((ticker, target, "metrics"))
    if gap and gap.get("reason") in ("not_disclosed", "not_listed", "not_reported_yet"):
        return {"no_growth_kind": "first_disclosure",
                "compare_period": target,
                "reason": f"首次披露：{target} 无同口径披露（已核一手件）",
                "compare_checked_source": gap.get("checked_source", "")}
    return {"no_growth_kind": "unchecked", "compare_period": target}


def rank(args):
    """表一 AI Monetization。只排主序列，主榜只收 A/B 级钱的口径且规模过闸门、口径干净的。

    这张表回答一个问题：公司单列出来的 AI 生意，做到多大、长多快。

    两类东西被挡在主榜外，各有各的去处：
      · 用量口径（MAU、tokens、渗透率、席位数）走表三。tokens/日的同比和 AI MAU
        的同比之间没有可比性，强行排一个名次是在制造假信号。
      · 比率口径（AI 收入占比、AI 毛利率）走表二 AI Monetization as % of Headline。
        它们按水平排，不按增速排——占比 20% 本身就是重要信息，跟涨了多少无关。
    """
    screen_args = argparse.Namespace(tier=None, track=args.track,
                                     with_price=args.with_price, primary_only=True)
    res = screen(screen_args)
    if not res.get("ok"):
        return res
    rows = res["results"]
    basis = args.by

    def growth(r):
        return r.get("yoy_pct") if basis == "yoy" else r.get("qoq_pct")

    def gsource(r):
        return r.get("yoy_source") if basis == "yoy" else r.get("qoq_source")

    boards = {"main": [], "proxy": [], "small_base": [], "caveat": [], "no_growth": []}
    skipped_usage = 0
    ratio_rows = []
    for r in rows:
        # 比率口径分流到表二：按水平排，不按增速排。
        if r["metric"] in RATIO_METRICS:
            ratio_rows.append(r)
            continue
        # 用量口径不进这张表。它们各自的口径互不可比，排名次等于制造假信号。
        if r["metric"] not in FINANCIAL_METRICS:
            skipped_usage += 1
            continue
        g = growth(r)
        entry = {
            "company": r["company"],
            "ticker": r["ticker"],
            "track": r["track"],
            "metric": r["metric"],
            "metric_label": r["metric_label"],
            "series_id": r["series_id"],
            "period": r["period"],
            "period_kind_label": r["period_kind_label"],
            "value": r["value"],
            "unit": r["unit"],
            "growth_pct": g,
            "growth_basis": basis,
            "growth_source": gsource(r),
            "second_derivative": r["second_derivative"],
            "scale_ok": r["scale_ok"],
            "value_usd_mn": r["value_usd_mn"],
            "arr_usd_mn": r.get("arr_usd_mn"),
            "currency": r.get("currency"),
            "currency_value_mn": r.get("currency_value_mn"),
            "usd_basis": r.get("usd_basis"),
            "usd_basis_label": r.get("usd_basis_label"),
            "fx_rate": r.get("fx_rate"),
            "revenue_share_pct": r["revenue_share_pct"],
            "tier": r["tier"],
            "tier_label": r["tier_label"],
            "tier_emoji": r["tier_emoji"],
            "flags": r["flags"],
            "growth_blocked": r["growth_blocked"],
            "bound_derived": gsource(r) == "computed_bound",
            "value_is_bound": r["value_is_bound"],
            "evidence_grade": r["evidence_grade"],
            "pricing_state": r.get("pricing_state"),
            "price": r.get("price"),
            "definition": r["definition"],
            "ai_basis": r.get("ai_basis", ""),
            "ai_basis_label": r.get("ai_basis_label", ""),
            "ai_basis_desc": r.get("ai_basis_desc", ""),
            "ai_basis_system_judged": r.get("ai_basis_system_judged", False),
            "source_url": r["source_url"],
            "source_date": r["source_date"],
            "proxy_only": r.get("proxy_only", False),
            "proxy_only_reason": r.get("proxy_only_reason", ""),
        }
        if g is None:
            entry["reason"] = "；".join(r["growth_blocked"]) or f"{basis} 无可比对比期"
            if not r["growth_blocked"]:
                # 「算不出」有两种：查过、公司历史上确实没披露过同口径（首次披露），
                # 和还没去查。前者是终态，后者是活没干完，混在一起看不出还剩多少活。
                entry.update(_no_growth_kind(r["ticker"], r["period"], basis))
            boards["no_growth"].append(entry)
        elif r["flags"]:
            entry["reason"] = "口径硬标记：" + "、".join(r["flags"])
            boards["caveat"].append(entry)
        elif r.get("proxy_only"):
            entry["reason"] = r.get("proxy_only_reason") or "人工判定不计入主表"
            boards["proxy"].append(entry)
        elif r["metric"] in FINANCIAL_METRICS and r["evidence_grade"] in ("A", "B"):
            (boards["main"] if r["scale_ok"] else boards["small_base"]).append(entry)
        else:
            boards["proxy"].append(entry)

    for name in ("main", "proxy", "small_base", "caveat"):
        if args.min_growth is not None:
            boards[name] = [e for e in boards[name]
                            if e["growth_pct"] is not None and e["growth_pct"] >= args.min_growth]
        boards[name].sort(key=lambda e: -(e["growth_pct"] if e["growth_pct"] is not None else -9e9))
        if args.limit:
            boards[name] = boards[name][: args.limit]
        for i, e in enumerate(boards[name], 1):
            e["rank"] = i

    share_board = build_share_board(ratio_rows, boards)

    lines = [f"{TABLE_NAMES['ai_monetization']}（{'同比' if basis == 'yoy' else '环比'}；只排主序列；"
             f"用量口径 {skipped_usage} 条已分流到表四；"
             f"比率口径 {len(ratio_rows)} 条已分流到表二）"]
    for name, title in (("main", "主榜 · A/B 级财务指标且规模过闸门"),
                        ("proxy", "降级榜 · 财务口径但人工判定不计入主表"),
                        ("small_base", "小基数榜 · 增速有效但规模未过闸门"),
                        ("caveat", "口径存疑榜 · 增速不可直接采信")):
        lines.append("")
        lines.append(f"[{title}] {len(boards[name])} 条")
        if not boards[name]:
            lines.append("  （空）")
        for e in boards[name]:
            scale = e["value_usd_mn"] or e["arr_usd_mn"]
            if scale:
                scale_txt = f"{scale}mn USD"
                if e.get("currency_value_mn") is not None:
                    # 原币金额跟着美元值一起印。榜按美元排，但读的人要能看见原值。
                    scale_txt += f"（原币 {e['currency_value_mn']} {e.get('currency')} mn）"
            elif e["revenue_share_pct"]:
                scale_txt = f"占总收入{e['revenue_share_pct']}%"
            elif e.get("usd_basis") == "unconvertible":
                scale_txt = f"原币 {e['value']}{e['unit']}，未折算"
            else:
                scale_txt = "无规模字段"
            price = f" | {e['pricing_state']}" if e.get("pricing_state") else ""
            lines.append(
                f"  {e['rank']}. {e['company']}({e['ticker']}) {e['metric_label']} "
                f"{e['period']} {e['value']}{e['unit']} | {basis} {e['growth_pct']}% "
                f"({e['growth_source']}) | {scale_txt} | {e['tier_emoji']}{e['tier_label']}{price}"
                + ("　⚑下限推算" if e.get("bound_derived") else "")
            )
            # 口径必须跟着数字一起出现：一个「AI 收入 +200%」如果不说它在数什么，
            # 读者无法判断它是整条产品线、一个 SKU，还是一句 ARR 口径的年化。
            lines.append(f"     口径：{e['definition'] or '缺口径说明，需回原始披露补'}"
                         f"（{e['period_kind_label']}，证据 {e['evidence_grade']} 级，"
                         f"AI 归属：{e.get('ai_basis_label') or '未标注'}）")
            if e.get("ai_basis_system_judged"):
                # 分子是本系统认定的，不是公司认定的。这句必须跟着数字走，
                # 否则读者会默认公司自己披露过「AI 收入」这个口径。
                lines.append(f"     ⚠️ {e.get('ai_basis_desc') or 'AI 归属由本系统判定'}")
            src = e["source_url"] or "缺来源链接"
            lines.append(f"     来源：{src}"
                         + (f"（{e['source_date']}）" if e.get("source_date") else ""))
            caveats = [FLAG_LABELS.get(f, f) for f in e.get("flags") or []]
            if e.get("value_is_bound"):
                caveats.append("原文只给下限，真实值更高")
            if e.get("bound_derived"):
                caveats.append("增速由下限推算，实际不低于此值")
            if e.get("proxy_only"):
                caveats.append("代理指标：" + (e.get("proxy_only_reason") or "人工判定不计入主表"))
            if caveats:
                lines.append("     注意：" + "；".join(caveats))
    lines.append("")
    lines.append(f"未给增速 {len(boards['no_growth'])} 条（缺可比对比期或两端口径不可比）")

    basis_counts = {}
    system_judged = []
    for name in ("main", "proxy", "small_base", "caveat", "no_growth"):
        for e in boards[name]:
            k = e.get("ai_basis") or "unlabeled"
            basis_counts[k] = basis_counts.get(k, 0) + 1
            if e.get("ai_basis_system_judged"):
                system_judged.append(f"{e['company']}({e['ticker']})")
    lines.append("")
    lines.append("[AI 归属依据] 证据等级说数字有多硬，这一栏说这笔钱凭什么算 AI。")
    for k, meta in AI_BASIS.items():
        n = basis_counts.get(k, 0)
        if n:
            mark = "⚠️ " if meta["judged_by"] == "system" else ""
            lines.append(f"  {mark}{meta['label']}：{n} 条 —— {meta['desc']}")
    if basis_counts.get("unlabeled"):
        lines.append(f"  未标注：{basis_counts['unlabeled']} 条，需回原始披露补判")
    if system_judged:
        uniq = sorted(set(system_judged))
        lines.append(f"  由本系统判定 AI 归属的公司（{len(uniq)} 家）："
                     + "、".join(uniq))
    lines.append("")
    lines.append(share_board["message"])

    return {
        "ok": True,
        "version": VERSION,
        "table": "ai_monetization",
        "table_label": TABLE_NAMES["ai_monetization"],
        "basis": basis,
        "ai_basis_counts": basis_counts,
        "ai_basis_taxonomy": AI_BASIS,
        "records": res["records"],
        "series": res["series"],
        "usage_rows_routed_to_table_three": skipped_usage,
        "counts": {k: len(v) for k, v in boards.items()},
        "boards": boards,
        "share_board": share_board,
        "message": "\n".join(lines),
    }


# 占比分档。表二按水平排，不按增速排：一家 AI 占总收入 20% 的公司，
# 不管今年涨了多少，它的 AI 生意已经决定公司估值，这件事本身就是入表理由。
SHARE_TIERS = [
    (20.0, "🏛", "主体档", "AI 已构成收入主体的一大块，公司估值绕不开它"),
    (10.0, "🧱", "支柱档", "AI 已是可见的收入支柱"),
    (5.0, "🌱", "成型档", "AI 变现成型，尚未主导报表"),
    (0.0, "🫧", "早期档", "有披露但占比很小，先看方向不看体量"),
]


def share_tier_of(pct):
    for gate, emoji, label, rule in SHARE_TIERS:
        if pct >= gate:
            return {"gate": gate, "emoji": emoji, "label": label, "rule": rule}
    return {"gate": 0.0, "emoji": "🫧", "label": "早期档", "rule": ""}


# 同一家、同一期间，不能既有公司直接披露的 ai_revenue_share，又有从某条 AI 收入记录
# 换算出来的 ai_revenue_share_derived：两个数会互相矛盾。智谱 2025FY 就是这种情况——
# 公司层面 AI 占比是 100%，而挂在开放平台及 API 那条记录上的 26.3% 只是这条产品线
# 占总收入的比重。期间不同则两条都留：Five9 的 2025Q3 enterprise-only 口径与 2026Q2
# 全口径本来就是两个读数，靠「口径时效」一栏区分，不是重复。


def build_share_board(ratio_rows, boards):
    """表二 AI Monetization Share：AI 变现在公司报表里占多大。

    两个来源合并：公司直接披露的 ai_revenue_share / ai_gross_margin 指标，
    以及挂在 AI 收入记录上的 revenue_share_pct 字段。前者优先，同一家不重复列。
    """
    rows, seen = [], set()
    # 公司直接披露过的 (ticker, period)，用于挡掉同期换算出来的第二个占比
    disclosed_share = {(r["ticker"], r["period"]) for r in ratio_rows
                       if r["metric"] == "ai_revenue_share" and r.get("value") is not None}

    def push(ticker, company, metric, label, pct, period, definition, grade,
             source_url, source_date, origin, growth=None, unit="%", basis=""):
        if pct is None:
            return
        key = (ticker, metric)
        if key in seen:
            return
        if metric == "ai_revenue_share_derived" and (ticker, period) in disclosed_share:
            return
        seen.add(key)
        t = share_tier_of(pct)
        rows.append({
            "company": company, "ticker": ticker, "metric": metric, "metric_label": label,
            "share_pct": pct, "unit": unit, "period": period, "definition": definition,
            "evidence_grade": grade, "source_url": source_url, "source_date": source_date,
            "origin": origin, "growth_pct": growth,
            "ai_basis": basis, "ai_basis_label": basis_note(basis) if basis else "",
            "ai_basis_desc": AI_BASIS.get(basis, {}).get("desc", ""),
            "ai_basis_system_judged": basis in SYSTEM_JUDGED_BASIS,
            "tier": t["label"], "tier_emoji": t["emoji"], "tier_rule": t["rule"],
        })

    for r in ratio_rows:
        push(r["ticker"], r["company"], r["metric"], r["metric_label"], r["value"],
             r["period"], r["definition"], r["evidence_grade"], r["source_url"],
             r["source_date"], "disclosed_metric", r.get("yoy_pct"), r.get("unit") or "%",
             r.get("ai_basis", ""))
    for name in ("main", "small_base", "proxy", "caveat"):
        for e in boards[name]:
            push(e["ticker"], e["company"], "ai_revenue_share_derived",
                 "AI 收入占总收入比重", e.get("revenue_share_pct"),
                 e["period"], e["definition"], e["evidence_grade"], e["source_url"],
                 e["source_date"], "field_on_metric", None, "%", e.get("ai_basis", ""))

    rows.sort(key=lambda e: -e["share_pct"])
    for i, e in enumerate(rows, 1):
        e["rank"] = i

    lines = [f"{TABLE_NAMES['ai_monetization_share']}（按占比水平排，不按增速排；{len(rows)} 条）"]
    if not rows:
        lines.append("  （空）库里还没有 AI 占比读数：既没有公司直接披露 ai_revenue_share，"
                     "也没有哪条 AI 收入记录带 revenue_share_pct。")
    for e in rows:
        g = f"，同比 {e['growth_pct']}%" if e.get("growth_pct") is not None else ""
        lines.append(f"  {e['rank']}. {e['company']}({e['ticker']}) {e['metric_label']} "
                     f"{e['period']} {e['share_pct']}{e['unit']}{g} | "
                     f"{e['tier_emoji']}{e['tier']}")
        lines.append(f"     口径：{e['definition'] or '缺口径说明，需回原始披露补'}"
                     f"（证据 {e['evidence_grade']} 级，"
                     f"AI 归属：{e.get('ai_basis_label') or '未标注'}）")
        if e.get("ai_basis_system_judged"):
            lines.append(f"     ⚠️ {e.get('ai_basis_desc') or 'AI 归属由本系统判定'}")
    counts = {}
    for e in rows:
        counts[e["tier"]] = counts.get(e["tier"], 0) + 1
    return {"table": "ai_monetization_share", "table_label": TABLE_NAMES["ai_monetization_share"],
            "rows": rows, "counts": counts, "message": "\n".join(lines)}


def attach_prices(results):
    """给筛选结果附 3M / 12M 涨跌幅，判断市场是否已经定价。取不到就写 null，不猜。"""
    try:
        import yfinance as yf
    except ImportError:
        for r in results:
            r["price"] = None
            r["price_note"] = "yfinance 未安装，未取价"
        return
    tickers = sorted({r["ticker"] for r in results if r.get("ticker")})
    cache = {}
    for t in tickers:
        try:
            hist = yf.Ticker(t).history(period="1y", auto_adjust=True)
            if hist.empty:
                cache[t] = None
                continue
            close = hist["Close"].dropna()
            last = float(close.iloc[-1])
            r3 = pct_change(last, float(close.iloc[-63])) if len(close) > 63 else None
            r12 = pct_change(last, float(close.iloc[0])) if len(close) > 200 else None
            cache[t] = {"last": round(last, 2), "ret_3m_pct": r3, "ret_12m_pct": r12}
        except Exception as exc:  # noqa: BLE001
            cache[t] = None
            print(f"warn: price fetch failed for {t}: {type(exc).__name__}", file=sys.stderr)
    for r in results:
        p = cache.get(r["ticker"])
        r["price"] = p
        if not p or p.get("ret_3m_pct") is None:
            r["pricing_state"] = "unknown"
            continue
        ret = p["ret_3m_pct"]
        if ret < 15:
            r["pricing_state"] = "🎯 市场尚未反应"
        elif ret < 60:
            r["pricing_state"] = "🔶 部分反映"
        else:
            r["pricing_state"] = "🔴 已充分反映"


# ------------------------------------------------- 通道二：内嵌型 AI 加速

def load_fundamentals(tickers=None):
    """公司级财务与 AI 归因。迁移后读分片，迁移前回落到旧的平铺文件。

    total_revenue_usd_mn 入库时就已经是美元，这里只把原币金额还原出来单列，
    排序和闸门仍然只认美元那一列。
    """
    rows = db.read_all("fundamentals", tickers=tickers)
    for r in rows:
        if r.get("total_revenue_usd_mn") is not None:
            r.update(fundamental_currency_view(r))
    return rows


def year_ago_period(p):
    d = parse_period(p)
    if not d:
        return None
    if d["kind"] == "FY":
        return f"{d['year'] - 1}FY"
    return prev_period(p, d["per_year"])


def revenue_yoy(by_period, p):
    """总收入同比 + 这个同比的口径。公司自己披露的优先，缺失时用库里同期基数算。

    返回三元组 (同比, 数字来源, 口径)。口径三选一：
      as_reported —— 这一期没有重大并表 / 剥离，同比可以跟别的期相减
      organic     —— 这一期跨了重大并表 / 剥离，但公司给了同口径（有机）同比，用它
      unusable    —— 跨了并表又没有同口径数。as-reported 同比里含并进来的收入，
                     跟上年比的不是同一家公司，任何增速抬升都可能只是并表规模

    没有 includes_ma 标记的旧记录一律按 as_reported 处理，行为与改动前一致。
    """
    rec = by_period.get(p)
    if not rec:
        return None, None, None
    if rec.get("revenue_yoy_pct") is not None:
        val, src = float(rec["revenue_yoy_pct"]), "disclosed"
    else:
        base = by_period.get(year_ago_period(p))
        g = pct_change(rec.get("total_revenue_usd_mn"),
                       base.get("total_revenue_usd_mn")) if base else None
        if g is None:
            return None, None, None
        val, src = g, "computed"
    if not rec.get("includes_ma"):
        return val, src, "as_reported"
    if rec.get("organic_yoy_pct") is not None:
        return float(rec["organic_yoy_pct"]), "disclosed_organic", "organic"
    # 数字仍带回去给展示用，但口径判为不可比，不进任何增速判定。
    return val, src, "unusable"


def screen_embedded(args):
    """通道二：不依赖 AI 分项披露，看公司级同比增速的变化 + 管理层 AI 归因。

    主榜的入榜前提是公司愿意把 AI 拆出来单列，这会系统性漏掉两类公司：
      1. AI 织进主产品、不单列口径的（Cloudflare、Datadog 的一部分）
      2. AI 工作负载推高主业用量、AI 本身不是 SKU 的（边缘网络、数据、安全）
    这两类恰恰是 AI 应用最重要的受益方，所以必须有一条不依赖分项披露的通道。
    """
    try:
        rows = load_fundamentals()
    except ValueError as exc:
        return {"ok": False, "error": str(exc),
                "hint": "修正 state/ai_app_fundamentals.jsonl 里的坏行后重跑。"}
    if not rows:
        return {"ok": True, "version": VERSION, "results": [], "counts": {},
                "fundamentals": str(fundamentals_path()), "records": 0,
                "message": "公司级财务库为空，先用 add-fundamental / add-attribution 入库。"}

    uni = {c["ticker"]: c for c in load_universe()["universe"]}
    funds, attrs = {}, {}
    for r in rows:
        if r.get("kind") == "attribution":
            attrs.setdefault(r.get("ticker"), []).append(r)
        elif r.get("kind") == "fundamental" and parse_period(r.get("period")):
            funds.setdefault(r["ticker"], {}).setdefault(period_kind(r["period"]), {})[r["period"]] = r

    results = []
    for ticker, kinds in funds.items():
        # 同一公司同时报季度和年度时，用期数最多的那种口径，不混算。
        kind = max(kinds, key=lambda k: (len(kinds[k]), k == "Q"))
        by_period = kinds[kind]
        periods = sorted(by_period, key=period_key)
        latest_p = periods[-1]
        latest = by_period[latest_p]

        yoy, yoy_src, yoy_basis = revenue_yoy(by_period, latest_p)
        prev_p = prev_period(latest_p, 1)
        yoy_prev, _, prev_basis = revenue_yoy(by_period, prev_p) if prev_p else (None, None, None)
        ya_p = year_ago_period(latest_p)
        yoy_year_ago, _, ya_basis = revenue_yoy(by_period, ya_p) if ya_p else (None, None, None)

        # 跨并表的期不进增速相减。两期口径不同，相减得到的「抬升」是并表规模，不是提速。
        basis_ok = yoy_basis != "unusable"
        basis_gap = prev_basis == "unusable" or ya_basis == "unusable"

        # 两个指标并列，名字直接写它是什么，不再叫「二阶导」：
        #   chg_vs_year_ago —— 本期同比增速 减 去年同期的同比增速（主判据，抗季节性）
        #   chg_vs_prev     —— 本期同比增速 减 上一期的同比增速（交叉检查，看有没有刚开始回落）
        # 单位都是百分点。两个基准期的同比本身也一起输出，读的人能直接看出
        # 「+4.2% 是从 -25.2% 爬回来的」还是「从 +10% 往上走」。
        chg_vs_year_ago = round(yoy - yoy_year_ago, 1) if (
            basis_ok and ya_basis != "unusable"
            and yoy is not None and yoy_year_ago is not None) else None
        chg_vs_prev = round(yoy - yoy_prev, 1) if (
            basis_ok and prev_basis != "unusable"
            and yoy is not None and yoy_prev is not None) else None
        accel = chg_vs_year_ago if chg_vs_year_ago is not None else chg_vs_prev
        accel_basis = "vs_year_ago" if chg_vs_year_ago is not None else ("vs_prev_period" if chg_vs_prev is not None else None)
        # 两个方向不一致时点出来：四期口径在抬升，但最近一期已经掉头。
        diverged = (chg_vs_year_ago is not None and chg_vs_prev is not None
                    and chg_vs_year_ago > 0 and chg_vs_prev < 0)

        # 前瞻缺口：cRPO / RPO 增速跑在收入增速前面，说明加速还没走完。
        fwd = latest.get("crpo_yoy_pct")
        fwd_label = "cRPO"
        if fwd is None:
            fwd, fwd_label = latest.get("rpo_yoy_pct"), "RPO"
        fwd_gap = round(fwd - yoy, 1) if (fwd is not None and yoy is not None) else None

        rev = latest.get("total_revenue_usd_mn")
        scale_ok = rev is not None and rev >= EMBEDDED_GATES["quarterly_revenue_usd_mn"]

        # 归因必须是 A / B 级，有效期按业务纯度定：陈年的一句话不能一直撑着入榜资格，
        # 但 embedded / native 这类公司不会每期都重复讲 AI，窗口放宽到四期。
        info = uni.get(ticker, {})
        purity = info.get("ai_purity") or "adjacent"
        win = ATTRIBUTION_WINDOW.get(purity, 2)
        window_periods = {latest_p} | {prev_period(latest_p, k) for k in range(1, win)}
        window_periods.discard(None)
        window_periods = set().union(*(period_aliases(p) for p in window_periods)) or window_periods
        live = [a for a in attrs.get(ticker, [])
                if a.get("evidence_grade") in ("A", "B")
                and (a.get("period") in window_periods or a.get("period") is None)]
        pos = [a for a in live if a.get("attribution_type") in
               ("demand_driver", "new_monetization", "product_embed")]
        neg = [a for a in live if a.get("attribution_type") == "erosion"]
        # 非收入归因的 AI 点评（2026-08-18 新增）：公司对 AI 有实际表态/使用，
        # 但未把增长归到 AI 上。不进加速档判定，单独输出给 board 展示公司口径。
        commentary = [a for a in live if a.get("attribution_type") in
                      ("efficiency_cost", "explicit_non_ai", "ai_usage")]
        has_attr = bool(pos or neg)

        reasons = []
        ma_hint = f"（{latest.get('ma_note')}）" if latest.get("ma_note") else ""
        if not basis_ok:
            # 口径先于一切：这一期的同比跟上年不是同一家公司，判它加速还是回落都没有意义。
            tier = "basis_unclear"
            reasons.append(
                f"本期 as-reported 同比 {yoy}% 跨了重大并表/剥离{ma_hint}，公司未给同口径增速。"
                "增速抬升算的是并表规模不是提速，本期不判档；"
                "补 --organic-yoy-pct 或等上年同期基数也含该并表后再判")
        elif neg and scale_ok and accel is not None and accel <= EMBEDDED_GATES["yoy_chg_erosion_pts"]:
            tier = "eroding"
        elif not has_attr and purity != "native":
            tier, _ = "watch", reasons.append(
                f"无 A/B 级 AI 归因证据（业务纯度 {purity}，不豁免举证），公司级加速不能算 AI 信号")
        elif not scale_ok:
            tier, _ = "watch", reasons.append(
                f"季度总收入未过闸门（{rev} < {EMBEDDED_GATES['quarterly_revenue_usd_mn']} 百万美元）")
        elif accel is None:
            if yoy is not None and yoy >= EMBEDDED_GATES["standout_yoy_pct"]:
                tier = "high_absolute"
                reasons.append(f"总收入同比 {yoy}%，可比期数不足算不出增速变化，先按绝对增速入档")
            elif basis_gap:
                tier, _ = "basis_unclear", reasons.append(
                    "本期口径干净，但对比基期跨了重大并表/剥离，增速变化不可比；"
                    "等基数回到同口径，或给基期补同口径同比后再判")
            else:
                tier, _ = "insufficient", reasons.append("可比期数不足：至少两期才能算增速变化，五期才算得准")
        elif yoy is not None and yoy < EMBEDDED_GATES["min_revenue_yoy_pct"]:
            tier, _ = "watch", reasons.append(f"总收入增速 {yoy}% 低于 {EMBEDDED_GATES['min_revenue_yoy_pct']}%")
        elif accel >= EMBEDDED_GATES["yoy_chg_vs_year_ago_min_pts"] or (
                accel >= EMBEDDED_GATES["yoy_chg_vs_prev_min_pts"] and (fwd_gap or 0) > 0):
            tier = "accelerating" if has_attr else "native_accel"
            if diverged:
                reasons.append(f"注意：同比增速较去年同期 {chg_vs_year_ago:+.1f} 个百分点（抬升），"
                               f"但较上期 {chg_vs_prev:+.1f} 个百分点（已回落）")
            if not has_attr:
                reasons.append("凭业务纯度 native 入档：主业即 AI 交付，"
                               "但尚无最近四期的管理层归因原话，补到原话后升加速档")
        # 增速变化没到抬升门槛，但绝对增速本身就够高。一家总收入同比 200% 的公司，
        # 哪怕去年同期是 250%、增速变化为负，它仍然是这一期最该看的公司之一。
        # 只看增速变化会把这类公司压到「高位平台」甚至「暂不入档」里，那是漏。
        elif yoy is not None and yoy >= EMBEDDED_GATES["standout_yoy_pct"]:
            tier = "high_absolute"
            reasons.append(
                f"总收入同比 {yoy}%，绝对增速本身就是关注理由；"
                + (f"同比增速较{'去年同期' if accel_basis == 'vs_year_ago' else '上期'}"
                   f"{accel:+.1f} 个百分点，未达抬升门槛" if accel is not None else "增速变化算不出"))
        elif accel > -EMBEDDED_GATES["yoy_chg_vs_year_ago_min_pts"]:
            tier, _ = "high_plateau", reasons.append("增速高但没有抬升，等下一期确认")
        else:
            tier, _ = "watch", reasons.append(f"同比增速较对比期回落 {abs(accel)} 个百分点")

        results.append({
            "company": latest.get("company") or info.get("name") or ticker,
            "ticker": ticker,
            "track": latest.get("track") or info.get("track"),
            "market": info.get("market"),
            "ai_purity": purity,
            "ai_purity_label": AI_PURITY_LEVELS.get(purity, purity),
            "attribution_window_periods": win,
            "has_attribution": has_attr,
            "period": latest_p,
            "period_kind": kind,
            "period_kind_label": PERIOD_KIND_LABEL.get(kind, kind),
            "total_revenue_usd_mn": rev,
            "reporting_currency": latest.get("reporting_currency"),
            "total_revenue_orig_mn": latest.get("total_revenue_orig_mn"),
            "revenue_orig_basis": latest.get("revenue_orig_basis"),
            "revenue_yoy_pct": yoy,
            "revenue_yoy_source": yoy_src,
            # 口径三选一，见 revenue_yoy()。unusable 的那条 revenue_yoy_pct 只能展示，不能比。
            "revenue_yoy_basis": yoy_basis,
            "revenue_yoy_as_reported_pct": latest.get("revenue_yoy_pct"),
            "organic_yoy_pct": latest.get("organic_yoy_pct"),
            "includes_ma": bool(latest.get("includes_ma")),
            "ma_note": latest.get("ma_note") or None,
            "basis_comparable": basis_ok and not basis_gap,
            "revenue_yoy_prev_pct": yoy_prev,
            "revenue_yoy_year_ago_pct": yoy_year_ago,
            "yoy_chg_pts": accel,
            "yoy_chg_basis": accel_basis,
            "yoy_chg_vs_year_ago_pts": chg_vs_year_ago,
            "yoy_chg_vs_prev_pts": chg_vs_prev,
            "yoy_chg_diverged": diverged,
            "forward_metric": fwd_label if fwd is not None else None,
            "forward_yoy_pct": fwd,
            "forward_gap_pts": fwd_gap,
            "nrr_pct": latest.get("nrr_pct"),
            "large_customers": latest.get("large_customers"),
            "scale_ok": scale_ok,
            "tier": tier,
            "tier_emoji": EMBEDDED_TIERS[tier]["emoji"],
            "tier_label": EMBEDDED_TIERS[tier]["label"],
            "reasons": reasons,
            "attribution": [{
                "type": a.get("attribution_type"),
                "type_label": ATTRIBUTION_TYPES.get(a.get("attribution_type"), a.get("attribution_type")),
                "period": a.get("period"),
                "quote": a.get("quote"),
                "evidence_grade": a.get("evidence_grade"),
                "source_url": a.get("source_url"),
                "source_date": a.get("source_date"),
            } for a in (pos + neg)],
            "ai_commentary": [{
                "type": a.get("attribution_type"),
                "type_label": ATTRIBUTION_TYPES.get(a.get("attribution_type"), a.get("attribution_type")),
                "period": a.get("period"),
                "quote": a.get("quote"),
                "evidence_grade": a.get("evidence_grade"),
                "source_url": a.get("source_url"),
                "source_date": a.get("source_date"),
            } for a in commentary],
            "ai_proxies": ai_proxies_for(ticker),
            "evidence_grade": latest.get("evidence_grade"),
            "source_url": latest.get("source_url"),
            "source_date": latest.get("source_date"),
            "history": [{"period": p,
                         "revenue": by_period[p].get("total_revenue_usd_mn"),
                         "yoy_pct": revenue_yoy(by_period, p)[0],
                         "yoy_basis": revenue_yoy(by_period, p)[2]} for p in periods[-8:]],
        })

    order = ["accelerating", "native_accel", "high_absolute", "high_plateau",
             "eroding", "basis_unclear", "insufficient", "watch"]
    # 档内排序按各自的入档理由：加速档看同比增速较去年同期的变化，高增档看绝对增速。
    def _within(r):
        if r["tier"] == "high_absolute":
            return -(r["revenue_yoy_pct"] if r["revenue_yoy_pct"] is not None else -9e9)
        return -(r["yoy_chg_pts"] if r["yoy_chg_pts"] is not None else -9e9)
    results.sort(key=lambda r: (order.index(r["tier"]), _within(r)))

    if getattr(args, "tier", None):
        results = [r for r in results if r["tier"] in args.tier]
    if getattr(args, "track", None):
        results = [r for r in results if r["track"] == args.track]
    if getattr(args, "purity", None):
        results = [r for r in results if r["ai_purity"] in args.purity]
    if getattr(args, "with_price", False):
        attach_prices(results)

    counts = {}
    for r in results:
        counts[r["tier"]] = counts.get(r["tier"], 0) + 1

    # 同一批公司的第二个排法：直接按总收入同比的绝对水平排。
    # 增速变化回答「有没有在提速」，绝对增速回答「现在长得有多快」，两个问题都要答。
    ranked_by_growth = [r for r in results
                        if r["revenue_yoy_pct"] is not None and r["scale_ok"]
                        and r["tier"] not in ("watch", "insufficient", "basis_unclear")]
    ranked_by_growth.sort(key=lambda r: -r["revenue_yoy_pct"])
    by_growth = [{
        "rank": i, "company": r["company"], "ticker": r["ticker"],
        "period": r["period"], "total_revenue_usd_mn": r["total_revenue_usd_mn"],
        "reporting_currency": r.get("reporting_currency"),
        "total_revenue_orig_mn": r.get("total_revenue_orig_mn"),
        "revenue_orig_basis": r.get("revenue_orig_basis"),
        "revenue_yoy_pct": r["revenue_yoy_pct"], "yoy_chg_pts": r["yoy_chg_pts"],
        "yoy_chg_basis": r["yoy_chg_basis"], "tier": r["tier"],
        "tier_label": EMBEDDED_TIERS[r["tier"]]["label"],
        "ai_purity": r["ai_purity"], "has_attribution": r["has_attribution"],
    } for i, r in enumerate(ranked_by_growth, 1)]

    lines = [f"{TABLE_NAMES['headline_monetization']}"
             "（不依赖 AI 分项披露；两个排法并列：先分档看同比增速的变化，再单独排绝对增速）",
             "增速变化两个指标：较上期 = 本期同比 − 上期同比；较去年同期 = 本期同比 − 去年同期同比，单位都是个百分点"]
    for name in order:
        picked = [r for r in results if r["tier"] == name]
        lines.append("")
        lines.append(f"[{EMBEDDED_TIERS[name]['emoji']} {EMBEDDED_TIERS[name]['label']}] "
                     f"{len(picked)} 家 · {EMBEDDED_TIERS[name]['rule']}")
        if not picked:
            lines.append("  （空）")
        for i, r in enumerate(picked, 1):
            chain = f"同比 {r['revenue_yoy_pct']}%"
            if r.get("revenue_yoy_basis") == "organic":
                chain += "（公司口径同口径增速）"
            elif r.get("revenue_yoy_basis") == "unusable":
                chain += "（as-reported，含并表，不可比）"
            if r["revenue_yoy_prev_pct"] is not None:
                chain += f" ← 上期 {r['revenue_yoy_prev_pct']}%"
            if r["revenue_yoy_year_ago_pct"] is not None:
                chain += f" ← 去年同期 {r['revenue_yoy_year_ago_pct']}%"
            chg = []
            if r["yoy_chg_vs_prev_pts"] is not None:
                chg.append(f"较上期 {r['yoy_chg_vs_prev_pts']:+.1f} 个百分点")
            if r["yoy_chg_vs_year_ago_pts"] is not None:
                chg.append(f"较去年同期 {r['yoy_chg_vs_year_ago_pts']:+.1f} 个百分点")
            accel = (" | ".join(chg) if chg
                     else "增速变化 —（口径不可比）" if r["tier"] == "basis_unclear"
                     else "增速变化 —（可比期数不足）")
            if r.get("yoy_chg_diverged"):
                accel += " ⚠单期已回落"
            fwd = ""
            if r.get("forward_gap_pts") is not None:
                gap = r["forward_gap_pts"]
                rel = f"快于收入 {gap:.1f}pct" if gap > 0 else (
                    f"慢于收入 {abs(gap):.1f}pct" if gap < 0 else "与收入同速")
                fwd = f" | {r['forward_metric']} 同比 {r['forward_yoy_pct']}%（{rel}）"
            price = f" | {r['pricing_state']}" if r.get("pricing_state") else ""
            lines.append(
                f"  {i}. {r['company']}({r['ticker']}) {r['period']} 总收入 "
                f"{r['total_revenue_usd_mn']}mn USD | {chain}"
                f" | {accel} | 纯度 {r['ai_purity']}{fwd}{price}")
            for a in r["attribution"][:1]:
                lines.append(f"     归因[{a['evidence_grade']}级·{a['type_label']}]：{a['quote']}")
            for p in r["ai_proxies"][:2]:
                lines.append(f"     代理量：{p['metric_label']} {p['period']} {p['value']}{p['unit']}")
            note_label = ("口径" if r["tier"] == "basis_unclear"
                          else "未入档" if r["tier"] in ("watch", "insufficient") else "说明")
            for why in r["reasons"][:1]:
                lines.append(f"     {note_label}：{why}")

    lines.append("")
    lines.append(f"[绝对增速榜] {len(by_growth)} 家 · 按总收入同比水平直接排名，"
                 "不看有没有抬升（已过规模闸门，且非暂不入档 / 序列不足 / 口径不可比）")
    if not by_growth:
        lines.append("  （空）")
    for e in by_growth:
        accel = (f"增速较{'去年同期' if e['yoy_chg_basis'] == 'vs_year_ago' else '上期'}"
                 f" {e['yoy_chg_pts']:+.1f} 个百分点"
                 if e["yoy_chg_pts"] is not None else "增速变化不可算")
        lines.append(f"  {e['rank']}. {e['company']}({e['ticker']}) {e['period']} 总收入同比 "
                     f"{e['revenue_yoy_pct']}% | {accel} | "
                     f"{EMBEDDED_TIERS[e['tier']]['emoji']}{e['tier_label']}")

    return {
        "ok": True,
        "version": VERSION,
        "table": "headline_monetization",
        "table_label": TABLE_NAMES["headline_monetization"],
        "fundamentals": str(fundamentals_path()),
        "records": len(rows),
        "companies": len(funds),
        "counts": counts,
        "gates": EMBEDDED_GATES,
        "tiers": EMBEDDED_TIERS,
        "results": results,
        "by_growth": by_growth,
        "message": "\n".join(lines),
    }


def ai_proxies_for(ticker):
    """从事实库取该公司的 AI 代理量（agent 流量占比、开发者数这类）。

    这些数字进不了主榜——它们不是收入——但它是内嵌型公司唯一可跟踪的 AI 量。
    """
    try:
        rows = load_store()
    except ValueError:
        return []
    picked = {}
    for r in rows:
        if r.get("ticker") != ticker or r.get("metric") not in EMBEDDED_PROXY_METRICS:
            continue
        key = (r.get("metric"), series_id_of(r))
        cur = picked.get(key)
        if cur is None or period_key(r.get("period")) > period_key(cur.get("period")):
            picked[key] = r
    return [{
        "metric": r.get("metric"),
        "metric_label": ALL_METRICS.get(r.get("metric"), r.get("metric")),
        "period": r.get("period"),
        "value": r.get("value"),
        "unit": r.get("unit") or "",
        "source_url": r.get("source_url"),
    } for r in sorted(picked.values(), key=lambda x: period_key(x.get("period")), reverse=True)]


def add_fundamental(args):
    """入库公司级季度财务。只抄公司披露的数字，不做期间拆分推算。

    总收入同比优先直接抄业绩新闻稿里的「up X% year-over-year」，
    因为 XBRL 里 Q2 / Q3 常常是累计口径，脚本自己相减很容易算错。

    唯一例外是重大并表 / 剥离：公司抄给你的是 as-reported 同比，跟上年比的不是同一家
    公司。这种期必须打 --includes-ma，并尽量补 --organic-yoy-pct；否则通道二会把并表
    规模读成提速。判据是「本期收入里含上年同期没有的实体，且影响大到能改变分档」。
    """
    if not parse_period(args.period):
        return {"ok": False, "error": f"期间格式非法: {args.period}",
                "hint": "格式为 2026Q2 / 2026H1 / 2026FY。"}
    if args.evidence_grade not in ("A", "B"):
        return {"ok": False, "error": "公司级财务只收 A / B 级",
                "hint": "A=业绩新闻稿或财报原文；B=管理层业绩会口头给出。第三方估算不入这个库。"}
    if args.currency and args.currency.upper() not in FX_TO_USD:
        return {"ok": False, "error": f"未知列报币种: {args.currency}",
                "hint": "汇率表现有 " + " / ".join(FX_TO_USD) +
                        "。要收新币种，先在 CLI 的 FX_TO_USD 里补一条固定汇率再入库。"}
    if (args.total_revenue_usd_mn is None and args.total_revenue_orig_mn is not None
            and args.currency):
        # 只给了原币值就当场折算，别让一条没有美元口径的记录进库——那正是榜上
        # 拿原币数字跟美元数字比大小的来源。
        args.total_revenue_usd_mn = to_usd_mn(args.total_revenue_orig_mn,
                                              args.currency.upper())
    if args.total_revenue_usd_mn is None and args.revenue_yoy_pct is None:
        return {"ok": False, "error": "至少要有总收入或总收入同比",
                "hint": "--total-revenue-usd-mn 与 --revenue-yoy-pct 至少给一个，否则算不出增速变化。"}
    if not args.source_url:
        return {"ok": False, "error": "缺少来源", "hint": "--source-url 填一手来源链接。"}

    # 给了同口径增速或并购说明，就是承认这一期跨了并表，标记自动置上，不让两者脱节。
    includes_ma = bool(args.includes_ma or args.ma_note or args.organic_yoy_pct is not None)
    if includes_ma and not args.ma_note:
        return {"ok": False, "error": "标了并表口径却没说是什么事",
                "hint": "--ma-note 写清并购/剥离对象与完成日期，例如 "
                        "\"2026-05-11 完成 Udemy 全股票合并，本期并表约七周\"。"
                        "下一期判档的人要靠这句话决定基数什么时候回到同口径。"}

    for old in load_fundamentals():
        if (old.get("kind") == "fundamental" and old.get("ticker") == args.ticker
                and old.get("period") == args.period and not args.allow_duplicate):
            return {"ok": False,
                    "error": f"同键记录已存在: {args.ticker} {args.period}",
                    "hint": "确属更正才用 --allow-duplicate。",
                    "existing": {"total_revenue_usd_mn": old.get("total_revenue_usd_mn"),
                                 "source_url": old.get("source_url")}}

    rec = {
        "kind": "fundamental",
        "company": args.company,
        "ticker": args.ticker,
        "track": args.track,
        "period": args.period,
        "total_revenue_usd_mn": args.total_revenue_usd_mn,
        # 原币金额和币种单独存字段，别再只写进 note。榜按美元排，回溯要看原币。
        "reporting_currency": (args.currency or "").upper() or None,
        "total_revenue_orig_mn": args.total_revenue_orig_mn,
        "revenue_yoy_pct": args.revenue_yoy_pct,
        # 并表口径三件套：标记 + 同口径同比 + 事由。缺标记时全库按老口径（as_reported）走。
        "includes_ma": includes_ma,
        "organic_yoy_pct": args.organic_yoy_pct,
        "ma_note": args.ma_note or "",
        "crpo_yoy_pct": args.crpo_yoy_pct,
        "rpo_yoy_pct": args.rpo_yoy_pct,
        "nrr_pct": args.nrr_pct,
        "large_customers": args.large_customers,
        "evidence_grade": args.evidence_grade,
        "source_type": args.source_type,
        "source_url": args.source_url,
        "source_date": args.source_date,
        "note": args.note or "",
        "quote": args.quote or "",
        "recorded_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = db.append_jsonl(db.shard_path(args.ticker, "fundamentals"), rec)
    return {"ok": True, "message": f"recorded fundamental {args.company} {args.period}",
            "record": rec, "store": str(path)}


def add_attribution(args):
    """入库 AI 归因证据：管理层把增长（或下滑）明确归到 AI / agent 上的原话。

    没有这条，公司级加速只是加速，不能算 AI 信号——这是通道二不退化成
    「谁增速快谁上榜」的唯一约束，所以 quote 必填。
    """
    if args.attribution_type not in ATTRIBUTION_TYPES:
        return {"ok": False, "error": f"未知归因类型: {args.attribution_type}",
                "hint": "可用类型: " + ", ".join(f"{k}（{v}）" for k, v in ATTRIBUTION_TYPES.items())}
    if args.period and not parse_period(args.period):
        return {"ok": False, "error": f"期间格式非法: {args.period}",
                "hint": "格式为 2026Q2 / 2026FY，或留空表示不绑定期间。"}
    if args.evidence_grade not in ("A", "B"):
        return {"ok": False, "error": "归因证据只收 A / B 级",
                "hint": "A=公司书面材料原文；B=管理层业绩会口头。媒体转述不算归因证据。"}
    if not args.quote:
        return {"ok": False, "error": "缺少原话",
                "hint": "--quote 填管理层把增长归到 AI 上的那句原话，逐字。没有原话就不是归因证据。"}
    if not args.source_url:
        return {"ok": False, "error": "缺少来源", "hint": "--source-url 填一手来源链接。"}

    rec = {
        "kind": "attribution",
        "company": args.company,
        "ticker": args.ticker,
        "track": args.track,
        "period": args.period or None,
        "attribution_type": args.attribution_type,
        "quote": args.quote,
        "evidence_grade": args.evidence_grade,
        "source_type": args.source_type,
        "source_url": args.source_url,
        "source_date": args.source_date,
        "note": args.note or "",
        "recorded_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = db.append_jsonl(db.shard_path(args.ticker, "fundamentals"), rec)

    # 归因本身就是一条管理层原话，同步进语料库，否则深度报告读 quotes.jsonl 会漏掉它。
    venue = getattr(args, "venue", "") or args.source_type or "other"
    if venue not in db.QUOTE_VENUES:
        venue = "other"
    fp = db.quote_fingerprint(args.ticker, args.period, args.quote)
    mirrored = False
    if fp not in {q.get("fingerprint") for q in db.read_company(args.ticker, "quotes")}:
        db.append_jsonl(db.shard_path(args.ticker, "quotes"), {
            "kind": "quote", "company": args.company, "ticker": args.ticker,
            "track": args.track, "period": args.period or None,
            "venue": venue, "venue_label": db.QUOTE_VENUES[venue],
            "speaker": getattr(args, "speaker", "") or "",
            "speaker_role": getattr(args, "speaker_role", "") or "",
            "analyst_question": "", "quote": args.quote,
            "anchors": ["result"], "attribution_type": args.attribution_type,
            "specificity": getattr(args, "specificity", "") or "directional",
            "polarity": "negative" if args.attribution_type == "erosion" else "positive",
            "evidence_grade": args.evidence_grade, "source_type": args.source_type,
            "source_url": args.source_url, "source_date": args.source_date,
            "transcript_file": "", "paragraph_index": None,
            "note": "与 add-attribution 同步写入", "origin": "attribution",
            "fingerprint": fp, "recorded_at": rec["recorded_at"],
        })
        mirrored = True
    return {"ok": True, "message": f"recorded attribution {args.company} {args.attribution_type}",
            "record": rec, "store": str(path), "quote_mirrored": mirrored}


def coverage(args):
    """核心池逐家的通道归属。一家公司没进主榜，必须能说清是哪一类没进。

    回填时判「未披露 AI 分项」的公司不是查完就丢，它们正是通道二的目标人群。
    """
    uni = load_universe()["universe"]
    rows = [c for c in uni if c.get("scan_tier") == 1]
    if getattr(args, "track", None):
        rows = [c for c in rows if c.get("track") == args.track]
    try:
        metrics = load_store()
        funds = load_fundamentals()
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": "先修好事实库里的坏行。"}

    has_metric = {r.get("ticker") for r in metrics}
    has_fund = {r.get("ticker") for r in funds if r.get("kind") == "fundamental"}
    has_attr = {r.get("ticker") for r in funds if r.get("kind") == "attribution"}

    out = {"disclosed": [], "embedded_ready": [], "embedded_partial": [], "uncovered": []}
    for c in rows:
        tk = c["ticker"]
        entry = {"ticker": tk, "company": c.get("name"), "track": c.get("track")}
        if tk in has_metric:
            out["disclosed"].append(entry)
        elif tk in has_fund and tk in has_attr:
            out["embedded_ready"].append(entry)
        elif tk in has_fund or tk in has_attr:
            entry["missing"] = "缺归因原话" if tk in has_fund else "缺公司级财务序列"
            out["embedded_partial"].append(entry)
        else:
            out["uncovered"].append(entry)

    lines = [f"核心池通道覆盖（共 {len(rows)} 家）",
             f"  通道一 已有 AI 分项数字：{len(out['disclosed'])} 家",
             f"  通道二 可判增速变化：{len(out['embedded_ready'])} 家",
             f"  通道二 材料不全：{len(out['embedded_partial'])} 家",
             f"  两条通道都没数据：{len(out['uncovered'])} 家"]
    if out["uncovered"]:
        lines.append("  待补（前 20 家）：" + "、".join(
            f"{e['company']}({e['ticker']})" for e in out["uncovered"][:20]))
    return {"ok": True, "version": VERSION, "core_pool": len(rows),
            "counts": {k: len(v) for k, v in out.items()},
            "groups": out, "message": "\n".join(lines)}


def add_metric(args):
    if args.metric not in ALL_METRICS:
        return {"ok": False,
                "error": f"未知指标: {args.metric}",
                "hint": "可用指标: " + ", ".join(sorted(ALL_METRICS))}
    if not parse_period(args.period):
        return {"ok": False,
                "error": f"期间格式非法: {args.period}",
                "hint": "格式为 2026Q2 / 2026H1 / 2026M07 / 2026FY。"}
    if args.evidence_grade not in ("A", "B", "C", "D"):
        return {"ok": False, "error": "证据等级必须是 A/B/C/D",
                "hint": "A=公司披露分项数字；B=管理层口头且口径清楚；C=第三方或专家估算；D=无口径转述（应丢弃）。"}
    # 原币金额能认出币种的，当场折成美元补上 value_usd_mn。不补的话这条记录
    # 上榜时就只有原币数字，规模列会拿它跟美元数字比大小。
    if args.value_usd_mn is None and args.metric in DISCLOSED_FINANCIAL_METRICS:
        auto = to_usd_mn(args.value, unit_currency(args.unit))
        if auto is not None:
            args.value_usd_mn = auto
    if args.metric in DISCLOSED_FINANCIAL_METRICS and args.value_usd_mn is None \
            and args.revenue_share_pct is None and args.arr_usd_mn is None:
        return {"ok": False,
                "error": "财务指标缺少规模字段",
                "hint": "至少给 --value-usd-mn、--arr-usd-mn 或 --revenue-share-pct 之一，否则无法判断是不是小基数。"
                        "　--unit 用 CNY_mn / HKD_mn / USD_mn 这种写法时会自动折算美元，"
                        "其他币种要么手工给 --value-usd-mn，要么先在 FX_TO_USD 里补汇率。"}
    if not args.definition:
        return {"ok": False, "error": "缺少口径", "hint": "--definition 填公司对该指标的原文定义，口径不明的数字不入库。"}
    if not args.source_url:
        return {"ok": False, "error": "缺少来源", "hint": "--source-url 填一手来源链接。"}
    basis = getattr(args, "ai_basis", None)
    if args.metric in DISCLOSED_FINANCIAL_METRICS and basis not in AI_BASIS:
        return {
            "ok": False,
            "error": "缺少 AI 归属依据 --ai-basis",
            "hint": "证据等级只说数字有多硬，说不了这笔钱凭什么算 AI。四选一："
                    + "；".join(f"{k}={v['desc']}" for k, v in AI_BASIS.items()),
        }
    if basis is not None and basis not in AI_BASIS:
        return {"ok": False, "error": f"未知 AI 归属依据: {basis}",
                "hint": "可用值: " + ", ".join(AI_BASIS)}

    sid = (args.series_id or "default").strip() or "default"
    if not args.allow_duplicate:
        probe = {"ticker": args.ticker, "metric": args.metric,
                 "period": args.period, "series_id": sid}
        for old in load_store():
            if series_key(old) == series_key(probe) and old.get("period") == args.period:
                return {
                    "ok": False,
                    "error": f"同键记录已存在: {args.ticker} {args.metric} {args.period} series_id={sid}",
                    "hint": "不同产品口径请显式传 --series-id 区分（例如 --series-id ai_applications）；"
                            "确属同一序列的更正才用 --allow-duplicate。",
                    "existing": {"value": old.get("value"), "definition": old.get("definition"),
                                 "source_url": old.get("source_url")},
                }

    rec = {
        "company": args.company,
        "ticker": args.ticker,
        "track": args.track,
        "metric": args.metric,
        "series_id": (args.series_id or "default").strip() or "default",
        "period": args.period,
        "value": args.value,
        "unit": args.unit,
        "value_usd_mn": args.value_usd_mn,
        "arr_usd_mn": args.arr_usd_mn,
        "revenue_share_pct": args.revenue_share_pct,
        "yoy_pct": args.yoy_pct,
        "qoq_pct": args.qoq_pct,
        "definition": args.definition,
        "definition_changed": args.definition_changed,
        "ai_basis": basis or "",
        "ai_basis_label": AI_BASIS[basis]["label"] if basis in AI_BASIS else "",
        "value_is_bound": args.value_is_bound,
        "primary_series": args.primary_series,
        "proxy_only": args.proxy_only,
        "proxy_only_reason": args.proxy_only_reason or "",
        "related_party": args.related_party,
        "one_off": args.one_off,
        "unaudited_estimate": args.evidence_grade == "C",
        "evidence_grade": args.evidence_grade,
        "source_type": args.source_type,
        "source_url": args.source_url,
        "source_date": args.source_date,
        "note": args.note or "",
        "quote": args.quote or "",
        "recorded_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = db.append_jsonl(db.shard_path(args.ticker, "metrics"), rec)
    return {"ok": True, "message": f"recorded {args.company} {args.metric} {args.period}",
            "record": rec, "store": str(path)}


# ---------------------------------------------------------------- plan

def window(as_of=None, days=7):
    end = datetime.fromisoformat(as_of).astimezone(TZ) if as_of else datetime.now(TZ)
    start = end - timedelta(days=days)
    return {
        "timezone": "Asia/Hong_Kong",
        "start_date": start.date().isoformat(),
        "end_date": end.date().isoformat(),
        "days": days,
    }


def build_plan(args):
    uni = load_universe()
    by_track = {}
    by_scan_tier = {}
    for c in uni["universe"]:
        by_track.setdefault(c["track"], []).append(c["ticker"])
        by_scan_tier.setdefault(str(c.get("scan_tier", 1)), []).append(c["ticker"])
    return {
        "ok": True,
        "version": VERSION,
        "window": window(args.as_of, args.days),
        "universe_size": len(uni["universe"]),
        "universe_by_track": {k: len(v) for k, v in by_track.items()},
        "universe_by_scan_tier": {k: len(v) for k, v in sorted(by_scan_tier.items())},
        "core_pool": sorted(by_scan_tier.get("1", [])),
        "thresholds": {"tiers": TIERS, "scale_gates": SCALE_GATES},
        "metrics": ALL_METRICS,
        "web_queries": {
            "monetization_en": [
                "AI revenue disclosed quarterly earnings call percent of total revenue",
                "usage-based pricing AI agent per resolution outcome pricing launched",
                "AI subscription price increase attach rate paid seats disclosed",
            ],
            "monetization_zh": [
                "AI 收入 占比 同比 增长 财报 业绩会 披露",
                "AI 订阅 付费用户 渗透率 涨价 按用量计费",
                "大模型 商业化 中标 合同额 落地 客户",
            ],
            "product_en": [
                "launched AI agent product general availability enterprise customers",
                "AI feature adoption metrics daily active users tokens processed disclosed",
            ],
            "product_zh": [
                "发布 AI Agent 产品 正式商用 客户 数量",
                "AI 应用 日活 调用量 使用量 披露",
            ],
            "science_en": [
                "AI drug discovery partnership milestone payment disclosed upfront",
                "AI for science platform revenue growth customers computational chemistry",
            ],
            "science_zh": [
                "AI 制药 合作 里程碑 首付款 订单 披露",
                "AI 材料 科研 平台 收入 增长 客户",
            ],
            "negative_en": [
                "AI product discontinued churn seat reduction cannibalization guidance cut",
            ],
        },
        "guidepoint_queries": [
            "Enterprise adoption and budget for AI agents: seat expansion, usage-based pricing and renewal behaviour",
            "Consumer AI subscription conversion, retention and willingness to pay across major internet platforms",
            "AI for science adoption: computational chemistry, drug discovery platforms, customer spend and validation rates",
        ],
        "alphaengine_queries": [
            "AI 应用 收入 增速 拆分",
            "AI Agent 商业化 定价",
            "AI for Science 制药 材料 平台",
        ],
        "third_party_data": [
            "Sensor Tower / 七麦：AI app 下载与内购榜",
            "QuestMobile：中国 AI 应用 MAU 月报",
            "Similarweb：AI 网站访问量",
            "App Store / Google Play 畅销榜排名变化",
            "OpenRouter / Poe 模型调用份额",
        ],
        "x_seed_handles": [
            "sama", "OpenAIDevs", "AnthropicAI", "AnthropicDev", "GoogleDeepMind",
            "swyx", "simonw", "arankomatsuzaki", "emollick", "jasonlk", "clarkeanalytics",
        ],
        "subreddits": ["LocalLLaMA", "OpenAI", "ChatGPTPro", "SaaS", "ArtificialInteligence", "singularity"],
        "message": "weekly plan ready",
    }


# ---------------------------------------------------------------- self-test / validate

def self_test():
    checks = {
        "python_3_12_plus": sys.version_info >= (3, 12),
        "universe_file": UNIVERSE_PATH.exists(),
        "template": (SKILL_DIR / "templates" / "weekly-report.md").exists(),
        "taxonomy_ref": (SKILL_DIR / "references" / "signal-taxonomy.md").exists(),
        "store_dir": store_path().parent.exists(),
        "db_writable": os.access(db.db_root().parent, os.W_OK),
        "db_migrated": db.is_migrated(),
        "xreach": shutil.which("xreach") is not None,
        "yfinance": False,
    }
    try:
        import yfinance  # noqa: F401
        checks["yfinance"] = True
    except ImportError:
        pass
    universe_size = 0
    if checks["universe_file"]:
        try:
            universe_size = len(load_universe()["universe"])
            checks["universe_parses"] = universe_size > 0
        except Exception as exc:  # noqa: BLE001
            checks["universe_parses"] = False
            checks["universe_error"] = f"{type(exc).__name__}: {exc}"
    else:
        checks["universe_parses"] = False

    store_rows = fund_rows = quote_rows = None
    try:
        store_rows = len(load_store())
        checks["store_parses"] = True
    except ValueError as exc:
        checks["store_parses"] = False
        checks["store_error"] = str(exc)
    try:
        fund_rows = len(load_fundamentals())
        checks["fundamentals_parse"] = True
    except ValueError as exc:
        checks["fundamentals_parse"] = False
        checks["fundamentals_error"] = str(exc)
    expert_rows = None
    try:
        quote_rows = len(db.read_all("quotes"))
        expert_rows = len(db.read_all("experts"))
        profile_rows = len(db.read_profiles())
        checks["quotes_parse"] = True
    except ValueError as exc:
        profile_rows = None
        checks["quotes_parse"] = False
        checks["quotes_error"] = str(exc)

    required = ["python_3_12_plus", "universe_file", "universe_parses", "template",
                "taxonomy_ref", "store_dir", "db_writable"]
    missing = [k for k in required if not checks.get(k)]
    if missing:
        return {"ok": False,
                "error": "required checks failed: " + ", ".join(missing),
                "hint": "确认 skill 文件完整、工作区 state 目录存在且可写，再重跑 self-test。",
                "checks": checks}
    if False in (checks.get("store_parses"), checks.get("fundamentals_parse"),
                 checks.get("quotes_parse")):
        return {"ok": False,
                "error": "事实库有坏行：" + (checks.get("store_error") or checks.get("fundamentals_error")
                                       or checks.get("quotes_error") or ""),
                "hint": "修掉坏行再重跑，坏行会让 screen / screen-embedded 整个跑不出来。",
                "checks": checks}
    return {"ok": True, "message": "self-test passed", "universe_size": universe_size,
            "store_records": store_rows, "fundamental_records": fund_rows,
            "quote_records": quote_rows, "expert_records": expert_rows,
            "profile_records": profile_rows, "tables": TABLE_NAMES,
            "data_types": list(db.DATA_TYPES),
            "db_root": str(db.db_root()), "checks": checks}


def validate(path):
    p = _checked(path)
    if not p.is_file():
        return {"ok": False, "error": "report file not found: " + str(path),
                "hint": "--file 传一个已存在的 Markdown 报告。"}
    text = p.read_text(encoding="utf-8")
    errors = []
    for h in REQUIRED_HEADINGS:
        if h not in text:
            errors.append("missing heading: " + h)
    if re.search(r"\n\|\s*-{3,}", text):
        errors.append("不允许 Markdown 表格，周报用项目符号")
    for phrase in VAGUE_PHRASES:
        for m in re.finditer(re.escape(phrase), text):
            tail = text[m.end():m.end() + 40]
            head = text[max(0, m.start() - 40):m.start()]
            if not re.search(r"\d", tail + head):
                errors.append(f"模糊表述缺数字: {phrase}")
                break
    if "证据等级" not in text:
        errors.append("缺少证据等级标注")
    if "口径" not in text:
        errors.append("缺少口径说明")
    if errors:
        return {"ok": False, "error": "; ".join(sorted(set(errors))),
                "hint": "对照 templates/weekly-report.md 补齐栏目，所有增速数字紧跟口径、证据等级和来源日期。"}
    return {"ok": True, "message": "report validation passed", "file": str(p)}


# 无论自身增速是否达闸门都必须留在核心池的公司。它们的 AI 表述、资本开支和口径
# 定义整条赛道的预期，漏掉一周就会漏掉全行业的定价起点。写成显式名单而不是手工
# 标注，是为了让例外可见、可审、可撤。
TIER1_PINNED = {
    "0700.HK": "中国 AI 应用的读数设定者：元宝、微信 AI 入口与广告 eCPM 定义 A/H 赛道预期",
    "9988.HK": "通义与阿里云的口径定义中国企业 AI 支出的基准",
    "GOOGL": "Gemini 与 Cloud 分部是全球 AI 应用需求的一级读数",
    "MSFT": "Copilot 与 Azure AI 的口径定义企业 AI 支出基准",
    "META": "Advantage+ 与推荐系统是 AI 变现最大的单一样本",
    "AMZN": "AWS 与广告的 AI 读数决定基础设施到应用的传导",
    "AAPL": "端侧 AI 与 App Store 分发决定 ToC AI 应用的落地路径",
    "ORCL": "OaaS 与 RPO 是 AI 合同能见度的先行读数",
    "BIDU": "文心与百度智能云是 A/H AI 应用的对照组",
    "PLTR": "AI 交付纯度最高的上市样本，市场用它给整条赛道定价",
}

RETIER_RULES = [
    ("disclosed_ai_metric", "通道一有 A/B 级 AI 分项指标入库"),
    ("native_purity", "业务纯度 native：主业即 AI 交付"),
    ("has_attribution", "有 A/B 级管理层 AI 归因原话"),
    ("embedded_accel", "公司级总收入同比 ≥ 20% 且同比增速较对比期未回落，通道二有戏"),
    ("pinned", "行业读数设定者，显式钉在核心池"),
]


def retier(args):
    """按数据重算 scan_tier，替代扩池那天的手工标注。

    核心池的定义从「会不会拆 AI 收入」改成「值不值得每周看公司级读数」。
    命中任一条规则进核心池，全不命中进观察池，每家都给出命中的规则名。
    --apply 不写文件。覆盖池调整改仓库里的 references/universe.json。
    """
    if getattr(args, "apply", False):
        return {
            "ok": False,
            "error": "retier 不接受 --apply",
            "hint": "覆盖池调整需要改仓库里的 references/universe.json。",
        }
    uni = load_universe()
    rows = uni["universe"]

    try:
        metrics = load_store()
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": "先修 state/ai_app_metrics.jsonl 的坏行。"}
    disclosed = {r.get("ticker") for r in metrics
                 if r.get("metric") in DISCLOSED_FINANCIAL_METRICS
                 and r.get("evidence_grade") in ("A", "B")
                 and not r.get("proxy_only")}

    emb = screen_embedded(argparse.Namespace(tier=None, track=None, purity=None, with_price=False))
    if not emb.get("ok"):
        return emb
    by_ticker = {r["ticker"]: r for r in emb["results"]}
    attributed = {r["ticker"] for r in emb["results"] if r.get("has_attribution")}

    changes, kept, summary = [], [], {1: 0, 2: 0}
    for c in rows:
        t = c["ticker"]
        hits = []
        if t in disclosed:
            hits.append("disclosed_ai_metric")
        if c.get("ai_purity") == "native":
            hits.append("native_purity")
        if t in attributed:
            hits.append("has_attribution")
        e = by_ticker.get(t)
        # 口径不可比的期不给升池：并表堆出来的同比不算「公司级加速」。
        if e and e.get("revenue_yoy_basis") != "unusable" \
                and (e.get("revenue_yoy_pct") or -999) >= EMBEDDED_GATES["min_revenue_yoy_pct"] \
                and (e.get("yoy_chg_pts") is not None and e["yoy_chg_pts"] >= 0):
            hits.append("embedded_accel")
        if t in TIER1_PINNED:
            hits.append("pinned")

        new_tier = 1 if hits else 2
        old_tier = c.get("scan_tier")
        rec = {"ticker": t, "name": c.get("name"), "track": c.get("track"),
               "ai_purity": c.get("ai_purity"), "from": old_tier, "to": new_tier,
               "rules": hits,
               "revenue_yoy_pct": (e or {}).get("revenue_yoy_pct"),
               "yoy_chg_pts": (e or {}).get("yoy_chg_pts"),
               "has_fundamentals": e is not None}
        summary[new_tier] += 1
        (changes if new_tier != old_tier else kept).append(rec)

    up = [c for c in changes if c["to"] == 1]
    down = [c for c in changes if c["to"] == 2]
    lines = [f"scan_tier 重算（试算，未写入）："
             f"核心池 {summary[1]} 家 / 观察池 {summary[2]} 家，变动 {len(changes)} 家"]
    lines.append("")
    lines.append(f"[升入核心池] {len(up)} 家")
    for c in sorted(up, key=lambda x: x["ticker"]):
        lines.append(f"  {c['ticker']} {c['name']} · {c['ai_purity']} · 命中 {'/'.join(c['rules'])}")
    lines.append("")
    lines.append(f"[降入观察池] {len(down)} 家")
    for c in sorted(down, key=lambda x: x["ticker"]):
        yoy = c["revenue_yoy_pct"]
        why = ("无公司级财务记录" if not c["has_fundamentals"]
               else f"总收入同比 {yoy}%、增速变化 {c['yoy_chg_pts']} 个百分点，且无 AI 分项与归因")
        lines.append(f"  {c['ticker']} {c['name']} · {c['ai_purity']} · {why}")

    return {"ok": True, "version": VERSION, "applied": False,
            "rules": dict(RETIER_RULES), "pinned": TIER1_PINNED,
            "summary": {"tier1": summary[1], "tier2": summary[2],
                        "changed": len(changes), "unchanged": len(kept)},
            "changes": changes, "message": "\n".join(lines)}


def universe_cmd(args):
    uni = load_universe()
    rows = uni["universe"]
    if args.track:
        rows = [r for r in rows if r["track"] == args.track]
    if args.market:
        rows = [r for r in rows if r["market"] == args.market]
    if args.scan_tier:
        rows = [r for r in rows if r.get("scan_tier") == args.scan_tier]
    if getattr(args, "purity", None):
        rows = [r for r in rows if r.get("ai_purity") in args.purity]
    return {"ok": True, "count": len(rows), "tracks": uni["tracks"],
            "scan_tiers": uni.get("scan_tiers", {}),
            "ai_purity_levels": uni.get("ai_purity_levels", AI_PURITY_LEVELS),
            "companies": rows, "private_watchlist": uni["private_watchlist"],
            "message": f"{len(rows)} companies"}


# ======================================== 底层数据库：画像 / 语料 / 台账 / 快照
# 周报只做采集入库和当周排序；深度报告不定期跑，从同一套库里按公司把历史捞出来。
# 两边共用的就是这一层：完整性不靠遍历顺序保证，靠 coverage 台账保证。

def next_quarter(p):
    return prev_period(p, -1)


def default_until(as_of=None):
    """最近一个已经过了 45 天的完整季度。财报没出的季度不该算进缺口。"""
    today = datetime.fromisoformat(as_of).astimezone(TZ) if as_of else datetime.now(TZ)
    ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    year, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(8):
        m, d = ends[q]
        if (today - datetime(year, m, d, tzinfo=TZ)).days >= 45:
            return f"{year}Q{q}"
        q -= 1
        if q == 0:
            q, year = 4, year - 1
    return f"{year}Q{q}"


def quarter_range(since, until):
    if not parse_period(since) or not parse_period(until):
        raise ValueError(f"期间格式非法: {since} / {until}")
    out, cur = [], since
    while period_key(cur) <= period_key(until) and len(out) < 60:
        out.append(cur)
        cur = next_quarter(cur)
    return out


def resolve_tickers(args):
    """把 --tickers / --track / --purity / --scan-tier / --all 解析成公司名单。"""
    uni = load_universe()["universe"]
    known = {c["ticker"]: c for c in uni}
    if getattr(args, "tickers", None):
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        return want, [t for t in want if t not in known]
    rows = uni
    picked = False
    if getattr(args, "track", None):
        rows = [c for c in rows if c.get("track") == args.track]
        picked = True
    if getattr(args, "purity", None):
        rows = [c for c in rows if c.get("ai_purity") in args.purity]
        picked = True
    if getattr(args, "scan_tier", None):
        rows = [c for c in rows if c.get("scan_tier") == args.scan_tier]
        picked = True
    if not picked and not getattr(args, "all", False):
        return None, None
    return [c["ticker"] for c in rows], []


SCOPE_HINT = ("用 --tickers NET,PLTR 点名，或用 --track / --purity / --scan-tier 圈定范围，"
              "确实要全池才传 --all。")


# ------------------------------------------------------------ 表三 个性化用量

def usage_board(args):
    """表三 个性化用量。不排名次，只按加速 / 持平 / 减速分组。

    每家并列写出自己的口径：tokens/日的同比和 AI MAU 的同比之间没有可比性，
    强行排一个名次是在制造假信号。
    """
    res = screen(argparse.Namespace(tier=None, track=getattr(args, "track", None),
                                    with_price=False, primary_only=True))
    if not res.get("ok"):
        return res
    labels = {"accelerating": ("📈", "加速"), "flat": ("➖", "持平"),
              "decelerating": ("📉", "减速"), "unknown": ("❔", "无法判断趋势")}
    groups = {k: [] for k in labels}
    buckets = build_series(load_store())
    for r in res["results"]:
        if r["metric"] in DISCLOSED_FINANCIAL_METRICS:
            continue
        # 用量口径的序列普遍很短，环比链条常常凑不齐。环比增速的变化判不出来时，
        # 退到同比口径：本期同比与上一期同比比，仍然判不出就如实写「无法判断」。
        trend, basis = r["second_derivative"], "环比增速较上期变化"
        if trend == "unknown":
            key = (r["ticker"], r["metric"], r["period_kind"], r["series_id"])
            by_period = buckets.get(key, {})
            d = parse_period(r["period"])
            prev_p = prev_period(r["period"], 1)
            if d and prev_p and r["yoy_pct"] is not None:
                base_p = prev_period(prev_p, d["per_year"])
                yoy_prev, src, _ = growth_between(by_period.get(prev_p), by_period.get(base_p))
                if yoy_prev is not None and src == "computed":
                    diff = r["yoy_pct"] - yoy_prev
                    trend = ("accelerating" if diff >= 5 else
                             "decelerating" if diff <= -5 else "flat")
                    basis = f"同比增速较上期变化（{yoy_prev}% → {r['yoy_pct']}%）"
        entry = {
            "trend_basis": basis if trend != "unknown" else "环比与同比的增速变化都算不出",
            "company": r["company"], "ticker": r["ticker"], "track": r["track"],
            "metric": r["metric"], "metric_label": r["metric_label"],
            "series_id": r["series_id"], "period": r["period"],
            "period_kind_label": r["period_kind_label"],
            "value": r["value"], "unit": r["unit"],
            "yoy_pct": r["yoy_pct"], "yoy_source": r["yoy_source"],
            "qoq_pct": r["qoq_pct"], "qoq_source": r["qoq_source"],
            "trend": trend, "definition": r["definition"],
            "evidence_grade": r["evidence_grade"], "value_is_bound": r["value_is_bound"],
            "flags": r["flags"], "growth_blocked": r["growth_blocked"],
            "source_url": r["source_url"], "source_date": r["source_date"],
            "is_embedded_proxy": r["metric"] in EMBEDDED_PROXY_METRICS,
        }
        groups[trend].append(entry)
    for g in groups.values():
        g.sort(key=lambda e: (e["ticker"], e["metric"]))

    lines = [f"{TABLE_NAMES['usage']}（不排名次；每家口径互不可比，只分组看方向）",
             "趋势判据：先看环比增速较上期的变化，算不出退到同比增速较上期的变化，±5 个百分点以内算持平"]
    for key, (emoji, label) in labels.items():
        lines.append("")
        lines.append(f"[{emoji} {label}] {len(groups[key])} 条")
        if not groups[key]:
            lines.append("  （空）")
        for e in groups[key]:
            growth = []
            if e["yoy_pct"] is not None:
                growth.append(f"同比 {e['yoy_pct']}%")
            if e["qoq_pct"] is not None:
                growth.append(f"环比 {e['qoq_pct']}%")
            growth_txt = "、".join(growth) or "无可比对比期"
            lines.append(f"  · {e['company']}({e['ticker']}) {e['metric_label']} "
                         f"{e['period']} {e['value']}{e['unit']} | {growth_txt}"
                         f" | 趋势依据：{e['trend_basis']}"
                         + ("　⚑代理量，不作收入读数" if e["is_embedded_proxy"] else ""))
            lines.append(f"     口径：{e['definition'] or '缺口径说明，需回原始披露补'}"
                         f"（{e['period_kind_label']}，证据 {e['evidence_grade']} 级）")
            lines.append(f"     来源：{e['source_url'] or '缺来源链接'}"
                         + (f"（{e['source_date']}）" if e["source_date"] else ""))
            caveats = [FLAG_LABELS.get(f, f) for f in e["flags"] or []]
            if e["value_is_bound"]:
                caveats.append("原文只给下限，真实值更高")
            caveats += e["growth_blocked"]
            if caveats:
                lines.append("     注意：" + "；".join(caveats))
    return {"ok": True, "version": VERSION, "table": "usage",
            "table_label": TABLE_NAMES["usage"],
            "counts": {k: len(v) for k, v in groups.items()},
            "groups": groups, "message": "\n".join(lines)}


# ---------------------------------------------------------------- 业务画像

def add_profile(args):
    """业务画像：这家公司做什么、AI 在这门生意里处在什么位置、该走哪张表。

    290 家全量覆盖，先于财务和原话两层做，因为它是后面所有取数的路由表。
    Palantir 这类公司必须读业务属性才知道它是 AI 公司，靠 AI 分项披露判不出来。
    """
    uni = {c["ticker"]: c for c in load_universe()["universe"]}
    if args.ticker not in uni and not args.allow_off_universe:
        return {"ok": False, "error": f"{args.ticker} 不在覆盖池",
                "hint": "先把公司加进 references/universe.json；确属池外样本才用 --allow-off-universe。"}
    if args.route not in db.PROFILE_ROUTES:
        return {"ok": False, "error": f"未知路由: {args.route}",
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in db.PROFILE_ROUTES.items())}
    if args.purity not in AI_PURITY_LEVELS:
        return {"ok": False, "error": f"未知业务纯度: {args.purity}",
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in AI_PURITY_LEVELS.items())}
    if not args.business or not args.ai_role:
        return {"ok": False, "error": "缺业务描述或 AI 定位",
                "hint": "--business 写主业是什么，--ai-role 写 AI 在这门生意里处在什么位置。两句都不能省。"}
    if not args.evidence_url:
        return {"ok": False, "error": "缺判断依据",
                "hint": "--evidence-url 填做出这个判断所依据的一手材料链接（年报业务描述、产品页、业绩会）。"}

    info = uni.get(args.ticker, {})
    rec = {
        "kind": "profile",
        "ticker": args.ticker,
        "company": args.company or info.get("name") or args.ticker,
        "track": args.track or info.get("track"),
        "market": info.get("market"),
        "business": args.business,
        "ai_role": args.ai_role,
        "ai_purity": args.purity,
        "universe_purity": info.get("ai_purity"),
        "purity_matches_universe": info.get("ai_purity") == args.purity,
        "route": args.route,
        "route_reason": args.route_reason or "",
        "discloses_ai_line_item": args.ai_line_item,
        "discloses_usage_metric": args.usage_metric,
        "transcript_available": args.transcript_available,
        "ai_products": [p.strip() for p in (args.ai_products or "").split(",") if p.strip()],
        "evidence_quote": args.evidence_quote or "",
        "evidence_url": args.evidence_url,
        "evidence_date": args.evidence_date or "",
        "note": args.note or "",
        "reviewed_at": datetime.now(TZ).date().isoformat(),
        "recorded_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = db.append_jsonl(db.profiles_path(), rec)
    return {"ok": True, "message": f"recorded profile {rec['company']}（{rec['route']}）",
            "record": rec, "store": str(path)}


def profiles_cmd(args):
    """画像覆盖情况。缺一家，后面就有一家进不了任何一张表。"""
    uni = load_universe()["universe"]
    profiles = db.read_profiles()
    have = [c for c in uni if c["ticker"] in profiles]
    missing = [c for c in uni if c["ticker"] not in profiles]
    routes, mismatch = {}, []
    for c in have:
        p = profiles[c["ticker"]]
        routes[p.get("route")] = routes.get(p.get("route"), 0) + 1
        if p.get("ai_purity") != c.get("ai_purity"):
            mismatch.append({"ticker": c["ticker"], "company": c.get("name"),
                             "universe": c.get("ai_purity"), "profile": p.get("ai_purity")})
    if getattr(args, "ticker", None):
        p = profiles.get(args.ticker)
        if not p:
            return {"ok": False, "error": f"{args.ticker} 还没有画像",
                    "hint": "用 add-profile 先补画像，它决定这家公司走哪张排序表。"}
        return {"ok": True, "profile": p,
                "message": json.dumps(p, ensure_ascii=False, indent=2)}
    lines = [f"业务画像覆盖：{len(have)} / {len(uni)} 家",
             "路由分布：" + "、".join(f"{k} {v} 家" for k, v in sorted(routes.items())),
             f"与 universe.json 纯度判断不一致：{len(mismatch)} 家"]
    if missing:
        lines.append(f"待补 {len(missing)} 家（前 20）：" + "、".join(
            f"{c.get('name')}({c['ticker']})" for c in missing[:20]))
    return {"ok": True, "version": VERSION, "total": len(uni), "covered": len(have),
            "routes": routes, "purity_mismatch": mismatch,
            "missing": [{"ticker": c["ticker"], "company": c.get("name")} for c in missing],
            "message": "\n".join(lines)}


# ---------------------------------------------------------------- 原话语料

def add_quote(args):
    """管理层原话入库。收录判据是「这段话有没有对象」，不是「重不重要」。

    一个季度管理层讲了 20 处 AI 业务进展就记 20 条，抽取这一步不做主观筛选；
    分类标签照打，但永远不参与过滤，筛选留到写报告时再做。
    """
    if args.venue not in db.QUOTE_VENUES:
        return {"ok": False, "error": f"未知场合: {args.venue}",
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in db.QUOTE_VENUES.items())}
    if not parse_period(args.period):
        return {"ok": False, "error": f"期间格式非法: {args.period}",
                "hint": "格式为 2026Q2 / 2026FY，与财务口径对齐才能按季度并排看。"}
    if args.evidence_grade not in ("A", "B"):
        return {"ok": False, "error": "原话只收 A / B 级",
                "hint": "A=公司书面材料原文（新闻稿、公告、正式转写）；B=业绩会口头。媒体转述不入库。"}
    if not args.quote or len(args.quote.strip()) < 10:
        return {"ok": False, "error": "原话为空或过短",
                "hint": "--quote 存完整段落，不要切成单句：Q&A 里那句话回答的是哪个问题本身就是信息。"}
    anchors = args.anchor or []
    bad = [a for a in anchors if a not in db.QUOTE_ANCHORS]
    if bad:
        return {"ok": False, "error": "未知收录判据: " + "、".join(bad),
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in db.QUOTE_ANCHORS.items())}
    if not anchors:
        return {"ok": False, "error": "这段话没有对象，不入库",
                "hint": "至少命中一条 --anchor（product/workflow/result/customer/commitment）。"
                        "只有立场没有对象的话（AI is important）不收录。"}
    if args.specificity not in db.QUOTE_SPECIFICITY:
        return {"ok": False, "error": f"未知表述强度: {args.specificity}",
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in db.QUOTE_SPECIFICITY.items())}
    if args.polarity not in db.QUOTE_POLARITY:
        return {"ok": False, "error": f"未知结果方向: {args.polarity}",
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in db.QUOTE_POLARITY.items())
                        + "。结果为负或为零同样入库。"}
    if args.attribution_type and args.attribution_type not in ATTRIBUTION_TYPES:
        return {"ok": False, "error": f"未知归因类型: {args.attribution_type}",
                "hint": "可用类型: " + ", ".join(ATTRIBUTION_TYPES)}
    if not args.source_url:
        return {"ok": False, "error": "缺少来源", "hint": "--source-url 填一手来源链接。"}

    fp = db.quote_fingerprint(args.ticker, args.period, args.quote)
    if not args.allow_duplicate:
        for old in db.read_company(args.ticker, "quotes"):
            if old.get("fingerprint") == fp:
                return {"ok": False, "error": f"同一段原话已入库: {args.ticker} {args.period}",
                        "hint": "确属更正才用 --allow-duplicate。",
                        "existing": {"venue": old.get("venue"), "recorded_at": old.get("recorded_at")}}

    transcript_rel = ""
    if args.transcript_file:
        tf = Path(args.transcript_file)
        transcript_rel = str(tf if not tf.is_absolute()
                             else tf.relative_to(db.db_root()) if str(tf).startswith(str(db.db_root()))
                             else tf)
    rec = {
        "kind": "quote",
        "company": args.company,
        "ticker": args.ticker,
        "track": args.track,
        "period": args.period,
        "venue": args.venue,
        "venue_label": db.QUOTE_VENUES[args.venue],
        "speaker": args.speaker,
        "speaker_role": args.speaker_role or "",
        "analyst_question": args.analyst_question or "",
        "analyst": args.analyst or "",
        "quote": args.quote.strip(),
        "language": args.language or "",
        "anchors": anchors,
        "attribution_type": args.attribution_type or "",
        "specificity": args.specificity,
        "polarity": args.polarity,
        "topics": [t.strip() for t in (args.topics or "").split(",") if t.strip()],
        "evidence_grade": args.evidence_grade,
        "source_type": args.source_type,
        "source_url": args.source_url,
        "source_date": args.source_date,
        "transcript_file": transcript_rel,
        "paragraph_index": args.paragraph_index,
        "note": args.note or "",
        "origin": "extraction",
        "fingerprint": fp,
        "recorded_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = db.append_jsonl(db.shard_path(args.ticker, "quotes"), rec)
    return {"ok": True, "message": f"recorded quote {args.company} {args.period} {args.venue}",
            "record": rec, "store": str(path)}


def quotes_cmd(args):
    """按时间正序取一家公司的全部原话。深度报告直接按这个顺序贴。"""
    rows = db.read_company(args.ticker, "quotes")
    if args.since:
        rows = [r for r in rows if period_key(r.get("period")) >= period_key(args.since)]
    if args.until:
        rows = [r for r in rows if period_key(r.get("period")) <= period_key(args.until)]
    if args.venue:
        rows = [r for r in rows if r.get("venue") in args.venue]
    if args.polarity:
        rows = [r for r in rows if r.get("polarity") in args.polarity]
    rows.sort(key=lambda r: (period_key(r.get("period")), r.get("venue") or "",
                             r.get("paragraph_index") if isinstance(r.get("paragraph_index"), int) else 0))
    if not rows:
        return {"ok": True, "count": 0, "quotes": [],
                "message": f"{args.ticker} 在该窗口没有入库的管理层原话。"}
    lines = [f"{rows[0].get('company') or args.ticker}（{args.ticker}）管理层 AI 表述 {len(rows)} 条，按时间正序"]
    cur = None
    for r in rows:
        if r.get("period") != cur:
            cur = r.get("period")
            lines.append("")
            lines.append(f"【{cur}】")
        who = " ".join(x for x in (r.get("speaker"), r.get("speaker_role")) if x) or "未署名"
        tags = "/".join(x for x in (r.get("specificity"), r.get("polarity")) if x) or "未标"
        lines.append(f"  · [{db.QUOTE_VENUES.get(r.get('venue'), r.get('venue'))}·{who}·{tags}]")
        if r.get("analyst_question"):
            lines.append(f"    分析师提问：{r['analyst_question']}")
        lines.append(f"    「{r.get('quote')}」")
        lines.append(f"    来源：{r.get('source_url') or '缺来源'}"
                     + (f"（{r.get('source_date')}）" if r.get("source_date") else ""))
    return {"ok": True, "version": VERSION, "ticker": args.ticker, "count": len(rows),
            "quotes": rows, "message": "\n".join(lines)}


# ---------------------------------------------------------------- 专家访谈层

def add_expert(args):
    """第三方专家口径入库。与管理层原话按公司按季度对齐，才能看出说法对不对得上。

    管理层是利益相关方，讲自己的 AI 时天然乐观；前员工、渠道、客户看到的是同一件事
    的另一面。两边都在库里，才做得出「管理层说渗透率快速提升，同期渠道说客户还在
    POC 打转」这个判断——它比任何一边单独说的话都重要。

    合规：verbatim 引用不超过 20 词，且必须带来源署名。claim 用自己的话概括，
    不要整段拷贝访谈正文。
    """
    if args.expert_role not in db.EXPERT_ROLES:
        return {"ok": False, "error": f"未知专家类型: {args.expert_role}",
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in db.EXPERT_ROLES.items())}
    if args.alignment not in db.EXPERT_ALIGNMENT:
        return {"ok": False, "error": f"未知一致性判定: {args.alignment}",
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in db.EXPERT_ALIGNMENT.items())}
    if not parse_period(args.period):
        return {"ok": False, "error": f"期间格式非法: {args.period}",
                "hint": "格式为 2026Q2，与财务和原话口径对齐才能并排看。"}
    if not args.claim or len(args.claim.strip()) < 10:
        return {"ok": False, "error": "claim 为空或过短",
                "hint": "--claim 用自己的话写清专家的主张：谁、在讲哪个业务环节、结论是什么。"}
    if args.verbatim:
        n = db.word_count(args.verbatim)
        if n > db.EXPERT_VERBATIM_WORD_LIMIT:
            return {"ok": False,
                    "error": f"verbatim 引用 {n} 词，超过 {db.EXPERT_VERBATIM_WORD_LIMIT} 词上限",
                    "hint": "Guidepoint 合规要求：逐字引用不超过 20 词，其余用 --claim 概括。"}
    if not args.source_attribution:
        return {"ok": False, "error": "缺少来源署名",
                "hint": "--source-attribution 填提供方给的 source_attribution.markdown 原文，"
                        "引用专家内容必须带它。"}
    if args.evidence_grade not in ("A", "B", "C"):
        return {"ok": False, "error": "专家证据只收 A / B / C 级",
                "hint": "A=一手访谈转写原文；B=转写摘要或结构化纪要；C=第三方转述。"}
    if args.alignment == "contradicts" and not args.management_ref:
        return {"ok": False, "error": "判为 contradicts 必须指出跟哪句管理层表述矛盾",
                "hint": "--management-ref 填对应管理层原话的 fingerprint 或期间+要点，"
                        "否则这条矛盾无法复核。"}

    fp = db.expert_fingerprint(args.ticker, args.period, args.claim, args.source_id or "")
    if not args.allow_duplicate:
        for old in db.read_company(args.ticker, "experts"):
            if old.get("fingerprint") == fp:
                return {"ok": False, "error": f"同一条专家主张已入库: {args.ticker} {args.period}",
                        "hint": "确属更正才用 --allow-duplicate。",
                        "existing": {"recorded_at": old.get("recorded_at")}}
    rec = {
        "kind": "expert",
        "company": args.company,
        "ticker": args.ticker,
        "track": args.track,
        "period": args.period,
        "expert_role": args.expert_role,
        "expert_role_label": db.EXPERT_ROLES[args.expert_role],
        "expert_background": args.expert_background or "",
        "topic": args.topic or "",
        "claim": args.claim.strip(),
        "verbatim": (args.verbatim or "").strip(),
        "verbatim_words": db.word_count(args.verbatim) if args.verbatim else 0,
        "polarity": args.polarity or "",
        "alignment": args.alignment,
        "alignment_label": db.EXPERT_ALIGNMENT[args.alignment],
        "management_ref": args.management_ref or "",
        "provider": args.provider or "guidepoint",
        "source_id": args.source_id or "",
        "source_url": args.source_url or "",
        "source_date": args.source_date or "",
        "source_attribution": args.source_attribution,
        "evidence_grade": args.evidence_grade,
        "note": args.note or "",
        "fingerprint": fp,
        "recorded_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = db.append_jsonl(db.shard_path(args.ticker, "experts"), rec)
    return {"ok": True,
            "message": f"recorded expert view {args.company} {args.period} "
                       f"{rec['expert_role_label']} · {rec['alignment_label']}",
            "record": rec, "store": str(path)}


def experts_cmd(args):
    """按时间正序取一家公司的全部专家口径。"""
    rows = db.read_company(args.ticker, "experts")
    if args.since:
        rows = [r for r in rows if period_key(r.get("period")) >= period_key(args.since)]
    if args.until:
        rows = [r for r in rows if period_key(r.get("period")) <= period_key(args.until)]
    if args.alignment:
        rows = [r for r in rows if r.get("alignment") in args.alignment]
    rows.sort(key=lambda r: (period_key(r.get("period")), r.get("expert_role") or ""))
    if not rows:
        return {"ok": True, "count": 0, "experts": [],
                "message": f"{args.ticker} 在该窗口没有入库的专家口径。"}
    lines = [f"{rows[0].get('company') or args.ticker}（{args.ticker}）专家口径 {len(rows)} 条，按时间正序"]
    cur = None
    for r in rows:
        if r.get("period") != cur:
            cur = r.get("period")
            lines.append("")
            lines.append(f"【{cur}】")
        lines.append(f"  · [{r.get('expert_role_label')}·{r.get('alignment_label')}] "
                     f"{r.get('topic') or '未标主题'}")
        lines.append(f"    主张：{r.get('claim')}")
        if r.get("verbatim"):
            lines.append(f"    原话（{r.get('verbatim_words')} 词）：「{r['verbatim']}」")
        lines.append(f"    来源：{r.get('source_attribution')}")
    return {"ok": True, "version": VERSION, "ticker": args.ticker, "count": len(rows),
            "experts": rows, "message": "\n".join(lines)}


def contradictions_cmd(args):
    """管理层说法与专家说法对不上的地方。这是专家层最值钱的产出。

    只报库里已经明确判为 contradicts 的条目，并把同期管理层原话并排贴出来，
    让人自己判断谁更可信。不做自动语义比对——那会造出假矛盾。
    """
    tickers, unknown = resolve_tickers(args)
    if tickers is None:
        tickers = db.known_tickers()
    if unknown:
        return {"ok": False, "error": "不在覆盖池: " + "、".join(unknown),
                "hint": "先加进 references/universe.json。"}
    since = args.since or "2025Q1"
    out = []
    for tk in tickers:
        exps = [r for r in db.read_company(tk, "experts")
                if r.get("alignment") == "contradicts"
                and period_key(r.get("period")) >= period_key(since)]
        if not exps:
            continue
        quotes = db.read_company(tk, "quotes")
        for e in exps:
            same = [q for q in quotes if q.get("period") == e.get("period")]
            out.append({
                "ticker": tk, "company": e.get("company") or tk, "period": e.get("period"),
                "expert_role": e.get("expert_role_label"), "topic": e.get("topic"),
                "expert_claim": e.get("claim"), "expert_verbatim": e.get("verbatim"),
                "source_attribution": e.get("source_attribution"),
                "management_ref": e.get("management_ref"),
                "management_quotes": [{
                    "venue": q.get("venue_label") or q.get("venue"),
                    "speaker": q.get("speaker"), "specificity": q.get("specificity"),
                    "polarity": q.get("polarity"), "quote": q.get("quote"),
                    "source_url": q.get("source_url"),
                } for q in same],
            })
    out.sort(key=lambda e: (period_key(e["period"]), e["ticker"]), reverse=True)
    lines = [f"管理层与专家说法对不上的地方（{since} 起，{len(out)} 处）"]
    if not out:
        lines.append("  库里没有判为 contradicts 的专家条目。这不等于没有矛盾，"
                     "只等于还没有人在入库时标出来。")
    for e in out:
        lines.append("")
        lines.append(f"· {e['company']}({e['ticker']}) {e['period']} | {e['expert_role']}"
                     f" | 主题：{e['topic'] or '未标'}")
        lines.append(f"  专家：{e['expert_claim']}")
        if e["expert_verbatim"]:
            lines.append(f"    「{e['expert_verbatim']}」")
        lines.append(f"    {e['source_attribution']}")
        if e["management_quotes"]:
            for q in e["management_quotes"][:2]:
                lines.append(f"  管理层同期（{q['venue']}·{q['specificity']}/{q['polarity']}）："
                             f"「{q['quote'][:160]}」")
        else:
            lines.append("  管理层同期：库里没有该期原话，无法并排对照（先补原话再判这条矛盾）")
    return {"ok": True, "version": VERSION, "since": since, "count": len(out),
            "contradictions": out, "message": "\n".join(lines)}


def add_transcript(args):
    """原始转写留档。以后换一套抽取标准时不必重拉 AlphaEngine。"""
    src = _checked(args.file)
    if not src.is_file():
        return {"ok": False, "error": "文件不存在: " + str(args.file),
                "hint": "--file 传已经落到本地的转写正文（纯文本）。"}
    if not parse_period(args.period):
        return {"ok": False, "error": f"期间格式非法: {args.period}",
                "hint": "格式为 2026Q2。"}
    dest = _checked(db.transcripts_dir() / db.ticker_slug(args.ticker) / f"{args.period}.txt")
    if dest.exists() and not args.overwrite:
        return {"ok": False, "error": "已存在: " + str(dest.relative_to(db.workspace_root())),
                "hint": "确要覆盖才加 --overwrite。"}
    text = src.read_text(encoding="utf-8", errors="replace")
    header = (f"# {args.ticker} {args.period}\n"
              f"# source_url: {args.source_url or ''}\n"
              f"# source_date: {args.source_date or ''}\n"
              f"# archived_at: {datetime.now(TZ).isoformat(timespec='seconds')}\n\n")
    card_io.write_bytes_exclusive(dest, (header + text).encode("utf-8"))
    rel = dest.relative_to(db.db_root())
    return {"ok": True, "message": f"archived transcript {args.ticker} {args.period} -> {rel}",
            "path": str(rel), "relative": str(rel), "chars": len(text)}


# ---------------------------------------------------------------- 补全台账

def mark_gap(args):
    """记一条缺口：这家这一季的这类数据缺什么、为什么缺。

    A 股和部分小盘港股没有规范季度分项披露，也没有转写。这些必须显式标出来，
    在榜单里显示「数据不足」，而不是静默消失——静默消失就是漏。
    """
    if args.reason not in db.GAP_REASONS:
        return {"ok": False, "error": f"未知缺口原因: {args.reason}",
                "hint": "可选: " + "、".join(f"{k}（{v}）" for k, v in db.GAP_REASONS.items())}
    if args.data_type not in db.DATA_TYPES + ("profile",):
        return {"ok": False, "error": f"未知数据类型: {args.data_type}",
                "hint": "可选: " + " / ".join(db.DATA_TYPES + ("profile",))}
    if args.period and not parse_period(args.period):
        return {"ok": False, "error": f"期间格式非法: {args.period}", "hint": "格式为 2026Q2。"}
    rec = {
        "kind": "gap", "ticker": args.ticker, "company": args.company or "",
        "period": args.period or None, "data_type": args.data_type,
        "reason": args.reason, "reason_label": db.GAP_REASONS[args.reason],
        "checked_source": args.checked_source or "", "note": args.note or "",
        "recorded_at": datetime.now(TZ).isoformat(timespec="seconds"),
    }
    path = db.append_jsonl(db.gaps_path(), rec)
    return {"ok": True, "message": f"marked gap {args.ticker} {args.period} {args.data_type}: {rec['reason_label']}",
            "record": rec, "store": str(path)}


# 一期回填的三类。专家层放二期：它按公司逐家约访谈、逐条读，节奏跟前三类不同，
# 而且没有管理层原话在库里就判不出「说法对不对得上」。台账现在就建四类格子，
# 是为了二期不用重建口径、不用重跑对账。
CORE_DATA_TYPES = ("fundamentals", "metrics", "quotes")


def coverage_ledger(args):
    """逐家逐季的补全台账。跑之前先把格子建好，跑完对账，空格子一目了然。

    完整性不靠遍历顺序保证，靠这张表保证。中途中断也不影响，台账知道断在哪。
    """
    since = args.since or "2025Q1"
    until = args.until or default_until(getattr(args, "as_of", None))
    try:
        periods = quarter_range(since, until)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "hint": "--since / --until 用 2025Q1 这种格式。"}

    uni = load_universe()["universe"]
    tickers, unknown = resolve_tickers(args)
    if tickers is None:
        tickers = [c["ticker"] for c in uni]
    if unknown:
        return {"ok": False, "error": "不在覆盖池: " + "、".join(unknown),
                "hint": "先加进 references/universe.json。"}
    scope = set(tickers)
    rows = [c for c in uni if c["ticker"] in scope]

    profiles = db.read_profiles()
    gaps = db.read_gaps()
    metrics, funds = load_store(), load_fundamentals()
    have = {dt: set() for dt in db.DATA_TYPES}
    for r in metrics:
        have["metrics"].add((r.get("ticker"), r.get("period")))
    for r in funds:
        if r.get("kind") == "fundamental":
            have["fundamentals"].add((r.get("ticker"), r.get("period")))
    quote_counts, expert_counts = {}, {}
    for tk in scope:
        for q in db.read_company(tk, "quotes"):
            key = (tk, q.get("period"))
            quote_counts[key] = quote_counts.get(key, 0) + 1
            have["quotes"].add(key)
        for e in db.read_company(tk, "experts"):
            key = (tk, e.get("period"))
            expert_counts[key] = expert_counts.get(key, 0) + 1
            have["experts"].add(key)

    grid, summary = {}, {"filled": 0, "gap": 0, "missing": 0}
    by_type = {dt: {"filled": 0, "gap": 0, "missing": 0} for dt in db.DATA_TYPES}
    for c in rows:
        tk = c["ticker"]
        cell = {}
        for p in periods:
            per = {}
            for dt in db.DATA_TYPES:
                if (tk, p) in have[dt]:
                    per[dt] = "filled"
                    summary["filled"] += 1
                    by_type[dt]["filled"] += 1
                elif (tk, p, dt) in gaps:
                    per[dt] = "gap:" + gaps[(tk, p, dt)]["reason"]
                    summary["gap"] += 1
                    by_type[dt]["gap"] += 1
                else:
                    per[dt] = "missing"
                    summary["missing"] += 1
                    by_type[dt]["missing"] += 1
            per["quote_count"] = quote_counts.get((tk, p), 0)
            per["expert_count"] = expert_counts.get((tk, p), 0)
            cell[p] = per
        prof = profiles.get(tk)
        grid[tk] = {
            "company": c.get("name"), "track": c.get("track"), "market": c.get("market"),
            "scan_tier": c.get("scan_tier"), "ai_purity": c.get("ai_purity"),
            "profile": bool(prof), "route": (prof or {}).get("route"),
            "periods": cell,
            "missing_cells": sum(1 for p in periods for dt in db.DATA_TYPES
                                 if cell[p][dt] == "missing"),
            "missing_core_cells": sum(1 for p in periods for dt in CORE_DATA_TYPES
                                      if cell[p][dt] == "missing"),
        }

    no_profile = [tk for tk, v in grid.items() if not v["profile"]]
    fully_missing = [tk for tk, v in grid.items()
                     if v["missing_core_cells"] == len(periods) * len(CORE_DATA_TYPES)]
    payload = {
        "built_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "since": since, "until": until, "periods": periods,
        "companies_in_scope": len(rows),
        "cells_total": len(rows) * len(periods) * len(db.DATA_TYPES),
        "core_cells_total": len(rows) * len(periods) * len(CORE_DATA_TYPES),
        "data_types": list(db.DATA_TYPES),
        "summary": summary,
        "summary_by_type": by_type,
        "profile_covered": len(rows) - len(no_profile),
        "profile_missing": no_profile,
        "fully_missing": fully_missing,
        "grid": grid,
    }
    if getattr(args, "write", False):
        _write_workspace_text(db.coverage_path(), json.dumps(payload, ensure_ascii=False, indent=2))
    pct = (summary["filled"] / payload["cells_total"] * 100) if payload["cells_total"] else 0
    lines = [f"补全台账 {since}–{until}（{len(periods)} 期 × {len(rows)} 家 × "
             f"{len(db.DATA_TYPES)} 类 = {payload['cells_total']} 格；"
             f"其中一期核心三类 {payload['core_cells_total']} 格，专家层为二期）",
             f"  已补：{summary['filled']} 格（{pct:.1f}%）",
             f"  已标缺口：{summary['gap']} 格（来源确实没有）",
             f"  待补：{summary['missing']} 格",
             f"  业务画像：{payload['profile_covered']} / {len(rows)} 家",
             f"  一期核心三类全空：{len(fully_missing)} 家"]
    for dt in db.DATA_TYPES:
        t = by_type[dt]
        phase = "一期" if dt in CORE_DATA_TYPES else "二期"
        lines.append(f"    {dt}（{phase}）：已补 {t['filled']} / 已标缺口 {t['gap']} / 待补 {t['missing']}")
    if getattr(args, "write", False):
        lines.append(f"  台账已写入 {db.coverage_path()}")
    return {"ok": True, "version": VERSION, **payload, "message": "\n".join(lines)}


def backfill_plan(args):
    """把没补齐的格子切成批次，供并行回填领任务。批次只决定谁先谁后，不决定谁被覆盖。"""
    led = coverage_ledger(argparse.Namespace(
        since=args.since, until=args.until, as_of=None, write=False,
        tickers=args.tickers, track=args.track, purity=args.purity,
        scan_tier=args.scan_tier, all=True))
    if not led.get("ok"):
        return led
    layer = args.layer
    grid = led["grid"]
    if layer == "profile":
        todo = [tk for tk in led["profile_missing"]]
        unit = "家（业务画像）"
    elif layer == "expert":
        # 二期。只对已经补完核心三类的公司排队：没有管理层原话就判不出说法对不对得上。
        ready = [tk for tk, v in grid.items() if v["missing_core_cells"] == 0]
        todo = sorted([tk for tk in ready
                       if any(v == "missing" for p in led["periods"]
                              for v in [grid[tk]["periods"][p]["experts"]])],
                      key=lambda tk: -grid[tk]["missing_cells"])
        unit = "家（专家口径，仅限核心三类已补齐的公司）"
    else:
        todo = sorted([tk for tk, v in grid.items() if v["missing_core_cells"] > 0],
                      key=lambda tk: -grid[tk]["missing_core_cells"])
        unit = "家（财务 + 指标 + 原话，一次做完）"
    size = max(1, args.batch_size)
    batches = []
    for i in range(0, len(todo), size):
        chunk = todo[i:i + size]
        batches.append({
            "batch": len(batches) + 1,
            "tickers": chunk,
            "companies": [{"ticker": tk, "company": grid[tk]["company"],
                           "track": grid[tk]["track"], "market": grid[tk]["market"],
                           "route": grid[tk]["route"],
                           "missing_cells": grid[tk]["missing_cells"]} for tk in chunk],
        })
    cmds = {
        "profile": "add-profile --ticker T --company C --track ... --purity ... --route ... "
                   "--business '...' --ai-role '...' --evidence-url https://...",
        "fundamental": "add-fundamental --company C --ticker T --track ... --period 2025Q1 "
                       "--total-revenue-usd-mn N --revenue-yoy-pct N --evidence-grade A "
                       "--source-url https://... --source-date YYYY-MM-DD --quote '...'",
        "metric": "add-metric --company C --ticker T --track ... --metric ai_revenue "
                  "--period 2025Q1 --value N --unit USD_mn --value-usd-mn N --definition '...' "
                  "--evidence-grade A --source-url https://... --quote '...'",
        "quote": "add-quote --company C --ticker T --track ... --period 2025Q1 "
                 "--venue call_qa --speaker '...' --anchor result --specificity quantified "
                 "--polarity positive --quote '<完整段落>' --evidence-grade B --source-url https://...",
        "transcript": "add-transcript --ticker T --period 2025Q1 "
                      "--file state/ai-app-weekly/temp/ai-app-backfill/x.txt --source-url https://...",
        "gap": "mark-gap --ticker T --period 2025Q1 --data-type quotes --reason no_transcript "
               "--checked-source '查过 AlphaEngine 与公司 IR，均无转写'",
        "expert": "add-expert --company C --ticker T --track ... --period 2025Q1 "
                  "--expert-role former_employee --topic '<主题>' --claim '<用自己的话写主张>' "
                  "--verbatim '<≤20 词>' --alignment contradicts --management-ref '<对应原话 fingerprint>' "
                  "--evidence-grade A --source-id <doc id> --source-attribution '<提供方署名原文>'",
    }
    lines = [f"回填批次：{layer} 层，{len(todo)} {unit}，切成 {len(batches)} 批，每批 {size} 家",
             f"窗口 {led['since']}–{led['until']}，共 {led['summary']['missing']} 格待补"]
    for b in batches[:10]:
        lines.append(f"  批 {b['batch']}：" + "、".join(
            f"{c['company']}({c['ticker']})" for c in b["companies"]))
    if len(batches) > 10:
        lines.append(f"  …… 其余 {len(batches) - 10} 批见 --json 输出")
    return {"ok": True, "version": VERSION, "layer": layer, "window": [led["since"], led["until"]],
            "batch_size": size, "batches": batches, "commands": cmds,
            "message": "\n".join(lines)}


# ---------------------------------------------------------------- 快照与本周变化

# 表述强度的高低顺序。从「模糊」跳到「量化」的那个季度，通常就是公司内部
# 确认这门生意成立的时点。榜单上看不到这一跳，因为它不是数字。
SPECIFICITY_RANK = {"": 0, "vague": 1, "directional": 2, "quantified": 3}
SPECIFICITY_LABEL = {"vague": "模糊", "directional": "定向", "quantified": "量化"}
POLARITY_LABEL = {"positive": "正面", "neutral": "中性", "negative": "负面"}


def narrative_profile(quotes):
    """一家公司当期的「说法状态」：最高强度档、方向构成、最新一期讲到哪。

    快照里存这个，diff 才能报出「这家从模糊变成量化了」「从正面转成负面了」。
    """
    rows = [q for q in quotes if q.get("period")]
    if not rows:
        return {"count": 0}
    latest = max(period_key(q["period"]) for q in rows)
    latest_period = [q["period"] for q in rows if period_key(q["period"]) == latest][0]
    cur = [q for q in rows if q.get("period") == latest_period]
    top = max((q.get("specificity") or "" for q in cur), key=lambda x: SPECIFICITY_RANK.get(x, 0))
    pol = {}
    for q in cur:
        k = q.get("polarity") or ""
        pol[k] = pol.get(k, 0) + 1
    attribution = sorted({q.get("attribution_type") for q in cur if q.get("attribution_type")})
    return {
        "count": len(rows),
        "company": next((q.get("company") for q in rows if q.get("company")), ""),
        "latest_period": latest_period,
        "latest_count": len(cur),
        "max_specificity": top,
        "max_specificity_rank": SPECIFICITY_RANK.get(top, 0),
        "polarity_mix": pol,
        "dominant_polarity": max(pol, key=lambda k: pol[k]) if pol else "",
        "attribution_types": attribution,
        "ever_quantified": any((q.get("specificity") == "quantified") for q in rows),
    }


def expert_profile(experts):
    """一家公司当期的专家层状态：条数、一致性构成、最新一期。"""
    align = {}
    for e in experts:
        k = e.get("alignment") or ""
        align[k] = align.get(k, 0) + 1
    periods = [e.get("period") for e in experts if e.get("period")]
    return {
        "count": len(experts),
        "alignment_mix": align,
        "contradicts": align.get("contradicts", 0),
        "latest_period": max(periods, key=period_key) if periods else "",
        "fingerprints": sorted({e.get("fingerprint") for e in experts if e.get("fingerprint")}),
    }


def build_snapshot(as_of=None):
    """当期状态的可比快照。下一期跟它对账，就知道这一周到底变了什么。

    快照存两类东西：榜单位置（数字层）和说法状态（定性层）。
    只存榜单位置的话，一家公司从「还没看到 AI 对定价产生影响」改口成
    「AI 相关 ARR 超过 X」，diff 只会输出「新增 3 条原话」——那是这家公司
    整个投资逻辑的转折点，不该被压成一个计数。
    """
    fin = rank(argparse.Namespace(by="yoy", track=None, limit=0,
                                  min_growth=None, with_price=False))
    if not fin.get("ok"):
        return fin
    emb = screen_embedded(argparse.Namespace(tier=None, track=None, with_price=False, purity=None))
    if not emb.get("ok"):
        return emb
    usage = usage_board(argparse.Namespace(track=None))
    if not usage.get("ok"):
        return usage

    financial = {}
    for board in ("main", "proxy", "small_base", "caveat"):
        for e in fin["boards"][board]:
            key = f"{e['ticker']}|{e['metric']}|{e['series_id']}"
            financial[key] = {
                "board": board, "company": e["company"], "ticker": e["ticker"],
                "metric_label": e["metric_label"], "period": e["period"],
                "value": e["value"], "unit": e["unit"], "growth_pct": e["growth_pct"],
                "tier": e["tier"], "tier_label": e["tier_label"],
                "definition": e["definition"], "flags": e["flags"],
                "evidence_grade": e["evidence_grade"],
            }
    embedded = {r["ticker"]: {"company": r["company"], "tier": r["tier"],
                              "tier_label": EMBEDDED_TIERS[r["tier"]]["label"],
                              "period": r["period"], "yoy_chg_pts": r["yoy_chg_pts"],
                              "revenue_yoy_pct": r["revenue_yoy_pct"]}
                for r in emb["results"]}
    usage_map = {}
    for group, rows in usage["groups"].items():
        for e in rows:
            usage_map[f"{e['ticker']}|{e['metric']}|{e['series_id']}"] = {
                "company": e["company"], "metric_label": e["metric_label"],
                "trend": group, "period": e["period"], "value": e["value"], "unit": e["unit"],
            }
    quotes_by_company, covered = {}, set()
    narrative, experts_by_company, coverage_counts = {}, {}, {}
    for tk in db.known_tickers():
        coverage_counts[tk] = {dt: len(db.read_company(tk, dt)) for dt in db.DATA_TYPES}
        qs = db.read_company(tk, "quotes")
        n = len(qs)
        if n:
            quotes_by_company[tk] = n
            narrative[tk] = narrative_profile(qs)
        exps = db.read_company(tk, "experts")
        if exps:
            experts_by_company[tk] = expert_profile(exps)
        if n or exps or db.read_company(tk, "fundamentals") or db.read_company(tk, "metrics"):
            covered.add(tk)
    if not db.is_migrated():
        for r in load_store() + load_fundamentals():
            if r.get("ticker"):
                covered.add(r["ticker"])
    return {
        "ok": True,
        "date": (datetime.fromisoformat(as_of) if as_of else datetime.now(TZ)).date().isoformat(),
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "version": VERSION,
        "financial": financial,
        "share": {f"{e['ticker']}|{e['metric']}": {
            "company": e["company"], "metric_label": e["metric_label"],
            "share_pct": e["share_pct"], "period": e["period"], "tier": e["tier"]}
            for e in fin.get("share_board", {}).get("rows", [])},
        "embedded": embedded,
        "embedded_growth_rank": {e["ticker"]: {"rank": e["rank"],
                                               "revenue_yoy_pct": e["revenue_yoy_pct"]}
                                 for e in emb.get("by_growth", [])},
        "usage": usage_map,
        "quotes_by_company": quotes_by_company,
        "narrative": narrative,
        "experts": experts_by_company,
        "coverage_counts": coverage_counts,
        "covered_companies": sorted(covered),
        "records": {"metrics": len(load_store()), "fundamentals": len(load_fundamentals()),
                    "quotes": sum(quotes_by_company.values()),
                    "experts": sum(v["count"] for v in experts_by_company.values())},
    }


def snapshot_cmd(args):
    snap = build_snapshot(getattr(args, "as_of", None))
    if not snap.get("ok"):
        return snap
    relative = f"state/ai_app_db/snapshots/{snap['date']}.json"
    path = db.snapshots_dir() / f"{snap['date']}.json"
    written = bool(getattr(args, "write", False))
    if written:
        blocked = _doc_write_block(snap["date"])
        if blocked is not None:
            # --no-doc is only for the no-change short message: the period must have
            # no document record and no failed attempt at all.
            if not (getattr(args, "no_doc", False) and blocked.get("reason") == "doc_missing"):
                return blocked
    if written:
        if path.exists() and not args.overwrite:
            return {"ok": False, "error": f"快照已存在: {path.name}",
                    "hint": "同一天重跑要覆盖就加 --overwrite。"}
        _write_workspace_text(path, json.dumps(snap, ensure_ascii=False, indent=2))
    return {"ok": True, "version": VERSION, "date": snap["date"], "path": relative,
            "written": written,
            "counts": {"financial": len(snap["financial"]), "embedded": len(snap["embedded"]),
                       "usage": len(snap["usage"]), **snap["records"]},
            "snapshot": snap if getattr(args, "full", False) else None,
            "message": (f"快照 {snap['date']}：财务 {len(snap['financial'])} 条 / "
                        f"内嵌 {len(snap['embedded'])} 家 / 用量 {len(snap['usage'])} 条 / "
                        f"原话 {snap['records']['quotes']} 条"
                        + (f"，已写入 {relative}" if written else "，未写文件"))}


def latest_snapshot(before=None):
    d = db.snapshots_dir()
    if not d.exists():
        return None, None
    files = sorted(p for p in d.glob("*.json"))
    if before:
        files = [p for p in files if p.stem < before]
    if not files:
        return None, None
    return files[-1], json.loads(files[-1].read_text(encoding="utf-8"))


def diff_cmd(args):
    """本周变化。放在周报开头：这一期跟上一期比，到底哪些公司和指标变了。

    全是本地数据比对，不新增任何外部取数。第一期没有对照快照，显示「基期」。
    """
    if getattr(args, "against", None):
        path = db.snapshots_dir() / f"{args.against}.json"
        if not path.exists():
            return {"ok": False, "error": f"找不到快照: {path}",
                    "hint": "先跑 snapshot --write，或用 --against 指定已存在的日期。"}
        prev = json.loads(path.read_text(encoding="utf-8"))
    else:
        path, prev = latest_snapshot()
    cur = build_snapshot()
    if not cur.get("ok"):
        return cur
    if prev is None:
        return {"ok": True, "version": VERSION, "baseline": True, "changes": {},
                "message": ("本周变化：无对照快照，本期为基期。"
                            f"当前财务 {len(cur['financial'])} 条 / 内嵌 {len(cur['embedded'])} 家 / "
                            f"用量 {len(cur['usage'])} 条。发出报告后跑 snapshot --write 建立基线。")}

    ch = {"financial_new": [], "financial_dropped": [], "financial_tier": [],
          "financial_growth": [], "definition_changed": [], "share_new": [], "share_moved": [],
          "embedded_new": [], "embedded_tier": [], "embedded_growth": [], "usage_trend": [],
          "specificity_shift": [], "polarity_flip": [], "first_quantified": [],
          "expert_new": [], "expert_contradiction": [],
          "new_quotes": [], "new_companies": []}

    pf, cf = prev.get("financial", {}), cur["financial"]
    for k, v in cf.items():
        if k not in pf:
            ch["financial_new"].append({"key": k, **v})
            continue
        o = pf[k]
        if o.get("tier") != v.get("tier"):
            ch["financial_tier"].append({"key": k, "company": v["company"],
                                         "metric_label": v["metric_label"],
                                         "from": o.get("tier_label"), "to": v.get("tier_label"),
                                         "period": v["period"]})
        og, ng = o.get("growth_pct"), v.get("growth_pct")
        if og is not None and ng is not None and abs(ng - og) >= args.min_delta:
            ch["financial_growth"].append({"key": k, "company": v["company"],
                                           "metric_label": v["metric_label"],
                                           "from": og, "to": ng, "delta": round(ng - og, 1),
                                           "period": v["period"], "prev_period": o.get("period")})
        if "definition_changed" in (v.get("flags") or []) and \
                "definition_changed" not in (o.get("flags") or []):
            ch["definition_changed"].append({"key": k, "company": v["company"],
                                             "metric_label": v["metric_label"],
                                             "period": v["period"],
                                             "definition": v.get("definition")})
    for k, v in pf.items():
        if k not in cf:
            ch["financial_dropped"].append({"key": k, "company": v.get("company"),
                                            "metric_label": v.get("metric_label"),
                                            "last_period": v.get("period"),
                                            "last_tier": v.get("tier_label"),
                                            "reason": "本期不在任何榜单，多为记录被更正或口径降级"})

    pe, ce = prev.get("embedded", {}), cur["embedded"]
    for tk, v in ce.items():
        if tk not in pe:
            ch["embedded_new"].append({"ticker": tk, **v})
        elif pe[tk].get("tier") != v.get("tier"):
            ch["embedded_tier"].append({"ticker": tk, "company": v["company"],
                                        "from": pe[tk].get("tier_label"),
                                        "to": v.get("tier_label"),
                                        "yoy_chg_pts": v.get("yoy_chg_pts"),
                                        "period": v.get("period")})
    ps, cs = prev.get("share", {}), cur.get("share", {})
    for k, v in cs.items():
        if k not in ps:
            ch["share_new"].append({"key": k, **v})
        else:
            o = ps[k].get("share_pct")
            n = v.get("share_pct")
            if o is not None and n is not None and abs(n - o) >= 1.0:
                ch["share_moved"].append({"key": k, "company": v["company"],
                                          "metric_label": v["metric_label"],
                                          "from": o, "to": n, "delta": round(n - o, 1),
                                          "period": v.get("period"), "tier": v.get("tier")})

    pg, cg = prev.get("embedded_growth_rank", {}), cur.get("embedded_growth_rank", {})
    for tk, v in cg.items():
        o = pg.get(tk)
        if o and o.get("revenue_yoy_pct") is not None and v.get("revenue_yoy_pct") is not None \
                and abs(v["revenue_yoy_pct"] - o["revenue_yoy_pct"]) >= args.min_delta:
            ch["embedded_growth"].append({
                "ticker": tk, "company": ce.get(tk, {}).get("company", tk),
                "from": o["revenue_yoy_pct"], "to": v["revenue_yoy_pct"],
                "delta": round(v["revenue_yoy_pct"] - o["revenue_yoy_pct"], 1),
                "rank_from": o.get("rank"), "rank_to": v.get("rank")})

    pu, cu = prev.get("usage", {}), cur["usage"]
    for k, v in cu.items():
        if k in pu and pu[k].get("trend") != v.get("trend"):
            ch["usage_trend"].append({"key": k, "company": v["company"],
                                      "metric_label": v["metric_label"],
                                      "from": pu[k].get("trend"), "to": v.get("trend"),
                                      "period": v.get("period")})
    pq, cq = prev.get("quotes_by_company", {}), cur["quotes_by_company"]
    for tk, n in cq.items():
        delta = n - pq.get(tk, 0)
        if delta > 0:
            ch["new_quotes"].append({"ticker": tk, "added": delta, "total": n})
    ch["new_quotes"].sort(key=lambda e: -e["added"])

    # 定性层：说法本身变了没有。这一段跟榜单无关，纯本地比对，不新增取数。
    pn, cn = prev.get("narrative", {}), cur.get("narrative", {})
    for tk, v in cn.items():
        o = pn.get(tk)
        if not o or not o.get("count"):
            continue
        same_period = v.get("latest_period") == o.get("latest_period")
        base = {"ticker": tk,
                "company": v.get("company") or ce.get(tk, {}).get("company") or tk,
                "from_period": o.get("latest_period"), "to_period": v.get("latest_period"),
                # 同期补录也报：库里对这家的说法确实变了，只是变化来自补抽而不是新财报。
                "same_period": same_period}
        if v.get("max_specificity_rank", 0) != o.get("max_specificity_rank", 0):
            ch["specificity_shift"].append({
                **base, "from": SPECIFICITY_LABEL.get(o.get("max_specificity"), "未标"),
                "to": SPECIFICITY_LABEL.get(v.get("max_specificity"), "未标"),
                "direction": "up" if v.get("max_specificity_rank", 0) > o.get("max_specificity_rank", 0) else "down"})
        if v.get("dominant_polarity") and o.get("dominant_polarity") \
                and v["dominant_polarity"] != o["dominant_polarity"]:
            ch["polarity_flip"].append({
                **base, "from": POLARITY_LABEL.get(o["dominant_polarity"], o["dominant_polarity"]),
                "to": POLARITY_LABEL.get(v["dominant_polarity"], v["dominant_polarity"])})
        if v.get("ever_quantified") and not o.get("ever_quantified"):
            ch["first_quantified"].append(base)

    pe2, ce2 = prev.get("experts", {}), cur.get("experts", {})
    for tk, v in ce2.items():
        o = pe2.get(tk, {})
        added = v.get("count", 0) - o.get("count", 0)
        if added > 0:
            ch["expert_new"].append({"ticker": tk, "added": added, "total": v["count"],
                                     "latest_period": v.get("latest_period")})
        new_contra = v.get("contradicts", 0) - o.get("contradicts", 0)
        if new_contra > 0:
            ch["expert_contradiction"].append({"ticker": tk, "added": new_contra,
                                               "total": v.get("contradicts", 0),
                                               "latest_period": v.get("latest_period")})
    ch["expert_new"].sort(key=lambda e: -e["added"])
    prev_cov = set(prev.get("covered_companies", []))
    ch["new_companies"] = [tk for tk in cur["covered_companies"] if tk not in prev_cov]

    total = sum(len(v) for v in ch.values())
    lines = [f"本周变化（对照 {prev.get('date')} 快照，共 {total} 项）"]
    if not total:
        lines.append("  本期无变化：没有新入榜、没有档位变动、没有新增原话。")

    def block(title, rows, fmt):
        if not rows:
            return
        lines.append("")
        lines.append(f"[{title}] {len(rows)} 项")
        for e in rows[: args.limit or len(rows)]:
            lines.append("  · " + fmt(e))

    block("新入榜 · 表一 财务口径", ch["financial_new"],
          lambda e: f"{e['company']} {e['metric_label']} {e['period']} "
                    f"{e['value']}{e['unit']} | 同比 {e['growth_pct']}% | {e['tier_label']}")
    block("掉榜 · 表一", ch["financial_dropped"],
          lambda e: f"{e['company']} {e['metric_label']}（上期 {e['last_period']}·{e['last_tier']}）"
                    f"：{e['reason']}")
    block("档位变动 · 表一", ch["financial_tier"],
          lambda e: f"{e['company']} {e['metric_label']} {e['from']} → {e['to']}（{e['period']}）")
    block(f"增速变化 ≥ {args.min_delta}pct · 表一", ch["financial_growth"],
          lambda e: f"{e['company']} {e['metric_label']} {e['from']}% → {e['to']}%"
                    f"（{e['delta']:+}pct，{e['prev_period']} → {e['period']}）")
    block("口径变更 · 最容易骗人的一类", ch["definition_changed"],
          lambda e: f"{e['company']} {e['metric_label']} {e['period']} 本期标了口径变更："
                    f"{e['definition'] or '缺口径说明'}")
    block("新入榜 · 表二 AI Monetization as % of Headline", ch["share_new"],
          lambda e: f"{e['company']} {e['metric_label']} {e['period']} 占比 {e['share_pct']}%"
                    f"（{e['tier']}）")
    block("占比变动 ≥ 1pct · AI Monetization Share", ch["share_moved"],
          lambda e: f"{e['company']} {e['metric_label']} {e['from']}% → {e['to']}%"
                    f"（{e['delta']:+}pct，{e['period']}·{e['tier']}）")
    block("新入榜 · 表三 Headline Monetization", ch["embedded_new"],
          lambda e: f"{e['company']}({e['ticker']}) {e['tier_label']} {e['period']} "
                    f"总收入同比 {e['revenue_yoy_pct']}%")
    block("档位变动 · 表二", ch["embedded_tier"],
          lambda e: f"{e['company']}({e['ticker']}) {e['from']} → {e['to']}"
                    f"（增速变化 {e['yoy_chg_pts']} 个百分点，{e['period']}）")
    block(f"绝对增速变化 ≥ {args.min_delta}pct · 表二", ch["embedded_growth"],
          lambda e: f"{e['company']}({e['ticker']}) 总收入同比 {e['from']}% → {e['to']}%"
                    f"（{e['delta']:+}pct，绝对增速榜 第{e['rank_from']} → 第{e['rank_to']}）")
    block("趋势变化 · 表四 Usage", ch["usage_trend"],
          lambda e: f"{e['company']} {e['metric_label']} {e['from']} → {e['to']}（{e['period']}）")

    def _shift_suffix(e):
        return "（同期补录，非新财报）" if e.get("same_period") else \
               f"（{e['from_period']} → {e['to_period']}）"

    block("表述强度迁移 · 从模糊走向量化的那一跳", ch["specificity_shift"],
          lambda e: f"{e['company']}({e['ticker']}) {e['from']} → {e['to']}"
                    + ("　↑" if e["direction"] == "up" else "　↓退回") + _shift_suffix(e))
    block("首次给出量化口径", ch["first_quantified"],
          lambda e: f"{e['company']}({e['ticker']}) 首次出现可核对的 AI 数字" + _shift_suffix(e))
    block("表述方向翻转 · 正负调头", ch["polarity_flip"],
          lambda e: f"{e['company']}({e['ticker']}) 主导表述 {e['from']} → {e['to']}" + _shift_suffix(e))
    block("专家口径与管理层说法对不上", ch["expert_contradiction"],
          lambda e: f"{e['ticker']} 本期新增 {e['added']} 处矛盾（累计 {e['total']} 处，"
                    f"最新 {e['latest_period']}）：跑 contradictions 看并排对照")
    block("本周新增专家口径", ch["expert_new"],
          lambda e: f"{e['ticker']} 新增 {e['added']} 条（累计 {e['total']} 条，最新 {e['latest_period']}）")
    block("本周新增管理层原话", ch["new_quotes"],
          lambda e: f"{e['ticker']} 新增 {e['added']} 条（累计 {e['total']} 条）")
    block("本周新增覆盖公司", [{"t": t} for t in ch["new_companies"]], lambda e: e["t"])

    return {"ok": True, "version": VERSION, "baseline": False,
            "against": prev.get("date"), "counts": {k: len(v) for k, v in ch.items()},
            "changes": ch, "message": "\n".join(lines)}


def reconcile_cmd(args):
    """本周应更新 vs 实际入库。差集必须逐家给理由，不允许静默跳过。

    事件驱动的扫描本身没问题——没发财报的公司确实没什么可查。问题是筛选过程
    如果不留痕，「本周应该有 12 家出财报，实际入库 9 家，漏的 3 家是什么原因」
    就答不出来。这条命令把这个问题变成机器可查的。
    """
    expected = [t.strip() for t in (args.expected or "").split(",") if t.strip()]
    if args.expected_file:
        p = _checked(args.expected_file)
        if not p.is_file():
            return {"ok": False, "error": "清单文件不存在: " + str(args.expected_file),
                    "hint": "--expected-file 传一个 JSON 数组或每行一个 ticker 的文本。"}
        raw = p.read_text(encoding="utf-8").strip()
        try:
            data = json.loads(raw)
            expected += [str(x) for x in data] if isinstance(data, list) else []
        except json.JSONDecodeError:
            expected += [ln.strip() for ln in raw.splitlines() if ln.strip()]
    expected = sorted(set(expected))
    if not expected:
        return {"ok": False, "error": "没有给本周应更新清单",
                "hint": "--expected NET,PLTR 或 --expected-file state/ai-app-weekly/temp/ai-app-weekly/due.json。"
                        "清单来自周更第 1 步的财报日历与事件扫描。"}

    if args.against:
        path = db.snapshots_dir() / f"{args.against}.json"
        if not path.exists():
            return {"ok": False, "error": f"找不到快照: {path}",
                    "hint": "先跑 snapshot --write 建立基线。"}
        prev = json.loads(path.read_text(encoding="utf-8"))
    else:
        path, prev = latest_snapshot()
    if prev is None:
        return {"ok": False, "error": "没有对照快照，无法判断本周新增",
                "hint": "第一次跑先 snapshot --write，下一期再对账。"}
    baseline_date = prev.get("date")
    prev_counts = prev.get("coverage_counts", {})
    gaps = db.read_gaps()

    done, explained, unexplained = [], [], []
    for tk in expected:
        now_counts = {dt: len(db.read_company(tk, dt)) for dt in db.DATA_TYPES}
        added = {dt: now_counts[dt] - prev_counts.get(tk, {}).get(dt, 0) for dt in db.DATA_TYPES}
        total_added = sum(v for v in added.values() if v > 0)
        if total_added > 0:
            done.append({"ticker": tk, "added": {k: v for k, v in added.items() if v > 0},
                         "total_added": total_added})
            continue
        fresh = [g for (t, p, dt), g in gaps.items()
                 if t == tk and (g.get("recorded_at") or "") >= baseline_date]
        if fresh:
            explained.append({"ticker": tk,
                              "gaps": [{"period": g.get("period"), "data_type": g.get("data_type"),
                                        "reason": g.get("reason_label"),
                                        "checked_source": g.get("checked_source")} for g in fresh]})
        else:
            unexplained.append(tk)

    lines = [f"本周应更新对账（对照 {baseline_date} 快照；应更新 {len(expected)} 家）",
             f"  已入库：{len(done)} 家",
             f"  已标缺失：{len(explained)} 家",
             f"  未交代：{len(unexplained)} 家"]
    if unexplained:
        lines.append("")
        lines.append("[未交代] 这几家本周应更新，但库里既没有新记录也没有缺口说明：")
        for tk in unexplained:
            lines.append(f"  · {tk}")
        lines.append("  处理方式：补数据，或用 mark-gap 写清查过哪些来源、为什么没有。"
                     "两样都不做就是静默漏掉。")
    if explained:
        lines.append("")
        lines.append("[已标缺失]")
        for e in explained:
            for g in e["gaps"][:3]:
                lines.append(f"  · {e['ticker']} {g['period']} {g['data_type']}：{g['reason']}")
    return {"ok": True, "version": VERSION, "against": baseline_date,
            "expected": expected, "done": done, "explained": explained,
            "unexplained": unexplained,
            "counts": {"expected": len(expected), "done": len(done),
                       "explained": len(explained), "unexplained": len(unexplained)},
            "clean": not unexplained,
            "message": "\n".join(lines)}


# ---------------------------------------------------------------- 深度报告导出

def export_cmd(args):
    """按公司把库里全部记录按时间正序导出，供不定期的深度报告使用。

    公司范围、时间窗、章节结构每次由人指定，这里只负责把底层数据完整交出去。
    """
    tickers, unknown = resolve_tickers(args)
    if tickers is None:
        return {"ok": False, "error": "没有指定范围", "hint": SCOPE_HINT}
    if unknown:
        return {"ok": False, "error": "不在覆盖池: " + "、".join(unknown),
                "hint": "先加进 references/universe.json，或核对代码写法（港股形如 1357.HK）。"}
    since = args.since or "2025Q1"
    until = args.until or default_until()
    uni = {c["ticker"]: c for c in load_universe()["universe"]}
    profiles = db.read_profiles()
    gaps = db.read_gaps()

    def in_window(p):
        return period_key(since) <= period_key(p) <= period_key(until)

    out = []
    for tk in tickers:
        info = uni.get(tk, {})
        funds = [r for r in db.read_company(tk, "fundamentals")
                 if r.get("kind") == "fundamental" and in_window(r.get("period"))]
        attrs = [r for r in db.read_company(tk, "fundamentals")
                 if r.get("kind") == "attribution" and (not r.get("period") or in_window(r["period"]))]
        mets = [r for r in db.read_company(tk, "metrics") if in_window(r.get("period"))]
        quotes = [r for r in db.read_company(tk, "quotes") if in_window(r.get("period"))]
        experts = [r for r in db.read_company(tk, "experts") if in_window(r.get("period"))]
        if not db.is_migrated():
            mets += [r for r in db.read_jsonl(store_path())
                     if r.get("ticker") == tk and in_window(r.get("period"))]
            legacy_f = [r for r in db.read_jsonl(fundamentals_path()) if r.get("ticker") == tk]
            funds += [r for r in legacy_f if r.get("kind") == "fundamental" and in_window(r.get("period"))]
            attrs += [r for r in legacy_f if r.get("kind") == "attribution"]
        funds.sort(key=lambda r: period_key(r.get("period")))
        mets.sort(key=lambda r: (r.get("metric") or "", series_id_of(r), period_key(r.get("period"))))
        attrs.sort(key=lambda r: period_key(r.get("period")))
        quotes.sort(key=lambda r: (period_key(r.get("period")), r.get("venue") or "",
                                   r.get("paragraph_index") if isinstance(r.get("paragraph_index"), int) else 0))
        experts.sort(key=lambda r: (period_key(r.get("period")), r.get("expert_role") or ""))
        company_gaps = [v for (t, p, dt), v in gaps.items() if t == tk]
        out.append({
            "ticker": tk, "company": info.get("name") or tk,
            "company_en": info.get("name_en"), "track": info.get("track"),
            "market": info.get("market"), "ai_purity": info.get("ai_purity"),
            "profile": profiles.get(tk),
            "fundamentals": funds, "metrics": mets, "attributions": attrs, "quotes": quotes,
            "experts": experts,
            "gaps": company_gaps,
            "counts": {"fundamentals": len(funds), "metrics": len(mets),
                       "attributions": len(attrs), "quotes": len(quotes),
                       "experts": len(experts)},
        })

    payload = {"ok": True, "version": VERSION, "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
               "since": since, "until": until, "companies": out,
               "totals": {k: sum(c["counts"][k] for c in out)
                          for k in ("fundamentals", "metrics", "attributions", "quotes", "experts")},
               "no_profile": [c["ticker"] for c in out if not c["profile"]]}

    if args.format == "md":
        md = [f"# AI 应用底库导出 {since}–{until}",
              f"生成时间 {payload['generated_at']}，{len(out)} 家公司。",
              "本文件是深度报告的原料，不是报告本身：数字与原话按时间正序排列，未做筛选。"]
        for c in out:
            md.append("")
            md.append(f"## {c['company']}（{c['ticker']}）")
            p = c["profile"]
            if p:
                md.append(f"**主业**：{p.get('business')}")
                md.append(f"**AI 定位**：{p.get('ai_role')}（纯度 {p.get('ai_purity')}，"
                          f"路由 {p.get('route')}）")
                if p.get("ai_products"):
                    md.append("**AI 产品**：" + "、".join(p["ai_products"]))
            else:
                md.append("**画像缺失**：这家还没有业务画像，判断它属于哪一类之前不要下结论。")
            if c["fundamentals"]:
                md.append("")
                md.append("### 公司级财务")
                for r in c["fundamentals"]:
                    yoy = r.get("revenue_yoy_pct")
                    ma = ""
                    if r.get("includes_ma"):
                        org = r.get("organic_yoy_pct")
                        ma = ("，⚠含重大并表/剥离，同比与上年不可比"
                              + (f"（{r['ma_note']}）" if r.get("ma_note") else "")
                              + (f"；同口径同比 {org}%" if org is not None else "；公司未给同口径同比"))
                    md.append(f"- {r['period']}：总收入 {r.get('total_revenue_usd_mn')}mn USD，"
                              + (f"同比 {yoy}%" if yoy is not None else "同比未披露")
                              + ma
                              + (f"，cRPO 同比 {r['crpo_yoy_pct']}%" if r.get("crpo_yoy_pct") is not None else "")
                              + f"（证据 {r.get('evidence_grade')} 级，{r.get('source_url')}）")
            if c["metrics"]:
                md.append("")
                md.append("### AI 分项与用量指标")
                for r in c["metrics"]:
                    md.append(f"- {r['period']} {ALL_METRICS.get(r.get('metric'), r.get('metric'))}"
                              f"（{series_id_of(r)}）：{r.get('value')}{r.get('unit') or ''}"
                              f" | 口径：{r.get('definition')}"
                              f" | 证据 {r.get('evidence_grade')} 级 | {r.get('source_url')}")
            if c["quotes"]:
                md.append("")
                md.append(f"### 管理层 AI 表述（{len(c['quotes'])} 条，按时间正序）")
                cur_p = None
                for r in c["quotes"]:
                    if r.get("period") != cur_p:
                        cur_p = r.get("period")
                        md.append("")
                        md.append(f"**{cur_p}**")
                    who = " ".join(x for x in (r.get("speaker"), r.get("speaker_role")) if x) or "未署名"
                    tags = "/".join(x for x in (r.get("specificity"), r.get("polarity")) if x) or "未标"
                    md.append(f"- [{db.QUOTE_VENUES.get(r.get('venue'), r.get('venue'))}·{who}·{tags}]")
                    if r.get("analyst_question"):
                        md.append(f"  - 分析师提问：{r['analyst_question']}")
                    md.append(f"  - 「{r.get('quote')}」")
                    md.append(f"  - 来源：{r.get('source_url')}"
                              + (f"（{r.get('source_date')}）" if r.get("source_date") else ""))
            if c["experts"]:
                md.append("")
                md.append(f"### 第三方专家口径（{len(c['experts'])} 条，按时间正序）")
                md.append("管理层是利益相关方；这一节是同一件事的第三方视角。"
                          "两边说法对不上的地方，比任何一边单独说的话都重要。")
                cur_p = None
                for r in c["experts"]:
                    if r.get("period") != cur_p:
                        cur_p = r.get("period")
                        md.append("")
                        md.append(f"**{cur_p}**")
                    md.append(f"- [{r.get('expert_role_label')}·{r.get('alignment_label')}]"
                              f" {r.get('topic') or '未标主题'}")
                    md.append(f"  - 主张：{r.get('claim')}")
                    if r.get("verbatim"):
                        md.append(f"  - 原话：「{r['verbatim']}」")
                    md.append(f"  - {r.get('source_attribution')}")
                contra = [r for r in c["experts"] if r.get("alignment") == "contradicts"]
                if contra:
                    md.append("")
                    md.append(f"**说法对不上 {len(contra)} 处**：写这家之前先把这几处跟同期"
                              "管理层原话并排读一遍。")
            if c["gaps"]:
                md.append("")
                md.append("### 已确认的数据缺口")
                for g in sorted(c["gaps"], key=lambda x: (x.get("period") or "", x.get("data_type"))):
                    md.append(f"- {g.get('period')} {g.get('data_type')}：{g.get('reason_label')}"
                              + (f"（{g.get('checked_source')}）" if g.get("checked_source") else ""))
        text = "\n".join(md)
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2)

    if args.out:
        out_path = _checked(args.out)
        card_io.write_bytes_exclusive(out_path, text.encode("utf-8"))
        payload["out"] = str(out_path)
        payload["message"] = (f"导出 {len(out)} 家 → {out_path}（财务 {payload['totals']['fundamentals']} 条 / "
                              f"指标 {payload['totals']['metrics']} 条 / 原话 {payload['totals']['quotes']} 条 / "
                              f"专家 {payload['totals']['experts']} 条）")
        if payload["no_profile"]:
            payload["message"] += f"；其中 {len(payload['no_profile'])} 家缺业务画像"
        return payload
    payload["message"] = text
    return payload


def migrate_cmd(args):
    try:
        return db.migrate(apply=args.apply)
    except ValueError as exc:
        return {"ok": False, "error": str(exc),
                "hint": "旧事实库里有坏行，修掉再迁移。"}


# ---------------------------------------------------------------- 可视化

def board_cmd(args):
    """把四张表渲染成一张自包含 HTML。不新增取数，全部复用三个排序命令的返回值。"""
    import board_html

    ns = argparse.Namespace
    one = rank(ns(by="yoy", track=args.track, limit=0, min_growth=None, with_price=False))
    two = screen_embedded(ns(tier=None, track=args.track, with_price=False, purity=None))
    three = usage_board(ns(track=args.track))
    for res, name in ((one, "ai-monetization"), (two, "headline-monetization"), (three, "usage")):
        if not res.get("ok"):
            return {"ok": False, "error": f"{name} 失败：{res.get('error')}",
                    "hint": res.get("hint") or "先跑 validate 看库有没有坏行。"}

    health = board_html.health_stats(str(db.db_root()), one, two, three)
    generated_at = datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")
    universe = load_universe()
    payload = board_html.build_payload(
        one, two, three, health, generated_at, VERSION,
        len(universe.get("companies", universe) if isinstance(universe, dict) else universe),
    )
    html = board_html.render_html(payload, title=args.title)

    out = args.out
    if not out:
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        out = f"research-outputs/ai-application-weekly/board-{day}.html"
    out_path = _checked(out)
    card_io.write_bytes_exclusive(out_path, html.encode("utf-8"))

    kb = out_path.stat().st_size / 1024
    lines = [
        f"四表排行榜已渲染：{out}（{kb:.0f} KB，自包含，可离线打开）",
        f"  表一主榜 {len(one['boards']['main'])} 条 · 分流 "
        f"{len(one['boards']['no_growth'])} 算不出增速 / {len(one['boards']['small_base'])} 小基数 / "
        f"{len(one['boards']['caveat'])} 口径存疑 / {len(one['boards']['proxy'])} 代理口径",
        f"  表二占比 {len(one['share_board']['rows'])} 条",
        f"  表三入档 {sum(1 for r in two['results'] if r['tier'] != 'watch')} 家 · 绝对增速榜 {len(two['by_growth'])} 家",
        f"  表四 {sum(len(v) for v in three['groups'].values())} 条口径，其中 {len(three['groups']['unknown'])} 条判不出趋势",
        f"  数据完整度：单点序列 {health['single_point_series']}/{health['series_total']} "
        f"（{health['single_point_series'] / max(health['series_total'], 1) * 100:.0f}%）· "
        f"表二只差归因原话 {len(health['t2_near_miss'])} 家",
    ]
    return {"ok": True, "out": str(out), "size_kb": round(kb, 1),
            "health": {k: v for k, v in health.items()
                       if k not in ("single_point_examples", "coverage", "t2_near_miss")},
            "near_miss": health["t2_near_miss"],
            "message": "\n".join(lines)}


def _feishu_doc_url(record):
    if not isinstance(record, dict):
        return None
    node = record.get("node_token")
    if isinstance(node, str) and _FEISHU_TOKEN_RE.fullmatch(node):
        return "https://feishu.cn/wiki/" + node
    document_id = record.get("document_id")
    if isinstance(document_id, str) and _FEISHU_TOKEN_RE.fullmatch(document_id):
        return "https://feishu.cn/docx/" + document_id
    return None


def _doc_relative(day):
    return f"state/ai-app-weekly/runs/{day}/doc.json"


def _doc_failed_relative(day):
    return f"state/ai-app-weekly/runs/{day}/doc.failed.json"


def _load_json_object(path):
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    return record


def _failed_attempt_state(path):
    if not path.exists():
        return 0, [], []
    current = _load_json_object(path)
    if current is None:
        return None
    try:
        attempts = int(current.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    returns = current.get("returns")
    if not isinstance(returns, list):
        returns = []
    raw_prints = current.get("fingerprints")
    if isinstance(raw_prints, list):
        fingerprints = [item for item in raw_prints if isinstance(item, str)]
    else:
        fingerprints = [
            hashlib.sha256(item.encode("utf-8")).hexdigest()
            for item in returns
            if isinstance(item, str)
        ]
    return attempts, list(returns), fingerprints


def _doc_period_status(day):
    relative = _doc_relative(day)
    path = card_io.workspace_file(db.workspace_root(), relative)
    if path.exists():
        record = _load_json_object(path)
        url = _feishu_doc_url(record)
        if url is None:
            return {"ok": True, "create": False, "exists": False, "stored": False,
                    "reason": "doc_record_unreadable", "path": relative, "url": None,
                    "body_incomplete": False}
        return {"ok": True, "create": False, "exists": True, "stored": True,
                "path": relative, "url": url,
                "body_incomplete": record.get("ok") is not True}
    failed_path = card_io.workspace_file(db.workspace_root(), _doc_failed_relative(day))
    state = _failed_attempt_state(failed_path)
    if state is None:
        return {"ok": True, "create": False, "exists": False, "stored": False,
                "reason": "doc_record_unreadable", "path": relative, "url": None,
                "body_incomplete": False}
    attempts, _returns, _fingerprints = state
    if attempts >= 2:
        return {"ok": True, "create": False, "exists": False, "stored": False,
                "reason": "create_failed_twice", "path": relative, "url": None,
                "attempts": attempts, "body_incomplete": False}
    return {"ok": True, "create": True, "exists": False, "stored": False,
            "path": relative, "url": None, "attempts": attempts,
            "body_incomplete": False}


def doc_status_cmd(args):
    day = args.date or ""
    if not _DATE_RE.fullmatch(day):
        return {"ok": False, "error": "missing or invalid --date",
                "hint": "--date 用 2026-11-13 这种日期。"}
    return _doc_period_status(day)


def _doc_write_block(day):
    """归档和快照写入前的文档闸门。判断与 doc-status 相同。"""
    status = _doc_period_status(day)
    if status.get("ok") is not True:
        return status
    if status.get("stored") and status.get("url") and status.get("body_incomplete") is not True:
        return None
    if status.get("body_incomplete"):
        reason = "body_incomplete"
    elif status.get("reason") in ("doc_record_unreadable", "create_failed_twice"):
        reason = status["reason"]
    elif int(status.get("attempts") or 0) >= 1:
        reason = "create_incomplete"
    else:
        reason = "doc_missing"
    messages = {
        "body_incomplete": (
            "飞书正文未完成",
            "正文未完成时不归档、不写快照。",
        ),
        "doc_record_unreadable": (
            "飞书文档记录不可读",
            "先处理损坏的 doc.json 或 doc.failed.json，再归档或写快照。",
        ),
        "create_failed_twice": (
            "飞书文档创建已失败两次",
            "同一期停止再建，不归档、不写快照。",
        ),
        "create_incomplete": (
            "飞书文档创建尚未完成",
            "创建失败尚未完成时不归档、不写快照。",
        ),
        "doc_missing": (
            "本期没有飞书文档记录",
            "没有完整文档时不归档、不写快照。无变化短消息写快照时加 --no-doc。",
        ),
    }
    error, hint = messages[reason]
    payload = {"ok": False, "error": error, "reason": reason, "hint": hint,
               "path": status.get("path")}
    if "attempts" in status:
        payload["attempts"] = status["attempts"]
    return payload


def _record_doc_failure(day, raw):
    relative = _doc_failed_relative(day)
    path = card_io.workspace_file(db.workspace_root(), relative)
    state = _failed_attempt_state(path)
    if state is None:
        return {"ok": False, "stored": False, "create": False,
                "reason": "doc_record_unreadable", "path": relative}
    attempts, returns, fingerprints = state
    digest = hashlib.sha256(raw).hexdigest()
    if digest in fingerprints:
        payload = {
            "ok": True,
            "stored": False,
            "skipped": True,
            "duplicate": True,
            "create": attempts < 2,
            "path": relative,
            "url": None,
            "attempts": attempts,
            "body_incomplete": False,
        }
        if attempts >= 2:
            payload["reason"] = "create_failed_twice"
        return payload
    attempts += 1
    returns.append(raw.decode("utf-8", "replace"))
    fingerprints.append(digest)
    card_io.write_bytes_exclusive(
        path,
        json.dumps(
            {"attempts": attempts, "returns": returns, "fingerprints": fingerprints},
            ensure_ascii=False,
        ).encode("utf-8"),
    )
    payload = {
        "ok": True,
        "stored": True,
        "skipped": False,
        "create": attempts < 2,
        "path": relative,
        "url": None,
        "attempts": attempts,
        "body_incomplete": False,
    }
    if attempts >= 2:
        payload["reason"] = "create_failed_twice"
    return payload


def save_doc_cmd(args):
    day = args.date or ""
    if not _DATE_RE.fullmatch(day):
        return {"ok": False, "error": "missing or invalid --date",
                "hint": "--date 用 2026-11-13 这种日期。"}
    if not getattr(args, "file", None):
        return {"ok": False, "error": "missing --file",
                "hint": "--file 传工作区内的飞书工具返回 JSON。"}
    src = _checked(args.file)
    if not src.is_file():
        return {"ok": False, "error": "file not found",
                "hint": "--file 传工作区内的飞书工具返回 JSON。"}
    relative = _doc_relative(day)
    dest = card_io.workspace_file(db.workspace_root(), relative)
    if dest.exists():
        return {"ok": True, "stored": False, "skipped": True, "create": False,
                "path": relative}
    raw = src.read_bytes()
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "stored": False, "error": "invalid json",
                "hint": "--file 传飞书工具返回的 JSON 对象。"}
    if not isinstance(record, dict):
        return {"ok": False, "stored": False, "error": "invalid json",
                "hint": "--file 传飞书工具返回的 JSON 对象。"}
    url = _feishu_doc_url(record)
    if url is None:
        return _record_doc_failure(day, raw)
    card_io.write_bytes_exclusive(dest, raw)
    return {"ok": True, "stored": True, "skipped": False, "create": False,
            "path": relative, "url": url,
            "body_incomplete": record.get("ok") is not True}


def archive_report_cmd(args):
    day = args.date or ""
    if not _DATE_RE.fullmatch(day):
        return {"ok": False, "error": "missing or invalid --date",
                "hint": "--date 用 2026-11-13 这种日期。"}
    if not getattr(args, "file", None):
        return {"ok": False, "error": "missing --file",
                "hint": "--file 传工作区内的周报 Markdown。"}
    src = _checked(args.file)
    if not src.is_file():
        return {"ok": False, "error": "file not found",
                "hint": "--file 传工作区内的周报 Markdown。"}
    blocked = _doc_write_block(day)
    if blocked is not None:
        return blocked
    relative = f"research-outputs/ai-application-weekly/{day}.md"
    dest = card_io.workspace_file(db.workspace_root(), relative)
    card_io.write_bytes_exclusive(dest, src.read_bytes())
    return {"ok": True, "archived": True, "path": relative}


def main(argv=None):
    try:
        workspace_value, rest = _take_workspace_dir(argv)
    except ValueError as exc:
        emit({"ok": False, "error": str(exc)}, True)
        return 1
    parser = argparse.ArgumentParser(description="AI application weekly monitor helper")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("self-test")

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--as-of")
    p_plan.add_argument("--days", type=int, default=7)

    p_win = sub.add_parser("window")
    p_win.add_argument("--as-of")
    p_win.add_argument("--days", type=int, default=7)

    p_uni = sub.add_parser("universe")
    p_uni.add_argument("--track")
    p_uni.add_argument("--market")
    p_uni.add_argument("--scan-tier", type=int, choices=[1, 2],
                       help="1=核心池每周全跑，2=观察池事件触发")
    p_uni.add_argument("--purity", action="append", choices=["native", "embedded", "adjacent"],
                       help="按业务纯度过滤，可重复")

    p_ret = sub.add_parser("retier", help="按数据重算 scan_tier，只试算")
    p_ret.add_argument("--apply", action="store_true",
                       help="已拒绝。覆盖池调整改仓库里的 references/universe.json")

    p_scr = sub.add_parser("screen")
    p_scr.add_argument("--tier", action="append", help="只保留指定分档，可重复")
    p_scr.add_argument("--track")
    p_scr.add_argument("--with-price", action="store_true", help="附 3M/12M 涨跌幅判断是否已定价")
    p_scr.add_argument("--primary-only", action="store_true", help="同一公司同一指标只保留主序列")

    p_rank = sub.add_parser("ai-monetization", aliases=["rank"],
                            help="表一 AI Monetization：AI 分项收入/ARR/bookings 增速榜，"
                                 "附 表二 AI Monetization as %% of Headline 占比")
    p_rank.add_argument("--by", choices=["yoy", "qoq"], default="yoy")
    p_rank.add_argument("--track")
    p_rank.add_argument("--limit", type=int, default=0, help="每个榜单取前 N 条，0 = 全量")
    p_rank.add_argument("--min-growth", type=float, default=None, help="只保留增速 ≥ 该值的条目")
    p_rank.add_argument("--with-price", action="store_true")

    p_emb = sub.add_parser("headline-monetization", aliases=["screen-embedded"],
                           help="表三 Headline Monetization：公司级总收入，按同比增速的变化分档 + 绝对增速排名")
    p_emb.add_argument("--tier", action="append",
                       help="只保留指定分档：accelerating / native_accel / high_absolute / "
                            "high_plateau / eroding / insufficient / watch")
    p_emb.add_argument("--track")
    p_emb.add_argument("--with-price", action="store_true")
    p_emb.add_argument("--purity", action="append",
                       choices=["native", "embedded", "adjacent"],
                       help="按业务纯度过滤，可重复")

    p_cov = sub.add_parser("coverage", help="核心池逐家的通道归属，看谁两条通道都没数据")
    p_cov.add_argument("--track")

    p_fund = sub.add_parser("add-fundamental", help="入库公司级季度财务（通道二输入）")
    p_fund.add_argument("--company", required=True)
    p_fund.add_argument("--ticker", required=True)
    p_fund.add_argument("--track", required=True,
                        choices=["toc_internet", "tob_software", "ai_for_science"])
    p_fund.add_argument("--period", required=True)
    p_fund.add_argument("--total-revenue-usd-mn", type=float,
                        help="折算成美元的总收入。排序与闸门只认这一列")
    p_fund.add_argument("--currency", default="",
                        help="公司列报币种，如 CNY / USD / HKD。原币金额配 --total-revenue-orig-mn")
    p_fund.add_argument("--total-revenue-orig-mn", type=float,
                        help="原币金额（百万），只用于显示与回溯，不参与排序")
    p_fund.add_argument("--revenue-yoy-pct", type=float,
                        help="公司披露的总收入同比，优先直接抄新闻稿，不要自己拆季度算")
    p_fund.add_argument("--includes-ma", action="store_true",
                        help="本期 as-reported 同比含重大并表/剥离，与上年同期不可比。"
                             "并表完成当季起连续四期都要标，直到上年基数也含该并表")
    p_fund.add_argument("--organic-yoy-pct", type=float,
                        help="同口径（有机）总收入同比，只抄公司披露的 pro-forma / 剔除并表口径，"
                             "自己估的不要填。给了这个，通道二用它判档")
    p_fund.add_argument("--ma-note", default="",
                        help="并购/剥离事由与完成日期，标 --includes-ma 时必填")
    p_fund.add_argument("--crpo-yoy-pct", type=float)
    p_fund.add_argument("--rpo-yoy-pct", type=float)
    p_fund.add_argument("--nrr-pct", type=float)
    p_fund.add_argument("--large-customers", type=float)
    p_fund.add_argument("--evidence-grade", required=True)
    p_fund.add_argument("--source-type", default="")
    p_fund.add_argument("--source-url", required=True)
    p_fund.add_argument("--source-date", default="")
    p_fund.add_argument("--note", default="")
    p_fund.add_argument("--quote", default="")
    p_fund.add_argument("--allow-duplicate", action="store_true")

    p_attr = sub.add_parser("add-attribution", help="入库管理层把增长归到 AI 上的原话")
    p_attr.add_argument("--company", required=True)
    p_attr.add_argument("--ticker", required=True)
    p_attr.add_argument("--track", required=True,
                        choices=["toc_internet", "tob_software", "ai_for_science"])
    p_attr.add_argument("--period", default="")
    p_attr.add_argument("--attribution-type", required=True,
                        choices=sorted(ATTRIBUTION_TYPES))
    p_attr.add_argument("--quote", required=True)
    p_attr.add_argument("--evidence-grade", required=True)
    p_attr.add_argument("--source-type", default="")
    p_attr.add_argument("--source-url", required=True)
    p_attr.add_argument("--source-date", default="")
    p_attr.add_argument("--note", default="")
    # 这条记录会同步镜像成一条管理层原话，下面四个字段决定镜像出来的那条长什么样。
    p_attr.add_argument("--venue", choices=sorted(db.QUOTE_VENUES), default="")
    p_attr.add_argument("--speaker", default="")
    p_attr.add_argument("--speaker-role", default="")
    p_attr.add_argument("--specificity", choices=sorted(db.QUOTE_SPECIFICITY), default="")

    p_add = sub.add_parser("add-metric")
    p_add.add_argument("--company", required=True)
    p_add.add_argument("--ticker", required=True)
    p_add.add_argument("--track", required=True,
                       choices=["toc_internet", "tob_software", "ai_for_science"])
    p_add.add_argument("--metric", required=True)
    p_add.add_argument("--period", required=True)
    p_add.add_argument("--value", type=float)
    p_add.add_argument("--unit", default="")
    p_add.add_argument("--value-usd-mn", type=float)
    p_add.add_argument("--arr-usd-mn", type=float)
    p_add.add_argument("--revenue-share-pct", type=float)
    p_add.add_argument("--yoy-pct", type=float)
    p_add.add_argument("--qoq-pct", type=float)
    p_add.add_argument("--definition", required=True)
    p_add.add_argument("--series-id", default="default",
                       help="同一公司同一指标下的口径序列名，例如 ai_applications / ai_cloud_infra；默认 default")
    p_add.add_argument("--definition-changed", action="store_true")
    p_add.add_argument("--ai-basis", choices=sorted(AI_BASIS),
                       help="AI 归属依据：这笔钱凭什么算 AI 收入。财务与比率指标必填。"
                            + "；".join(f"{k}={v['desc']}" for k, v in AI_BASIS.items()))
    p_add.add_argument("--value-is-bound", action="store_true",
                       help="数值是「超过 / 接近 / 约」这类下限或近似值，不参与 computed 增速")
    p_add.add_argument("--primary-series", action="store_true",
                       help="同一公司同一指标有多条序列时，指定本序列进主表")
    p_add.add_argument("--proxy-only", action="store_true",
                       help="财务口径但不计入主表（例如存量订阅重贴标签），只作拐点观察")
    p_add.add_argument("--proxy-only-reason", default="")
    p_add.add_argument("--allow-duplicate", action="store_true",
                       help="明知同键仍要追加（更正历史记录时用）")
    p_add.add_argument("--related-party", action="store_true")
    p_add.add_argument("--one-off", action="store_true")
    p_add.add_argument("--evidence-grade", required=True)
    p_add.add_argument("--source-type", default="")
    p_add.add_argument("--source-url", required=True)
    p_add.add_argument("--source-date", default="")
    p_add.add_argument("--note", default="")
    p_add.add_argument("--quote", default="",
                       help="来源原文里含该数字的那句话，逐字。缺 quote 的记录不可追溯，回填时必填。")

    p_val = sub.add_parser("validate")
    p_val.add_argument("--file", required=True)

    # ---------------- 底层数据库

    p_mig = sub.add_parser("migrate-db", help="把旧的两个平铺 jsonl 导入按公司分片的库")
    p_mig.add_argument("--apply", action="store_true", help="不传只试算，不写文件")

    p_usage = sub.add_parser("usage", aliases=["usage-board"],
                             help="表四 Usage：个性化用量，不排名次只分组")
    p_usage.add_argument("--track")

    p_prof = sub.add_parser("add-profile", help="入库业务画像（决定这家走哪张排序表）")
    p_prof.add_argument("--ticker", required=True)
    p_prof.add_argument("--company", default="")
    p_prof.add_argument("--track", default="",
                        choices=["", "toc_internet", "tob_software", "ai_for_science"])
    p_prof.add_argument("--purity", required=True, choices=sorted(AI_PURITY_LEVELS))
    p_prof.add_argument("--route", required=True, choices=sorted(db.PROFILE_ROUTES))
    p_prof.add_argument("--route-reason", default="")
    p_prof.add_argument("--business", required=True, help="主业是什么，一到三句")
    p_prof.add_argument("--ai-role", required=True, help="AI 在这门生意里处在什么位置")
    p_prof.add_argument("--ai-products", default="", help="点名的 AI 产品，逗号分隔")
    p_prof.add_argument("--ai-line-item", action="store_true", help="财报单列 AI 分项收入")
    p_prof.add_argument("--usage-metric", action="store_true", help="披露 AI 用量口径")
    p_prof.add_argument("--transcript-available", action="store_true", help="能拿到电话会转写")
    p_prof.add_argument("--evidence-quote", default="")
    p_prof.add_argument("--evidence-url", required=True)
    p_prof.add_argument("--evidence-date", default="")
    p_prof.add_argument("--note", default="")
    p_prof.add_argument("--allow-off-universe", action="store_true")

    p_profs = sub.add_parser("profiles", help="画像覆盖情况；带 --ticker 看单家")
    p_profs.add_argument("--ticker")

    p_q = sub.add_parser("add-quote", help="入库管理层原话（全量语料，抽取不做主观筛选）")
    p_q.add_argument("--company", required=True)
    p_q.add_argument("--ticker", required=True)
    p_q.add_argument("--track", required=True,
                     choices=["toc_internet", "tob_software", "ai_for_science"])
    p_q.add_argument("--period", required=True)
    p_q.add_argument("--venue", required=True, choices=sorted(db.QUOTE_VENUES))
    p_q.add_argument("--speaker", required=True, help="讲话人；来源没署名就写 未署名")
    p_q.add_argument("--speaker-role", default="")
    p_q.add_argument("--analyst-question", default="", help="Q&A 里这段回答对应的分析师提问")
    p_q.add_argument("--analyst", default="")
    p_q.add_argument("--quote", required=True, help="完整段落，逐字，不要切成单句")
    p_q.add_argument("--language", default="")
    p_q.add_argument("--anchor", action="append", choices=sorted(db.QUOTE_ANCHORS),
                     help="收录判据，至少一条；只有立场没有对象的话不入库")
    p_q.add_argument("--specificity", required=True, choices=sorted(db.QUOTE_SPECIFICITY))
    p_q.add_argument("--polarity", required=True, choices=sorted(db.QUOTE_POLARITY),
                     help="结果为负或为零同样入库")
    p_q.add_argument("--attribution-type", default="", choices=[""] + sorted(ATTRIBUTION_TYPES))
    p_q.add_argument("--topics", default="")
    p_q.add_argument("--evidence-grade", required=True)
    p_q.add_argument("--source-type", default="")
    p_q.add_argument("--source-url", required=True)
    p_q.add_argument("--source-date", default="")
    p_q.add_argument("--transcript-file", default="")
    p_q.add_argument("--paragraph-index", type=int)
    p_q.add_argument("--note", default="")
    p_q.add_argument("--allow-duplicate", action="store_true")

    p_ql = sub.add_parser("quotes", help="按时间正序取一家公司的全部原话")
    p_ql.add_argument("--ticker", required=True)
    p_ql.add_argument("--since")
    p_ql.add_argument("--until")
    p_ql.add_argument("--venue", action="append", choices=sorted(db.QUOTE_VENUES))
    p_ql.add_argument("--polarity", action="append", choices=sorted(db.QUOTE_POLARITY))

    p_tr = sub.add_parser("add-transcript", help="原始转写留档，供以后换抽取标准重跑")
    p_tr.add_argument("--ticker", required=True)
    p_tr.add_argument("--period", required=True)
    p_tr.add_argument("--file", required=True)
    p_tr.add_argument("--source-url", default="")
    p_tr.add_argument("--source-date", default="")
    p_tr.add_argument("--overwrite", action="store_true")

    p_gap = sub.add_parser("mark-gap", help="标记确认缺失的数据格，避免静默漏掉")
    p_gap.add_argument("--ticker", required=True)
    p_gap.add_argument("--company", default="")
    p_gap.add_argument("--period", default="")
    p_gap.add_argument("--data-type", required=True,
                       choices=list(db.DATA_TYPES) + ["profile"])
    p_gap.add_argument("--reason", required=True, choices=sorted(db.GAP_REASONS))
    p_gap.add_argument("--checked-source", default="", help="查过哪些来源才判定缺失")
    p_gap.add_argument("--note", default="")

    p_led = sub.add_parser("coverage-ledger", help="逐家逐季的补全台账，跑完对账用")
    p_led.add_argument("--since", default="2025Q1")
    p_led.add_argument("--until")
    p_led.add_argument("--as-of")
    p_led.add_argument("--tickers")
    p_led.add_argument("--track")
    p_led.add_argument("--purity", action="append", choices=["native", "embedded", "adjacent"])
    p_led.add_argument("--scan-tier", type=int, choices=[1, 2])
    p_led.add_argument("--all", action="store_true")
    p_led.add_argument("--write", action="store_true", help="写入 state/ai_app_db/coverage.json")

    p_bf = sub.add_parser("backfill-plan", help="把待补的格子切成并行批次")
    p_bf.add_argument("--layer", choices=["profile", "company", "expert"], default="company",
                      help="profile=画像层；company=一期核心三类；expert=二期专家层")
    p_bf.add_argument("--batch-size", type=int, default=12)
    p_bf.add_argument("--since", default="2025Q1")
    p_bf.add_argument("--until")
    p_bf.add_argument("--tickers")
    p_bf.add_argument("--track")
    p_bf.add_argument("--purity", action="append", choices=["native", "embedded", "adjacent"])
    p_bf.add_argument("--scan-tier", type=int, choices=[1, 2])

    p_exp_add = sub.add_parser("add-expert", help="入库第三方专家口径（Guidepoint 等）")
    p_exp_add.add_argument("--company", required=True)
    p_exp_add.add_argument("--ticker", required=True)
    p_exp_add.add_argument("--track", required=True,
                           choices=["toc_internet", "tob_software", "ai_for_science"])
    p_exp_add.add_argument("--period", required=True, help="与财务和原话对齐，形如 2025Q1")
    p_exp_add.add_argument("--expert-role", required=True, choices=sorted(db.EXPERT_ROLES))
    p_exp_add.add_argument("--expert-background", default="", help="专家背景，不写姓名")
    p_exp_add.add_argument("--topic", default="", help="这条讲的是哪个业务环节")
    p_exp_add.add_argument("--claim", required=True, help="用自己的话写清主张，不要整段拷贝正文")
    p_exp_add.add_argument("--verbatim", default="",
                           help=f"逐字引用，不超过 {db.EXPERT_VERBATIM_WORD_LIMIT} 词")
    p_exp_add.add_argument("--polarity", choices=sorted(db.QUOTE_POLARITY), default=None)
    p_exp_add.add_argument("--alignment", required=True, choices=sorted(db.EXPERT_ALIGNMENT),
                           help="与同期管理层表述的关系；contradicts 是这层最值钱的产出")
    p_exp_add.add_argument("--management-ref", default="",
                           help="判 contradicts 时必填：对应管理层原话的 fingerprint 或期间+要点")
    p_exp_add.add_argument("--provider", default="guidepoint")
    p_exp_add.add_argument("--source-id", default="", help="提供方的文档 id")
    p_exp_add.add_argument("--source-url", default="")
    p_exp_add.add_argument("--source-date", default="")
    p_exp_add.add_argument("--source-attribution", required=True,
                           help="提供方给的 source_attribution.markdown 原文，引用必须带")
    p_exp_add.add_argument("--evidence-grade", required=True, choices=["A", "B", "C"])
    p_exp_add.add_argument("--note", default="")
    p_exp_add.add_argument("--allow-duplicate", action="store_true")

    p_exps = sub.add_parser("experts", help="按时间正序取一家公司的全部专家口径")
    p_exps.add_argument("--ticker", required=True)
    p_exps.add_argument("--since")
    p_exps.add_argument("--until")
    p_exps.add_argument("--alignment", action="append", choices=sorted(db.EXPERT_ALIGNMENT))

    p_con = sub.add_parser("contradictions", help="管理层与专家说法对不上的地方，并排贴出来")
    p_con.add_argument("--tickers")
    p_con.add_argument("--track")
    p_con.add_argument("--purity", action="append", choices=["native", "embedded", "adjacent"])
    p_con.add_argument("--scan-tier", type=int, choices=[1, 2])
    p_con.add_argument("--all", action="store_true")
    p_con.add_argument("--since", default="2025Q1")

    p_rec = sub.add_parser("reconcile", help="本周应更新 vs 实际入库，差集必须逐家给理由")
    p_rec.add_argument("--expected", help="逗号分隔的本周应更新清单")
    p_rec.add_argument("--expected-file", help="JSON 数组或每行一个 ticker 的文本")
    p_rec.add_argument("--against", help="对照的快照日期 YYYY-MM-DD，默认取最近一份")

    p_snap = sub.add_parser("snapshot", help="当期状态快照，供下一期比对")
    p_snap.add_argument("--write", action="store_true")
    p_snap.add_argument("--overwrite", action="store_true")
    p_snap.add_argument("--no-doc", action="store_true",
                        help="无变化且该期没有任何飞书记录时才加。仅 reason 为 doc_missing 时写入快照")
    p_snap.add_argument("--as-of")
    p_snap.add_argument("--full", action="store_true", help="JSON 里带上完整快照内容")

    p_diff = sub.add_parser("diff", help="本周变化，放在周报开头")
    p_diff.add_argument("--against", help="对照的快照日期 YYYY-MM-DD，默认取最近一份")
    p_diff.add_argument("--min-delta", type=float, default=5.0,
                        help="增速变化超过多少个百分点才报，默认 5")
    p_diff.add_argument("--limit", type=int, default=0, help="每类最多列几条，0 = 全列")

    p_exp = sub.add_parser("export", help="按公司导出底库全量记录，供深度报告使用")
    p_exp.add_argument("--tickers", help="逗号分隔，例如 NET,PLTR")
    p_exp.add_argument("--track")
    p_exp.add_argument("--purity", action="append", choices=["native", "embedded", "adjacent"])
    p_exp.add_argument("--scan-tier", type=int, choices=[1, 2])
    p_exp.add_argument("--all", action="store_true", help="全池导出")
    p_exp.add_argument("--since", default="2025Q1")
    p_exp.add_argument("--until")
    p_exp.add_argument("--format", choices=["json", "md"], default="json")
    p_exp.add_argument("--out", help="写到文件，不传则打到标准输出")

    p_board = sub.add_parser("board", help="四表排行榜可视化：渲染成一张自包含 HTML，可离线归档")
    p_board.add_argument("--out", help="输出路径，默认 research-outputs/ai-application-weekly/board-YYYY-MM-DD.html")
    p_board.add_argument("--track", help="只渲染一条赛道")
    p_board.add_argument("--title", default="AI 应用四表排行榜")

    p_doc = sub.add_parser("doc-status", help="飞书文档记录是否已存在")
    p_doc.add_argument("--date", required=True)

    p_save = sub.add_parser("save-doc", help="保存飞书工具返回；记录已存在则不覆盖")
    p_save.add_argument("--date", required=True)
    p_save.add_argument("--file", required=True)

    p_arch = sub.add_parser("archive-report", help="把周报复制到归档目录")
    p_arch.add_argument("--date", required=True)
    p_arch.add_argument("--file", required=True)

    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 2
    try:
        bind_workspace(workspace_value)
        if args.command == "self-test":
            result = self_test()
        elif args.command == "plan":
            result = build_plan(args)
        elif args.command == "window":
            result = {"ok": True, "window": window(args.as_of, args.days)}
        elif args.command == "universe":
            result = universe_cmd(args)
        elif args.command == "screen":
            result = screen(args)
        elif args.command in ("ai-monetization", "rank"):
            result = rank(args)
        elif args.command in ("headline-monetization", "screen-embedded"):
            result = screen_embedded(args)
        elif args.command == "retier":
            result = retier(args)
        elif args.command == "coverage":
            result = coverage(args)
        elif args.command == "add-fundamental":
            result = add_fundamental(args)
        elif args.command == "add-attribution":
            result = add_attribution(args)
        elif args.command == "add-metric":
            result = add_metric(args)
        elif args.command == "migrate-db":
            result = migrate_cmd(args)
        elif args.command in ("usage", "usage-board"):
            result = usage_board(args)
        elif args.command == "add-profile":
            result = add_profile(args)
        elif args.command == "profiles":
            result = profiles_cmd(args)
        elif args.command == "add-quote":
            result = add_quote(args)
        elif args.command == "quotes":
            result = quotes_cmd(args)
        elif args.command == "add-transcript":
            result = add_transcript(args)
        elif args.command == "mark-gap":
            result = mark_gap(args)
        elif args.command == "coverage-ledger":
            result = coverage_ledger(args)
        elif args.command == "backfill-plan":
            result = backfill_plan(args)
        elif args.command == "add-expert":
            result = add_expert(args)
        elif args.command == "experts":
            result = experts_cmd(args)
        elif args.command == "contradictions":
            result = contradictions_cmd(args)
        elif args.command == "reconcile":
            result = reconcile_cmd(args)
        elif args.command == "snapshot":
            result = snapshot_cmd(args)
        elif args.command == "diff":
            result = diff_cmd(args)
        elif args.command == "export":
            result = export_cmd(args)
        elif args.command == "board":
            result = board_cmd(args)
        elif args.command == "validate":
            result = validate(args.file)
        elif args.command == "doc-status":
            result = doc_status_cmd(args)
        elif args.command == "save-doc":
            result = save_doc_cmd(args)
        elif args.command == "archive-report":
            result = archive_report_cmd(args)
        else:
            result = {"ok": False, "error": "unknown command"}
    except FileNotFoundError as exc:
        result = {"ok": False, "error": str(exc),
                  "hint": "确认 skill 目录完整（references/universe.json、templates/weekly-report.md）。"}
    except (ValueError, RuntimeError) as exc:
        result = {"ok": False, "error": str(exc)}
    emit(result, args.json)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
