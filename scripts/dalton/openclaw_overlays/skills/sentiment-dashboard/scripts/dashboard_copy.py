#!/usr/bin/env python3
"""Centralized Chinese UI copy dictionary for the sentiment dashboard (single-page & split panels)."""

from __future__ import annotations

from typing import Any

UI_COPY: dict[str, Any] = {
    "site": {
        "title": "市场情绪追踪看板",
        "subtitle": "每行代表一项情绪或拥挤度指标，依次标出1个月前、1周前与最新位置；点距越大说明近期变化越剧烈，同一市场内各行越分散说明内部分化越明显。",
        "top_link_label": "中概互联网相对强弱看板 →",
        "top_link_href": "https://eve.lumos.space/kweb.html",
        "color_legend_aria": "颜色图例",
        "color_legend_dir": [
            ("lg-up", "涨 / 净流入：红"),
            ("lg-down", "跌 / 净流出：绿"),
        ],
        "color_legend_temp": [
            ("lg-cold", "冷清 / 低位：蓝"),
            ("lg-neutral", "中性：灰"),
            ("lg-hot", "偏热 / 高拥挤：橙"),
        ],
        "color_legend_note": "（涨跌/资金流与冷热/拥挤度两套颜色相互独立）",
        "color_legend": (
            "颜色图例：涨跌幅与资金流采用「红涨 / 净流入、绿跌 / 净流出」（A股习惯）；"
            "冷热温度带与拥挤度条采用「蓝（冷清 / 低位）→ 灰（中性）→ 橙（偏热 / 高拥挤）」，"
            "两套颜色相互独立。"
        ),
        "footer_template": (
            "生成时间 {generated_at}（{tz_label}）｜"
            "仅供参考：相对强弱（RSI）与拥挤度仅描述当前相对历史位置，不构成涨跌概率预测"
        ),
        "panel_footer_template": (
            "数据截至 {data_as_of}（以各模块标注时区为准）｜"
            "仅供参考：相对强弱（RSI）与拥挤度仅描述当前相对历史位置，不构成涨跌概率预测"
        ),
        "nav_label": "面板导航",
    },
    "panels": [
        {
            "slug": "market-overview",
            "title": "市场情绪总览",
            "nav": "市场总览",
            "category": "overview",
            "tab_key": "overview",
            "subtitle": "跨市场冷热温度带、各数据源更新时点与指标口径说明。",
        },
        {
            "slug": "us-market",
            "title": "美股市场情绪与监控",
            "nav": "美股",
            "category": "us",
            "tab_key": "us",
            "subtitle": "美股底部/顶部监测规则、高盛 Prime 仓位、行业ETF强弱、宏观利差与估值快照。",
        },
        {
            "slug": "cn-market",
            "title": "A股情绪、资金流与拥挤度",
            "nav": "A股",
            "category": "cn",
            "tab_key": "cn_a",
            "subtitle": "A股杠杆环境、两融余额动能、宽基相对强弱、盘中资金流、成交量能与拥挤度观察。",
        },
        {
            "slug": "hk-market",
            "title": "港股AH溢价与板块强弱",
            "nav": "港股",
            "category": "hk",
            "tab_key": "cn_hk",
            "subtitle": "恒生AH股溢价指数动能及港股核心板块相对强弱。",
        },
        {
            "slug": "asia-leverage",
            "title": "日韩台融资杠杆",
            "nav": "日韩台",
            "category": "asia",
            "tab_key": "asia",
            "subtitle": "韩国、日本、台湾融资杠杆规模与市场指数对照（含各自最新截至日期与更新频率）。",
        },
        {
            "slug": "stocks",
            "title": "美股与中国个股监控",
            "nav": "个股",
            "category": "stocks",
            "tab_key": "stocks",
            "subtitle": "美股核心科技股相对大盘强弱、支撑/通道线与中国重点个股趋势观察。",
        },
    ],
    "single_tabs": [
        ("overview", "总览"),
        ("us", "美股市场"),
        ("cn_a", "A股市场"),
        ("cn_hk", "港股市场"),
        ("asia", "日韩台市场"),
        ("us_stocks", "美股个股"),
        ("china_stocks", "中国个股"),
    ],
    "display_names": {
        "Staples": "必选消费",
        "Staples等权": "必选消费等权",
        "必需消费": "必选消费",
        "Internet等权": "互联网平台等权",
        "CS电池": "中证电池主题",
        "BRKB": "伯克希尔（BRK-B）",
        "GSPUCYDE": "周期股/防御股相对指数（GSPUCYDE）",
        "GSCBHMOM": "高盛高动量股指数（GSCBHMOM）",
    },
    "currency_units": {
        "兆₩": "万亿韩元",
        "兆円": "万亿日元",
        "亿NT$": "亿新台币",
    },
    "summary_replacements": [
        ("正常趋势候选", "处于常规上升趋势中"),
        ("趋势转弱候选", "长期趋势转弱"),
        ("持续熊市候选", "均线空头排列（熊市环境）"),
        ("急性冲击候选", "波动率急性冲击"),
        ("监控20日新低失效", "若后续收盘跌破近20日最低点，则本轮底部信号作废"),
        ("监控任一指数20日收盘新高失效", "若任一指数创近20日收盘新高，则本轮顶部信号作废"),
        ("等待5DMA确认", "等待站上5日均线确认"),
        ("观察窗", "观察期"),
        ("无活动信号", "暂无触发信号"),
        ("无put保护提醒", "无看跌期权（Put）保护提醒"),
        ("指数put保护提醒", "指数看跌期权（Put）保护提醒"),
        ("1月平值put粗估", "1个月期平值看跌期权粗估成本约"),
        ("1月平值put≈", "1个月期平值看跌期权约"),
    ],
    "stale": {
        "not_updated_tpl": "数据未更新（上次成功：{as_of}）",
        "not_updated_banner_tpl": "⚠️ {source}数据未更新（上次成功：{as_of}），当前展示历史缓存，请注意时效。",
        "manual_snapshot_tpl": "非自动更新，截至 {as_of}（手工导入快照）",
        "lag_notice_tpl": "更新频率：{freq}，截至 {as_of}",
    },
    "overview": {
        "strip_titles": {
            "us": "美股温度带",
            "cn_a": "A股温度带",
            "cn_hk": "港股温度带",
            "asia": "日韩台温度带",
        },
        "strip_notes": {
            "us": "○ 1个月前，● 1周前，⬤ 最新；鼠标悬停查看具体原值与截至日期，行序固定。",
            "cn_a": "首行「两融余额」按自身历史偏离度定位，下方七行为第三方 0–100 拥挤度分位，两套标尺不可直接跨行对比。",
            "cn_hk": "统一标尺：AH股溢价动能与四个港股核心板块相对{bench_name}的30日相对强弱（RSI30，越高代表越强或溢价越收窄）。",
            "asia_with_trail": "各地融资杠杆 0–100 综合分（越高代表杠杆存量越拥挤、潜在去杠杆空间越大，不直接等同于卖出信号）。{trail_rows} 地含1周/1个月位置轨迹，其中日本为周频更新。",
            "asia_no_trail": "各地融资杠杆 0–100 综合分（越高代表杠杆存量越拥挤、潜在去杠杆空间越大，不直接等同于卖出信号）。暂无综合分历史轨迹，仅展示最新读数。",
        },
        "strip_hints": {
            "cyc": "周期股/防御股相对指数（GSPUCYDE）14日相对强弱（RSI14，越高代表周期股占优、风险偏好越强）",
            "mom": "高盛高动量股指数（GSCBHMOM）{period}日相对强弱（RSI，越低越超跌、越高越过热）",
            "ndx_spw": "纳斯达克100相对标普500等权（NDX/SPW）14日相对强弱（越高代表行情向大市值科技股集中）",
            "mags": "美股六大科技股等权相对纳指100 ETF（QQQ）30日相对强弱（越高代表大科技越占优）",
            "sox": "费城半导体指数（SOX）相对纳指100 ETF（QQQ）30日相对强弱（越高代表半导体越强势）",
            "xsw": "标普软件等权ETF（XSW）相对纳指100 ETF（QQQ）30日相对强弱（越高代表软件板块越强势）",
            "internet": "互联网平台等权篮子（NFLX、BKNG、ABNB、DASH）相对QQQ 30日相对强弱",
            "staples": "必选消费等权篮子（WMT、COST、TMUS、PEP、CMCSA）相对QQQ 30日相对强弱",
            "cn_margin": "沪深两市融资融券余额14日相对强弱（RSI14，越低越冷却、越高越活跃）",
            "ah_rsi": "彭博AH溢价30日强弱指标（HT006反向计分，越靠右代表A股相对H股溢价越收窄）",
        },
        "freshness_title": "数据新鲜度",
        "freshness_headers": ["数据类别", "最新日期", "来源与更新说明"],
        "freshness_rows": {
            "bbg_label": "彭博终端市场数据",
            "bbg_desc_ok": "日频更新，覆盖周期/防御比价、高盛高动量股（GSCBHMOM）与AH股溢价指标",
            "bbg_desc_stale": "数据未更新（上次成功：{as_of}），暂沿用历史文件",
            "public_label": "海外市场公开行情",
            "public_desc": "日频复权收盘价，覆盖美股大盘指数、行业ETF与个股",
            "gs_prime_label": "高盛 Prime 对冲基金仓位",
            "gs_prime_val_tpl": "杠杆截至 {lev_as_of} / 七大科技占比截至 {mag7_as_of}",
            "gs_prime_desc": "周频提取美股多空净/总杠杆（共 {lev_count} 期）与七大科技股敞口占比（共 {mag7_count} 期）",
            "cn_margin_label": "A股两融余额",
            "cn_margin_desc": "日频更新，沪深两市融资融券余额合计",
            "cn_crowd_label": "指数与板块拥挤度",
            "cn_crowd_desc": "日频更新，第三方拥挤度数据库（共 {count} 项指标）",
            "index_hist_label": "中港指数长历史行情",
            "index_hist_desc_ok": "日频更新，用于2023年以来A股与港股走势对照（已更新 {refreshed} 项）",
            "index_hist_desc_stale": "部分指数数据未更新（上次成功：{as_of}）：{stale_count} 项使用缓存、{missing_count} 项暂缺",
            "seed_label": "宏观一致预期与估值快照",
            "seed_desc": "非自动更新，截至 {as_of}（手工导入快照，不随每日行情自动滚动）",
            "trend_label": "美股个股趋势线行情",
            "trend_desc_ok": "日频复权行情，采用预设支撑/通道参数（{ok_count} 条正常更新）",
            "trend_desc_stale": "部分个股趋势线数据未更新（上次成功：{as_of}），暂沿用缓存",
            "flow_label": "A股盘中资金流",
            "flow_desc": "申万三级行业主力净流入；页面打开时自动拉取最新盘中数据（交易时段每{mins}分钟更新）",
            "turnover_label": "A股全市场成交量能",
            "turnover_desc_ok": "沪深两市累计成交额；基线取前{days}个完整交易日，交易时段每{mins}分钟更新",
            "turnover_desc_missing": "沪深两市累计成交额；量能数据缺失，交易时段每{mins}分钟更新",
            "turnover_desc_no_baseline": "沪深两市累计成交额；完整交易日参考数据缺失，交易时段每{mins}分钟更新",
        },
        "methodology_title": "口径与限制",
        "methodology_bullets": [
            "横轴位置反映指标相对自身历史的高低区域，仅用于衡量市场冷热与拥挤程度，不构成涨跌概率预测。",
            "同一市场内各指标横向分布越分散，代表内部结构分化越大；聚拢则代表走势一致性较高。",
            "偏离度行以过去1年正常中枢为0，越靠右代表相对历史越强或越拥挤，越靠左代表越弱或越冷清（极端数值截断显示在边界上）；1个月前与1周前取目标日前后共5个交易日的中位数，当前点取最新交易日读数。",
            "第三方拥挤度分位与杠杆综合分均采用各自 0–100 标尺，跨不同标尺的点位距离不可直接比较。",
            "处于冷区仅代表相对自身历史偏低，不自动等价于绝对超卖或买点，请结合具体原值（悬停可见）判断。",
            "标题下方蓝色标签代表冷区（低位）、橙色标签代表热区（拥挤）：偏离度与综合分按 0–100 轴的 20 / 80 划分；第三方拥挤度因常年贴边，收紧至 ≥90 / ≤5 才提示；带独立阈值的指标（两融、AH溢价、周期防御、个股顶底组合）按各自阈值提示。",
            "颜色约定：涨跌幅与资金流采用红涨（净流入）/绿跌（净流出）；冷热温度带与拥挤度条采用蓝（冷）→橙（热），避免语义混淆。",
            "手工导入快照会明示「非自动更新，截至日期」；任何外部数据拉取失败沿用旧值时均会提示「数据未更新（上次成功：时间）」。",
        ],
    },
    "us_bottom": {
        "section_title": "美股底部监控",
        "section_lead": "先确认市场环境（长期均线位置与行业广度），再观察底部预警、压力、深度超卖与站上5日均线确认信号；市场环境与广度仅作置信度参考，不改变底部触发规则。",
        "card_stage": "当前底部阶段",
        "card_env": "市场环境",
        "card_cyc": "周期/防御相对指数（GSPUCYDE）14日RSI",
        "card_qqq_dd": "纳指100 ETF（QQQ）近63日回撤",
        "card_vix": "标普500波动率指数（VIX）",
        "card_vvix": "波动率的波动率指数（VVIX）",
        "card_spx_rsi": "标普500指数（SPX）14日RSI",
        "card_qqq_rsi": "纳指100 ETF（QQQ）14日RSI",
        "no_window": "当前未处于底部压力观察期",
        "wait_confirm": "已进入压力区，等待站上5日均线确认（不受20日观察期限制）",
        "window_remaining_tpl": "压力观察期剩余：{n} 个交易日",
        "chart_cyc_title": "底部预警：周期股/防御股相对指数（GSPUCYDE）14日RSI",
        "chart_cyc_note_tpl": "这是风险偏好指标，≤{thr} 触发底部预警；越低代表防御占优越极端。当前 {val}{tag}。",
        "chart_dd_title": "底部预警：纳指100 ETF（QQQ）近63日最大回撤",
        "chart_dd_note_tpl": "衡量近期跌幅深度，≤{thr}% 触发预警；越负代表短期下跌越深。当前 {val}{tag}。",
        "chart_vol_title": "底部压力参考：恐慌指数（VIX）与波动率之波动率（VVIX）",
        "chart_vol_note_tpl": "VIX≥{vix_thr} 为进入底部压力区条件（越高恐慌越强）；VVIX≥{vvix_thr} 为罕见极端恐慌警戒线，不计入自动触发规则。",
        "chart_idx_rsi_title": "压力与深度超卖：标普500（SPX）与纳指100（QQQ）14日RSI",
        "chart_idx_rsi_note_tpl": "RSI14 衡量超买超卖（≤{p_thr} 为压力区、<{d_thr} 为深度超卖）；当前 SPX {spx_val}{spx_tag}、QQQ {qqq_val}{qqq_tag}。",
        "card_env_sub": "评估长期均线与行业广度健康度",
        "breadth_tpl": "中期广度：标普一级行业站上200日均线 {sector_text} 个；半导体成分股站上200日均线比例 {semi_pct}",
        "dma_tpl": "均线位置：SPX距200日均线 {spx_200}；QQQ距200日均线 {qqq_200}；SPX 50日均线较200日均线 {ma50_200}；双指数连续低于200日均线 {below_days}",
        "vix_meta_prefix": "高于压力线代表恐慌情绪升温，满足底部压力条件；",
        "vvix_meta_prefix": "历史极端恐慌罕见触发，触发后命中大底概率极高（仅作抄底提示，不计入自动规则）；",
    },
    "us_top": {
        "section_title": "美股顶部监控",
        "section_lead": (
            "顶部预警开启后进入 {window_td} 个交易日观察期，每日同步监测过热读数与转跌确认；"
            "若出现过热读数，确认等待期延长至 {timeout_td} 个交易日；确认转跌后若任一指数创近20日收盘新高则信号作废。"
            "隐含波动率比值仅作环境参考，恐慌波动率比值（VVIX/VIX）单独提示尾部对冲窗口，不计入顶部触发规则。"
        ),
        "card_stage": "顶部监测阶段",
        "card_env": "顶部预警环境",
        "card_cyc": "周期/防御相对指数（GSPUCYDE）30日RSI",
        "card_quantum": "量子计算等权篮子较60日低点涨幅",
        "card_qqq_rsi": "纳指100 ETF（QQQ）14日RSI",
        "card_qqq_200": "纳指100 ETF（QQQ）距200日均线乖离",
        "card_mom": "高盛高动量指数（GSCBHMOM）{period}日RSI",
        "card_confirm": "转跌确认进度：QQQ较本轮高点",
        "card_vol_ratio": "QQQ 30日/60日隐含波动率比值",
        "card_put_hedge": "指数看跌期权（Put）对冲窗口提醒",
        "no_active_round": "暂无顶部预警轮次",
        "no_window": "当前未处于顶部观察期",
        "vol_meta_prefix": "仅作环境参考：比值塌陷时需更重视已亮起的顶部预警或过激信号，不计入触发条件；",
        "chart_cyc_title_tpl": "顶部预警：{label} 30日RSI",
        "chart_cyc_note_tpl": "衡量风险偏好过热程度，预警线 ≥{thr}；当前 {val}{tag}。",
        "chart_quantum_title": "顶部预警：量子计算等权篮子较60日低点涨幅（对数坐标）",
        "chart_quantum_note_tpl": "衡量投机情绪过热程度，69.3 对应较60日低点翻倍（预警线）；当前涨幅 {val}{tag}。",
        "chart_excess_title": "顶部过激：纳指100 ETF（QQQ）14日RSI 与距200日均线乖离",
        "chart_excess_note_tpl": "左轴 RSI14（过激≥{rsi_thr}），右轴距200日均线（过激≥+{ma_thr}%）；当前 {rsi_val}{rsi_tag}、{ma_val}{ma_tag}。",
        "chart_confirm_title": "转跌确认与作废：标普500（SPX）、纳指100（QQQ）距20日均线乖离",
        "chart_confirm_note_tpl": "双指数均收在 0 轴下方且 QQQ 较本轮高点回撤 ≥{dd_thr}% 才确认转跌；当前 SPX {spx_val}{spx_tag}、QQQ {qqq_val}{qqq_tag}。",
        "chart_vol_title": "环境参考：QQQ 30日/60日隐含波动率比值（叠加QQQ收盘价）",
        "chart_vol_note_tpl": "≤{crush} 代表短期波动率塌陷（只作环境参考，警惕情绪过于麻痹），当前 {val}{tag}。",
        "chart_hedge_title": "对冲窗口提醒：恐慌波动率比值 VVIX/VIX（叠加QQQ收盘价）",
        "chart_hedge_note_tpl": "高于 {thr} 代表看跌期权（Put）对冲性价比较高（独立于顶部触发规则），当前 {val}{tag}。",
    },
    "us_state": {
        "section_title": "美股市场状态、行业与估值",
        "section_lead": "趋势位置、高盛 Prime 对冲基金仓位、风格与一级行业相对强弱、宏观信用/利率利差及估值快照。",
        "macro_group_title": "宏观利率、信用利差与估值快照",
        "spx_model_title": "标普500（SPX）长期趋势模型参数",
        "valuation_title": "远期一致预期市盈率（Blended Forward P/E）快照",
        "macro_missing_title": "宏观利率与信用利差图表",
        "macro_missing_msg": "美联储经济数据库（FRED）本次拉取失败，页面未用旧值冒充实时读数。",
        "macro_specs": [
            (
                "美国高收益债信用利差（OAS）",
                "单位：百分点（来源：FRED BAMLH0A0HYM2），衡量企业信用违约压力；数值越高代表信用风险担忧越强。",
            ),
            (
                "美国10年期通胀保值国债实际收益率（DFII10）",
                "单位：%（来源：美联储经济数据库 FRED DFII10），衡量无风险实际融资成本；越高代表金融环境越紧。",
            ),
            (
                "美国10年期与2年期国债期限利差（DGS10−DGS2）",
                "单位：百分点（来源：FRED），衡量收益率曲线斜率；转正走陡通常反映降息预期或经济再通胀。",
            ),
        ],
    },
    "gs_prime": {
        "group_title": "高盛 Prime 账本对冲基金仓位监控",
        "missing_title": "高盛 Prime 仓位监控",
        "missing_msg": "高盛 Prime 历史数据本次不可用，未展示旧值。",
        "card_net_title": "美股多空（L/S）净杠杆（GS Prime）",
        "card_gross_title": "美股多空（L/S）总杠杆（GS Prime）",
        "card_mag7_net_title": "美股七大科技龙头（Mag7）净敞口占比",
        "card_mag7_gross_title": "美股七大科技龙头（Mag7）总敞口占比",
        "source_label": "高盛 Prime 周报",
        "rule_tpl": (
            "左图提取自高盛 Prime 周报美股多空基金净/总杠杆（1年或5年分位 ≤{cold_pct}% 为蓝区极度出清、≥{hot_pct}% 为橙区极度拥挤）；"
            "右图为2021年10月以来七大科技龙头（Mag7）占美股总敞口比例（≥{mag7_hot:.0f}% 拥挤、≤{mag7_cold:.0f}% 出清）。"
        ),
    },
    "cn": {
        "margin_title": "A股两融余额14日相对强弱（RSI14）与中证1000对照",
        "margin_note_tpl": "左轴：中证1000 点位；右轴：两融余额 RSI（沪深合计，14日口径，≥70偏热、≤30偏冷），截至 {as_of}（北京时间）。",
        "broad_title": "宽基动能：指数点位与30日相对强弱（RSI30）",
        "broad_rule_tpl": "彩色线是指数点位（左轴），橙线是指数自身的 {rsi_period} 日相对强弱（右轴，参考线 {top_rsi}/50/{bottom_rsi}），两轴量纲不同，自 {start} 起。",
        "watchlist_title": "观察名单：拥挤度与指数走势",
        "watchlist_rule_tpl": "彩色线是指数点位（左轴），橙线是拥挤度（右轴 0–100，≥80 为高拥挤、≤20 为低位冷清），自 {start} 起。小微盘跟踪国证2000，因其历史序列更完整。",
        "flow_title": "A股盘中主力资金流向（申万三级行业）",
        "flow_note_tpl": "东方财富申万三级行业主力净流入当日累计（单位：亿元），展示净流入与净流出前 {side_n} 名；红=净流入、绿=净流出。盘中每 {mins} 分钟自动更新。",
        "turnover_title": "A股全市场成交量能（累计成交额）",
        "crowd_title": "指数与板块拥挤度总表",
        "crowd_prob_col": "历史同向概率（参考）",
        "crowd_prob_sort": "历史同向概率：高到低",
        "crowd_note_tpl": (
            "沿用第三方拥挤度数据库公开指标，中国指数按拥挤度从低到高排序。「历史同向概率」指第三方统计的相似拥挤度下未来对应周期沿所示方向（上涨/下跌）出现的频率，仅供参考，不构成预测。"
            "小图每 {step} 个交易日采样一点（蓝线为归一化价格、橙线为 0–100 拥挤度，两轴独立不可互比），悬停可放大。"
        ),
        "crowd_spark_price_label": "价格 ",
        "crowd_spark_crowd_label": "拥挤 ",
        "crowd_spark_empty": "暂无一年历史",
        "crowd_zoom_price_legend": "蓝线：价格，窗口起点=100、独立纵轴",
        "crowd_zoom_crowd_legend": "橙线：拥挤度0–100、独立纵轴",
    },
    "hk": {
        "ah_price_title": "恒生AH股溢价指数（HSAHP，纵轴倒序）",
        "ah_price_note": "纵轴已倒序处理：曲线向上代表A股相对H股溢价收窄（港股相对占优），向下代表溢价扩大；与每日跟踪口径一致。",
        "ah_rsi_title": "AH股溢价30日强弱指标（彭博 HT006 反向计分）",
        "ah_rsi_note_tpl": "采用彭博 HT006 反向计分（取值 -100 至 0，越高代表溢价越偏向收窄）：超买线 -36、超卖线 -60（接近 -60 时溢价有扩大压力），当前 {val}。",
        "sector_title": "港股核心板块相对强弱",
        "sector_rule_tpl": "左轴为距200日均线偏离幅度（%），右轴为相对基准的 {rsi_period} 日相对强弱，左轴 0 与右轴 50 对齐。基准为{bench_name}，高股息板块跟踪恒生港股通高股息低波动指数。",
    },
    "asia": {
        "card_missing_title": "日韩台融资杠杆",
        "card_missing_sub": "跨市场杠杆摘要暂未生成",
        "method_title": "指标口径与更新频率说明",
        "method_body": "上方卡片展示韩国、日本、台湾融资杠杆 0–100 综合分与核心存量指标；高分代表可出清的杠杆燃料较多，需警惕去杠杆放大波动，但不直接等同于当日卖出信号。",
        "method_note_tpl": "下方各国图表：左轴为本币口径融资杠杆指标，右轴为该国 MSCI 价格指数（本币计价，起点 {start}）。韩国、台湾主要为日频更新，日本为周频更新（会自然滞后数日）。两轴量纲不同，仅用于观察拐点与方向。",
    },
    "stocks": {
        "split_us_heading": "美股个股监控",
        "split_cn_heading": "中国个股监控",
        "us_mags_title": "美股六大科技股相对强弱信号",
        "us_mags_note": "跟踪名单：Amazon（AMZN）、Alphabet（GOOGL）、Meta（META）、Microsoft（MSFT）、Apple（AAPL）、NVIDIA（NVDA），均相对纳指100 ETF（QQQ）比较。",
        "us_mags_headers": ["股票", "相对QQQ 30日RSI", "距200日均线乖离", "近2年乖离所处区间", "组合状态"],
        "us_mags_rule_tpl": "统一判定口径：顶部组合需相对RSI > {top_rsi}、乖离处于近2年最高20%区间且 ≥{top_ma:+.0f}%；底部组合需相对RSI < {bot_rsi}、处于最低20%区间且 ≤{bot_ma:+.0f}%。",
        "us_extras_rule": "同行对比规则：台积电（TSM）与英特尔（INTC）以费城半导体指数（SOX）为基准并采用100日均线；伯克希尔（BRK-B）以标普500等权为基准并采用200日均线。",
        "cn_title": "中国重点个股覆盖名单",
        "cn_note": "跟踪宁德时代与长江电力（主动精选标的，基准为沪深300）。多头趋势条件：收盘价高于200日均线且200日均线较20个交易日前上行。",
        "cn_headers": ["公司", "证券代码", "14日RSI", "近20日涨跌", "近60日涨跌", "距200日均线乖离", "长期均线趋势"],
        "cn_trend_on": "走强（多头排列）",
        "cn_trend_off": "走弱（未满足多头条件）",
    },
}


def display_asset_name(raw_name: str) -> str:
    """Normalize inconsistent asset/sector names (e.g. Staples/必需消费 -> 必选消费)."""
    mapping = UI_COPY["display_names"]
    return str(mapping.get(raw_name, raw_name))


def display_currency_unit(raw_unit: str) -> str:
    """Translate mixed symbol currency units into unified Chinese units (supports substrings)."""
    text = str(raw_unit)
    for src, dst in UI_COPY["currency_units"].items():
        text = text.replace(src, dst)
    return text


def humanize_summary(text: str) -> str:
    """Translate internal framework summary phrases into plain Chinese investment copy."""
    out = str(text or "")
    for src, dst in UI_COPY["summary_replacements"]:
        out = out.replace(src, dst)
    return out


def format_stale_notice(source: str, as_of: str | None) -> str:
    label = as_of or "未知"
    return UI_COPY["stale"]["not_updated_banner_tpl"].format(source=source, as_of=label)


def format_manual_snapshot_notice(as_of: str | None) -> str:
    return UI_COPY["stale"]["manual_snapshot_tpl"].format(as_of=as_of or "未知")
