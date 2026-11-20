"""Single-source bridge to the skill's config-driven bull/bear ticker validator."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
BULLBEAR_SKILL_DIR = (
    REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
)
_TRACKER_CONFIG_PATH = (
    REPO_ROOT
    / "plugins"
    / "dalton"
    / "skills"
    / "dalton-bullbear-tracker"
    / "scripts"
    / "tracker_config.py"
)


def _load_tracker_config() -> Any | None:
    name = "dalton_bullbear_tracker_config"
    path = _TRACKER_CONFIG_PATH
    cached = sys.modules.get(name)
    if cached is not None and getattr(cached, "__file__", None) == str(path):
        return cached
    if not path.is_file():
        logger.warning("Bull/bear tracker config module missing at %s", path)
        return None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            logger.warning("Failed to build module spec for %s", path)
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except Exception:
        sys.modules.pop(name, None)
        logger.exception("Failed loading bull/bear tracker config module %s", path)
        return None


def supported_bullbear_tickers(skill_dir: Path | None = None) -> tuple[str, ...]:
    """Return sorted supported tickers using the single shared skill validator."""
    cfg_mod = _load_tracker_config()
    if cfg_mod is None:
        return ()
    target = Path(skill_dir) if skill_dir is not None else BULLBEAR_SKILL_DIR
    try:
        return tuple(cfg_mod.supported_tickers(skill_dir=target))
    except Exception:
        logger.exception("Failed resolving supported bull/bear tickers from %s", target)
        return ()


def bullbear_report_profiles(skill_dir: Path | None = None) -> tuple[str, ...]:
    """Return `<ticker>-tracker` profile names for all supported bull/bear tickers."""
    cfg_mod = _load_tracker_config()
    if cfg_mod is None:
        return ()
    target = Path(skill_dir) if skill_dir is not None else BULLBEAR_SKILL_DIR
    try:
        return tuple(cfg_mod.bullbear_report_profiles(skill_dir=target))
    except Exception:
        logger.exception("Failed resolving bull/bear report profiles from %s", target)
        return ()


def explain_unsupported_bullbear_ticker(
    ticker: str,
    skill_dir: Path | None = None,
) -> str:
    """Return human-readable reason why ``ticker`` is not in the supported bull/bear set."""
    cfg_mod = _load_tracker_config()
    target = Path(skill_dir) if skill_dir is not None else BULLBEAR_SKILL_DIR
    if cfg_mod is None or not hasattr(cfg_mod, "explain_unsupported_ticker"):
        chosen = str(ticker or "").strip().upper()
        return f"不支持的 ticker {chosen!r}：无法加载技能配置或缺少 config/{chosen}.json / references/{chosen.lower()}-rules.md"
    try:
        return str(cfg_mod.explain_unsupported_ticker(ticker, skill_dir=target))
    except Exception:
        logger.exception("Failed explaining unsupported ticker %r", ticker)
        return f"不支持的 ticker {ticker!r}"
