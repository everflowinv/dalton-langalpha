#!/usr/bin/env python3
"""Skew snapshot.

1. CBOE SKEW (^SKEW): latest value, percentile since 2010 and over the last year,
   and S&P 500 forward 21-day median return / median drawdown / P(drawdown > 5%)
   by SKEW quintile.
2. Per ticker: Black-Scholes IV from bid-ask mid for 90%/95%/100% puts and
   100%/105%/110% calls at the latest expiry inside the DTE window, plus
   90P-ATM and 90P-110C skew in vol points.

Usage:
  python3 option_skew.py --tickers SPY,QQQ,SMH,NVDA --rate 0.039 [--dte-min 25 --dte-max 50] [--expiry YYYY-MM-DD]
"""
import argparse
import datetime as dt
import math

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.optimize import brentq
from scipy.stats import norm


def bs_price(S, K, T, v, r, kind):
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    if kind == "p":
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
    return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)


def implied_vol(price, S, K, T, r, kind):
    try:
        return brentq(lambda v: bs_price(S, K, T, v, r, kind) - price, 0.01, 4)
    except Exception:
        return np.nan


def skew_index():
    h = yf.download(["^SKEW", "^GSPC"], start="2010-01-01", progress=False, auto_adjust=False)["Close"]
    s = h["^SKEW"].dropna()
    spx = h["^GSPC"].dropna()
    last = s.iloc[-1]
    print("SKEW", s.index[-1].date(), round(last, 1),
          "pct_since_2010", round((s < last).mean() * 100, 1),
          "pct_1y", round((s.iloc[-252:] < last).mean() * 100, 1))
    df = pd.DataFrame({"skew": s, "spx": spx}).dropna()
    df["fwd21"] = df.spx.shift(-21) / df.spx - 1
    df["fwdmin21"] = df.spx[::-1].rolling(21, min_periods=1).min()[::-1].shift(-1) / df.spx - 1
    df["q"] = pd.qcut(df["skew"], 5, labels=False)
    print(df.groupby("q").agg(
        skew=("skew", "mean"),
        fwd21=("fwd21", "median"),
        fwdmin=("fwdmin21", "median"),
        p_dd5=("fwdmin21", lambda x: (x < -0.05).mean()),
    ).round(3))


def chain_skew(tk, today, dmin, dmax, r, expiry=None):
    t = yf.Ticker(tk)
    S = t.history(period="5d")["Close"].iloc[-1]
    if expiry:
        e = expiry
    else:
        exps = [x for x in t.options if dmin <= (dt.date.fromisoformat(x) - today).days <= dmax]
        if not exps:
            print(tk, "no expiry in window")
            return
        e = exps[-1]
    T = (dt.date.fromisoformat(e) - today).days / 365
    ch = t.option_chain(e)
    out = {}
    for m, kind in [(0.90, "p"), (0.95, "p"), (1.0, "p"), (1.0, "c"), (1.05, "c"), (1.10, "c")]:
        d = ch.puts if kind == "p" else ch.calls
        row = d.iloc[(d.strike - S * m).abs().argsort()[:1]].iloc[0]
        mid = (row.bid + row.ask) / 2 if row.bid > 0 and row.ask > 0 else row.lastPrice
        out[f"{kind}{int(m * 100)}"] = round(implied_vol(mid, S, row.strike, T, r, kind) * 100, 1)
    print(tk, e, round(S, 2), out,
          "90P-ATM", round(out["p90"] - out["p100"], 1),
          "90P-110C", round(out["p90"] - out["c110"], 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="SPY,QQQ,SMH")
    ap.add_argument("--rate", type=float, required=True, help="short-term risk-free rate, e.g. 0.039")
    ap.add_argument("--dte-min", type=int, default=25)
    ap.add_argument("--dte-max", type=int, default=50)
    ap.add_argument("--expiry", default=None)
    ap.add_argument("--today", default=None, help="YYYY-MM-DD, defaults to today")
    a = ap.parse_args()
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    skew_index()
    for tk in [x.strip() for x in a.tickers.split(",") if x.strip()]:
        chain_skew(tk, today, a.dte_min, a.dte_max, a.rate, a.expiry)


if __name__ == "__main__":
    main()
