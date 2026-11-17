#!/usr/bin/env python3
"""Centralized Chinese UI copy dictionary for the HK Momentum Indices monitor."""

from __future__ import annotations

from typing import Any

UI_COPY: dict[str, Any] = {
    "title": "港股动量指数追踪",
    "subtitle_tpl": (
        "基期 {base_date}（基点 1000）· 最新收盘 {last_date} · 每月月末调仓、"
        "等权持有风险调整动量排名前 {top_n} 只港股（兼顾近 6 个月与近 12 个月剔除近 1 个月涨幅）"
    ),
    "legend_dir": [
        ("lg-up", "上涨：红"),
        ("lg-down", "下跌：绿"),
    ],
    "legend_sec": [
        ("lg-cold", "① 小市值动量：蓝线"),
        ("lg-pink", "② 非港股通动量：粉线"),
        ("lg-gray", "恒生指数（对照）：灰虚线"),
    ],
    "legend_note": "（采用统一红涨绿跌口径；盘中每分钟刷新实时估值，收盘后固定当日终值）",
    "stale_banner_tpl": "⚠️ 数据未更新（上次成功：{last_ok}）：{reason}。当前展示历史缓存，请注意时效。",
    "poll_error_tpl": "⚠️ 实时数据拉取失败（{err}），上次成功：{last_ok}。",
    "cards": {
        "smallcap": "① 小市值动量指数",
        "nonggt": "② 非港股通动量指数",
        "hsi": "恒生指数（对照）",
        "stats_tpl": "基期至今累计 {total} · 年化收益 {cagr} · 年化波动 {vol} · 最大回撤 {mdd}",
        "hsi_norm_tpl": "自基期起归一化点位（基点 1000）：{norm}（用于下方走势图同起点对比）",
        "partial_tpl": "报价覆盖 {quoted}/{expected} 只（缺 {missing} 只暂按平盘计入）",
    },
    "ranges": [
        (63, "近3个月"),
        (126, "近6个月"),
        (245, "近1年"),
        (0, "全部"),
    ],
    "range_note": "切换时间区间后，三条曲线自动按所选起点重设为 1000 点对齐比较（港币计价）。",
    "tables": {
        "smallcap_title": "① 小市值动量前 50 成分股",
        "smallcap_sub_tpl": "入池口径：调仓日总市值 < 50 亿港元 · 本名单于 {rebal_date} 调仓日确定（按最新动量分降序展示）",
        "nonggt_title": "② 非港股通动量前 50 成分股",
        "nonggt_sub_tpl": "入池口径：不在深交所港股通标的名单 · 本名单于 {rebal_date} 调仓日确定（按最新动量分降序展示）",
        "headers": [
            "排名",
            "股票代码",
            "股票名称",
            "动量分(越高越强)",
            "12-1月涨幅(剔除近1月)",
            "总市值(亿港元)",
        ],
        "show_all": "显示全部 50 只 ▾",
        "collapse": "收起至前 10 只 ▴",
    },
    "methodology": {
        "title": "编制方法、口径与数据说明",
        "bullets_tpl": [
            "编制逻辑：参考 MSCI / 标普动量指数方法，综合动量分 = 0.5 ×（近 6 个月不含近 1 月收益 ÷ 年化波动率）标准化分 + 0.5 ×（近 12 个月不含近 1 月收益 ÷ 年化波动率）标准化分，标准化得分限制在 -3 到 +3 之间，避免极端个股拉偏。",
            "调仓与权重：仅在已完成最后一个交易日的月末（依港交所日历）执行月度调仓，选池内得分最高 {top_n} 只等权买入持有；未结束的当月不会逐日重选成分。",
            "成分池过滤条件：① 小市值池为调仓日总市值 < {smallcap_limit} 亿港元；② 非港股通池为不在港股通（南向）标的名单内。共同要求：上市满 13 个月、近 1 年有效交易日 ≥60%、近 3 个月日成交额中位数 ≥ {min_turn_wan} 万港元、股价 ≥ {min_price} 港元、近 10 个交易日有成交。",
            "历史冻结与幸存者偏差说明：历史回测区间（{launch_date} 以前）基于当前可得证券名单与股本回溯，未包含已退市股票，存在幸存者偏差；自 {launch_date} 上线起，每日收盘指数与每月调仓成分均永久冻结落盘，不再随后续日频重算改写。",
        ],
    },
    "footer": "仅供内部投研参考 · 数据来源：港交所证券列表、深交所港股通名单、腾讯前复权行情（港币计价）。",
}
