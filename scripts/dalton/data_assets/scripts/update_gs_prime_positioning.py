#!/usr/bin/env python3
"""Standalone CLI entrypoint to update GS Prime positioning data for sentiment-dashboard."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPTS = ROOT / "skills/sentiment-dashboard/scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))

from gs_prime_positioning import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
