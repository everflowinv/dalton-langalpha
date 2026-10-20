"""Dalton behavioral configuration and environment accessors.

Centralizes all environment variable and YAML config reads for Dalton modules
so service/handler modules never call ``os.getenv`` directly (per ``src/server/AGENTS.md``).
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from src.config.env import HOST_IP, LOCAL_DEV_USER_ID
from src.config.settings import HOST_MODE

logger = logging.getLogger(__name__)

DALTON_YAML_PATH = Path(__file__).resolve().parent / "dalton.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]


class DaltonBrokerModelsConfig(BaseModel):
    """OpenClaw broker v2 parallel model path configuration (P0N.1)."""

    enabled: bool = True


class DaltonModelsConfig(BaseModel):
    """Model provider switches and assist role defaults (P0N.1, P0N.8, L27)."""

    broker: DaltonBrokerModelsConfig = Field(default_factory=DaltonBrokerModelsConfig)
    disabled_providers: list[str] = Field(default_factory=list)
    assist_model: str = "gemini-3.8-flash-low-oauth"
    assist_fallback_model: str | list[str] = Field(default_factory=list)


class DaltonBudgetDefaults(BaseModel):
    """Default parameters for LocalBudgetPort (P0.8)."""

    default_daily_cap_usd: float | None = None
    default_task_cap_usd: float | None = None
    reserve_chunk_usd: float = Field(default=1.00, gt=0)
    admission_floor_usd: float = Field(default=0.25, ge=0)
    lease_ttl_seconds: int = Field(default=300, ge=30)
    timezone: str = "Asia/Shanghai"


class DaltonKnowledgeConfig(BaseModel):
    """Company knowledge layer settings (PK.1-PK.7)."""

    enabled: bool = True
    max_doc_bytes: int = Field(default=1_048_576, gt=0)


class DaltonPublishConfig(BaseModel):
    """Publication governance settings (PK.8-PK.13, P0N.5)."""

    enabled: bool = True
    reviewer_models: list[str] = Field(
        default_factory=lambda: [
            "gpt-6.1-sol-oauth",
            "gemini-4-argon-high-oauth",
            "grok-4.7",
            "claude-opus-5-5-oauth",
        ]
    )
    extract_model: str | None = None
    approval_ttl_hours: int = Field(default=168, gt=0)
    max_file_bytes: int = Field(default=20_971_520, gt=0)
    consensus_providers: list[str] = Field(
        default_factory=lambda: ["estimates:", "yf_analysis"]
    )


class DaltonBackoffConfig(BaseModel):
    """Time-based backoff parameters (PK.18)."""

    base_hours: float = Field(default=24.0, gt=0)
    max_days: float = Field(default=30.0, gt=0)


class DaltonAutonomyConfig(BaseModel):
    """Autonomous research agenda and preflight settings (PK.14-PK.20)."""

    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    min_wake_usd: float = Field(default=0.50, ge=0)
    reserve_for_events_usd: float = Field(default=5.00, ge=0)
    event_reserve_ratio: float = Field(default=0.2, ge=0, le=1.0)
    preflight_timeout_s: float = Field(default=45.0, gt=0)
    backoff: DaltonBackoffConfig = Field(default_factory=DaltonBackoffConfig)
    vendor_quota_alert_ratio: float = Field(default=0.8, gt=0, le=1.0)
    est_cost_by_kind: dict[str, float] = Field(
        default_factory=lambda: {
            "initial_coverage": 4.50,
            "full_coverage": 12.00,
            "earnings_preview": 2.50,
            "earnings_flash": 1.20,
            "earnings_deep": 6.00,
            "filing_review": 1.50,
            "thesis_delta": 1.00,
            "stock_move": 1.20,
            "bullbear": 2.50,
            "topic_research": 5.00,
            "weekly_review": 4.00,
            "question_followup": 1.50,
        }
    )


class DaltonConfig(BaseModel):
    """Root Dalton behavioral configuration loaded from ``src/dalton/dalton.yaml``."""

    enabled: bool = True
    pm_user_ids: list[str] = Field(default_factory=lambda: [LOCAL_DEV_USER_ID])
    timezone: str = "Asia/Shanghai"
    models: DaltonModelsConfig = Field(default_factory=DaltonModelsConfig)
    budget: DaltonBudgetDefaults = Field(default_factory=DaltonBudgetDefaults)
    knowledge: DaltonKnowledgeConfig = Field(default_factory=DaltonKnowledgeConfig)
    publish: DaltonPublishConfig = Field(default_factory=DaltonPublishConfig)
    autonomy: DaltonAutonomyConfig = Field(default_factory=DaltonAutonomyConfig)


def _parse_env_pm_user_ids() -> list[str] | None:
    """Parse ``DALTON_PM_USER_IDS`` (comma-separated) when set and non-empty (L39)."""
    raw = os.getenv("DALTON_PM_USER_IDS")
    if raw is None:
        return None
    parsed = [u.strip() for u in raw.split(",") if u.strip()]
    return parsed if parsed else None


def get_pm_user_ids_source() -> str:
    """Return whether ``pm_user_ids`` comes from ``env`` (``DALTON_PM_USER_IDS``) or ``default``."""
    return "env" if _parse_env_pm_user_ids() is not None else "default"


@lru_cache(maxsize=1)
def _load_base_dalton_config() -> DaltonConfig:
    """Load and cache ``src/dalton/dalton.yaml``."""
    if not DALTON_YAML_PATH.exists():
        return DaltonConfig()
    raw: dict[str, Any] = yaml.safe_load(DALTON_YAML_PATH.read_text(encoding="utf-8")) or {}
    return DaltonConfig.model_validate(raw)


def get_dalton_config() -> DaltonConfig:
    """Return DaltonConfig with ``DALTON_PM_USER_IDS`` override applied when set (L39)."""
    base = _load_base_dalton_config()
    env_pm = _parse_env_pm_user_ids()
    if env_pm is not None:
        return base.model_copy(update={"pm_user_ids": env_pm})
    return base


get_dalton_config.cache_clear = _load_base_dalton_config.cache_clear  # type: ignore[attr-defined]


get_dalton_settings = get_dalton_config


def is_dalton_enabled() -> bool:
    """Whether Dalton features are enabled in this deployment."""
    env_flag = os.getenv("DALTON_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().enabled


def is_budget_gate_enabled() -> bool:
    """Whether LocalBudgetPort is active (OSS mode + DALTON_BUDGET_GATE=1)."""
    return HOST_MODE == "oss" and os.getenv("DALTON_BUDGET_GATE", "").strip() == "1"


is_dalton_budget_gate_enabled = is_budget_gate_enabled


def is_broker_models_enabled() -> bool:
    """Whether OpenClaw broker v2 (`ocb-*`) models are enabled in the catalog (P0N.1)."""
    env_flag = os.getenv("DALTON_BROKER_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().models.broker.enabled


def get_disabled_providers() -> set[str]:
    """Return disabled provider IDs from `DALTON_DISABLED_PROVIDERS` or `dalton.yaml` (L17, P0N.8)."""
    env_raw = os.getenv("DALTON_DISABLED_PROVIDERS")
    if env_raw is not None:
        return {p.strip() for p in env_raw.split(",") if p.strip()}
    return {p.strip() for p in get_dalton_config().models.disabled_providers if p.strip()}


def get_assist_fallback_models() -> list[str]:
    """Return configured assist fallback model(s) from env or `dalton.yaml` `models.assist_fallback_model` (P0J.J2)."""
    env_raw = os.getenv("DALTON_ASSIST_FALLBACK_MODEL")
    if env_raw is not None:
        return [m.strip() for m in env_raw.split(",") if m.strip()]
    raw = get_dalton_config().models.assist_fallback_model
    if isinstance(raw, str):
        return [m.strip() for m in raw.split(",") if m.strip()]
    if isinstance(raw, list):
        return [str(m).strip() for m in raw if str(m).strip()]
    return []


def log_dalton_startup_warnings() -> None:
    """Log prominent startup warnings if budget gate or APP_ENV=dalton is not active (G1.4, G7.1)."""
    if not is_dalton_enabled():
        return
    if not is_budget_gate_enabled():
        logger.warning(
            "DALTON WARNING (G1.4): Local daily USD budget gate is NOT enabled "
            "(HOST_MODE=%r, DALTON_BUDGET_GATE=%r). Set DALTON_BUDGET_GATE=1 to enforce daily caps.",
            HOST_MODE,
            os.getenv("DALTON_BUDGET_GATE", ""),
        )
    app_env = os.getenv("APP_ENV", "").strip()
    if app_env != "dalton":
        logger.warning(
            "DALTON WARNING (G7.1): APP_ENV=%r is not 'dalton'. "
            "agent_config.dalton.yaml is not active and MCP call_timeout_s will fall back to 65s!",
            app_env,
        )


def get_dalton_broker_token() -> str:
    """Return ``DALTON_BROKER_TOKEN`` from environment (empty string if unset)."""
    return os.getenv("DALTON_BROKER_TOKEN", "").strip()


def get_dalton_broker_url(host: str | None = None) -> str:
    """Return the resolved OpenClaw broker v2 base URL."""
    resolved_host = host or HOST_IP
    explicit = os.getenv("DALTON_BROKER_URL", "").strip()
    if explicit:
        return explicit.replace("{HOST_IP}", resolved_host).rstrip("/")
    return f"http://{resolved_host}:18789/dalton-broker/v1"


def get_dalton_researchdata_url() -> str:
    """Return ``DALTON_RESEARCHDATA_URL`` from environment (empty string if unset)."""
    url = os.getenv("DALTON_RESEARCHDATA_URL", "").strip()
    return url.replace("{HOST_IP}", HOST_IP) if url else ""


def get_sec_user_agent() -> str:
    """Return ``SEC_USER_AGENT`` from environment with a compliant fallback."""
    return (
        os.getenv("SEC_USER_AGENT", "").strip()
        or os.getenv("EDGAR_IDENTITY", "").strip()
        or "DaltonResearch research@everflowinv.com"
    )


def get_openclaw_gateway_token() -> str:
    """Return ``OPENCLAW_GATEWAY_TOKEN`` (used only by P0.5 --target gateway control group)."""
    return os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()
