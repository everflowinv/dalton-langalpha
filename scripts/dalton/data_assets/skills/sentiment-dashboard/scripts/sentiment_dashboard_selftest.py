#!/usr/bin/env python3
"""Dependency-free source and formula checks for the sentiment dashboard.

Split out of ``sentiment_dashboard_cli.py`` on 2026-09-03: the Skill Workshop
refuses support files above 256 KB and the CLI crossed that line when the top
state machine was wired in. The checks are unchanged; ``src`` still reads the
CLI file itself, so every "this string must (not) be in the source" rule keeps
looking at the same code it always did.
"""

from __future__ import annotations

from sentiment_dashboard_cli import *  # noqa: F401,F403  (constants, helpers, pandas/numpy, Path, json, html, re)
from sentiment_dashboard_cli import CONFIG_PATH, SCRIPT_DIR, SEED_PATH


def self_test(as_json: bool) -> int:
    checks: list[dict[str, Any]] = []

    def check(name: str, condition: bool, hint: str) -> None:
        checks.append({"name": name, "ok": bool(condition), "hint": "" if condition else hint})

    check("config_exists", CONFIG_PATH.exists(), f"Restore {CONFIG_PATH}")
    check("seed_exists", SEED_PATH.exists(), f"Restore {SEED_PATH}")
    try:
        config, seed = load_json(CONFIG_PATH), load_json(SEED_PATH)
        check("mags_six_no_tsla", config.get("mags") == ["AMZN", "GOOGL", "META", "MSFT", "AAPL", "NVDA"], "Keep the approved six-stock basket and exclude TSLA")
        check("xsw_ticker_present", config.get("market_tickers", {}).get("XSW") == "XSW", "Keep XSW in market_tickers; the US tab needs the XSW/QQQ chart under the SOX one")
        sector_tickers = [item.get("ticker") for item in config.get("sp_sector_etfs", [])]
        check("sp_sector_etfs_all_11", sector_tickers == ["XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLK", "XLU"], "Keep all 11 S&P GICS level-1 sector ETFs in the approved display order")
        check("sp_sector_etfs_unique", len(sector_tickers) == len(set(sector_tickers)), "Each S&P sector ETF must appear exactly once")
        baskets = {b.get("key"): b for b in config.get("equal_weight_baskets", [])}
        check("internet_basket_members", baskets.get("INTERNET", {}).get("members") == ["NFLX", "BKNG", "ABNB", "DASH"], "Keep the approved Internet equal-weight basket under the XSW chart")
        check("staples_basket_members", baskets.get("STAPLES", {}).get("members") == ["WMT", "COST", "TMUS", "PEP", "CMCSA"], "Keep the approved Staples equal-weight basket under the Internet chart")
        check("basket_benchmarks_qqq", all(b.get("benchmark") == "QQQ" for b in baskets.values()), "Equal-weight baskets compare against QQQ, same as the SOX and XSW charts")
        extras = {x.get("ticker"): x for x in config.get("relative_extras", [])}
        check("relative_extras_roster", list(extras) == ["TSM", "INTC", "BRK-B"],
              "美股个股页在六大科技后固定跟 TSM、INTC、BRKB 三张图；改名单同时改这条期望值")
        check("relative_extras_semis_vs_sox",
              all(extras.get(t, {}).get("ma") == 100 and extras.get(t, {}).get("benchmark") == "SOX" for t in ("TSM", "INTC")),
              "TSM/INTC 是半导体，基准用 SOX、均线用 100 日；换成 QQQ/200 日就读不出它们相对同行的强弱")
        check("relative_extras_brkb_vs_spw",
              extras.get("BRK-B", {}).get("ma") == 200 and extras.get("BRK-B", {}).get("benchmark") == "SPW",
              "BRKB 对标普等权（SPW）、用 200 日均线：它的对照组是等权大盘，不是纳指也不是半导体")
        trends = {x.get("ticker"): x for x in config.get("trend_lines", [])}
        check("trend_lines_roster",
              trends.get("AMZN", {}).get("mode") == "channel" and trends.get("AMZN", {}).get("start") == "2022-07-01"
              and trends.get("AFRM", {}).get("mode") == "support" and trends.get("AFRM", {}).get("start") == "2023-07-01",
              "AMZN 是 22H2 起的平行通道（要顶也要底），AFRM 是 23H2 起的支撑线（只要底）；起点和模式都是用户口径")
        check("trend_lines_frozen_in_config",
              all(isinstance(trends.get(t, {}).get("fit"), dict)
                  and {"anchor", "slope", "bottom"} <= set(trends[t]["fit"])
                  for t in ("AMZN", "AFRM"))
              and trends.get("AMZN", {}).get("fit", {}).get("top") is not None,
              "两条线的斜率和 anchor 线值必须冻结在 config 里，build 只做平移；缺 fit 就跑 "
              "`run.sh refit-trend --write` 重新拟合，别把重拟合放回构建流程")
        check("trend_amzn_calibration_documented", "287.200" in trends.get("AMZN", {}).get("note", ""),
              "一侧削减回归的轮数是照着用户给的那张图标定的；把校验点（软件标 287.200）写在 config 里，"
              "否则后人改 TREND_ITERS 时不知道这条线原本对齐过什么")
        signal = config.get("relative_signal", {})
        check("megacap_cron_signal", signal.get("rsi_period") == 30 and signal.get("top_rsi") == 60 and signal.get("bottom_rsi") == 40 and signal.get("top_ma200_dev_pct") == 20 and signal.get("bottom_ma200_dev_pct") == -5 and signal.get("position_lookback_days") == 504, "Restore the approved megacap relative-RSI/200DMA rules")
        c = seed["normal_formula"]
        check("spx_formula", all(k in c for k in ["nominal_gdp_beta", "intercept", "earnings_share_growth", "buyback_growth"]), "Restore SPX model coefficients")
        check("spx_anchor", seed.get("anchor", {}).get("date") == "2019-06-28" and seed.get("anchor", {}).get("spx") == 2941.76, "Restore workbook anchor")
        check("spx_history_seed", min(map(int, seed.get("annual_nominal_gdp_pct", {9999: 0}))) <= 2015, "Restore the historical macro inputs needed for the 2015+ premium series")
    except Exception as exc:
        check("json_parse", False, f"Fix JSON: {exc}")
    sample = pd.Series(np.arange(1, 40, dtype=float))
    check("rsi_wilder", abs(float(wilder_rsi(sample, 14).iloc[-1]) - 100) < 1e-9, "Fix Wilder RSI implementation")

    cn_dates = pd.bdate_range("2024-01-01", periods=400)
    cn_price = pd.Series(100 + np.arange(400) * .1 + np.sin(np.arange(400)), index=cn_dates)
    cn_bench = pd.Series(3000 + np.arange(400), index=cn_dates)
    # Reproduce Yahoo's long missing block followed by a lone current quote.
    broken_bench = cn_bench.drop(cn_dates[300:-1])
    full_dev, full_rel = china_stock_indicators(cn_price, cn_bench)
    broken_dev, broken_rel = china_stock_indicators(cn_price, broken_bench)
    check("cn_dma_independent_of_benchmark", full_dev.equals(broken_dev) and len(full_dev) == 201,
          "基准缺日线不能删除股票自身行情或缩短200DMA的交易日窗口")
    check("cn_relative_gap_not_one_day", cn_dates[-1] not in broken_rel.index and full_rel.index.max() == cn_dates[-1],
          "长缺口后的孤立报价不能当成下一根连续日线计算相对RSI")
    empty_dev, empty_rel = china_stock_indicators(cn_price, pd.Series(dtype=float))
    check("cn_no_benchmark_keeps_dma", empty_dev.equals(full_dev) and empty_rel.empty,
          "基准不可用时仍画股票自身200DMA，只将相对RSI标成暂无")
    payload = series_payload(full_dev, "test", "#4da3ff", max_gap_days=14)
    check("cn_chart_gap_policy", payload["maxGapDays"] == 14 and len(payload["data"]) == len(full_dev),
          "日线图保留真实采样日期；缺口应断线，不能补值或删除有效行情")

    # ── 趋势线：一侧削减回归 + 通道 ──
    tt = np.arange(600, dtype=float)
    # 造一条在 0.5t+100 与 0.5t+112 之间来回摆的序列：下包络应贴回 100 一侧、上包络贴回 112 一侧。
    band = 0.5 * tt + 106.0 + 6.0 * np.sin(tt / 23.0)
    env_b, env_c, env_pts = envelope_line(tt, band, upper=False)
    check("trend_envelope_hugs_lower_edge", abs(env_b - 0.5) < 0.02 and abs(env_c - 100.0) < 1.5 and env_pts >= TREND_MIN_PTS,
          "一侧削减回归必须收敛到下包络：序列在 0.5t+100 到 0.5t+112 之间摆动，下沿要拟合回 0.5t+100 附近")
    up_b, up_c, _ = envelope_line(tt, band, upper=True)
    check("trend_envelope_sides_symmetric", abs(up_b - 0.5) < 0.02 and abs(up_c - 112.0) < 1.5,
          "上包络与下包络必须是同一套逻辑的镜像；只有一侧准说明 upper 分支写错了")
    idx = pd.bdate_range("2023-01-02", periods=610)
    tt610 = np.arange(610, dtype=float)
    demo = pd.DataFrame({"High": 0.5 * tt610 + 112.0, "Low": 0.5 * tt610 + 100.0, "Close": 0.5 * tt610 + 106.0}, index=idx)
    base = demo.iloc[:600]
    fit_ch = fit_trend_line(base, "2023-01-02", "channel")
    check("trend_channel_parallel_and_bracketing",
          fit_ch is not None and abs(fit_ch["top"] - fit_ch["bottom"] - 12.0) < 1.5 and abs(fit_ch["slope"] - 0.5) < 0.02,
          "平行通道的上下沿共用一个斜率，所以两条边的间距应当等于造数时的 12；差太多说明截距没按同一斜率重解")
    ch = trend_line_stats(base, "2023-01-02", "channel", fit_ch)
    check("trend_rise_uses_trading_days",
          ch is not None and abs(ch["rise_6m"] - ch["slope"] * 126) < 1e-9 and abs(ch["rise_12m"] - ch["slope"] * 252) < 1e-9,
          "6M/12M 抬升按 126/252 个交易日折算：自变量是 K 线序号，用日历日会把休市天数也算成抬升")
    check("trend_needs_enough_bars", trend_line_stats(base.iloc[:120], "2023-01-02", "channel", fit_ch) is None,
          "不足 250 根 K 线不出趋势线：几个月的数据拟合出来的斜率外推一年没有意义，宁可空着")
    check("trend_needs_frozen_fit", trend_line_stats(base, "2023-01-02", "channel", None) is None,
          "config 里没有 fit 就不出线：build 不允许临时拟合，否则图注上的趋势顶底会天天变")
    # 冻结的线必须原样照用：这里故意把下沿压到 50，正确实现应当照搬 50 这条线，而不是重新贴回 100。
    off = {"anchor": iso_date(idx[599]), "slope": 0.5, "bottom": 0.5 * 599 + 50.0, "top": 0.5 * 599 + 112.0, "bottom_pts": 9, "top_pts": 9, "fitted_on": "2026-01-01"}
    frozen = trend_line_stats(base, "2023-01-02", "channel", off)
    check("trend_uses_frozen_fit_not_refit",
          frozen is not None and abs(frozen["bottom"] - (0.5 * 599 + 50.0)) < 1e-6 and frozen["pierce_pct"] == 0.0,
          "build 必须照搬 config 里冻结的线；若结果又贴回最低价包络，说明还在重新拟合")
    shifted = trend_line_stats(demo, "2023-01-02", "channel", off)
    check("trend_frozen_line_shifts_by_bars",
          shifted is not None and abs(shifted["bottom"] - frozen["bottom"] - 0.5 * 10) < 1e-6,
          "多出 10 根 K 线，冻结的线只应沿斜率平移 10 根；对不上说明 anchor 定位或平移算错了")
    check("trend_gaps_track_latest_close",
          shifted is not None and abs(shifted["bottom_gap"] - (shifted["bottom"] / shifted["close"] - 1) * 100) < 1e-9
          and shifted["close"] == float(demo["Close"].iloc[-1]),
          "距离百分比必须按最新收盘重算：线冻结，读数不冻结")
    fit_sup = fit_trend_line(base, "2023-01-02", "support")
    sup = trend_line_stats(base, "2023-01-02", "support", fit_sup)
    check("trend_support_has_no_top", sup is not None and "top" not in sup and "top" not in (fit_sup or {}),
          "support 模式只算下沿；顺手把上沿也算出来会让 AFRM 面板多出用户明确不要的数字")
    check("trend_notes_within_cap",
          all(len(trend_note({**(ch or {}), "mode": m, "start": "2022-07-01"})) <= NOTE_MAX_CHARS for m in ("channel", "support"))
          and len(trend_fit_note(shifted)) <= NOTE_MAX_CHARS and len(trend_fit_note(sup)) <= NOTE_MAX_CHARS,
          f"趋势线读数是图注，和别处一样卡在 {NOTE_MAX_CHARS} 字以内；拟合口径写 config 的 note 和 methodology.md")
    check("trend_note_empty_when_missing", trend_note(None) == "" and append_note("<article>x</article>", "") == "<article>x</article>",
          "算不出趋势线时不挂空图注，也不留“暂无”占位——面板上少一行比多一行没数的行好读")
    check("trend_panel_reports_only_bottom",
          "当前趋势底" in trend_panel(sup, "AFRM") and "趋势顶" not in trend_panel(sup, "AFRM"),
          "AFRM 面板按用户口径只保留趋势底与 6M/12M 抬升，不放顶部")
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    ew = equal_weight_index({"A": pd.Series([100.0, 110.0, 110.0], index=idx), "B": pd.Series([50.0, 45.0, 45.0], index=idx)}, ["A", "B"])
    check("equal_weight_daily_rebalance", not ew.empty and abs(float(ew.iloc[1]) - 100.0) < 1e-9, "Equal-weight index must average member returns each day, not average prices")
    check("equal_weight_needs_all_members", equal_weight_index({"A": pd.Series([1.0, 2.0], index=idx[:2])}, ["A", "B"]).empty, "A missing member must blank the basket instead of silently reweighting it")
    ramp = pd.Series(np.arange(300, dtype=float), index=pd.date_range("2024-01-01", periods=300, freq="B"))
    check("strip_percentile_top", abs(float(rolling_pct_rank(ramp).iloc[-1]) - 100) < 1e-6, "Fix rolling percentile rank; strips still report it in hover")
    marks = path_marks([float(v) for v in range(STRIP_TRAIL + 1)])
    check("path_marks_offsets", marks == (float(STRIP_TRAIL - STRIP_WEEK), 0.0), f"1W mark must sit {STRIP_WEEK} points back and 1M mark {STRIP_TRAIL} points back")
    check("path_marks_short", path_marks([1.0, 2.0]) == (None, None), "Short history must yield no 1W/1M marks instead of fabricated ones")
    symmetric = pd.Series(np.concatenate([np.arange(-100.0, 101.0), [0.0]]), index=pd.date_range("2024-01-01", periods=202, freq="B"))
    check("robust_z_center", abs(float(robust_z(symmetric).iloc[-1])) < 1e-9, "A value sitting on the window median must score 0 sigma")
    clean = pd.Series(np.concatenate([np.arange(-100.0, 101.0), [60.0]]), index=pd.date_range("2024-01-01", periods=202, freq="B"))
    spiked = clean.copy()
    spiked.iloc[50] = 1e6
    check("robust_z_outlier_resistant", abs(float(robust_z(spiked).iloc[-1]) - float(robust_z(clean).iloc[-1])) < 0.05, "Median/MAD must not let one extreme print rescale the whole ruler")
    check("z_to_x_linear", z_to_x(0) == 50.0 and z_to_x(STRIP_Z_FULL) == 100.0 and z_to_x(-STRIP_Z_FULL) == 0.0, "The axis must map +/-STRIP_Z_FULL sigma linearly onto 0-100")
    check("z_to_x_clamped", z_to_x(9) == 100.0 and z_to_x(-9) == 0.0 and z_to_x(None) is None, "Extreme sigma must clamp to the edge instead of overflowing the strip")
    check("z_axis_matches_js", STRIP_Z_FULL == 2.5, "The sigma tick row in the embedded JS hardcodes 2.5; keep both in sync")
    spike = pd.Series([50.0] * 40, index=pd.date_range("2024-01-01", periods=40, freq="B"))
    spike.iloc[20] = 90.0
    check("anchor_smooth_kills_spike", float(anchor_smooth(spike).max()) == 50.0, f"A {STRIP_SMOOTH}-day median must absorb a single-day print at an anchor")
    step = pd.Series([0.0] * 20 + [10.0] * 20, index=pd.date_range("2024-01-01", periods=40, freq="B"))
    check("anchor_smooth_centred", float(anchor_smooth(step).iloc[20]) == 10.0 and float(anchor_smooth(step).iloc[19]) == 0.0, f"Anchors must use a centred T-2..T+2 window (a trailing {STRIP_SMOOTH}-day median would still read 0 at the step)")
    tail_spike = pd.Series([50.0] * 40, index=pd.date_range("2024-01-01", periods=40, freq="B"))
    tail_spike.iloc[-1] = 90.0
    check("locator_current_is_raw", float(locator_series(tail_spike).iloc[-1]) == 90.0 and float(anchor_smooth(tail_spike).iloc[-1]) == 50.0, "The current marker must sit on today's raw value, not on a smoothed one")
    check("locator_smooths_history", float(locator_series(spike).iloc[20]) == 50.0, "History behind the current point still goes through the median filter")
    check("robust_z_ruler_from_raw", abs(float(robust_z(ramp, locator=ramp).iloc[-1]) - float(robust_z(ramp).iloc[-1])) < 1e-9, "Passing the raw series as locator must reproduce the plain z; the ruler is always raw-built")
    rising = strip_item("t", ramp)
    check("strip_item_scale_z", rising is not None and rising["scale"] == "z" and rising["z"] is not None and rising["pct"] is not None, "Series-backed rows must carry robust z for position and percentile for hover")
    check("strip_path_rising", rising is not None and rising["x1m"] is not None and rising["x1w"] is not None and rising["x1m"] <= rising["x1w"] <= rising["x"], "A rising series must show 1M ≤ 1W ≤ now on its row path")
    check("strip_item_range", rising is not None and "–" in rising["range"], "Hover must expose the 1-year raw range so a narrow ruler is visible")
    noisy = ramp.copy()
    noisy.iloc[-1] = float(ramp.iloc[-1]) + 40.0
    check("strip_item_current_tracks_raw", strip_item("t", noisy)["z"] > rising["z"], "A jump on the last print must move the current marker; it is no longer smoothed away")
    check("strip_item_anchors_unmoved", strip_item("t", noisy)["z1w"] == rising["z1w"], "A jump on the last print must not disturb the 1W/1M anchors")
    inverted = strip_item("t", ramp, invert=True)
    check("strip_invert_z", inverted is not None and rising is not None and abs(inverted["z"] + rising["z"]) < 1e-9, "Inverted rows must flip the sigma sign, not the percentile")
    flat = strip_point("t", 40.0)
    check("strip_point_no_path", flat is not None and flat["x1w"] is None and flat["x1m"] is None, "Composite scores without history must not fake a path")
    check("strip_point_scale_pct", flat is not None and flat["scale"] == "pct" and flat["pct"] == 40.0, "Upstream percentile rows keep their own 0-100 ruler")
    composite = strip_point("t", 62.0, scale="score")
    check("strip_point_scale_score", composite is not None and composite["scale"] == "score" and composite["pct"] is None, "Leverage composites must not be relabelled as percentiles")
    chips = zone_chips([
        {"name": "z_hot", "x": 86.0, "z": 1.8, "scale": "z"},
        {"name": "z_cold", "x": 12.0, "z": -1.9, "scale": "z"},
        {"name": "z_mid", "x": 55.0, "z": 0.25, "scale": "z"},
        {"name": "crowd_88", "x": 88.0, "scale": "pct"},
        {"name": "crowd_96", "x": 96.0, "scale": "pct"},
        {"name": "crowd_2", "x": 2.0, "scale": "pct"},
        {"name": "score_hot", "x": 84.0, "scale": "score"},
    ])
    tones = {c["text"].split()[0]: c["tone"] for c in chips}
    check("zone_chip_tone_split", tones.get("z_hot") == "hot" and tones.get("z_cold") == "cold" and tones.get("score_hot") == "hot",
          "落进热区的行给黄色 chip、冷区给蓝色，和图上冷热区阴影同一套判定")
    check("zone_chip_skips_middle", "z_mid" not in tones,
          "中性区的行不出 chip，否则提示行会把整条带重复念一遍")
    check("zone_chip_crowd_threshold", "crowd_88" not in tones and tones.get("crowd_96") == "hot" and tones.get("crowd_2") == "cold",
          "上游拥挤度分位天然贴边，要收紧到 ≥90 / ≤5；用 20/80 判定会让 A股 带常年挂五个 chip")
    check("zone_chip_skip_list", not zone_chips([{"name": "两融余额", "x": 95.0, "z": 2.2, "scale": "z"}], skip={"两融余额"}),
          "已经有绝对阈值专用提示的行不再出 zone chip，同一行挂两个说法会互相打架")
    # 拆分前 head/body 都是 CLI 里 self_test 之前的部分；self_test 搬走后用 refit_trend 作同一条边界。
    head = (SCRIPT_DIR / "sentiment_dashboard_cli.py").read_text(encoding="utf-8").split("def refit_trend")[0]
    copy_src = (SCRIPT_DIR / "dashboard_copy.py").read_text(encoding="utf-8")
    check("header_kweb_link", '<a class="top-link" href="{html.escape(site["top_link_href"])}">{html.escape(site["top_link_label"])}</a>' in head and "中概互联网相对强弱看板 →" in copy_src,
          "主看板标题下必须保留中概互联网相对强弱看板入口")
    check("header_kweb_link_style", ".top-link{" in head,
          "Richard 相对看板入口需要独立样式，不能退成不易识别的默认链接")
    check("zone_chip_all_strips", all(f"zone_chips({name}" in head for name in ["us_items", "cn_a_items", "cn_hk_items", "asia_items"]),
          "四条温度带都要走同一套冷热判定；只给 A股 加提示就回到了改版前的样子")
    check("strip_shape_removed", "strip_shape" not in head and "同口径指标" not in head and "没有可比较的历史序列" not in head,
          "标题右侧那句“同口径指标…最热X｜最冷Y”已按用户要求删除，别再被加回来")
    check("strip_alert_ring_removed", "dotR+3.4" not in head and "it.alert" not in head,
          "当前点不再额外描一圈、行名不再标黄；这两处只是重复了 chip 已经说过的事")
    check("chip_tone_css", ".alerts.on span.hot{" in head and ".alerts.on span.cold{" in head and "#9ccbff" in head,
          "冷 chip 必须有自己的蓝色样式；只留一套琥珀色的话冷热提示长得一模一样")
    check("shape_pill_css_kept", ('class="shape"' not in head) or (".shape{" in head),
          "温度带标题那句删掉了，但拥挤度总表的日期胶囊还在用 .shape；删样式会让它退成无边框裸文字")
    long_doc = '<p class="note">' + "口" * (NOTE_MAX_CHARS + 1) + '</p><p class="note rule">短</p>'
    check("long_notes_flags_overlong", len(long_notes(long_doc)) == 1 and f"{NOTE_MAX_CHARS + 1}字" in long_notes(long_doc)[0],
          f"脚注体检必须抓出超过 {NOTE_MAX_CHARS} 字的图注，并把字数写进 warning")
    check("long_notes_ignores_markup", long_notes('<p class="note">' + "<b>x</b>&amp;" * 3 + "</p>") == [],
          "体检要先剥标签再数字，否则 HTML 转义会把短图注误判成超长")
    check("build_reports_overlong_notes", '"overlong_notes"' in head and "图注超过{NOTE_MAX_CHARS}字" in head,
          "build 结果要带 overlong_notes 并进 warnings，否则脚注变长了上传前看不出来")
    check("notes_carry_no_config_prose", all(s not in head for s in [
        "{indicator.get('note', '')}", "{basket['note']}", "{spec.get('note','')}",
        "{item['featured_note']}", 'asia_charts["note"]',
    ]), "图注只写口径和读数；config 里的长说明不再拼进脚注（它们留在 config 和 methodology.md 里）")
    flow_cfg = load_json(CONFIG_PATH).get("cn_fund_flow", {}) if CONFIG_PATH.exists() else {}
    check("flow_config_present", bool(flow_cfg), "Restore the cn_fund_flow block in dashboard_config.json")
    check("flow_refresh_15min", int(flow_cfg.get("refresh_minutes", 0)) == FLOW_REFRESH_MIN == 15, "The A-share panel refreshes every 15 minutes; keep config and FLOW_REFRESH_MIN in step")
    check("flow_side_n", int(flow_cfg.get("side_n", 0)) == FLOW_SIDE_N, "Config side_n and FLOW_SIDE_N must agree, otherwise the snapshot and the browser refresh pick different boards")
    check("flow_host_push2delay", flow_cfg.get("host") == FLOW_HOST == "push2delay.eastmoney.com", "push2.eastmoney.com returns 502 here; the panel must stay on push2delay")
    endpoints = flow_endpoints()
    check("flow_endpoints_https", all(u.startswith(f"https://{FLOW_HOST}/") for u in endpoints.values()), "Both endpoints must be HTTPS on the approved host, or the browser blocks them as mixed content")
    check("flow_board_scope_sw3", flow_cfg.get("board_scope") == "m:90 s:8" and all("fs=m%3A90%2Bs%3A8" in endpoints[k] for k in ("rankTop", "rankBottom")), "Only 申万三级 (m:90 s:8) may be charted; m:90 t:2 mixes three levels and double-counts the same money")
    check("flow_rank_sorted_by_main", "fid=f62" in endpoints["rankTop"] and endpoints["rankTop"].endswith("po=1") and endpoints["rankBottom"].endswith("po=0"), "Rank both ends by 主力净流入 f62: po=1 for inflow, po=0 for outflow")
    check("flow_rank_two_sided", endpoints["rankTop"] != endpoints["rankBottom"] and "pz=100" in endpoints["rankTop"], "clist returns at most 100 rows per page, so the outflow side needs its own po=0 call instead of slicing the inflow page")
    check("flow_kline_minutes", "klt=1" in endpoints["kline"] and "f52" in endpoints["kline"], "The intraday curve needs 1-minute bars and the 主力净额 field f52")
    check("flow_sessions", FLOW_SESSIONS == [["09:30", "11:30"], ["13:00", "15:00"]], "Keep the two A-share cash sessions; the browser only auto-refreshes inside them")
    src = (SCRIPT_DIR / "sentiment_dashboard_cli.py").read_text(encoding="utf-8")
    monitor_src = (SCRIPT_DIR / "us_market_sections.py").read_text(encoding="utf-8")
    us_block = src.split("us_items = [")[1].split("\n    ]")[0] if "us_items = [" in src else ""
    us_rows = re.findall(r'strip_item\("([^"]+)"', us_block)
    us_panel_call = src.split('strip_panel(strip_titles["us"]')[1].split('strip_panel(strip_titles["cn_a"]')[0] if 'strip_panel(strip_titles["us"]' in src else ""
    check("us_strip_row_order", us_rows == ["周期/防御", "高盛动量股（GSCBHMOM）", "纳指/等权", "六大科技", "半导体", "软件板块（XSW）", "互联网", "必选消费"],
          "美股温度带行序已定稿：周期/防御、高盛动量股（GSCBHMOM）、纳指/等权在上，六大科技、半导体、软件板块（XSW）、互联网、必选消费在下")
    check("us_strip_no_splv_row", "低波防御" not in us_rows,
          "SPLV/SPX 方向与本带其他行相反，已移出美股温度带，只保留美股页那张 SPLV 图")
    check("us_strip_fixed_not_sorted", "fixed=True" in us_panel_call,
          "美股温度带必须传 fixed=True；否则前端按当日冷热重排，同口径的五行会被拆散")
    check("strip_fixed_order_js", "d.fixed?rk(a.scale)-rk(b.scale):rk(a.scale)-rk(b.scale)||b.x-a.x" in src,
          "前端排序要认 payload.fixed：fixed 时只按 scale 分组（Array.sort 稳定，保留声明顺序），否则仍按 x 降序")
    check("flow_axis_lunch_collapsed", "FLOW_SPAN=240" in src and "return v<=690?v-570:v-660" in src and "[180,'14:00']" in src,
          "午休 11:30-13:00 不占横轴：上午映射 0-120，下午 13:00 接 120-240，FLOW_SPAN=240；否则曲线在午休段被拉成一条横线")
    parsed, stamp = parse_flow_kline({"data": {"klines": ["2026-08-10 09:31,64903780.0,-7899385.0,-57034579.0,30138238.0,34765542.0"]}})
    check("flow_kline_parse_yi", parsed == [["09:31", 0.649]] and stamp == "2026-08-10 09:31", "Minute rows must map to (HH:MM, 主力净额亿元) with 1e8 scaling")
    empty = fund_flow_panel({"ok": False, "error": "接口失败", "asof": "", "boards": [], "rank": {"top": [], "bottom": []}})
    check("flow_panel_degrades", "data-flow=" in empty and "接口失败" in empty, "A failed snapshot must still render the panel shell and show the reason instead of breaking the build")
    import sentiment_dashboard_cli as _cli
    import urllib.error as _ue

    def _raise(code: int):
        def fake(url: str, timeout: int = 20):
            raise _ue.HTTPError(url, code, "test", None, None)
        return fake

    _orig_http = _cli.flow_http_json
    try:
        _cli.flow_http_json = _raise(502)
        blocked = _cli.fetch_fund_flow()
        _cli.flow_http_json = _raise(500)
        broken = _cli.fetch_fund_flow()
    finally:
        _cli.flow_http_json = _orig_http
    check("flow_502_is_blocked", blocked.get("blocked") is True and blocked.get("error") == FLOW_BLOCKED_NOTE and not blocked.get("ok"),
          "东财对本机 IP 回 502 要记成 blocked（只进构建日志 notices），不能当新故障天天上卡片")
    check("flow_other_errors_still_warn", not broken.get("blocked") and "板块排名接口失败" in broken.get("error", ""),
          "502 以外的失败仍是故障，必须照常进 warnings")
    check("flow_blocked_off_card", 'not flow_snapshot.get("ok") and not flow_snapshot.get("blocked")' in (SCRIPT_DIR / "sentiment_dashboard_cli.py").read_text(encoding="utf-8"),
          "卡片 warnings 要跳过 blocked 的资金流快照，改由 notices 记录")
    sample_rows = [
        {"index_code": "A.CSI", "index_name": "甲", "category": "行业", "crowding_percentile": 80.0, "crowding_level": "偏高",
         "daily_change_pct": 1.0, "probability_direction": "上涨", "probability_pct": 50.0, "probability_horizon_days": 10,
         "detail_url": "https://example.com/a", "history_1y": [[f"2025-0{1+i//9}-0{1+i%9}", 100.0 + i, 40.0 + i] for i in range(9)]},
        {"index_code": "B.CSI", "index_name": "乙", "category": "宽基", "crowding_percentile": 20.0, "crowding_level": "较低",
         "daily_change_pct": -1.0, "probability_direction": "下跌", "probability_pct": 44.0, "probability_horizon_days": 10,
         "detail_url": "https://example.com/b", "history_1y": [["2025-01-01", 100.0, 10.0], ["2025-01-02", 101.0, 11.0]]},
        {"index_code": "C.GI", "index_name": "丙", "category": "海外", "crowding_percentile": 5.0, "crowding_level": "低",
         "daily_change_pct": 0.5, "probability_direction": "上涨", "probability_pct": 60.0, "probability_horizon_days": 10,
         "detail_url": "https://example.com/c", "history_1y": []},
    ]
    thinned = crowding_board_rows(sample_rows)
    check("crowd_board_thins_history", len(thinned[0]["hp"]) == 5 and thinned[0]["hp"][-1] == 108.0 and thinned[0]["hc"][-1] == 48.0,
          "Sparkline history is thinned by CROWD_SPARK_STEP but the newest day must survive, so the last dot and the table number are the same day")
    check("crowd_board_pairs_stay_aligned", all(len(r["hp"]) == len(r["hc"]) for r in thinned),
          "Price and crowding arrays feed one chart; a length mismatch would silently shift the two lines")
    board_html = crowding_board({"rows": sample_rows, "trade_date": "2026-08-06"})
    check("crowd_board_ranks_split", '"rk_cn":1' in board_html.replace(" ", "") and '"rk_all":1' in board_html.replace(" ", ""),
          "Board must ship both a China-only rank and an all-index rank; reusing the vendor rank leaves gaps once overseas rows are hidden")
    check("crowd_board_hides_overseas_by_default", 'id="cbOverseas" type="checkbox">' in board_html and "含海外 1 个" in board_html,
          "海外 rows belong to the vendor board but not to the China tab default view; they stay behind an unchecked box")
    check("crowd_board_replaces_fixed_table", "指数与板块拥挤度" in board_html and 'id="cbRows"' in board_html and "地区" not in board_html,
          "China tab shows the full vendor board, not the old 11-name fixed table")
    check("crowd_board_degrades", '<p class="empty">' in crowding_board({"rows": []}),
          "A missing crowding mirror must render an explicit empty panel instead of stale numbers")
    check("crowd_board_js_reads_json", 'getElementById(\'cbData\')' in src and "JSON.parse(node.textContent)" in src,
          "Board payload rides a JSON script tag; escaping it into an HTML attribute would inflate the page")
    scoped = ".cb-spark" + " .cb-price"  # 拼出来找，写成字面量会把这一行自己算进去
    check("crowd_board_svg_style_unscoped", scoped not in src and ".cb-price{fill:none" in src and ".cb-crowd{fill:none" in src,
          "小图和悬停大图共用同一段 SVG；描边规则一旦写成 .cb-spark 后代选择器，大图里的 path 就退回默认黑色填充，整块变漆黑")

    # 左轴0 / 右轴50 对齐
    check("chart_align_constant", ALIGN_ZERO_TO_50 == (0.0, 50.0),
          "200DMA 乖离的中性位是 0，相对强弱 RSI 的中性位是 50；这两个锚点定死，不要改成别的组合")
    aligned_html = chart("t", [series_payload(pd.Series([1.0, 2.0], index=pd.date_range("2024-01-01", periods=2)), "l", "#fff", "left"),
                               series_payload(pd.Series([40.0, 60.0], index=pd.date_range("2024-01-01", periods=2)), "r", "#fff", "right")],
                         "", [0, 100], 300, None, False, None, ALIGN_ZERO_TO_50)
    check("chart_align_payload", "&quot;align&quot;:{&quot;left&quot;:0.0,&quot;right&quot;:50.0}" in aligned_html,
          "chart(align=...) 必须把锚点写进 payload，前端才知道要把左轴重新拉伸")
    check("chart_align_default_off", '&quot;align&quot;:null' in chart("t", [], "", None, 300).replace(" ", ""),
          "没传 align 的图保持原来的 auto-fit，不要给全站图表悄悄换纵轴")
    check("chart_align_js", "function alignLeft(L,R,a)" in src and "if(d.align&&hasRight&&!d.leftDomain&&!d.reverseLeft)L=alignLeft(L,R,d.align)" in src,
          "前端要有 alignLeft，并且只在有右轴、左轴未写死、左轴未反转时启用；左轴写死或反转时对齐会改掉既定口径")
    hover_uses_interpolated_date = "new Date(" + "t).toISOString()" in src and "nearest==null?t:nearest" not in src
    check(
        "chart_hover_clamps_to_last_date",
        "Math.min(c.xmax,Math.max(c.xmin," in src
        and "nearest==null||Math.abs(bt-t)" in src
        and not hover_uses_interpolated_date,
        "折线 hover 必须把日期钳到最后一根真实数据，不能按鼠标位置插出未来日期",
    )
    friday_morning = pd.Timestamp("2026-08-14 10:00", tz="America/New_York")
    friday_after = pd.Timestamp("2026-08-14 17:00", tz="America/New_York")
    monday_morning = pd.Timestamp("2026-08-17 10:00", tz="America/New_York")
    check("price_cache_stale_midweek", not price_cache_fresh("2026-08-11", friday_morning),
          "周二收盘在周五盘中不能再当新鲜缓存")
    check("price_cache_fresh_prev_session", price_cache_fresh("2026-08-13", friday_morning),
          "周五盘中，周四收盘仍可用")
    check("price_cache_stale_after_close", not price_cache_fresh("2026-08-13", friday_after),
          "周五收盘后应重拉，不能继续用周四")
    check("price_cache_weekend_uses_friday", price_cache_fresh("2026-08-14", monday_morning),
          "周一盘中，上周五收盘仍可用")
    saturday = pd.Timestamp("2026-08-15 12:00", tz="America/New_York")
    check("reuse_cache_only_when_us_closed",
          not should_reuse_price_cache("2026-08-13", friday_morning)
          and should_reuse_price_cache("2026-08-14", saturday)
          and not should_reuse_price_cache("2026-08-11", saturday),
          "工作日必须重拉 yfinance；只有美股没开盘且缓存已覆盖上一根完整日线才复用")
    check("download_prices_uses_session_gate", "should_reuse_price_cache(latest)" in src,
          "download_prices 必须走 should_reuse_price_cache，不能只看自然日新旧")

    def align_left_py(L: list[float], R: list[float], a: tuple[float, float]) -> list[float]:
        """Mirror of the JS alignLeft so the pixel math is testable without a browser."""
        rs = R[1] - R[0]
        p = (R[1] - a[1]) / rs
        k = p / (1 - p)
        top, bot = max(L[1] - a[0], 0.0), max(a[0] - L[0], 0.0)
        bot = max(bot, top / k)
        raw = bot * (1 + k)
        exp = math.floor(math.log10(abs(raw / 4) or 1))
        u = (raw / 4) / (10 ** exp)
        step = (1 if u <= 1 else 2 if u <= 2 else 2.5 if u <= 2.5 else 5 if u <= 5 else 10) * (10 ** exp)
        span = max(math.ceil(raw / step) * step, step)
        nb = span / (1 + k)
        return [a[0] - nb, a[0] + k * nb]

    def row(v: float, dom: list[float]) -> float:
        return (dom[1] - v) / (dom[1] - dom[0])

    lft = align_left_py([-8.0, 24.0], [0.0, 100.0], ALIGN_ZERO_TO_50)
    check("chart_align_same_row", abs(row(0.0, lft) - row(50.0, [0.0, 100.0])) < 1e-9,
          "对齐后左轴0与右轴50必须落在同一像素行；否则读者还得自己配准两条中性线")
    check("chart_align_keeps_data", lft[0] <= -8.0 and lft[1] >= 24.0,
          "重新拉伸左轴不能把数据裁掉：新区间必须仍然包住原来的最大最小值")
    check("chart_align_symmetric_at_50", abs(lft[0] + lft[1]) < 1e-9,
          "右轴是0–100时50在正中，所以左轴必须对称到0两侧；不对称说明比例算错了")
    check("chart_align_nice_ticks", abs(lft[1] / 2 - round(lft[1] / 2, 6)) < 1e-9 and lft[1] > 0,
          "对齐后仍要落在 niceStep 的整数刻度上，不能出现 23.7 这种轴标")
    check("us_dual_axis_charts_aligned", src.split("def refit_trend")[0].count("ALIGN_ZERO_TO_50") == 9,
          "「左轴距均线、右轴相对RSI30」这一族图共八处（六大科技、TSM/INTC/BRKB、SOX、XSW、两个等权篮子、SPLV、11个一级行业、港股四板块）"
          "加常量定义共 9 处引用；新增同族图必须一起传 ALIGN_ZERO_TO_50，否则同一页会出现两种纵轴基准")
    # ── 中国拆成 A股 / 港股两页 ──
    try:
        cfg = load_json(CONFIG_PATH)
    except Exception:
        cfg = {}
    cn_a_cfg = cfg.get("cn_a_indices", [])
    hk_cfg = cfg.get("hk_relative", {})
    check("cn_a_watchlist_order", [x.get("name") for x in cn_a_cfg] == ["国证2000", "创业板指", "科创50", "通信设备", "中证全指半导体", "红利低波", "CS电池"],
          "A股观察名单的顺序是约定好的，温度带和图表都照它排：国证2000 在最前，其余按创业板指、科创50、通信设备、中证全指半导体、红利低波、CS电池；改顺序等于改两处展示，必须在配置里一次改完")
    check("cn_a_featured_is_gz2000", [x.get("name") for x in cn_a_cfg if x.get("featured")] == ["国证2000"],
          "只有国证2000 标 featured，它的图单独挂在盘中资金流上方；标多个会把资金流面板挤到页面下半截")
    check("cn_a_featured_color_split", next((x.get("color") for x in cn_a_cfg if x.get("featured")), "") != "#ffbf3c"
          and next((x.get("color") for x in cn_a_cfg if x.get("featured")), "").lower() not in {"#ff9d3c", "#ffb03c"},
          "国证2000 那条点位线不能再用橙系：右轴拥挤度固定 #ffbf3c，两根线颜色太近就分不出哪根是哪根")
    check("cn_a_watchlist_codes", all(x.get("code") and x.get("color") for x in cn_a_cfg),
          "每个观察指数都要有供应商代码和线色；缺代码就取不到长历史，缺颜色会和拥挤度那根橙线撞色")
    check("cn_a_smallcap_choice_documented", any("中证2000" in str(x.get("note", "")) for x in cn_a_cfg),
          "小微盘这一格用的是国证2000而不是更纯的中证2000，原因（中证2000拥挤度只有2025-09起）必须写在配置里，"
          "不能让后面的人以为是随手选的")
    # ── A股宽基 RSI30：创业板指与沪深300，各一张「指数点位 + 自身 RSI」 ──
    broad_cfg = cfg.get("cn_broad_rsi", {})
    broad_src = src.split("def refit_trend")[0].split("── A股宽基：")[-1].split("── 港股：")[0]
    check("cn_broad_rsi_roster", [x.get("name") for x in broad_cfg.get("members", [])] == ["创业板指", "沪深300"],
          "宽基那组图固定是创业板指、沪深300 两张；顺序决定页面上的排布，改名单只改配置")
    check("cn_broad_rsi_codes", all(x.get("code") and x.get("color") for x in broad_cfg.get("members", [])),
          "每个宽基指数都要有供应商代码和线色；缺代码取不到长历史，缺颜色会和右轴那根橙线撞色")
    check("cn_broad_no_relative", "benchmark" not in broad_cfg and ("cn_broad" + "_bench") not in src.split("def refit_trend")[0],
          "用户 2026-08-12 去掉了「除以中证1000」的两张相对 RSI，只留自身动能；config 里再出现 benchmark 就是把相对图加回来了")
    check("cn_broad_above_featured", src.index("+ cn_broad_html") < src.index("+ cn_featured_html"),
          "宽基两张图排在国证2000 那张拥挤度图上方，位置是用户 2026-08-12 指定的")
    check("cn_broad_no_zero_align", "ALIGN_ZERO_TO_50" not in broad_src,
          "宽基两张图左轴是指数点位、没有有意义的零点，不做 0=50 对齐；硬对齐会把点位轴拉到 0、指数线压成平线")
    check("cn_broad_shares_rsi_period", "rsi_period = rel_signal[\"rsi_period\"]" in src and "wilder_rsi(frame[\"close\"], rsi_period)" in broad_src,
          "宽基图的 RSI 周期跟 relative_signal 走，和港股、美股行业那一族相对 RSI 用同一把尺子，不在这里写死 30")
    check("cn_broad_not_in_strip", "cn_broad" not in src.split("cn_a_items = [strip_item")[-1].split("cn_a_alerts =")[0],
          "宽基两张图不进总览A股温度带：温度带那八行读的是拥挤度分位，RSI 是另一套量纲，混进去点与点的距离就不可比了")
    check("cn_broad_one_chart_per_row", 'cn_broad_html = "".join(cn_broad_charts)' in broad_src and 'grid two">\' + "".join(cn_broad_charts)' not in broad_src,
          "宽基两张图各占一行（用户 2026-08-12 的要求）：套回 grid two 就又变成并排半宽，2023 年以来那段的拐点看不出来")
    turnover_cfg = cfg.get("cn_turnover", {})
    check("price_retry_singles", "for ticker in [t for t in unique if t not in result]" in src,
          "yfinance 批量下载会整块失败（本次掉过 MU）；漏掉的标的必须逐个补一次，否则等权篮子和半导体广度会少成分照画不误")
    check("price_missing_warns", "price_missing" in src and "行情缺失标的" in src,
          "补不回来的标的要进 warnings：上传前要求 warnings 为空，静默少成分等于把这条规矩绕过去")
    check("trend_ohlc_cache_survives_no_cache",
          'download_ohlc(\n        [x["ticker"] for x in trend_cfg],\n        "2014-01-01",\n        WORKSPACE / "state/sentiment-dashboard/yfinance_ohlc.pkl",\n    )' in src,
          "趋势线 OHLC 的缓存路径不能跟 use_cache 走：cron 用 --no-cache，没有兜底时 yfinance 抽一次风，AMZN 通道和 AFRM 支撑线整块消失而 build 仍退出 0")
    check("trend_ohlc_fresh_wins_over_cache",
          "fresh = dict(result)" in src and "if ticker not in result and ticker in cached" in src
          and "pd.to_pickle({**cached, **fresh}, cache_path)" in src,
          "缓存只在本次取数失败时兜底，且只有真取到的数据才回写缓存；否则兜底值会被当成新数据固化下来，缓存永远追不上")
    check("bbg_staleness_warns",
          stale_days(iso_date(pd.Timestamp.today() - timedelta(days=9))) == 9
          and stale_days("未知") is None and "彭博数据落后" in src,
          "彭博数据由外部链路投喂，断了页面照画不误且落后必须进 warnings 并提示未更新")
    check("trend_ohlc_stale_warns", "from_cache" in src and "趋势线OHLC取数失败，退回缓存" in src,
          "退回缓存必须进 warnings：兜底是为了不掉图，不是为了把几天前的价格当成今天的距离百分比默默发出去")
    check("turnover_components_exchange_wide", [c.get("sina_symbol") for c in turnover_cfg.get("components", [])] == ["sh000001", "sz399106"],
          "两市量能必须用上证综指+深证综指：深证成指只有500只成分，新浪按成分汇总的成交额比深市实际少四成，会把合计系统性画低")
    check("turnover_em_secids", [c.get("em_secid") for c in turnover_cfg.get("components", [])] == ["1.000001", "0.399106"],
          "浏览器端的分钟线 secid 要和历史基线用的是同两个指数，否则盘中曲线和基线不是同一个口径")
    check("turnover_full_bars_48", TURNOVER_FULL_BARS == 48,
          "A股一个完整交易日是48根5分钟K线；这个数用来剔除半日和缺数的日子，改小会把残缺日拉进基线中位数")
    check("turnover_unit_wanyi", TURNOVER_UNIT == 1e12,
          "累计成交额按万亿元显示，前端和后端必须共用同一个除数，否则轴标签和曲线差三个数量级")
    check("turnover_minute_matches_flow", [turnover_minute(t) for t in ("09:35", "11:30", "13:05", "15:00")] == [5, 120, 125, 240],
          "量能图和资金流图共用同一套横轴映射：午休不占横轴，上午09:30-11:30映射0-120，下午接120-240")
    empty_vol = turnover_panel({"ok": False, "error": "接口失败", "days": [], "today": [], "prev": [], "median": [], "asof": ""}, cfg)
    check("turnover_panel_degrades", "data-vol=" in empty_vol and "接口失败" in empty_vol,
          "取数失败时量能面板要照样渲染外壳并写出原因，不能让整次 build 挂掉，也不能画一张空图冒充正常")
    check("turnover_js_live_refresh", "function volFetch" in src and "cnOpen(st.cfg)" in src.split("function volInit")[1][:600],
          "今日曲线要在A股交易时段内自行刷新；只在 build 时抓一次的话，盘中打开页面看到的是上一次 cron 的量能")
    check("turnover_js_fallback_to_build", "st.live&&st.live.length?st.live:(s.today||[])" in src,
          "浏览器取数失败时必须回落到构建时那条曲线并在状态行标明，不能直接空白")
    check("hk_members_order", [x.get("name") for x in hk_cfg.get("members", [])] == ["恒生科技", "港股通高股息低波", "恒生消费", "恒生医疗"],
          "港股四个板块的顺序同样是约定好的，温度带和图表共用")
    check("hk_benchmark_substitution_documented", "MSCI" in str(hk_cfg.get("benchmark_note", "")) and hk_cfg.get("benchmark", {}).get("code") == "HSCE.HK",
          "基准不是 MSCI China ex A 而是恒生中国企业指数，替换理由必须留在配置里；否则页面等于默认宣称自己用了 MSCI 口径")
    hk_dividend = next((x for x in hk_cfg.get("members", []) if x.get("code") == "HSHYLV.HK"), {})
    check("hk_dividend_substitution_documented", "930914" in str(hk_dividend.get("note", "")) and "932596" in str(hk_dividend.get("note", "")),
          "用户点名的是中证 930914 / 932596，数据源两个都取不到，用的是恒生 HSHYLV 代替；替换理由必须留在配置里")
    check("hk_dividend_currency_reason", "930915" in str(hk_dividend.get("note", "")) and "港元" in str(hk_dividend.get("note", "")),
          "数据源里有 930915（930914 的人民币版），没选它是因为四张港股图都在和港元计价的基准比强弱；这个取舍要写下来")
    body = src.split("def refit_trend")[0]
    copy_compact = " ".join(copy_src.split())
    check("nav_split_china", 'UI_COPY["single_tabs"]' in src and '("cn_a", "A股市场"), ("cn_hk", "港股市场")' in copy_compact and '("china", "中国市场")' not in copy_compact + body,
          "中国页已经拆成 A股市场 / 港股市场两个 tab；留着旧的 china tab 会出现两套互相矛盾的中国视图")
    check("sections_match_nav", all(f'"{k}": ' in src or f'"{k}":' in src for k in ["overview", "us", "cn_a", "cn_hk", "asia", "us_stocks", "china_stocks"]),
          "sections 字典必须给 nav 里每个 tab 都备好内容，少一个 render_html 会 KeyError")
    check("split_panels_present", "def write_split_panels" in src and 'panels_dir = output.parent / "panels"' in src and len(UI_COPY_PANELS := ["market-overview", "us-market", "cn-market", "hk-market", "asia-leverage", "stocks"]) == 6 and all(slug in copy_src for slug in UI_COPY_PANELS),
          "build_dashboard 必须同时输出 6 个拆分监控面板目录与 _shared 资源")
    check("cn_leverage_card_moved", 'if x["name"] != "中国A股"' in src and "cn_leverage_card" in src,
          "中国A股杠杆卡搬到 A股市场页顶部；日韩台页必须同时把它去掉，否则同一张卡出现两次")
    check("ah_charts_on_hk_tab", '"cn_hk": (' in src and "ah_charts" in src.split('"cn_hk": (')[1][:400],
          "AH溢价两张图归港股市场页；留在 A股页会让人以为它是 A股指标")
    check("quantum_rolling_chart_removed",
          "量子计算等权组合" not in monitor_src and "quantum_rolling_60" not in src + monitor_src and "quantum_60" not in src + monitor_src,
          "量子等权 60 日滚动对数图已删（用户 2026-09-04）；预警那张「较 60 日低点涨幅」才是状态机用的，别把滚动图加回来")
    check("price_axis_no_zero_align", "(0.0, 0.0)" not in body + monitor_src,
          "隐波比图右轴是 QQQ 收盘价、没有零点，零线对齐会把价格轴拉到 0 附近、把 QQQ 压成一条平线")
    top_fn_src = monitor_src.split("def top_monitor_section")[1].split("def state_monitor_section")[0]
    check("qqq_vol_chart_after_confirm",
          'copy["chart_vol_title"]' in top_fn_src
          and top_fn_src.index('copy["chart_confirm_title"]') < top_fn_src.index('copy["chart_vol_title"]'),
          "QQQ 隐波比值图排在确认图之后；它后面只剩 put 保护提醒那张 VVIX/VIX 图，两张都是环境图")
    check("top_env_card_strong_weak_only",
          'env_value = "弱" if environment.get("weak_now") else "强"' in monitor_src
          and '预警环境：{env_value}' in monitor_src and "弱预警条件成立" not in monitor_src,
          "预警环境卡和标题标签的值只写「强」/「弱」（用户 2026-09-04）")
    env_reason_src = monitor_src.split("def _env_reasons")[1].split("def top_monitor_section")[0]
    check("top_env_card_reasons_both_states",
          "env_sub = _env_reasons(environment, top_cfg, signed)" in monitor_src
          and all(x in env_reason_src for x in ["未处于信号冷却期", "冷却期（剩余", "高于长期趋势线", "低于长期趋势线", "未达"])
          and "cool_drawdown_15_pct" in env_reason_src and "weak_trend_premium_pct" in env_reason_src,
          "预警环境卡强弱都在小字写原因：冷却期一项带本轮最低点和跌幅，趋势线一项带折溢价读数；门槛读 top_cfg，不另写死")
    vol_chart_block = top_fn_src.split('copy["chart_vol_title"]')[1][:600]
    check("qqq_vol_chart_price_only_right_axis",
          vol_chart_block.count('"QQQ 收盘价（右轴，美元）"') == 1 and "隐波(%)" not in vol_chart_block,
          "这张图右轴只放 QQQ 收盘价；把两条隐波原值加回来会让右轴同时装价格和百分比")
    check("qqq_vol_chart_shares_digest_series",
          "vol_ratio = finite(vol_ratio_series.iloc[-1])" in body and body.count('.rename("m1")') == 1,
          "图和速览卡片共用同一条 vol_ratio_series；各算一遍迟早会出现图上 0.85、卡片 0.84 的情况")
    check("qqq_vol_chart_threshold_line", 'hline(vol_crush, "left"' in monitor_src and 'hline(1.0, "left", "倒挂1.00"' in monitor_src,
          "塌陷线 0.85 与倒挂线 1.00 直接画在图里，阈值不藏进图注")
    check("vol_crush_is_085_environment_only", abs(US_VOL_CRUSH - 0.85) < 1e-9 and "level_down(vol_ratio, US_VOL_CRUSH" not in src.split("def refit_trend")[0],
          "隐波比线是 0.85，而且只作环境：不能再拿它当顶部信号写进 top_hits")
    check("top_charts_in_machine_order",
          top_fn_src.index('copy["chart_quantum_title"]') > top_fn_src.index('copy["chart_cyc_title_tpl"]')
          and top_fn_src.index('copy["chart_excess_title"]') < top_fn_src.index("momentum_chart,") < top_fn_src.index('copy["chart_confirm_title"]') < top_fn_src.index('copy["chart_vol_title"]') < top_fn_src.index('copy["chart_hedge_title"]')
          and '"过激：VVIX/VIX 与 Momentum RSI"' not in top_fn_src,
          "顶部图按预警、过激、确认、环境参考、尾部对冲窗口顺序排")
    top_call_src = src.split("top_monitor_html = top_monitor_section(", 1)[1].split("bbg_series_asof =", 1)[0]
    check("momentum_chart_in_top_excess",
          "momentum_chart=momentum_chart" in top_call_src and "过热阶段参考：高盛高动量股指数" in src
          and 'hline(top_cfg.excess_momentum_rsi, "left", f"过热{top_cfg.excess_momentum_rsi:.0f}"' in src
          and "momentum_chart" not in monitor_src.split("def state_monitor_section")[1],
          "高盛高动量图挂在顶部监控过激段，状态监控不再重复收它")
    us_section_src = src.split('"us": (', 1)[1].split('"cn_a":', 1)[0]
    check("us_monitor_section_order",
          us_section_src.index("bottom_monitor_html") < us_section_src.index("top_monitor_html") < us_section_src.index("state_monitor_section")
          and all(x in us_section_src for x in ["spx_trend_chart", "us_style_charts"]) and "momentum_chart" not in us_section_src,
          "美股页必须依次显示底部、顶部、市场状态监控")
    from dashboard_copy import UI_COPY as _UI_COPY
    us_bottom_copy = _UI_COPY.get("us_bottom", {})
    us_top_copy = _UI_COPY.get("us_top", {})
    us_state_copy = _UI_COPY.get("us_state", {})
    bottom_fn_src = monitor_src.split("def bottom_monitor_section", 1)[1].split("def _env_reasons", 1)[0]
    state_fn_src = monitor_src.split("def state_monitor_section", 1)[1]
    check("us_monitor_contents",
          all(x in bottom_fn_src for x in [
              "VVIX警戒", 'copy["card_stage"]', 'copy["card_env"]', 'copy["breadth_tpl"]',
              'copy["card_cyc"]', 'copy["card_qqq_dd"]', 'copy["card_vix"]', 'copy["card_vvix"]',
              'copy["card_spx_rsi"]', 'copy["card_qqq_rsi"]', "copy['vvix_meta_prefix']",
          ])
          and all(x in top_fn_src for x in [
              "并非顶部做空信号", 'copy["card_stage"]', 'copy["card_env"]', 'copy["card_cyc"]',
              'copy["card_quantum"]', 'copy["card_qqq_rsi"]', 'copy["card_qqq_200"]',
              'copy["card_mom"]', 'copy["card_confirm"]', 'copy["card_vol_ratio"]',
              'copy["card_put_hedge"]', "copy['vol_meta_prefix']", 'copy["chart_confirm_title"]',
          ])
          and all(x in state_fn_src for x in [
              'copy["section_title"]', 'copy["macro_group_title"]',
              'copy["spx_model_title"]', 'copy["valuation_title"]',
              "style_charts = us_style_charts",
          ])
          and "当前底部阶段" in str(us_bottom_copy.get("card_stage", ""))
          and "市场环境" in str(us_bottom_copy.get("card_env", ""))
          and "中期广度：标普一级行业站上200日均线" in str(us_bottom_copy.get("breadth_tpl", ""))
          and "历史极端恐慌罕见触发，触发后命中大底概率极高" in str(us_bottom_copy.get("vvix_meta_prefix", ""))
          and "顶部监测阶段" in str(us_top_copy.get("card_stage", ""))
          and "预警环境" in str(us_top_copy.get("card_env", ""))
          and "QQQ 30日/60日隐含波动率比值" in str(us_top_copy.get("card_vol_ratio", ""))
          and "指数看跌期权（Put）对冲窗口提醒" in str(us_top_copy.get("card_put_hedge", ""))
          and "仅作环境参考" in str(us_top_copy.get("vol_meta_prefix", ""))
          and "宏观利率、信用利差与估值快照" in str(us_state_copy.get("macro_group_title", ""))
          and "顶部环境：双重警戒" not in monitor_src and "量子组合尚未设阈值" not in monitor_src
          and "候选标签仅用于研究，不改变状态机" not in monitor_src
          and bottom_fn_src.count("压力区 ≤") >= 2
          and bottom_fn_src.count('class="monitor-chart-stack"') == 1
          and top_fn_src.count('class="monitor-chart-stack"') == 1
          and "us_style_charts.replace" not in state_fn_src
          and "'<div class=\"grid two\">' + \"\".join(sector_charts)" in body
          and "evaluate_bottom_framework(\n        cyc_rsi14,\n        spx,\n        qqq,\n        vix_price," in body,
          "美股监控内容或布局错误")

    payload_ok = {"code": 0, "data": [{"tradeDt": "20240102", "value": "12.5", "close": "1000"}, {"tradeDt": "20240101", "value": 11.0, "close": 990.0}, {"tradeDt": "20240102", "value": 13.0, "close": 1001.0}]}
    frame = index_history_frame(payload_ok)
    check("index_history_sorted_dedup", list(frame.index) == sorted(frame.index) and len(frame) == 2 and abs(float(frame["close"].iloc[-1]) - 1001.0) < 1e-9,
          "长历史必须按日期升序、同日只留最后一条；重复日会让 200DMA 和拥挤度错位一格")
    check("index_history_numeric", frame["value"].dtype.kind == "f" and frame["close"].dtype.kind == "f",
          "供应商偶尔把数字发成字符串，解析后必须是浮点，否则 rolling 会静默失败")
    check("index_history_rejects_partial", index_history_frame({"data": [{"tradeDt": "20240101", "close": 1.0}]}).empty,
          "缺 value（拥挤度）的载荷要判空，不能只画价格线假装拥挤度也在")
    check("index_history_empty_payload", index_history_frame({"data": []}).empty,
          "空载荷必须返回空表，交给上层显示“本次不可用”")
    absent = load_index_history(["__NOPE__.XX"], WORKSPACE, refresh=False)
    check("index_history_missing_reported", absent["missing"] == ["__NOPE__.XX"] and not absent["frames"],
          "取不到的代码要出现在 missing 里并进 warnings，不能悄悄少画一张图")
    check("index_history_cache_isolated", 'cache_dir = workspace / "state/sentiment-dashboard/index_history"' in src and 'seed_dir = workspace / "skills/index-crowding-research/state/cache/history"' in src,
          "刷新结果写本 skill 自己的 state；index-crowding-research 的缓存只读，不回写别人的目录")

    asia_cfg = cfg.get("asia_leverage_charts") or {}
    asia_countries = asia_cfg.get("countries") or []
    check("asia_charts_countries", [c.get("key") for c in asia_countries] == ["kr", "jp", "tw"],
          "日韩台页的图按韩国→日本→台湾排，顺序写在 config 里；总览那条温度带是按当日冷热排的，两处不必一致。中国A股不在这页，它的杠杆卡和两融图归 A股市场页")
    check("asia_charts_start_2023", asia_cfg.get("start") == "2023-01-01",
          "用户要求这组图统一取 2023-01 至今；改起点要同时改图注")
    asia_by_key = {c.get("key"): c for c in asia_countries}
    check("asia_msci_codes", {k: asia_by_key.get(k, {}).get("msci_code") for k in ("jp", "kr", "tw")} == {"jp": "939200", "kr": "941000", "tw": "915800"},
          "MSCI 指数代码：日本939200、韩国941000、台湾915800；三条都用月度收益对 EWJ/EWY/EWT 校验过（0.95/0.97/0.93）。按 key 取，页面顺序改了不影响这一条")
    check("asia_msci_local_currency", {k: asia_by_key.get(k, {}).get("msci_currency") for k in ("jp", "kr", "tw")} == {"jp": "JPY", "kr": "KRW", "tw": "TWD"},
          "MSCI 指数按本币取；换成 USD 会把汇率画进指数线，和本币口径的杠杆余额假性背离")
    check("asia_msci_price_variant", "index_variant=STRD" in src,
          "取 STRD 价格指数；换成 NETR/GRTR 是含息口径，和图注写的“不含股息”对不上")
    asia_ids = [i.get("id") for c in asia_countries for i in c.get("indicators", [])]
    check("asia_core_indicators", sorted(asia_ids) == sorted([
        "jp_margin_long", "jp_margin_ratio", "kr_credit_total", "kr_credit_ratio",
        "tw_margin_balance", "tw_usage_p90"]),
          "六个一级指标要和 crossview 卡片“核心指标”那行一一对应，别混进 tier2")
    check("asia_indicator_pairs_per_country", all(
        [i.get("id") for i in asia_by_key.get(k, {}).get("indicators", [])] == v
        for k, v in {"jp": ["jp_margin_long", "jp_margin_ratio"], "kr": ["kr_credit_total", "kr_credit_ratio"], "tw": ["tw_margin_balance", "tw_usage_p90"]}.items()),
          "每国两张图的先后也是定好的：日本先买残后倍率、韩国先余额后比率、台湾先余额后使用率")
    asia_series = asia_leverage_series(WORKSPACE)
    check("asia_series_all_present", all(not asia_series.get(x, pd.Series(dtype=float)).empty for x in asia_ids),
          "六条杠杆序列都要有值；缺哪条先跑对应的 jp/kr/tw-leverage-monitor")
    kr_ratio = asia_series.get("kr_credit_ratio", pd.Series(dtype=float))
    check("asia_kr_ratio_unit_pct", (not kr_ratio.empty) and 0.05 <= float(kr_ratio.iloc[-1]) <= 5.0,
          "信用融资/两市市值是百分数（近年 0.3—1.5%）；数量级不对说明分子分母的单位换算错了")
    jp_long = asia_series.get("jp_margin_long", pd.Series(dtype=float))
    check("asia_jp_unit_trillion", (not jp_long.empty) and 1.0 <= float(jp_long.iloc[-1]) <= 30.0,
          "信用买残换算到兆円后应在个位数到两位数；偏离说明百万円→兆円那步除错了")
    check("asia_composite_history_readonly", "asia_composite_history" in src and "composite_history.csv" in src
          and "to_csv" not in src.split("def asia_composite_history")[1].split("def stock_metrics")[0],
          "综合分历史由 scripts/asia_leverage_crossview.py 落盘，看板只读不写：同一个分数只能有一个定义，看板自己再算一遍迟早会和卡片对不上")
    check("asia_trail_jp_calendar_aligned", "timedelta(days=int(round(k * 28 / STRIP_TRAIL)))" in src,
          "日本是周频，往回数20个观测会跑到五个月前；周频行的1W/1M必须按日历日回溯才能和韩台两行同义")
    asia_panel_call = src.split('strip_titles["asia"]', 1)[1].split(")", 1)[0] if 'strip_titles["asia"]' in src else ""
    asia_strip_notes = _UI_COPY.get("overview", {}).get("strip_notes", {})
    check("asia_strip_note_no_current_only", "asia_trail_rows" in asia_panel_call and "asia_with_trail" in asia_panel_call and ("只显示" + "当前值") not in str(asia_strip_notes.get("asia_with_trail", "")) and "仅展示最新读数" not in str(asia_strip_notes.get("asia_with_trail", "")),
          "日韩台温度带已经有1W/1M位移；注释里不得出现只显示当前值/仅展示最新读数，仅保留历史缺失兜底文案")
    check("asia_no_zero_align", "asia_country_charts" in src and "ALIGN_ZERO_TO_50" not in src.split("def asia_country_charts")[1].split("def crowding_board_rows")[0],
          "这组图两轴量纲无关且 MSCI 指数没有零点，不能套左0右50的对齐；对齐会把指数压成平线")
    check("asia_msci_cache_isolated", 'cache_dir = workspace / "state/sentiment-dashboard/msci"' in src,
          "MSCI 缓存写本 skill 自己的 state 目录，不写进任何杠杆监控的 research-outputs")
    check("asia_missing_reported", 'f"日韩台图缺数据：' in src and 'f"MSCI指数用缓存未刷新：' in src,
          "取不到的序列和只走缓存的 MSCI 都要进 warnings，不能悄悄少画一张图或用旧数据冒充实时")

    # ── 每日速览卡片：阈值必须和页面同源，卡片只读 signals.json ──
    # 标记用拼接写，否则本函数里的字面量会成为第二个匹配点，切出来的就不是 build 那段代码了。
    digest_src = src.split("# ── 每日速览" + "的结构化读数")[1].split("    generated_at = ")[0]
    run_digest_src = src.split("def " + "run_digest")[1].split("def " + "main")[0]
    check("digest_writes_signals_json", 'SIGNALS_NAME).write_text(json.dumps(signals' in src and '"signals": str(output.parent / SIGNALS_NAME)' in src,
          "build 必须把 signals.json 和 HTML 一起落盘：卡片读这份 JSON，两边才是同一批数字")
    check("digest_reads_signals_only", "yfinance" not in run_digest_src and "download_" not in run_digest_src,
          "digest 只读 signals.json，不得自己取行情；一旦另取一次数，卡片和页面就会因为取数时点不同而对不上")
    check("digest_refuses_stale_signals", "DIGEST_STALE_DAYS" in run_digest_src,
          "signals.json 过期必须拒发：旧读数发出去在卡片上看不出来，比不发更糟")
    check("digest_thresholds_from_config", 'config["thresholds"]["us_top_rsi30"]' in digest_src
          and 'config["us_bottom_framework"]' in src
          and 'config["thresholds"]["rsi_hot"]' in digest_src and 'rel_signal["top_rsi"]' in digest_src,
          "速览的阈值取 config，不在这段里写死；页面上画的线改了，卡片必须跟着改，否则两边说法不一致")
    check("digest_tech_zone_matches_strip", abs(z_to_x(DIGEST_TECH_ZONE_Z) - ZONE_HOT) < 1e-9,
          "科技轮动的「过热」边界必须落在总览温度带的热区线上；两处不一致时读者会看到卡片说过热、页面没出 chip")
    check("digest_tech_lean_inside_zone", 0 < DIGEST_TECH_LEAN_Z < DIGEST_TECH_ZONE_Z,
          "偏热/偏冷是热区里侧的一档，门槛必须严格小于热区边界，否则五档退化成三档")
    check("digest_level_up_bands", level_up(63, 63, 3) == "触发" and level_up(61, 63, 3) == "接近" and level_up(55, 63, 3) == "" and level_up(None, 63, 3) == "",
          "上行阈值三档：到线=触发、差near以内=接近、其余空；读不到值必须返回空而不是接近")
    check("digest_level_down_bands", level_down(32, 32, 3) == "触发" and level_down(34, 32, 3) == "接近" and level_down(50, 32, 3) == "",
          "下行阈值三档同上；把接近算成触发会让卡片天天喊信号")
    check("digest_tech_state_bands", (tech_state(1.6), tech_state(1.0), tech_state(0.1), tech_state(-1.0), tech_state(-1.6), tech_state(None))
          == ("过热", "偏热", "中性", "偏冷", "过冷", "无数据"),
          "科技轮动五档：过热/偏热/中性/偏冷/过冷，读不到值单独一档，不能默认成中性")
    check("digest_afrm_gap_sign", "level_up(afrm_gap, 0.0, DIGEST_NEAR_TREND_PCT)" in digest_src,
          "AFRM 的 bottom_gap 是（趋势底÷现价−1）×100，负数表示线还在现价下方；跌破是 gap 上穿 0，方向写反会天天报跌破")
    check("digest_covers_five_markets", all(f'"## {x}"' in src.split("def digest_lines")[1].split("def signals_join")[0]
                                            for x in ("美股市场", "美股个股", "A股市场", "港股市场", "日韩台市场")),
          "卡片固定五个栏目，缺一个就等于那块市场当天没被看过；栏目由用户 2026-08-12 指定")
    check("digest_quiet_sections_show_reading", all(f'{x}.get("reading"' in src.split("def digest_lines")[1].split("def signals_join")[0] or f"'reading'" in src.split("def digest_lines")[1].split("def signals_join")[0]
                                                    for x in ("us", "cn", "hk")),
          "无信号的栏目也要带上当前读数：只写「无信号」时读者分不清是真没信号还是取数挂了")
    digest_render = src.split("def digest_lines")[1].split("def signals_join")[0]
    momentum_block_src = digest_src.split("momentum_hits: list[str] = []")[1].split("ndx_spw_hits: list[str] = []")[0]
    ndx_block_src = digest_src.split("ndx_spw_hits: list[str] = []")[1].split("rotation_names")[0]
    check("digest_momentum_is_gscbhmom_only",
          "last_ratio" not in momentum_block_src and "last_mom" in momentum_block_src
          and "过热" not in ndx_block_src and "过冷" not in ndx_block_src and '"ndx_spw": {' in digest_src
          and "mom_lag" in momentum_block_src,
          "Momentum 行只判 GSCBHMOM，Bloomberg CSV 落后于美股收盘时写出截止日；纳指/等权自成一块，不用过热/过冷字眼（CY 2026-09-30）")
    check("digest_rel_readings_follow_hits",
          '"reading": sector_reading' in digest_src and '"reading": megacap_reading' in digest_src
          and "if not sector_hits" in digest_src and "if not megacap_hits" in digest_src,
          "行业、六大科技的读数句按实际命中数写，没命中才写「全在 40—60 之间」（2026-09-29 六个行业过冷，reading 仍写 11 个全在中间）")
    check("digest_stocks_quiet_when_no_hit", 'hit = st.get(key, {}).get("hit")' in digest_render and "if hit:" in digest_render,
          "TSM/AFRM 没信号就整行不写（用户 2026-08-12 口径）；写成「未接近」天天一样，读者会连真出信号那天一起略过")
    check("digest_has_no_methodology_section", "## 数据与口" + "径" not in digest_render,
          "「数据与口径」整栏已按用户 2026-08-12 的要求删除，口径只留在页面上，卡片不复述")
    check("digest_keeps_warnings", 'signals.get("warnings"' in digest_render,
          "warnings必须保留数据缺失提示")

    # ── 卡片 2.0 版式（用户 2026-09-07）：顶部/底部各成一节，信号着色，无信号读数缩成小字 ──
    def card_fixture(top_active: bool, bottom_active: bool) -> dict[str, Any]:
        top_fw = {
            "ok": True, "stage": "预警" if top_active else "无信号", "active": top_active,
            "summary": "预警：2028-01-05 周期/防御 RSI30≥63" if top_active else "无活动信号",
            "metrics": {"gsp_rsi30": 65.2, "quantum_from_low_pct": 40.0, "qqq_rsi14": 66.0, "qqq_vs_200dma_pct": 12.0, "momentum_rsi": 71.0},
            "environment": {"summary": "强预警环境", "weak_now": False, "cooldown": {"active": False}, "trend_premium_pct": 9.5},
            "put_hedge": {"on": False, "summary": "无put保护提醒：VVIX/VIX 5.0（提醒线≥6.5）"},
            "projection": {"active": False},
        }
        bottom_fw = {
            "ok": True, "stage": "预警" if bottom_active else "无信号", "active": bottom_active,
            "projection": {"active": bottom_active, "scenario": {"available": False},
                           "thresholds": {"pressure_spx_close": 7000.0, "pressure_qqq_close": 650.0, "pressure_vix": 20, "deep_spx_close": 6800.0, "deep_qqq_close": 630.0}},
            "environment": {"summary": "正常趋势候选", "tone": "neutral",
                            "breadth": {"sector_above_200dma": 8, "sector_expected": 11, "semiconductor_above_200dma_pct": 90.9}},
        }
        topbottom = {
            "top_framework": top_fw, "bottom_framework": bottom_fw, "vol_ratio": 0.9, "lines": {"vol_crush": 0.85},
            "bottom_hits": ["预警：2028-01-05 周期股/防御股相对指数RSI14≤32；等待压力"] if bottom_active else [],
            "bottom_reading": "底部框架：无活动信号；周期股/防御股相对指数 RSI14 44.1",
        }
        loud = top_active or bottom_active
        return {
            "date": "2028-01-06",
            "us_market": {"topbottom": topbottom,
                          "momentum": {"hits": ["Momentum 过热（GSCBHMOM RSI14 71.0）"] if loud else [], "reading": "GSCBHMOM RSI14 44.4"},
                          "ndx_spw": {"hits": ["触发偏窄 · 纳指/等权 RSI14 73.0（线 70），纳指跑赢等权过快，涨幅集中在大权重股"] if loud else [],
                                      "reading": "纳指/等权 RSI14 52.0"},
                          "tech_rotation": [{"name": "Mega tech", "state": "过热"}, {"name": "半导体", "state": "中性"}],
                          "sectors": {"hits": [], "reading": "11 个一级行业相对SPX RSI30 全在中间区"}},
            "us_stocks": {"megacap": {"hits": [], "reading": "六只相对QQQ RSI30 全在 40—60 之间"},
                          "TSM": {"hit": "TSM 距100DMA -1.0%，触发" if loud else ""}, "AFRM": {"hit": ""}},
            "cn_a": {"hits": ["红利低波 拥挤度 91.7% 过高（线 90）"] if loud else [], "reading": "两融余额 RSI14 33.2"},
            "hk": {"hits": [], "reading": "AH溢价 RSI30 -49.5"},
            "asia": {"hits": [], "reading": "韩国 50 · 日本 69 · 台湾 78"},
            "warnings": ["BBG CSV 停在 2028-01-03"] if loud else [],
        }

    def card_elements(card: dict[str, Any]) -> list[dict[str, Any]]:
        return card.get("body", {}).get("elements", [])

    def section_index(card: dict[str, Any], title: str) -> int:
        for i, el in enumerate(card_elements(card)):
            if el.get("tag") == "markdown" and str(el.get("content", "")).startswith(f"**{title}**"):
                return i
        return -1

    try:
        loud_card = digest_card(card_fixture(True, True))
        bottom_card = digest_card(card_fixture(False, True))
        quiet_card = digest_card(card_fixture(False, False))
        loud_dump = json.dumps(loud_card, ensure_ascii=False)
        quiet_dump = json.dumps(quiet_card, ensure_ascii=False)
        loud_md = "\n".join(digest_lines(card_fixture(True, True)))
        tags_used = {el.get("tag") for c in (loud_card, bottom_card, quiet_card) for el in card_elements(c)}
        check("digest_card_schema_2", all(c.get("schema") == "2.0" for c in (loud_card, bottom_card, quiet_card))
              and tags_used <= {"markdown", "hr"} and len(loud_card["header"]["text_tag_list"]) == 2,
              "速览卡片走 Card Schema 2.0，只用 markdown/hr 两种元素（2.0 不支持 note），标题栏两枚标签写顶部、底部阶段")
        top_i, bottom_i = section_index(loud_card, DIGEST_CARD_TITLE_TOP), section_index(loud_card, DIGEST_CARD_TITLE_BOTTOM)
        top_lead = card_elements(loud_card)[top_i]["content"].splitlines()[0] if top_i >= 0 else ""
        bottom_lead = card_elements(loud_card)[bottom_i]["content"].splitlines()[0] if bottom_i >= 0 else ""
        check("digest_card_top_bottom_split", 0 <= top_i < bottom_i
              and f"<font color='{DIGEST_CARD_HOT}'>" in top_lead and f"<font color='{DIGEST_CARD_COLD}'>" in bottom_lead
              and "预警：2028-01-05 周期股/防御股相对指数RSI14≤32；等待压力" in bottom_lead
              and "- 底部收盘门槛：" in card_elements(loud_card)[bottom_i]["content"],
              "顶部和底部各成一节、顶部在前；活动的顶部信号标红、底部信号标绿；底部活动时收盘门槛以 bullet 列在底部那节里")
        check("digest_card_header_follows_state_machines",
              loud_card["header"]["template"] == DIGEST_CARD_HOT and bottom_card["header"]["template"] == DIGEST_CARD_COLD
              and quiet_card["header"]["template"] == "indigo"
              and loud_card["header"]["text_tag_list"][0]["color"] == DIGEST_CARD_HOT
              and quiet_card["header"]["text_tag_list"][0]["color"] == DIGEST_CARD_QUIET,
              "标题栏颜色跟状态机走：顶部活动=红，只有底部活动=绿，都没有=indigo；标签颜色同理")
        quiet_top = section_index(quiet_card, DIGEST_CARD_TITLE_TOP)
        quiet_small = card_elements(quiet_card)[quiet_top + 1] if quiet_top >= 0 else {}
        check("digest_card_quiet_readings_small",
              quiet_small.get("text_size") == "notation" and "周期股/防御股相对指数 RSI30 65.2" in quiet_small.get("content", "")
              and f"<font color='{DIGEST_CARD_QUIET}'>" in quiet_small.get("content", "")
              and "<font color='red'>" not in card_elements(quiet_card)[quiet_top]["content"]
              and "TSM" not in quiet_dump and "⚠️" not in quiet_dump,
              "无信号时读数照写但缩成灰色小字（notation），第一行不着色；TSM/AFRM 没信号整行不出现；没有 warnings 就没有 ⚠️ 行")
        check("digest_card_hits_and_warnings_painted",
              "TSM：" in loud_dump and "红利低波 拥挤度 91.7% 过高（线 90）" in loud_dump
              and f"<font color='{DIGEST_CARD_WARN}'>⚠️ BBG CSV 停在 2028-01-03</font>" in loud_dump
              and json.dumps(card_elements(loud_card)[-1], ensure_ascii=False).find("⚠️") >= 0,
              "触发的信号按方向着色（热红、冷绿、其余橙），warnings 橙色放在卡片最后一段")
        check("digest_card_text_from_same_signals",
              all(text in loud_md and text in loud_dump for text in (
                  "预警：2028-01-05 周期股/防御股相对指数RSI14≤32；等待压力", "Momentum 过热（GSCBHMOM RSI14 71.0）",
                  "红利低波 拥挤度 91.7% 过高（线 90）", "TSM 距100DMA -1.0%，触发", "周期股/防御股相对指数 RSI30 65.2")),
              "卡片 2.0 和 digest.md 读同一份 signals.json、用同一批渲染函数：信号文字两边必须一字不差")
        check("digest_ndx_spw_own_line",
              "- 纳指/等权：触发偏窄 · 纳指/等权 RSI14 73.0" in loud_md
              and not any(line.startswith("- Momentum：") and "纳指/等权" in line for line in loud_md.splitlines())
              and f"纳指/等权：<font color='{DIGEST_CARD_WARN}'>触发偏窄" in loud_dump
              and "纳指/等权：无信号（纳指/等权 RSI14 52.0）" in quiet_dump,
              "纳指/等权单独一行、不并进 Momentum；热端写偏窄、冷端写扩散，卡片标橙（CY 2026-09-30：RSP 跌了一个月，卡片却写 Momentum 触发过热）")
    except Exception as exc:  # noqa: BLE001
        check("digest_card_renders", False, f"digest_card 在合成 signals 上抛错：{type(exc).__name__}: {exc}")
    check("digest_writes_card_json", "digest_card(signals)" in run_digest_src and '"card_path"' in run_digest_src,
          "run_digest 必须同时落 digest.card.json 并在 JSON 里回 card_path，scripts/sentiment_dashboard_digest.sh 靠它决定发 2.0 卡片")
    bottom_cfg = cfg.get("us_bottom_framework", {})
    environment_cfg = cfg.get("us_bottom_environment", {})
    bottom_engine_src = (SCRIPT_DIR / "us_bottom_framework.py").read_text(encoding="utf-8")
    check("bottom_framework_vix_present", cfg.get("market_tickers", {}).get("VIX") == "^VIX",
          "压力条件需要^VIX数据")
    check("bottom_framework_pressure_rsi38", bottom_cfg.get("pressure_index_rsi") == 38 and bottom_cfg.get("pressure_vix") == 20,
          "压力口径固定为RSI14≤38且VIX≥20")
    check("bottom_framework_deep_strict30", "row.spx_rsi14) < cfg.deep_index_rsi" in bottom_engine_src
          and "row.qqq_rsi14) < cfg.deep_index_rsi" in bottom_engine_src,
          "深度超卖要求SPX和QQQ同日RSI14<30")
    for name, ok, hint in bottom_environment_checks(environment_cfg, src, digest_render):
        check(name, ok, hint)
    check("digest_contains_bottom_framework", '"bottom_framework": bottom_framework' in digest_src
          and "bottom_framework.get(\"active\")" in digest_src,
          "每日速览必须读取完整底部状态机")
    check("digest_bottom_always_visible", '"- 美股底部："' in digest_render
          and ("topbottom.get('bottom_reading'" in digest_render or 'topbottom.get("bottom_reading"' in digest_render),
          "每日速览必须单列美股底部状态")
    bottom_digest_src = digest_src.split("bottom_hits: list[str] = []")[1].split("momentum_hits: list[str] = []")[0]
    check("digest_no_vvix_bottom_signal", "US_VVIX_PANIC" not in digest_src and "VVIX" not in bottom_digest_src,
          "定稿底部框架不用 VVIX；不能再把 VVIX≥160 单独冒充底部信号（顶部 put 保护提醒的 VVIX/VIX 是另一回事）")
    check("bottom_framework_uses_completed_close", "_completed_us_daily_frame" in bottom_engine_src
          and "time(16, 15)" in bottom_engine_src,
          "底部框架只认完整收盘；美东16:15前必须剔除Yahoo仍在变化的当日daily bar")
    check("bottom_projection", "projection_digest_lines" in digest_render, "活动观察窗必须显示收盘预测")
    # 无信号阶段的盘中预警情景（用户 2026-10-14：盘中周期股/防御股相对指数 RSI14 已到 27.6，卡片仍只写无活动信号）
    try:
        from zoneinfo import ZoneInfo
        idx = pd.bdate_range(end="2028-01-06", periods=300)
        t = np.arange(len(idx))
        syn_spx = pd.Series(5000 + 3 * t + 40 * np.sin(t / 5), index=idx)
        syn_qqq = pd.Series(500 + 0.3 * t + 5 * np.sin(t / 5), index=idx)
        syn_vix = pd.Series(15.0, index=idx)
        syn_gsp = pd.Series(100 + 0.05 * t + 0.5 * np.sin(t / 3), index=idx)
        syn_gsp.iloc[-1] = syn_gsp.iloc[-2] * 0.97
        syn_cfg = BottomFrameworkConfig(**bottom_cfg)
        et = ZoneInfo("America/New_York")
        intraday = evaluate_bottom_framework(wilder_rsi(syn_gsp), syn_spx, syn_qqq, syn_vix, syn_cfg,
                                             now=datetime(2028, 1, 6, 10, 30, tzinfo=et), gsp_close=syn_gsp)
        settled = evaluate_bottom_framework(wilder_rsi(syn_gsp), syn_spx, syn_qqq, syn_vix, syn_cfg,
                                            now=datetime(2028, 1, 6, 17, 0, tzinfo=et), gsp_close=syn_gsp)
        gate = intraday["alert_projection"]["thresholds"]["gsp_close"]
        gate_rsi = wilder_rsi(pd.concat([syn_gsp.iloc[:-1], pd.Series([gate], index=[idx[-1]])])).iloc[-1]
        alert_lines = alert_projection_digest_lines(intraday)
        check("bottom_alert_projection_intraday",
              intraday["stage"] == "无信号" and intraday["alert_projection"]["scenario"]["alert_would_trigger"]
              and abs(gate_rsi - syn_cfg.gsp_alert_rsi) < 0.01
              and len(alert_lines) == 2 and alert_lines[0].startswith("- 底部盘中情景：") and "会触发底部预警" in alert_lines[0]
              and alert_lines[1].startswith("- 底部预警收盘门槛：周期股/防御股相对指数≤")
              and settled["stage"] == "预警" and not settled["alert_projection"].get("active"),
              "无信号阶段盘中破预警线要写情景和收盘门槛（门槛价代回去 RSI14 正好是预警线）；收盘后状态机自己进预警，这段就不再出")
        quiet_fixture = card_fixture(False, False)
        quiet_fixture["us_market"]["topbottom"]["bottom_framework"]["alert_projection"] = intraday["alert_projection"]
        alert_card = digest_card(quiet_fixture)
        alert_bottom = card_elements(alert_card)[section_index(alert_card, DIGEST_CARD_TITLE_BOTTOM)]["content"]
        check("digest_card_bottom_alert_intraday",
              alert_bottom.splitlines()[0].endswith(f"无活动信号 · <font color='{DIGEST_CARD_COLD}'>盘中已破预警线，待收盘确认</font>")
              and "- 底部盘中情景：" in alert_bottom and "- 底部预警收盘门槛：" in alert_bottom
              and alert_card["header"]["text_tag_list"][1]["text"]["content"] == "底部 盘中破预警线"
              and alert_card["header"]["template"] == "indigo"
              and "盘中已破预警线" not in json.dumps(quiet_card, ensure_ascii=False)
              and "alert_projection_lead(bottom_framework)" in src.split("us_alerts = zone_chips")[1][:1200]
              and "gsp_close=cyc_price" in body,
              "盘中破预警线时卡片底部标题行补绿字、情景和门槛正常字号、右上角标签改「底部 盘中破预警线」，页面温度带挂 chip；标题栏颜色仍跟正式状态机走；没破线时一个字都不多")
    except Exception as exc:  # noqa: BLE001
        check("bottom_alert_projection_intraday", False, f"盘中预警情景在合成数据上抛错：{type(exc).__name__}: {exc}")
    top_cfg_dict = cfg.get("us_top_framework", {})
    top_engine_src = (SCRIPT_DIR / "us_top_framework.py").read_text(encoding="utf-8")
    check("top_framework_rsi30_single_source", top_cfg_dict.get("gsp_warn_rsi30") == cfg.get("thresholds", {}).get("us_top_rsi30"),
          "顶部预警线在 us_top_framework.gsp_warn_rsi30 与 thresholds.us_top_rsi30 各存一份，两处必须相等，否则图上的线和状态机的门槛对不上")
    check("top_framework_uses_completed_close", "_completed_us_daily_frame" in top_engine_src,
          "顶部框架只认完整收盘；美东16:15前必须剔除Yahoo仍在变化的当日daily bar")
    check("top_framework_vvix_present", cfg.get("market_tickers", {}).get("VVIX") == "^VVIX" and cfg.get("market_tickers", {}).get("VIX") == "^VIX",
          "指数 put 保护提醒的 VVIX/VIX 需要 ^VVIX 和 ^VIX 两条序列")
    check("top_framework_inputs_from_page_series", "evaluate_top_framework(\n        cyc_rsi30,\n        quantum_index,\n        mom,\n        spx,\n        qqq,\n        vix_price,\n        vvix_price,\n        trend_premium," in body,
          "状态机输入必须是页面上画图的同一批序列（周期/防御 RSI30、量子等权指数、Momentum、SPX/QQQ、VIX/VVIX、趋势折溢价），不另取一份")
    check("digest_top_always_visible", "top_digest_lines(topbottom, fmt)" in digest_render and '"- 美股顶部：' in top_engine_src and '"- 顶部环境：' in top_engine_src
          and '"- 指数 put 保护：' in top_engine_src,
          "每日速览必须单列美股顶部阶段、顶部环境和指数 put 保护提醒，没信号也要带读数")
    check("top_framework_excess_three_vvix_put_hedge",
          'frame["excess"] = frame["exc_qqq_rsi"] | frame["exc_qqq_200"] | frame["exc_mom"]' in top_engine_src
          and "exc_vvix_vix" not in top_engine_src and '"put_hedge": put_hedge' in top_engine_src
          and "put_hedge" in src.split("us_alerts = zone_chips")[1][:600] and '"put_hedge_vvix_vix": top_cfg.put_hedge_vvix_vix' in src
          and 'metric_card(copy["card_put_hedge"]' in monitor_src and '指数看跌期权（Put）对冲窗口提醒' in copy_src and top_cfg_dict.get("put_hedge_vvix_vix") == 6.5,
          "过激只剩三条，VVIX/VIX 拆成独立的每日 put 保护提醒：引擎写 put_hedge，页面卡片、温度带 chip 和 signals.lines 都要带上，门槛 6.5 读 config")
    check("top_status_command", '"top-status"' in src and "def run_top_status" in src,
          "run.sh top-status 读取 build 同批次的顶部状态，和 bottom-status 对称")
    for name, ok, hint in top_framework_checks(top_cfg_dict, src, digest_render):
        check(name, ok, hint)
    check("digest_us_asof_from_us_tickers", '"QQQ", "SPY", "^GSPC"' in digest_src and '"us": us_as_of' in digest_src,
          "美股截止日只能取美股序列。拿全部行情的最大值，美东盘前跑时会把A股当天收盘写成美股截止日，卡片就自称有还没发生的美股数据")

    for rel in ["temp/bbg_data_latest.csv", "state/cn-leverage/margin_core.csv", "state/cn-leverage/index_prices.csv", "research-outputs/index-crowding/official-all.json",
                "research-outputs/jp-leverage/data/margin_weekly.csv", "research-outputs/kr-leverage/data/kofia_credit.csv", "research-outputs/tw-leverage/data/margin_market.csv",
                "state/sentiment-dashboard/gs_prime_positioning.json"]:
        check("source_" + Path(rel).name, (WORKSPACE / rel).exists(), f"Run the upstream monitor that creates {rel}")

    from gs_prime_positioning import evaluate_gs_prime_signals, extract_leverage_from_text
    sample_extract = extract_leverage_from_text(
        "Risk Exposures: Across US Fundamental L/S, Gross leverage rose +2.2 pts to 216.1% (64th percentile 1-Year) "
        "while Net leverage fell -0.5 pts to 43.4% (five-year low). L/S ratio (MV) fell -0.9% to 1.54x (1st percentile 5-Year).",
        "2026-10-26", "mail:134703", "US Equities Color: On The Week",
    )
    check("gs_prime_parser_extracts_leverage",
          len(sample_extract) == 1 and sample_extract[0]["date"] == "2026-10-23"
          and sample_extract[0]["gross_leverage"] == 216.1 and sample_extract[0]["gross_pctile_1y"] == 64
          and sample_extract[0]["net_leverage"] == 43.4 and sample_extract[0]["net_pctile_5y"] == 0,
          "GS Prime 周报解析器必须准确提取总杠杆/净杠杆、1Y/5Y分位数并将周末/周一汇总日期对齐到周五收盘")
    syn_gp = evaluate_gs_prime_signals(
        {
            "leverage_history": [{"date": "2026-10-23", "doc_id": "mail:134703", "gross_leverage": 216.1, "net_leverage": 43.4, "gross_pctile_1y": 64, "net_pctile_5y": 0}],
            "mag7_history": [{"date": "2026-10-09", "net_exp": 21.19, "gross_exp": 10.14}],
        },
        cfg,
    )
    check("gs_prime_signal_alert_and_lag_rule",
          len(syn_gp["hits"]) == 1 and "极度出清" in syn_gp["hits"][0] and "43.4%" in syn_gp["hits"][0]
          and syn_gp["chips"] and syn_gp["chips"][0]["tone"] == "cold"
          and "GS Prime Mag7：净敞口 21.2%" in syn_gp["mag7_quiet"]
          and "gs_prime_html=gs_prime_html" in us_section_src,
          "GS Prime 净杠杆5年0分位触发极度出清绿字预警，滞后超过7天的Mag7历史读数进quiet小字不误报当期拥挤")

    # ── JEPQ—QQQ Put 价差三级灯：离线计算、阈值两侧、陈旧拦截、统一校验与变色检测 ──
    import tempfile
    from jepq_signal import (
        JEPQ_DISCLAIMER,
        detect_jepq_light_change,
        evaluate_jepq_signal,
        load_jepq_state,
        save_jepq_light,
        validate_jepq,
    )

    def _jepq_series(daily_step_std: float, d8_gap_target: float, n: int = 25) -> tuple[pd.Series, pd.Series]:
        dates = pd.bdate_range("2026-10-01", periods=n)
        # 构造交替涨跌幅使近 20 日样本标准差精确对应目标年化波动率。
        alt = np.array([1.0 if i % 2 == 0 else -1.0 for i in range(n - 1)], dtype=float)
        rets = alt * (daily_step_std / float(pd.Series(alt[-20:]).std())) if daily_step_std > 0 else np.zeros(n - 1)
        qqq_vals = [100.0]
        for r in rets:
            qqq_vals.append(qqq_vals[-1] * (1.0 + float(r)))
        qqq_s = pd.Series(qqq_vals, index=dates, dtype=float)
        jepq_vals = [50.0] * n
        qqq_8d_ret = qqq_vals[-1] / qqq_vals[-9] - 1.0
        jepq_8d_ret = qqq_8d_ret - d8_gap_target
        jepq_vals[-1] = jepq_vals[-9] * (1.0 + jepq_8d_ret)
        jepq_s = pd.Series(jepq_vals, index=dates, dtype=float)
        return qqq_s, jepq_s

    check("jepq_wired_in_build_and_signals",
          'tickers += ["QQQ", "JEPQ"]' in src
          and "evaluate_jepq_signal(" in src
          and '"jepq": jepq_signal' in src
          and "any(t in prices for t in existing_tickers)" in src,
          "JEPQ 必须复用看板现有 download_prices 批量取价、保留既有标的下载失败门禁并写入 signals['jepq']")
    qqq_green, jepq_green = _jepq_series(0.199 / math.sqrt(252), 0.00299)
    res_green = evaluate_jepq_signal(qqq_green, jepq_green)
    qqq_yellow_lo, jepq_yellow_lo = _jepq_series(0.201 / math.sqrt(252), 0.00299)
    res_yellow_lo = evaluate_jepq_signal(qqq_yellow_lo, jepq_yellow_lo)
    qqq_yellow_hi, jepq_yellow_hi = _jepq_series(0.249 / math.sqrt(252), 0.001)
    res_yellow_hi = evaluate_jepq_signal(qqq_yellow_hi, jepq_yellow_hi)
    qqq_red_gap, jepq_red_gap = _jepq_series(0.15 / math.sqrt(252), 0.00301)
    res_red_gap = evaluate_jepq_signal(qqq_red_gap, jepq_red_gap)
    qqq_red_vol, jepq_red_vol = _jepq_series(0.251 / math.sqrt(252), 0.001)
    res_red_vol = evaluate_jepq_signal(qqq_red_vol, jepq_red_vol)
    qqq_red_both, jepq_red_both = _jepq_series(0.28 / math.sqrt(252), 0.005)
    res_red_both = evaluate_jepq_signal(qqq_red_both, jepq_red_both)
    res_stale_jepq = evaluate_jepq_signal(qqq_green, jepq_green.iloc[:-1])
    res_stale_ref = evaluate_jepq_signal(
        qqq_green.iloc[:-1], jepq_green.iloc[:-1], reference_series=qqq_green
    )
    res_short = evaluate_jepq_signal(qqq_green.iloc[:15], jepq_green.iloc[:15])
    res_missing = evaluate_jepq_signal(qqq_green, pd.Series(dtype=float))
    check("jepq_signal_three_lights_and_boundaries",
          res_green["ok"] and res_green["light"] == "🟢"
          and res_yellow_lo["ok"] and res_yellow_lo["light"] == "🟡"
          and res_yellow_hi["ok"] and res_yellow_hi["light"] == "🟡"
          and res_red_gap["ok"] and res_red_gap["light"] == "🔴" and res_red_gap["reason"] == "暂停观察 — D-8 gap 超过阈值"
          and res_red_vol["ok"] and res_red_vol["light"] == "🔴" and res_red_vol["reason"] == "暂停观察 — 波动率不低于 25%"
          and res_red_both["ok"] and res_red_both["light"] == "🔴" and res_red_both["reason"] == "暂停观察 — D-8 gap 超过阈值"
          and (not res_stale_jepq["ok"]) and "不一致" in res_stale_jepq["error"]
          and (not res_stale_ref["ok"]) and "早于" in res_stale_ref["error"]
          and (not res_short["ok"]) and res_short["light"] is None and "不足" in res_short["error"]
          and (not res_missing["ok"]) and res_missing["light"] is None and "JEPQ" in res_missing["error"],
          "JEPQ 三级灯阈值两侧、双超只报 D-8 gap、最新交易日对齐/陈旧拦截与缺数降级必须全部符合原版口径")

    first_chg = detect_jepq_light_change("🟡", None, current_asof="2028-01-06")
    same_chg = detect_jepq_light_change("🟡", "🟡", current_asof="2028-01-06", prev_asof="2028-01-05")
    same_day_diff_chg = detect_jepq_light_change("🔴", "🟡", current_asof="2028-01-05", prev_asof="2028-01-05")
    older_day_chg = detect_jepq_light_change("🔴", "🟡", current_asof="2028-01-04", prev_asof="2028-01-05")
    turn_chg = detect_jepq_light_change("🔴", "🟡", current_asof="2028-01-06", prev_asof="2028-01-05")
    with tempfile.TemporaryDirectory() as _tmp_dir:
        _state = Path(_tmp_dir) / "jepq_light.json"
        _missing_load = load_jepq_state(_state)
        save_jepq_light(_state, "🟡", "2028-01-05")
        _saved_state = load_jepq_state(_state)
    jepq_fix = card_fixture(False, False)
    jepq_fix["jepq"] = dict(res_red_gap)
    card_same = digest_card(jepq_fix, prev_jepq_light="🔴")
    card_first = digest_card(jepq_fix, prev_jepq_light=None)
    card_changed = digest_card(jepq_fix, prev_jepq_light="🟡")
    bad_fix = card_fixture(False, False)
    bad_fix["jepq"] = {"ok": True, "light": "🟡", "d8_gap": "not-a-number"}
    bad_card_dump = json.dumps(digest_card(bad_fix), ensure_ascii=False)
    bad_md = "\n".join(digest_lines(bad_fix))
    same_dump = json.dumps(card_same, ensure_ascii=False)
    first_dump = json.dumps(card_first, ensure_ascii=False)
    changed_dump = json.dumps(card_changed, ensure_ascii=False)
    changed_md = "\n".join(digest_lines(jepq_fix, prev_jepq_light="🟡"))
    check("jepq_light_change_detection",
          (not first_chg["changed"]) and first_chg["should_save"]
          and (not same_chg["changed"]) and same_chg["should_save"]
          and (not same_day_diff_chg["changed"]) and (not same_day_diff_chg["should_save"])
          and (not older_day_chg["changed"]) and (not older_day_chg["should_save"])
          and turn_chg["changed"] and turn_chg["should_save"] and turn_chg["transition"] == "JEPQ—QQQ 信号灯由 🟡 转为 🔴"
          and _missing_load is None and _saved_state == {"light": "🟡", "asof": "2028-01-05"}
          and validate_jepq(bad_fix["jepq"]) is None and "数据不足" in bad_card_dump and "数据不足" in bad_md
          and "信号灯由" not in same_dump and "信号灯由" not in first_dump
          and f"<font color='{DIGEST_CARD_HOT}'>JEPQ—QQQ 信号灯由 🟡 转为 🔴</font>" in changed_dump
          and JEPQ_DISCLAIMER in same_dump and JEPQ_DISCLAIMER in changed_dump
          and "- JEPQ—QQQ 信号灯由 🟡 转为 🔴：" in changed_md,
          "首次、同色、同日/早日均按未变色处理且同日/早日不写回；无效字段经 validate_jepq 降级不抛错；变色时标题醒目着色")
    ok = all(x["ok"] for x in checks)
    payload = {"ok": ok, "checks": checks, "count": len(checks), "hint": "Run scripts/run.sh build --json after fixing failed checks." if not ok else ""}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in checks:
            print(("PASS" if item["ok"] else "FAIL") + f"  {item['name']}" + (f" — {item['hint']}" if item["hint"] else ""))
        print(f"{sum(x['ok'] for x in checks)}/{len(checks)} checks passed")
    return 0 if ok else 1

