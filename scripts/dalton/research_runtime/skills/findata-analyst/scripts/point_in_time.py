"""Date-level SEC evidence boundaries; keep periods, units and filing lineage."""
from datetime import date
import argparse
import pandas as pd


def parse_as_of(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or parsed > date.today():
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("--as-of requires YYYY-MM-DD, no later than today")


def dated_filings(filings, as_of=None):
    if not as_of:
        return filings
    return filings.filter(filing_date=f":{parse_as_of(as_of)}")


def facts_at_date(df, as_of=None):
    """Filter before selecting concepts or revisions, never by fiscal period alone."""
    out = df.copy()
    if not as_of:
        return out
    cutoff = pd.Timestamp(parse_as_of(as_of))
    if "filing_date" not in out.columns:
        raise ValueError("Historical facts require filing_date metadata; no current-data fallback is allowed.")
    filed = pd.to_datetime(out["filing_date"], errors="coerce")
    ends = pd.to_datetime(out["period_end"], errors="coerce")
    return out[filed.notna() & (filed <= cutoff) & ends.notna() & (ends <= cutoff)].copy()


def select_fact_rows(df, form, limit, as_of=None):
    """Select the latest filing per exact period/unit, preserving QTD versus YTD."""
    out = facts_at_date(df, as_of)
    out["period_end"] = pd.to_datetime(out["period_end"], errors="coerce")
    out = out.dropna(subset=["period_end"])
    if as_of:
        if "form_type" not in out.columns:
            raise ValueError("Historical facts require form_type metadata.")
        if form:
            out = out[out["form_type"].isin([form, form + "/A"])]
        # Same end date can hold a quarter, YTD and full year. Do not collapse them.
        keys = [k for k in ("concept", "period_start", "period_end", "unit") if k in out.columns]
        out["filing_date"] = pd.to_datetime(out["filing_date"], errors="coerce")
        order = ["filing_date"] + (["accession"] if "accession" in out.columns else [])
        out = out.sort_values(order, kind="stable").drop_duplicates(keys, keep="last")
    else:
        # Preserve the legacy number of rows/default period selection.
        if form == "10-K" and "fiscal_period" in out.columns:
            fy = out[out["fiscal_period"] == "FY"]
            if not fy.empty:
                out = fy
        order = ["period_end"] + (["filing_date"] if "filing_date" in out.columns else [])
        out = out.sort_values(order, kind="stable").drop_duplicates(["period_end"], keep="last")
    out = out.sort_values("period_end", ascending=False, kind="stable").head(limit).copy()
    out["derived_year"] = out["period_end"].dt.year
    out["fiscal_year_raw"] = pd.to_numeric(out.get("fiscal_year", pd.Series(index=out.index, dtype=float)), errors="coerce")
    out["year_mismatch"] = out["fiscal_year_raw"].notna() & (out["fiscal_year_raw"] != out["derived_year"])
    for key in ("period_start", "period_end", "filing_date"):
        if key in out.columns:
            dates = pd.to_datetime(out[key], errors="coerce")
            out[key] = dates.dt.strftime("%Y-%m-%d").where(dates.notna(), None)
    columns = [k for k in ("period_start", "period_end", "derived_year", "fiscal_year_raw", "fiscal_period", "numeric_value", "unit", "year_mismatch", "filing_date", "accession", "form_type") if k in out.columns]
    result = out[columns].astype(object).where(pd.notna(out[columns]), None)
    return result.to_dict(orient="records")
