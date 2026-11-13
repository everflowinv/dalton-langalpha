#!/usr/bin/env python3
"""AI 应用底层数据库：按公司分片的存储层。

平铺 jsonl 在全量回填之后会长成单文件几万行、几十 MB，并行回填还会互相踩写。
这里把事实库切成 state/ai_app_db/companies/<TICKER>/ 三个分片：每个回填批次只碰
自己那几家的目录，返工只影响一家，深度报告只读点名的那几家。

目录布局：

    state/ai_app_db/
      profiles.jsonl                        业务画像，一家一条，全池覆盖
      companies/<TICKER>/fundamentals.jsonl 公司级季度财务 + AI 归因（kind 区分）
      companies/<TICKER>/metrics.jsonl      AI 分项与用量指标
      companies/<TICKER>/quotes.jsonl       管理层原话全量语料
      companies/<TICKER>/experts.jsonl      专家访谈口径（Guidepoint 等第三方）
      transcripts/<TICKER>/<PERIOD>.txt     原始转写留档
      coverage_gaps.jsonl                   逐家逐季的缺口说明
      coverage.json                         补全台账，由 coverage-ledger 生成
      snapshots/YYYY-MM-DD.json             周报快照，供「本周变化」比对

留转写原文是为了以后换一套抽取标准时不必重拉 AlphaEngine：quotes 每条带
transcript_file 与段落序号，能一路回指原文。transcripts/ 不进 git。
"""
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Hong_Kong")

SHARDS = {
    "metrics": "metrics.jsonl",
    "fundamentals": "fundamentals.jsonl",
    "quotes": "quotes.jsonl",
    "experts": "experts.jsonl",
}

# 一家一期的四类事实。台账按这四类建格，缺一类就是一个空格子。
DATA_TYPES = ("fundamentals", "metrics", "quotes", "experts")

# 原话的场合。分析师追问出来的那句比新闻稿那句有信息量，写报告时要能区分。
QUOTE_VENUES = {
    "call_qa": "业绩会 Q&A",
    "call_prepared": "业绩会 prepared remarks",
    "press_release": "业绩新闻稿",
    "investor_day": "投资者日 / 分析师日",
    "filing": "定期报告或公告正文",
    "interview": "公开访谈或行业会议",
    "other": "其他公开场合",
}

# 收录判据：这段话有没有「对象」。只有立场没有对象的（AI is important）不入库。
# 判据只管收不收，不管重不重要；强度标签照打，但不参与过滤。
QUOTE_ANCHORS = {
    "product": "点名了具体的 AI 产品、功能或模块",
    "workflow": "说清 AI 用在哪个业务环节、产品线或客户群",
    "result": "给了结果，定量或定向都算（收入、渗透率、续约率、成本、效率）",
    "customer": "举了具体客户或部署案例",
    "commitment": "承诺了有细节的动作（时间表、投入规模、定价方式、目标）",
}

QUOTE_SPECIFICITY = {
    "vague": "只有方向，没有量",
    "directional": "说清了影响哪个指标，但没给数",
    "quantified": "给了可核对的数字",
}

# 结果为负或为零同样入库。「还没看到 AI 对定价产生影响」有对象、有结果，
# 只是结果是负的，它对判断谁在讲故事、谁在做生意的价值不低于正面表述。
QUOTE_POLARITY = {
    "positive": "AI 正在带来增量",
    "neutral": "陈述现状，未给方向",
    "negative": "AI 尚未见效、进展慢于预期，或正在侵蚀原有收入",
}

# ------------------------------------------------------------ 专家访谈层
# 管理层是利益相关方，说自己的 AI 时天然乐观。第三方从业者看到的是同一件事的另一面。
# 两边都在库里、能按公司按季度对齐，才能做出「说法对不上」这个判断——
# 它比任何一边单独说的话都重要。

EXPERT_ROLES = {
    "former_employee": "前员工，看内部执行与产品真实进度",
    "channel_partner": "渠道 / 经销 / 集成商，看订单与部署落地",
    "customer": "客户侧采购或使用方，看付费意愿与续约",
    "competitor": "竞争对手，看份额与打法",
    "supplier": "供应商，看上游拉货与产能",
    "industry_consultant": "行业顾问 / 研究方，看整体渗透与周期",
    "other": "其他第三方视角",
}

# 与同期管理层表述的关系。矛盾这一档是这层最值钱的产出。
EXPERT_ALIGNMENT = {
    "confirms": "与管理层同期表述一致",
    "contradicts": "与管理层同期表述相反或明显更弱",
    "extends": "补充了管理层没讲的细节，方向不冲突",
    "unrelated": "谈的不是管理层讲过的那件事",
    "no_management_claim": "同期没有可比的管理层表述",
}

# Guidepoint 合规：verbatim 引用不超过 20 词，且必须带来源署名。
EXPERT_VERBATIM_WORD_LIMIT = 20

PROFILE_ROUTES = {
    "channel_one": "单列 AI 分项数字，走表一 财务口径",
    "channel_two": "不单列 AI 分项，走表二 公司级加速",
    "usage_only": "只有用量口径，走表三 个性化用量",
    "insufficient": "披露不足，两条通道都进不去，按数据不足处理",
}

GAP_REASONS = {
    "no_transcript": "无电话会转写",
    "no_expert_coverage": "第三方专家库没有覆盖这家这一期",
    "no_chinese_minutes": "只有中文纪要或无规范纪要",
    "not_disclosed": "该期未单独披露",
    "no_earnings_call": "该市场不开业绩会",
    "not_listed": "该期尚未上市",
    "not_reported_yet": "该期财报尚未发布",
    "source_unavailable": "来源暂时取不到，需重试",
}

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


_workspace = None


def set_workspace_root(path):
    """记住 CLI 传入的工作区。不解析符号链接，也不读环境变量猜位置。"""
    global _workspace
    _workspace = Path(path)
    return _workspace


def workspace_root():
    if _workspace is None:
        raise RuntimeError("missing --workspace-dir")
    return _workspace


def workspace_checked(path):
    """把绝对路径折回工作区相对路径，再做越界和符号链接检查。"""
    root = workspace_root()
    raw = Path(path)
    if raw.is_absolute():
        try:
            relative = raw.relative_to(root)
        except ValueError as exc:
            raise ValueError("path must stay inside the workspace") from exc
    else:
        relative = raw
    import card_io

    return card_io.workspace_file(root, relative.as_posix())


def db_root():
    return workspace_root() / "state" / "ai_app_db"


def legacy_metrics_path():
    return workspace_root() / "state" / "ai_app_metrics.jsonl"


def legacy_fundamentals_path():
    return workspace_root() / "state" / "ai_app_fundamentals.jsonl"


def companies_dir():
    return db_root() / "companies"


def transcripts_dir():
    return db_root() / "transcripts"


def snapshots_dir():
    return db_root() / "snapshots"


def profiles_path():
    return db_root() / "profiles.jsonl"


def gaps_path():
    return db_root() / "coverage_gaps.jsonl"


def coverage_path():
    return db_root() / "coverage.json"


def migration_marker():
    return db_root() / ".migrated.json"


def ticker_slug(ticker):
    slug = _SAFE.sub("_", (ticker or "").strip())
    if not slug:
        raise ValueError("ticker 不能为空")
    return slug


def company_dir(ticker):
    return companies_dir() / ticker_slug(ticker)


def shard_path(ticker, kind):
    if kind not in SHARDS:
        raise ValueError(f"未知分片: {kind}")
    return company_dir(ticker) / SHARDS[kind]


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p}:{i} 不是合法 JSON: {exc}") from exc
    return rows


def append_jsonl(path, rec):
    """带文件锁的单行追加。并行回填按公司分目录，这里再兜一层。"""
    checked = workspace_checked(path)
    line = (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")
    import card_io

    card_io.append_bytes_nofollow(checked, line)
    return checked


def is_migrated():
    return migration_marker().exists()


def known_tickers():
    root = companies_dir()
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


def read_company(ticker, kind):
    return read_jsonl(shard_path(ticker, kind))


def read_all(kind, tickers=None):
    """读全库。迁移前回落到旧的平铺文件，迁移后只读分片。"""
    rows = []
    if not is_migrated() and kind in ("metrics", "fundamentals"):
        legacy = legacy_metrics_path() if kind == "metrics" else legacy_fundamentals_path()
        rows.extend(read_jsonl(legacy))
    names = [ticker_slug(t) for t in tickers] if tickers else known_tickers()
    for name in names:
        rows.extend(read_jsonl(companies_dir() / name / SHARDS[kind]))
    return rows


def read_profiles(tickers=None):
    rows = read_jsonl(profiles_path())
    latest = {}
    for r in rows:
        tk = r.get("ticker")
        if not tk:
            continue
        cur = latest.get(tk)
        if cur is None or (r.get("recorded_at") or "") >= (cur.get("recorded_at") or ""):
            latest[tk] = r
    if tickers:
        want = set(tickers)
        return {k: v for k, v in latest.items() if k in want}
    return latest


def read_gaps():
    """逐家逐季的缺口说明。同键取最新一条。"""
    latest = {}
    for r in read_jsonl(gaps_path()):
        key = (r.get("ticker"), r.get("period"), r.get("data_type"))
        cur = latest.get(key)
        if cur is None or (r.get("recorded_at") or "") >= (cur.get("recorded_at") or ""):
            latest[key] = r
    return latest


def quote_fingerprint(ticker, period, quote):
    norm = re.sub(r"\s+", " ", (quote or "")).strip().lower()
    raw = f"{ticker}|{period}|{norm}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def expert_fingerprint(ticker, period, claim, source_id=""):
    norm = re.sub(r"\s+", " ", (claim or "")).strip().lower()
    raw = f"{ticker}|{period}|{source_id}|{norm}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def word_count(text):
    """英文按空白计词，中日韩按字符计。verbatim 长度闸门用。"""
    s = (text or "").strip()
    if not s:
        return 0
    cjk = len(re.findall(r"[㐀-鿿豈-﫿]", s))
    latin = len([w for w in re.split(r"\s+", re.sub(r"[㐀-鿿豈-﫿]", " ", s)) if w])
    return cjk + latin


def now_iso():
    return datetime.now(TZ).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 迁移

def _legacy_kind(rec):
    if rec.get("kind") in ("fundamental", "attribution"):
        return "fundamentals"
    if rec.get("metric"):
        return "metrics"
    return None


def migrate(apply=False):
    """把旧的两个平铺文件导入分片库。可重复跑，按记录指纹去重。

    迁移后写 .migrated.json，之后 read_all 不再读旧文件；旧文件留作只读备份。
    """
    plan = {"metrics": 0, "fundamentals": 0, "quotes": 0, "skipped": 0, "unknown": 0}
    sources = {
        "metrics": legacy_metrics_path(),
        "fundamentals": legacy_fundamentals_path(),
    }
    # 只跟分片里已有的比。迁移前 read_all 会把旧文件也读进来，那样每条都会判成重复。
    existing = {}
    for kind in ("metrics", "fundamentals", "quotes"):
        seen = set()
        for name in known_tickers():
            for r in read_jsonl(companies_dir() / name / SHARDS[kind]):
                seen.add(json.dumps({k: v for k, v in r.items() if k != "migrated_at"},
                                    sort_keys=True, ensure_ascii=False))
        existing[kind] = seen

    quote_seen = set()
    for name in known_tickers():
        for r in read_jsonl(companies_dir() / name / SHARDS["quotes"]):
            if r.get("fingerprint"):
                quote_seen.add(r["fingerprint"])

    writes = []
    for src_kind, path in sources.items():
        for rec in read_jsonl(path):
            kind = _legacy_kind(rec) or src_kind
            ticker = rec.get("ticker")
            if not ticker:
                plan["unknown"] += 1
                continue
            key = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            if key in existing[kind]:
                plan["skipped"] += 1
            else:
                writes.append((ticker, kind, dict(rec)))
                plan[kind] += 1
            # 归因记录本身就是一条管理层原话，镜像进语料库，
            # 否则深度报告读 quotes.jsonl 会看不到已有的这批。
            if rec.get("kind") == "attribution" and rec.get("quote"):
                fp = quote_fingerprint(ticker, rec.get("period"), rec.get("quote"))
                if fp in quote_seen:
                    continue
                quote_seen.add(fp)
                writes.append((ticker, "quotes", {
                    "kind": "quote",
                    "company": rec.get("company"),
                    "ticker": ticker,
                    "track": rec.get("track"),
                    "period": rec.get("period"),
                    "venue": rec.get("source_type") or "other",
                    "speaker": "",
                    "speaker_role": "",
                    "analyst_question": "",
                    "quote": rec.get("quote"),
                    "anchors": [],
                    "attribution_type": rec.get("attribution_type"),
                    "specificity": "",
                    "polarity": "negative" if rec.get("attribution_type") == "erosion" else "positive",
                    "evidence_grade": rec.get("evidence_grade"),
                    "source_type": rec.get("source_type"),
                    "source_url": rec.get("source_url"),
                    "source_date": rec.get("source_date"),
                    "transcript_file": "",
                    "paragraph_index": None,
                    "note": "由 attribution 记录迁移而来，未经段落级重抽",
                    "origin": "attribution_migration",
                    "fingerprint": fp,
                    "recorded_at": rec.get("recorded_at") or now_iso(),
                }))
                plan["quotes"] += 1

    if not apply:
        return {"ok": True, "applied": False, "plan": plan,
                "sources": {k: str(v) for k, v in sources.items()},
                "db_root": str(db_root()),
                "message": "试算，未写文件。加 --apply 执行。"}

    stamp = now_iso()
    for ticker, kind, rec in writes:
        rec.setdefault("migrated_at", stamp)
        append_jsonl(shard_path(ticker, kind), rec)
    marker = {
        "migrated_at": stamp,
        "sources": {k: {"path": str(v), "records": len(read_jsonl(v))}
                    for k, v in sources.items()},
        "written": plan,
        "note": "旧的平铺文件从此只读，read_all 不再读取。",
    }
    payload = json.dumps(marker, ensure_ascii=False, indent=2).encode("utf-8")
    dest = workspace_checked(migration_marker())
    import card_io

    card_io.write_bytes_exclusive(dest, payload)
    return {"ok": True, "applied": True, "plan": plan, "db_root": str(db_root()),
            "marker": str(migration_marker()),
            "message": f"迁移完成，写入 {sum(plan[k] for k in ('metrics', 'fundamentals', 'quotes'))} 条。"}
