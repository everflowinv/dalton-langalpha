"""Collect one AI-finance batch and check its records.

The package gathers GitHub momentum and earnings candidates, keeps signal
memory, and validates the brief against those candidates.
"""

from .ai_finance_daily_validate import (
    validate_github_tracking,
    validate_markdown,
    validate_records,
)
from .ai_finance_github_momentum import evaluate_history, github_json, momentum, significance
from .ai_finance_github_trending import collect_trending, select_candidates
from .ai_finance_signal_memory import compare_records, ingest_records, stats
from .alphaengine_earnings_ai_scan import scan
from .prepare import PrepareError, prepare_batch

__all__ = [
    "PrepareError",
    "collect_trending",
    "compare_records",
    "evaluate_history",
    "github_json",
    "ingest_records",
    "momentum",
    "prepare_batch",
    "scan",
    "select_candidates",
    "significance",
    "stats",
    "validate_github_tracking",
    "validate_markdown",
    "validate_records",
]
