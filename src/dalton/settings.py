"""Dalton behavioral configuration and environment accessors.

Centralizes all environment variable and YAML config reads for Dalton modules
so service/handler modules never call ``os.getenv`` directly (per ``src/server/AGENTS.md``).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from src.config.env import HOST_IP
from src.config.settings import HOST_MODE

DALTON_YAML_PATH = Path(__file__).resolve().parent / "dalton.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]


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

    max_doc_bytes: int = Field(default=1_048_576, gt=0)


class DaltonPublishConfig(BaseModel):
    """Publication governance settings (PK.8-PK.13)."""

    reviewer_models: list[str] = Field(
        default_factory=lambda: [
            "ocb-openai-gpt-6.1-sol",
            "ocb-google-gemini-4-argon",
            "ocb-claude-cli-gateway-claude-opus-5-5",
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
    min_wake_usd: float = Field(default=0.50, ge=0)
    event_reserve_ratio: float = Field(default=0.2, ge=0, le=1.0)
    preflight_timeout_s: float = Field(default=20.0, gt=0)
    backoff: DaltonBackoffConfig = Field(default_factory=DaltonBackoffConfig)
    vendor_quota_alert_ratio: float = Field(default=0.8, gt=0, le=1.0)


class DaltonConfig(BaseModel):
    """Root Dalton behavioral configuration loaded from ``src/dalton/dalton.yaml``."""

    enabled: bool = True
    pm_user_ids: list[str] = Field(default_factory=lambda: ["local-dev-user"])
    timezone: str = "Asia/Shanghai"
    budget: DaltonBudgetDefaults = Field(default_factory=DaltonBudgetDefaults)
    knowledge: DaltonKnowledgeConfig = Field(default_factory=DaltonKnowledgeConfig)
    publish: DaltonPublishConfig = Field(default_factory=DaltonPublishConfig)
    autonomy: DaltonAutonomyConfig = Field(default_factory=DaltonAutonomyConfig)


@lru_cache(maxsize=1)
def get_dalton_config() -> DaltonConfig:
    """Load and cache ``src/dalton/dalton.yaml``."""
    if not DALTON_YAML_PATH.exists():
        return DaltonConfig()
    raw: dict[str, Any] = yaml.safe_load(DALTON_YAML_PATH.read_text(encoding="utf-8")) or {}
    return DaltonConfig.model_validate(raw)


def is_dalton_enabled() -> bool:
    """Whether Dalton features are enabled in this deployment."""
    env_flag = os.getenv("DALTON_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().enabled


def is_budget_gate_enabled() -> bool:
    """Whether LocalBudgetPort is active (OSS mode + DALTON_BUDGET_GATE=1)."""
    return HOST_MODE == "oss" and os.getenv("DALTON_BUDGET_GATE", "").strip() == "1"


def get_dalton_broker_token() -> str:
    """Return ``DALTON_BROKER_TOKEN`` from environment (empty string if unset)."""
    return os.getenv("DALTON_BROKER_TOKEN", "").strip()


def get_dalton_broker_url() -> str:
    """Return the resolved OpenClaw broker v2 base URL."""
    explicit = os.getenv("DALTON_BROKER_URL", "").strip()
    if explicit:
        return explicit.replace("{HOST_IP}", HOST_IP).rstrip("/")
    return f"http://{HOST_IP}:18789/dalton-broker/v1"


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
