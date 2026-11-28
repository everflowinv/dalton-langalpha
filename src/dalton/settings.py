"""Dalton behavioral configuration and environment accessors.

Centralizes all environment variable and YAML config reads for Dalton modules
so service/handler modules never call ``os.getenv`` directly (per ``src/server/AGENTS.md``).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import uuid
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from src.config.env import HOST_IP, LOCAL_DEV_USER_ID
from src.config.settings import HOST_MODE
from src.dalton.profile import DeploymentProfile, resolve_profile
from src.dalton.sec_identity import (
    DEFAULT_SEC_USER_AGENT as DEFAULT_SEC_USER_AGENT,
    resolve_sec_user_agent,
)

logger = logging.getLogger(__name__)

DALTON_YAML_PATH = Path(__file__).resolve().parent / "dalton.yaml"
DALTON_CORE_YAML_PATH = Path(__file__).resolve().parent / "dalton.core.yaml"
REPO_ROOT = Path(__file__).resolve().parents[2]


class DaltonModelsConfig(BaseModel):
    """Model provider switches and assist role defaults (P0N.1, P0N.8, L27)."""

    disabled_providers: list[str] = Field(default_factory=list)
    assist_model: str = ""
    assist_fallback_model: str | list[str] = Field(default_factory=list)
    probe_models: dict[str, str] = Field(default_factory=dict)


class DaltonInternalLLMConfig(BaseModel):
    """Host internal LLM endpoint allowlist (read from `dalton.yaml`)."""

    allowed_models: list[str] = Field(default_factory=list)


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


class DaltonMandateSettings(BaseModel):
    """Underwriting mandate hurdle and staleness parameters (§5, §12.1, §12.8, §12.9)."""

    long_hurdle_annual: float = Field(default=0.15, ge=0.0)
    short_hurdle_annual: float = Field(default=0.20, ge=0.0)
    min_total_return: float = Field(default=0.10, ge=0.0)
    max_adverse_move_act_now: float = Field(default=0.25, ge=0.0)
    max_entry_distance: float = Field(default=0.25, ge=0.0)
    call_hurdle_annual: float = Field(default=0.15, ge=0.0)
    anchor_stale_days: int = Field(default=14, ge=1)
    anchor_stale_move: float = Field(default=0.15, ge=0.0)

    @model_validator(mode="before")
    @classmethod
    def _coerce_legacy_bear_loss(cls, data: Any) -> Any:
        if isinstance(data, dict) and "max_adverse_move_act_now" not in data:
            legacy = data.get("max_bear_loss_act_now")
            if legacy is not None:
                data = dict(data)
                data["max_adverse_move_act_now"] = abs(float(legacy))
        return data

    @property
    def max_bear_loss_act_now(self) -> float:
        """Signed compatibility alias for §5 (`-max_adverse_move_act_now`)."""
        return -self.max_adverse_move_act_now

    def snapshot_hurdles(self, horizon_months: int = 12) -> dict[str, float | int]:
        """Return the hurdle snapshot stored on `underwriting.hurdles` at write time (§12.9)."""
        return {
            "long_hurdle_annual": self.long_hurdle_annual,
            "short_hurdle_annual": self.short_hurdle_annual,
            "min_total_return": self.min_total_return,
            "max_adverse_move_act_now": self.max_adverse_move_act_now,
            "horizon_months": int(horizon_months),
        }


class DaltonPublishConfig(BaseModel):
    """Publication governance settings (PK.8-PK.13, P0N.5)."""

    enabled: bool = True
    reviewer_models: list[str] = Field(default_factory=list)
    # Number audit (check_draft / publish): labelling and judging figures. Its own
    # models, not the shared assist slot. Named in dalton.yaml only; empty here so a
    # missing config fails loudly instead of quietly picking a model.
    audit_model: str = ""
    # Language review (check_language). Empty falls back to audit_model in
    # dalton_model_default, so one yaml key moves both unless split deliberately.
    language_model: str = ""
    audit_fallback_models: list[str] = Field(default_factory=list)
    audit_timeout_s: float = Field(default=300.0, gt=0)
    audit_concurrency: int = Field(default=4, ge=1, le=16)
    # Wall-time budget for one whole audit (extraction, labelling, matching, judge), seconds.
    audit_total_timeout_s: float = Field(default=1800.0, gt=0)
    audit_prompt_version: str = "audit_v1"
    prompt_version: str = "audit_v1"
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
    # Probe-phase budget. A finished run then triages historical filings with a
    # fresh wait of this same length, so one preflight can take about twice
    # preflight_timeout_s.
    preflight_timeout_s: float = Field(default=45.0, gt=0)
    # Company-watch only. One reminder per consecutive-timeout cycle, checked
    # at the start of the next run. The cycle starts when the count goes from
    # 0 to 1. The streak resets only when every probe this run requested
    # actually succeeded. Backoff, a degraded or partial probe, an exhausted
    # budget, and any early return do not reset it.
    preflight_timeout_streak: int = Field(default=3, ge=1)
    data_health_timeout_s: float = Field(default=600.0, gt=0)
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


class DaltonConversationsConfig(BaseModel):
    """OpenClaw conversation archive settings (MVP Phase 1)."""

    enabled: bool = True
    extractor_model: str = ""
    extractor_fallback_model: str = "gemini-4-argon-medium-oauth"
    segment_deadline_s: float = Field(default=600.0, gt=0)
    prompt_version: str = "conv_card_v2_3"
    embedding_model: str = ""
    embedding_model_preferred: str = ""
    embedding_dimensions: int = Field(default=3072, gt=0)
    hourly_token_budget: int = Field(default=120_000, gt=0)
    extract_concurrency: int = Field(default=1, ge=1)
    default_backfill_days: int = Field(default=14, gt=0)
    max_segment_gap_minutes: int = Field(default=30, gt=0)
    max_segment_chars: int = Field(default=6000, gt=0)
    max_segment_events: int = Field(default=25, gt=0)
    rate_limit_initial_backoff_seconds: float = Field(default=30.0, gt=0)
    rate_limit_max_backoff_seconds: float = Field(default=900.0, gt=0)
    recency_half_life_days: float = Field(default=180.0, gt=0)
    recency_floor: float = Field(default=0.7, ge=0.0, le=1.0)
    inbox_dir: str = "var/dalton/conversations/inbox"
    cursors_file: str = "var/dalton/conversations/cursors.json"
    export_cursors_file: str = "var/dalton/conversations/export_cursors.json"
    agents: list[str] = Field(default_factory=lambda: ["main", "chem", "marco"])


class DaltonCredentialsConfig(BaseModel):
    """Team credential sharing configuration (`credentials.sharing: team | "off"`)."""

    sharing: Literal["team", "off"] = "team"

    @field_validator("sharing", mode="before")
    @classmethod
    def _normalize_sharing(cls, v: Any) -> Any:
        if v is False:
            return "off"
        if v is True:
            return "team"
        if isinstance(v, str):
            lowered = v.strip().lower()
            if lowered in ("off", "false"):
                return "off"
            if lowered in ("team", "true", "on"):
                return "team"
        return v


class DaltonColleagueDispatchConfig(BaseModel):
    """Master feature flag for colleague workspace task dispatch & questioning (d020)."""

    enabled: bool = False


class DaltonComputerCliTokensConfig(BaseModel):
    """Master flag for #656's per-computer CLI tokens (`lac1.*`, what the ``la``
    CLI presents from inside a sandbox).

    Off (the default): no token file is written into a sandbox, no
    ``computer_cli_tokens`` row is minted, the sweep never claims or prunes,
    and a presented token authenticates nothing (401). Turning it from on to
    off also calls for the one-shot full-table revocation
    (``scripts/dalton/revoke_computer_cli_tokens.py``).
    """

    enabled: bool = False


class DaltonArtifactPublishingConfig(BaseModel):
    """Master flag for #666's artifact publishing, versions and file comments.

    Off (the code default; production sets it true — user decision 11-03):
    publishing and ``keep_updated`` answer 403, the comment write routes
    answer 403, a send carrying ``artifact_comment_ids`` is refused 409 before
    the stream starts, ``keep_updated_after_turn`` returns without cutting a
    version, and the agent's ``reply_to_comment`` tool is not registered
    (a deployment-level choice, so the tool list holds still within one
    deployment; flipping it is a deployment change that re-records the
    request lock). Old links keep working: reading, unsharing and the legacy
    first-read conversion stay open.
    """

    enabled: bool = False


class DaltonProblemReportsConfig(BaseModel):
    """Master flag for problem report submission & thumb-down forwarding.

    Off (the default): ``/api/v1/reports/config`` answers ``enabled=false``,
    ``POST /api/v1/reports`` answers 404, and thumb-down feedback stays local.
    """

    enabled: bool = False


class DaltonCoverageUnitsConfig(BaseModel):
    """Coverage-unit identity registry switch (`dalton_coverage_units`, d023, sector layer P0).

    Off (the default): workspace coverage identity uses the compatible rules only
    (company knowledge doc → implicit company; Home; everything else unclassified)
    and the registry table is never read. On: a registered active unit bound to
    the workspace wins over the compatible rules.
    """

    enabled: bool = False


class DaltonDataJobsConfig(BaseModel):
    """Master switch and memory admission threshold for OC-1 deterministic data tasks (`d029`).

    Off (the default): the scheduler loop does not start and no task containers are created.
    """

    enabled: bool = False
    min_free_mem_mb: int = Field(default=1280, ge=0)


class DaltonChatUploadsConfig(BaseModel):
    """Server-side streaming deadlines and sweeper toggle for chat composer uploads (`d033`)."""

    part_read_idle_timeout_s: float = Field(default=30.0, gt=0)
    part_read_total_timeout_s: float = Field(default=180.0, gt=0)
    cleanup_enabled: bool = True


# A fake-IP exemption may never reach these, whatever the config says: they are
# the internal services and metadata endpoint the fetch guard exists to protect.
NEVER_EXEMPT_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


# IPv6 ranges that carry an IPv4 address inside them (mapped, compatible, NAT64,
# 6to4), with the bit offset of the embedded address. A range in one of them is
# checked as the IPv4 range it embeds, so ``::ffff:10.0.0.0/104`` cannot exempt 10/8.
_EMBEDDED_IPV4_PREFIXES: tuple[tuple[ipaddress.IPv6Network, int], ...] = (
    (ipaddress.IPv6Network("::ffff:0:0/96"), 0),
    (ipaddress.IPv6Network("::/96"), 0),
    (ipaddress.IPv6Network("64:ff9b::/96"), 0),
    (ipaddress.IPv6Network("2002::/16"), 80),
)


def embedded_ipv4_networks(net: ipaddress.IPv6Network) -> list[ipaddress.IPv4Network]:
    """The IPv4 ranges an IPv6 range can reach through embedded-IPv4 encodings."""
    found: list[ipaddress.IPv4Network] = []
    for prefix, shift in _EMBEDDED_IPV4_PREFIXES:
        if not net.overlaps(prefix):
            continue
        if net.prefixlen <= prefix.prefixlen:
            found.append(ipaddress.IPv4Network("0.0.0.0/0"))
            continue
        bits = min(net.prefixlen - prefix.prefixlen, 32)
        v4 = (int(net.network_address) >> shift) & 0xFFFFFFFF
        found.append(ipaddress.IPv4Network((v4, bits), strict=False))
    return found


def _protected_clash(
    net: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    candidates = [net]
    if net.version == 6:
        candidates.extend(embedded_ipv4_networks(net))
    for cand in candidates:
        for protected in NEVER_EXEMPT_NETWORKS:
            if protected.version == cand.version and cand.overlaps(protected):
                return protected
    return None


DEFAULT_FAKE_IP_REAL_RESOLVERS = ["https://1.1.1.1/dns-query", "https://dns.google/resolve"]


class DaltonNetworkConfig(BaseModel):
    """Network environment the backend container runs in."""

    fake_ip_cidrs: list[str] = Field(default_factory=list)
    # DNS-over-HTTPS JSON endpoints, tried in order, that give a fake-IP host's
    # real addresses: the proxy connects to those, so they are what the guard checks.
    fake_ip_real_resolver: list[str] = Field(
        default_factory=lambda: list(DEFAULT_FAKE_IP_REAL_RESOLVERS)
    )

    @field_validator("fake_ip_real_resolver", mode="before")
    @classmethod
    def _validate_resolvers(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        kept: list[str] = []
        for raw in v:
            url = str(raw).strip()
            if url.startswith("https://"):
                kept.append(url)
            else:
                logger.error("network.fake_ip_real_resolver: dropping %r, not an https URL", raw)
        return kept

    @field_validator("fake_ip_cidrs", mode="before")
    @classmethod
    def _validate_fake_ip_cidrs(cls, v: Any) -> list[str]:
        # A bad entry is dropped, not raised: failing validation would take the
        # whole Dalton config down, and a dropped range only exempts nothing.
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        normalized: list[str] = []
        for raw in v:
            try:
                net = ipaddress.ip_network(str(raw).strip(), strict=False)
            except ValueError:
                logger.error("network.fake_ip_cidrs: dropping %r, not a valid CIDR", raw)
                continue
            clash = _protected_clash(net)
            if clash is not None:
                logger.error(
                    "network.fake_ip_cidrs: dropping %s, it overlaps protected range %s "
                    "(internal or metadata addresses must stay blocked)",
                    net,
                    clash,
                )
                continue
            normalized.append(str(net))
        return normalized


class DaltonWeixinConfig(BaseModel):
    """Multi-account personal Weixin channel defaults (overridable via DB config / env)."""

    allow_external: bool = True
    max_concurrent_accounts: int = Field(default=200, ge=1, le=5000)


class DaltonConfig(BaseModel):
    """Root Dalton behavioral configuration loaded from ``src/dalton/dalton.yaml``."""

    enabled: bool = True
    pm_user_ids: list[str] = Field(default_factory=lambda: [LOCAL_DEV_USER_ID])
    configured_pm_user_ids: list[str] = Field(default_factory=list)
    timezone: str = "Asia/Shanghai"
    credentials: DaltonCredentialsConfig = Field(default_factory=DaltonCredentialsConfig)
    colleague_dispatch: DaltonColleagueDispatchConfig = Field(
        default_factory=DaltonColleagueDispatchConfig
    )
    computer_cli_tokens: DaltonComputerCliTokensConfig = Field(
        default_factory=DaltonComputerCliTokensConfig
    )
    artifact_publishing: DaltonArtifactPublishingConfig = Field(
        default_factory=DaltonArtifactPublishingConfig
    )
    problem_reports: DaltonProblemReportsConfig = Field(
        default_factory=DaltonProblemReportsConfig
    )
    coverage_units: DaltonCoverageUnitsConfig = Field(
        default_factory=DaltonCoverageUnitsConfig
    )
    data_jobs: DaltonDataJobsConfig = Field(
        default_factory=DaltonDataJobsConfig
    )
    chat_uploads: DaltonChatUploadsConfig = Field(
        default_factory=DaltonChatUploadsConfig
    )
    models: DaltonModelsConfig = Field(default_factory=DaltonModelsConfig)
    internal_llm: DaltonInternalLLMConfig = Field(default_factory=DaltonInternalLLMConfig)
    budget: DaltonBudgetDefaults = Field(default_factory=DaltonBudgetDefaults)
    knowledge: DaltonKnowledgeConfig = Field(default_factory=DaltonKnowledgeConfig)
    mandate: DaltonMandateSettings = Field(default_factory=DaltonMandateSettings)
    publish: DaltonPublishConfig = Field(default_factory=DaltonPublishConfig)
    autonomy: DaltonAutonomyConfig = Field(default_factory=DaltonAutonomyConfig)
    conversations: DaltonConversationsConfig = Field(
        default_factory=DaltonConversationsConfig
    )
    network: DaltonNetworkConfig = Field(default_factory=DaltonNetworkConfig)
    weixin: DaltonWeixinConfig = Field(default_factory=DaltonWeixinConfig)
    stop_backup_attach_timeout_s: float = Field(default=60.0, gt=0)
    stop_backup_total_timeout_s: float = Field(default=120.0, gt=0)
    stop_backup_close_timeout_s: float = Field(default=10.0, gt=0)


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


def get_configured_pm_user_ids() -> list[str]:
    """Return explicitly configured PM user IDs from ``DALTON_PM_USER_IDS`` (empty when unset or blank)."""
    return list(get_dalton_config().configured_pm_user_ids)


def _merge_dalton_yaml(
    base: dict[str, Any],
    overlay: dict[str, Any],
    *,
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Deep-merge ``dalton.core.yaml`` over ``dalton.yaml``.

    Nested mappings merge key-by-key, lists replace outright, and
    ``models.probe_models`` replaces wholesale so core clears Everflow's
    ``*-oauth`` probe entries cleanly.
    """
    merged = dict(base)
    for key, value in overlay.items():
        cur_path = (*path, key)
        if value is None:
            continue
        if cur_path == ("models", "probe_models") and isinstance(value, dict):
            merged[key] = dict(value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dalton_yaml(merged[key], value, path=cur_path)
        else:
            merged[key] = value
    return merged


@lru_cache(maxsize=4)
def _load_profile_dalton_raw(profile: DeploymentProfile) -> dict[str, Any]:
    """Load merged raw YAML mapping for ``profile`` without mutating module state."""
    if not DALTON_YAML_PATH.exists():
        return {}
    raw: dict[str, Any] = yaml.safe_load(DALTON_YAML_PATH.read_text(encoding="utf-8")) or {}
    if profile is DeploymentProfile.CORE and DALTON_CORE_YAML_PATH.exists():
        core_raw = yaml.safe_load(DALTON_CORE_YAML_PATH.read_text(encoding="utf-8")) or {}
        if not isinstance(core_raw, dict):
            raise ValueError(f"{DALTON_CORE_YAML_PATH} must be a mapping of config keys")
        raw = _merge_dalton_yaml(raw, core_raw)
    return raw


@lru_cache(maxsize=4)
def _load_profile_dalton_config(profile: DeploymentProfile) -> DaltonConfig:
    """Load and cache ``src/dalton/dalton.yaml`` (plus ``dalton.core.yaml`` in core profile)."""
    raw = _load_profile_dalton_raw(profile)
    if not raw:
        return DaltonConfig()
    return DaltonConfig.model_validate(raw)


def get_yaml_capabilities(profile: DeploymentProfile | None = None) -> dict[str, bool]:
    """Return optional ``capabilities:`` boolean overrides from the merged profile YAML.

    Kept outside ``DaltonConfig`` so ``DaltonConfig.model_dump()`` stays
    byte-identical for Everflow parity while still allowing ``dalton.yaml`` /
    ``dalton.core.yaml`` to declare capability overrides.
    """
    active = resolve_profile() if profile is None else profile
    raw = _load_profile_dalton_raw(active)
    caps_raw = raw.get("capabilities")
    if not isinstance(caps_raw, dict):
        return {}
    out: dict[str, bool] = {}
    for key, val in caps_raw.items():
        if isinstance(key, str) and isinstance(val, bool):
            out[key.strip().lower()] = val
    return out


def get_brand_name() -> str:
    """Return the deployment public brand name (``BRAND_NAME`` env, else ``Dalton`` in core / ``Everflow`` in everflow)."""
    explicit = os.getenv("BRAND_NAME", "").strip()
    if explicit:
        return explicit
    return "Dalton" if resolve_profile() is DeploymentProfile.CORE else "Everflow"


def get_profile_dalton_config(profile: DeploymentProfile) -> DaltonConfig:
    """Return the file-backed ``DaltonConfig`` for an explicit deployment ``profile``."""
    return _load_profile_dalton_config(profile)


def _load_base_dalton_config() -> DaltonConfig:
    """Return the cached file config for the active profile.

    Kept argument-free because tests replace it to inject a base config.
    """
    return _load_profile_dalton_config(resolve_profile())


def get_dalton_config() -> DaltonConfig:
    """Return DaltonConfig for the active profile with runtime env overrides applied (L39)."""
    profile = resolve_profile()
    base = _load_base_dalton_config()
    updates: dict[str, Any] = {}
    env_pm = _parse_env_pm_user_ids()
    if env_pm is not None:
        updates["pm_user_ids"] = env_pm
        updates["configured_pm_user_ids"] = env_pm
    if profile is DeploymentProfile.CORE:
        env_tz = os.getenv("DALTON_TIMEZONE", "").strip()
        if env_tz:
            updates["timezone"] = env_tz
            updates["budget"] = base.budget.model_copy(update={"timezone": env_tz})
            updates["autonomy"] = base.autonomy.model_copy(update={"timezone": env_tz})
    if updates:
        return base.model_copy(update=updates)
    return base


def _clear_dalton_config_caches() -> None:
    _load_profile_dalton_raw.cache_clear()
    _load_profile_dalton_config.cache_clear()


get_dalton_config.cache_clear = _clear_dalton_config_caches  # type: ignore[attr-defined]


get_dalton_settings = get_dalton_config


def get_dalton_mandate_settings() -> DaltonMandateSettings:
    """Return configured `DaltonMandateSettings` (§5, §12.1, §12.8)."""
    return get_dalton_config().mandate


def is_dalton_enabled() -> bool:
    """Whether Dalton features are enabled in this deployment."""
    env_flag = os.getenv("DALTON_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().enabled


def is_colleague_dispatch_enabled() -> bool:
    """Whether colleague workspace task dispatch & questioning (d020) is enabled."""
    env_flag = os.getenv("DALTON_COLLEAGUE_DISPATCH_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().colleague_dispatch.enabled


def is_computer_cli_tokens_enabled() -> bool:
    """Whether #656's per-computer CLI tokens (`lac1.*`) are enabled (sync3 B9)."""
    env_flag = os.getenv("DALTON_COMPUTER_CLI_TOKENS_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().computer_cli_tokens.enabled


def is_artifact_publishing_enabled() -> bool:
    """Whether #666's artifact publishing, versions and file comments are enabled (sync3 B11)."""
    env_flag = os.getenv("DALTON_ARTIFACT_PUBLISHING_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().artifact_publishing.enabled


def is_problem_reports_enabled() -> bool:
    """Whether problem report submission and thumb-down forwarding are enabled (default False)."""
    env_flag = os.getenv("DALTON_PROBLEM_REPORTS_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().problem_reports.enabled


def is_coverage_units_enabled() -> bool:
    """Whether the coverage-unit registry (`dalton_coverage_units`, d023) takes part in identity resolution."""
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.COLLEAGUE_SHARING):
        return False
    env_flag = os.getenv("DALTON_COVERAGE_UNITS_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes")
    return get_dalton_config().coverage_units.enabled


def is_data_jobs_enabled() -> bool:
    """Whether the OC-1 deterministic data tasks scheduler loop is enabled.

    ``DALTON_CAP_DATA_JOBS`` / profile config controls whether the data-jobs
    feature exists; ``DALTON_DATA_JOBS_ENABLED`` keeps its original meaning of
    pausing/resuming only the background scheduler loop.
    """
    if not is_dalton_enabled():
        return False
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.DATA_JOBS):
        return False
    env_flag = os.getenv("DALTON_DATA_JOBS_ENABLED")
    if env_flag is not None:
        return env_flag.strip().lower() in ("1", "true", "yes", "on")
    return True




def get_data_task_min_free_mem_mb() -> int:
    """Return the minimum free host memory (MB) required before launching a container data task (§2.2.2)."""
    raw = os.getenv("DALTON_DATA_TASK_MIN_FREE_MEM_MB", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return int(get_dalton_config().data_jobs.min_free_mem_mb)


def get_dalton_host_var_dir() -> str:
    """Return the host-side ``var/dalton`` directory for Docker bind mounts (§2.1.3)."""
    raw = os.getenv("DALTON_HOST_VAR_DIR", "").strip()
    if raw:
        return raw
    return str(REPO_ROOT / "var" / "dalton")


def get_dalton_backend_var_dir() -> str:
    """Return the backend container's internal ``var/dalton`` path (§2.1.3, §4.1.1)."""
    raw = os.getenv("DALTON_BACKEND_VAR_DIR", "").strip()
    if raw:
        return raw
    return "/app/var/dalton"


def is_budget_gate_enabled() -> bool:
    """Whether LocalBudgetPort is active (OSS mode + DALTON_BUDGET_GATE=1)."""
    return HOST_MODE == "oss" and os.getenv("DALTON_BUDGET_GATE", "").strip() == "1"


is_dalton_budget_gate_enabled = is_budget_gate_enabled


SUPABASE_PUBLISHABLE_KEY: str = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
_IMPORT_SUPABASE_PUBLISHABLE_KEY = SUPABASE_PUBLISHABLE_KEY


def get_supabase_publishable_key() -> str:
    """Return ``SUPABASE_PUBLISHABLE_KEY`` from environment or settings."""
    from src.config import settings as app_settings

    app_val = getattr(app_settings, "SUPABASE_PUBLISHABLE_KEY", None)
    if isinstance(app_val, str) and app_val.strip():
        return app_val.strip()
    if SUPABASE_PUBLISHABLE_KEY != _IMPORT_SUPABASE_PUBLISHABLE_KEY:
        return SUPABASE_PUBLISHABLE_KEY.strip()
    return os.getenv("SUPABASE_PUBLISHABLE_KEY", SUPABASE_PUBLISHABLE_KEY).strip()


VALID_DALTON_AUTH_MODES: frozenset[str] = frozenset({"local", "supabase", "oss"})
MIN_JWT_SIGNING_SECRET_BYTES: int = 32
MIN_TRANSCRIPTION_TICKET_SECRET_BYTES: int = 32


def get_dalton_auth_mode() -> str:
    """Return ``DALTON_AUTH_MODE`` normalized to lowercase.

    Defaults to ``local`` under ``DALTON_PROFILE=core`` and ``oss`` under Everflow.
    """
    raw = os.getenv("DALTON_AUTH_MODE")
    if raw is not None and raw.strip():
        return raw.strip().lower()
    if resolve_profile() is DeploymentProfile.CORE:
        return "local"
    return "oss"


def get_jwt_signing_secret() -> str:
    """Return ``JWT_SIGNING_SECRET`` from environment (stripped, empty if unset)."""
    return os.getenv("JWT_SIGNING_SECRET", "").strip()


def get_dalton_trusted_proxy_hops() -> int:
    """Return number of trusted reverse-proxy hops for ``X-Forwarded-For`` (default ``0`` = peer IP only)."""
    raw = os.getenv("DALTON_TRUSTED_PROXY_HOPS", "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def get_dalton_auth_cookie_secure_mode() -> str:
    """Return cookie ``Secure`` policy: ``'true'``, ``'false'``, or ``'auto'`` (default)."""
    raw = os.getenv("DALTON_AUTH_COOKIE_SECURE", "").strip().lower()
    if raw in ("1", "true", "yes", "always"):
        return "true"
    if raw in ("0", "false", "no", "never"):
        return "false"
    return "auto"


def is_core_oss_auth_explicitly_allowed() -> bool:
    """Return True when ``DALTON_ALLOW_DEV_OSS_AUTH`` explicitly permits ``oss`` under core profile."""
    return os.getenv("DALTON_ALLOW_DEV_OSS_AUTH", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _parse_http_origin_tuple(raw: str) -> tuple[str, str, int] | None:
    """Parse an http(s) URL into ``(scheme, lowercase_host, effective_port)``."""
    from urllib.parse import urlparse

    cleaned = str(raw or "").strip()
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    return (scheme, host, effective_port)


def _is_loopback_bind_address(raw: str) -> bool:
    """Return True iff ``raw`` is an explicit loopback bind host (``127.0.0.0/8``, ``::1``, or ``localhost``)."""
    cleaned = raw.strip()
    if not cleaned:
        return True
    if cleaned.startswith("[") and cleaned.endswith("]"):
        inner = cleaned[1:-1].strip()
        try:
            return ipaddress.IPv6Address(inner).is_loopback
        except ValueError:
            return False
    if cleaned.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def validate_dalton_auth_config() -> str:
    """Fail-closed startup validation for ``DALTON_AUTH_MODE`` (Standalone Core §4.2 #1).

    Raises :class:`DaltonStartupConfigError` with remediation instructions when:
    - ``DALTON_AUTH_MODE`` is not in ``{"local", "supabase", "oss"}``;
    - ``HOST_MODE=platform`` is combined with ``DALTON_AUTH_MODE=local``;
    - ``DALTON_AUTH_MODE=local`` lacks a ``JWT_SIGNING_SECRET`` of at least 32 bytes;
    - ``DALTON_AUTH_MODE=local`` has cross-origin frontend and API URLs configured;
    - ``DALTON_AUTH_MODE=local`` has transcription enabled without a dedicated
      ``DALTON_TRANSCRIPTION_TICKET_SECRET`` (>= 32 bytes);
    - ``DALTON_PROFILE=core`` sets a non-loopback ``DALTON_EDGE_TLS_BIND`` with
      an authentication mode other than ``local``;
    - ``DALTON_PROFILE=core`` uses ``DALTON_AUTH_MODE=oss`` without
      ``DALTON_ALLOW_DEV_OSS_AUTH=1``.
    """
    from src.config import settings as app_settings
    from src.dalton.profile import DaltonStartupConfigError

    profile = resolve_profile()
    mode = get_dalton_auth_mode()
    if mode not in VALID_DALTON_AUTH_MODES:
        raise DaltonStartupConfigError(
            f"Invalid DALTON_AUTH_MODE={mode!r}. "
            "Supported values are 'local', 'supabase', and 'oss'. "
            "Set DALTON_AUTH_MODE=local for standalone core deployments, "
            "DALTON_AUTH_MODE=supabase for Everflow, or DALTON_AUTH_MODE=oss for local dev."
        )
    if app_settings.HOST_MODE == "platform" and mode == "local":
        raise DaltonStartupConfigError(
            "Invalid auth configuration: DALTON_AUTH_MODE=local cannot be combined "
            "with HOST_MODE=platform. Set HOST_MODE=oss for standalone local auth, "
            "or unset DALTON_AUTH_MODE when running in platform mode."
        )
    if mode == "local":
        secret_bytes = len(get_jwt_signing_secret().encode("utf-8"))
        if secret_bytes < MIN_JWT_SIGNING_SECRET_BYTES:
            raise DaltonStartupConfigError(
                f"DALTON_AUTH_MODE=local requires JWT_SIGNING_SECRET with at least "
                f"{MIN_JWT_SIGNING_SECRET_BYTES} bytes (got {secret_bytes}). "
                "Generate one with `openssl rand -hex 32` and set JWT_SIGNING_SECRET in .env."
            )
        # Local auth uses an HttpOnly SameSite=Lax refresh cookie + strict Origin check,
        # so the web frontend and backend API must be served from the same origin.
        api_candidates: list[tuple[str, str, tuple[str, str, int]]] = []
        for api_key in ("SERVER_BASE_URL", "VITE_API_BASE_URL"):
            raw_api = os.getenv(api_key, "").strip()
            api_origin = _parse_http_origin_tuple(raw_api)
            if api_origin is not None:
                api_candidates.append((api_key, raw_api, api_origin))
        frontend_candidates: list[tuple[str, str, tuple[str, str, int]]] = []
        for fe_key in ("DALTON_FRONTEND_URL", "PUBLIC_APP_URL", "FRONTEND_URL"):
            raw_fe = os.getenv(fe_key, "").strip()
            fe_origin = _parse_http_origin_tuple(raw_fe)
            if fe_origin is not None:
                frontend_candidates.append((fe_key, raw_fe, fe_origin))
        for fe_key, raw_fe, fe_origin in frontend_candidates:
            for api_key, raw_api, api_origin in api_candidates:
                if fe_origin != api_origin:
                    raise DaltonStartupConfigError(
                        "DALTON_AUTH_MODE=local only supports same-origin frontend and API "
                        f"deployments ({fe_key}={raw_fe!r} conflicts with {api_key}={raw_api!r}). "
                        "Serve both the frontend and /api/* behind the same origin (for example "
                        "via Caddy edge gateway) and align PUBLIC_APP_URL / DALTON_FRONTEND_URL "
                        "with SERVER_BASE_URL."
                    )
        transcription_flag = os.getenv("DALTON_TRANSCRIPTION_ENABLED", "").strip().lower()
        if transcription_flag in ("1", "true", "yes", "on"):
            ticket_secret_bytes = len(
                get_dalton_transcription_ticket_secret().encode("utf-8")
            )
            if ticket_secret_bytes < MIN_TRANSCRIPTION_TICKET_SECRET_BYTES:
                raise DaltonStartupConfigError(
                    "DALTON_AUTH_MODE=local with DALTON_TRANSCRIPTION_ENABLED=true requires "
                    f"a dedicated DALTON_TRANSCRIPTION_TICKET_SECRET of at least "
                    f"{MIN_TRANSCRIPTION_TICKET_SECRET_BYTES} bytes (got {ticket_secret_bytes}). "
                    "Generate one with `openssl rand -hex 32`."
                )
    edge_tls_bind = os.getenv("DALTON_EDGE_TLS_BIND", "").strip()
    if (
        profile is DeploymentProfile.CORE
        and edge_tls_bind
        and not _is_loopback_bind_address(edge_tls_bind)
        and mode != "local"
    ):
        raise DaltonStartupConfigError(
            f"Non-loopback DALTON_EDGE_TLS_BIND={edge_tls_bind!r} under "
            f"DALTON_PROFILE=core requires DALTON_AUTH_MODE=local (got {mode!r}). "
            "Set DALTON_AUTH_MODE=local (with a >=32-byte JWT_SIGNING_SECRET and "
            "VITE_DALTON_AUTH=local) to listen on a non-loopback address, or set "
            "DALTON_EDGE_TLS_BIND=127.0.0.1 (or [::1] for IPv6) for loopback-only access."
        )
    if profile is DeploymentProfile.CORE and mode == "oss":
        if not is_core_oss_auth_explicitly_allowed():
            raise DaltonStartupConfigError(
                "DALTON_AUTH_MODE=oss (passwordless local-dev-user) is refused under "
                "DALTON_PROFILE=core. Set DALTON_AUTH_MODE=local (the core default), "
                "or set DALTON_ALLOW_DEV_OSS_AUTH=1 explicitly for local development/CI."
            )
    return mode


def get_dalton_allowed_emails() -> frozenset[str]:
    """Return lowercase email allowlist parsed from ``DALTON_ALLOWED_EMAILS`` (comma-separated)."""
    raw = os.getenv("DALTON_ALLOWED_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_dalton_local_auth_enabled() -> bool:
    """Whether Dalton local account authentication is active in OSS mode."""
    from src.config import settings as app_settings

    return app_settings.HOST_MODE == "oss" and get_dalton_auth_mode() == "local"


def is_dalton_supabase_auth_enabled() -> bool:
    """Whether Dalton Supabase authentication + email allowlist is active in OSS mode.

    Returns True whenever ``HOST_MODE == 'oss'`` and ``DALTON_AUTH_MODE == 'supabase'``
    so a missing ``SUPABASE_URL`` enters the Dalton auth path and fails closed with 503
    instead of falling back to passwordless ``local-dev-user``.
    """
    from src.config import settings as app_settings

    return app_settings.HOST_MODE == "oss" and get_dalton_auth_mode() == "supabase"


def is_dalton_single_user_oss_mode() -> bool:
    """显式单人回环模式：HOST_MODE == 'oss' 且 DALTON_AUTH_MODE == 'oss'。"""
    from src.config import settings as app_settings

    return app_settings.HOST_MODE == "oss" and get_dalton_auth_mode() == "oss"


def is_dalton_auth_enabled() -> bool:
    """Whether Dalton multi-user authentication (``supabase`` or ``local``) is active in OSS mode."""
    return is_dalton_supabase_auth_enabled() or is_dalton_local_auth_enabled()


def get_dalton_operator_llm_endpoints() -> tuple[str, ...]:
    """Return operator-allowlisted LLM base URLs from ``DALTON_OPERATOR_LLM_ENDPOINTS`` (comma-separated)."""
    raw = os.getenv("DALTON_OPERATOR_LLM_ENDPOINTS", "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def is_dalton_sharing_auth_enabled() -> bool:
    """Whether colleague workspace sharing & cross-workspace agent access is active.

    Requires ``CapabilityId.COLLEAGUE_SHARING`` and Dalton multi-user auth
    (``supabase``, or ``local`` now that Batch 4b wires ``dalton_user_roles``
    and ``require_team_reader``).
    """
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.COLLEAGUE_SHARING):
        return False
    return is_dalton_auth_enabled()


def is_dalton_multiuser_auth_enabled() -> bool:
    """Whether multi-user authentication (Supabase or local multi-user) is active.

    Used for public share link & artifact publish role checks independent of
    the colleague workspace sharing capability switch.
    """
    return is_dalton_supabase_auth_enabled() or is_dalton_local_auth_enabled()


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


def get_fake_ip_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Return the configured fake-IP ranges the in-house fetch guard may let through.

    Empty when Dalton is disabled, so the guard keeps upstream behaviour.
    """
    if not is_dalton_enabled():
        return ()
    return tuple(ipaddress.ip_network(c) for c in get_dalton_config().network.fake_ip_cidrs)


def get_fake_ip_real_resolvers() -> list[str]:
    """Return the DoH endpoints used to look up a fake-IP host's real addresses."""
    return list(get_dalton_config().network.fake_ip_real_resolver)


def log_dalton_startup_warnings() -> None:
    """Log prominent startup warnings for budget gate, Everflow APP_ENV, and SEC identity."""
    if not is_dalton_enabled():
        return
    profile = resolve_profile()
    if not is_budget_gate_enabled():
        logger.warning(
            "DALTON WARNING (G1.4): Local daily USD budget gate is NOT enabled "
            "(HOST_MODE=%r, DALTON_BUDGET_GATE=%r). Set DALTON_BUDGET_GATE=1 to enforce daily caps.",
            HOST_MODE,
            os.getenv("DALTON_BUDGET_GATE", ""),
        )
    if profile is DeploymentProfile.EVERFLOW:
        app_env = os.getenv("APP_ENV", "").strip()
        if app_env != "dalton":
            logger.warning(
                "DALTON WARNING (G7.1): APP_ENV=%r is not 'dalton'. "
                "agent_config.dalton.yaml is not active and MCP call_timeout_s will fall back to 65s!",
                app_env,
            )
    sec_identity, sec_configured = resolve_sec_user_agent()
    if not sec_configured:
        logger.warning(
            "DALTON WARNING: Neither SEC_USER_AGENT nor EDGAR_IDENTITY is set. "
            "Falling back to neutral placeholder %r; configure SEC_USER_AGENT "
            "(or EDGAR_IDENTITY) as '<Organization> <contact_email>' for SEC EDGAR fair-access compliance.",
            sec_identity,
        )


def get_dalton_broker_token() -> str:
    """Return ``DALTON_BROKER_TOKEN`` from the environment (empty string if unset).

    ``provider_events.sanitize_error_text`` uses this helper to redact the token.
    The x_search bridge reads ``DALTON_BROKER_TOKEN`` and ``DALTON_BROKER_URL``
    from the environment itself and does not call a settings helper.
    """
    return os.getenv("DALTON_BROKER_TOKEN", "").strip()


XAI_OAUTH_CLIENT_ID: str = os.getenv("DALTON_XAI_OAUTH_CLIENT_ID", "")
_IMPORT_XAI_OAUTH_CLIENT_ID = XAI_OAUTH_CLIENT_ID


def get_xai_oauth_client_id() -> str:
    """Return ``DALTON_XAI_OAUTH_CLIENT_ID`` from environment (no default value)."""
    if XAI_OAUTH_CLIENT_ID != _IMPORT_XAI_OAUTH_CLIENT_ID:
        return XAI_OAUTH_CLIENT_ID.strip()
    return os.getenv("DALTON_XAI_OAUTH_CLIENT_ID", XAI_OAUTH_CLIENT_ID).strip()


# cli-chat-proxy rejects clients below its minimum with HTTP 426 (1.0.4 was refused on
# 2026-10-23, minimum 1.0.13). Track the installed Grok CLI; override with the env var.
DEFAULT_XAI_GROK_CLIENT_VERSION = "1.0.67"
# The Codex backend hides models newer than the client it believes it serves:
# at 0.164.0 a ChatGPT subscription lists no GPT-6.2 model, at 0.170.0 it lists
# both (2026-11-18). One value for the request path and the model list.
DEFAULT_CODEX_CLIENT_VERSION = "0.170.0"


def get_codex_client_version() -> str:
    """Return the Codex CLI version Dalton presents to the codex backend."""
    raw = os.getenv("DALTON_CODEX_CLIENT_VERSION", "").strip()
    return raw or DEFAULT_CODEX_CLIENT_VERSION


def get_xai_grok_client_version() -> str:
    """Return the Grok CLI version sent as ``x-grok-client-version``."""
    raw = os.getenv("DALTON_XAI_GROK_CLIENT_VERSION", "").strip()
    return raw or DEFAULT_XAI_GROK_CLIENT_VERSION


def get_dalton_researchdata_url() -> str:
    """Return ``DALTON_RESEARCHDATA_URL`` from environment (empty string if unset or capability disabled)."""
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.RESEARCHDATA_BRIDGE):
        return ""
    url = os.getenv("DALTON_RESEARCHDATA_URL", "").strip()
    return url.replace("{HOST_IP}", HOST_IP) if url else ""


def _extract_bridge_origin_from_url(
    raw_url: str,
    *,
    host_override: str | None = None,
) -> str | None:
    """Parse ``scheme://host[:port]`` origin from a researchdata URL supporting ``.../mcp`` and ``/mcp/...``."""
    from urllib.parse import urlparse, urlunparse

    cleaned = str(raw_url or "").strip()
    if not cleaned:
        return None
    parsed = urlparse(cleaned)
    if not parsed.scheme or not parsed.hostname:
        return None
    path = parsed.path or ""
    if not (
        path == "/mcp"
        or path.startswith("/mcp/")
        or path.endswith("/mcp")
        or "/mcp/" in path
    ):
        return None
    target_host = host_override or parsed.hostname
    if parsed.port:
        netloc = f"{target_host}:{parsed.port}"
    elif host_override:
        netloc = f"{target_host}:8960"
    else:
        netloc = target_host
    return urlunparse((parsed.scheme, netloc, "", "", "", "")).rstrip("/")


def get_dalton_bridge_token() -> str:
    """Return ``DALTON_BRIDGE_TOKEN`` from environment or parsed from ``DALTON_RESEARCHDATA_URL``."""
    from urllib.parse import urlparse

    tok = os.getenv("DALTON_BRIDGE_TOKEN", "").strip()
    if tok:
        return tok
    rd_url = get_dalton_researchdata_url()
    if not rd_url:
        return ""
    parsed_path = urlparse(rd_url).path or ""
    if "/mcp/" in parsed_path:
        return parsed_path.split("/mcp/", 1)[1].strip("/")
    return ""


def get_dalton_bridge_admin_token() -> str:
    """Return ``DALTON_BRIDGE_ADMIN_TOKEN`` from environment (empty string if unset)."""
    return os.getenv("DALTON_BRIDGE_ADMIN_TOKEN", "").strip()


def get_market_digest_owner_user_id() -> str | None:
    """Return the operator user allowed to read prepared market-digest batches.

    ``DALTON_MARKET_DIGEST_OWNER_ID`` must be one hyphenated UUID. Blank,
    malformed, and non-canonical values return None. There is no default user.
    """
    raw = os.getenv("DALTON_MARKET_DIGEST_OWNER_ID", "").strip()
    if len(raw) != 36:
        return None
    try:
        parsed = uuid.UUID(raw)
    except (TypeError, ValueError, AttributeError):
        return None
    canonical = str(parsed)
    if canonical != raw.lower():
        return None
    return canonical


def get_ai_finance_owner_user_id() -> str | None:
    """Return the operator user allowed to prepare AI-finance batches.

    ``DALTON_AI_FINANCE_OWNER_USER_ID`` must be one hyphenated UUID. Blank,
    malformed, and non-canonical values return None. There is no default user.
    """
    raw = os.getenv("DALTON_AI_FINANCE_OWNER_USER_ID", "").strip()
    if len(raw) != 36:
        return None
    try:
        parsed = uuid.UUID(raw)
    except (TypeError, ValueError, AttributeError):
        return None
    canonical = str(parsed)
    if canonical != raw.lower():
        return None
    return canonical


def get_internal_service_token() -> str:
    """Return ``INTERNAL_SERVICE_TOKEN`` from environment (empty string if unset)."""
    return os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()


def get_dalton_transcription_token() -> str:
    """Return ``DALTON_TRANSCRIPTION_TOKEN`` from environment (empty string if unset)."""
    return os.getenv("DALTON_TRANSCRIPTION_TOKEN", "").strip()


def get_dalton_research_host_token() -> str:
    """Return sandbox read-only MCP token ``DALTON_RESEARCH_HOST_TOKEN`` (empty string if unset)."""
    return os.getenv("DALTON_RESEARCH_HOST_TOKEN", "").strip()


def get_dalton_research_admin_token() -> str:
    """Return backend-only research host admin token ``DALTON_RESEARCH_ADMIN_TOKEN`` (empty string if unset)."""
    return os.getenv("DALTON_RESEARCH_ADMIN_TOKEN", "").strip()


def get_dalton_transcription_ticket_secret() -> str:
    """Return ``DALTON_TRANSCRIPTION_TICKET_SECRET`` from environment (empty string if unset)."""
    return os.getenv("DALTON_TRANSCRIPTION_TICKET_SECRET", "").strip()


def is_dalton_transcription_ticket_using_dev_fallback() -> bool:
    """Return True when transcription tickets are signed by the predictable local dev seed.

    Decoupled from ``LANGALPHA_OWNER_SECRET``: whenever ``DALTON_TRANSCRIPTION_TICKET_SECRET``,
    ``INTERNAL_SERVICE_TOKEN``, and ``DALTON_BRIDGE_ADMIN_TOKEN`` are all unset in ``oss``
    auth mode, tickets use the predictable dev seed and MUST be restricted to direct
    loopback non-proxied requests.
    """
    if is_dalton_local_auth_enabled():
        return False
    explicit_seed = (
        get_dalton_transcription_ticket_secret()
        or get_internal_service_token()
        or get_dalton_bridge_admin_token()
    )
    return not explicit_seed and get_dalton_auth_mode() == "oss"


def get_dalton_transcription_ticket_hmac_key() -> bytes | None:
    """Derive the HMAC-SHA256 signing key for per-job transcription tickets.

    - ``local`` mode requires a dedicated ``DALTON_TRANSCRIPTION_TICKET_SECRET`` (no fallback).
    - Predictable dev seed fallback is kept ONLY in explicit ``oss`` development mode.
    """
    if is_dalton_local_auth_enabled():
        seed = get_dalton_transcription_ticket_secret()
    else:
        seed = (
            get_dalton_transcription_ticket_secret()
            or get_internal_service_token()
            or get_dalton_bridge_admin_token()
        )
        if not seed and get_dalton_auth_mode() == "oss":
            seed = f"dalton-local-dev-transcription-ticket-seed:{LOCAL_DEV_USER_ID}"
    if not seed:
        return None
    return hmac.new(
        seed.encode("utf-8"), b"dalton-transcription-ticket-v1", hashlib.sha256
    ).digest()


def managed_secret_token_values() -> tuple[str, ...]:
    """Non-empty values of the management tokens that must never reach a sandbox.

    ``DALTON_BRIDGE_ADMIN_TOKEN`` (the host bridge's admin credential),
    ``INTERNAL_SERVICE_TOKEN`` (the backend's service-to-service credential),
    ``DALTON_TRANSCRIPTION_TICKET_SECRET`` (the transcription ticket signing secret),
    and ``DALTON_TRANSCRIPTION_TOKEN`` (the standalone transcription service token).
    Any may be unset; only the non-empty ones are checked, so one missing
    never disables the others' scrub.
    """
    return tuple(
        t
        for t in (
            get_dalton_bridge_admin_token(),
            get_internal_service_token(),
            get_dalton_transcription_ticket_secret(),
            get_dalton_transcription_token(),
            get_dalton_research_admin_token(),
        )
        if t
    )


def redact_managed_tokens(text: Any) -> Any:
    """Replace each management token's value with ``***`` in a log field.

    Host-side logs can carry plaintext expanded from ``${...}`` config refs
    (a failing MCP server echoing its argv to stderr, an exception message)
    before any sandbox-bound sanitizing runs. Non-strings pass through.
    """
    if not isinstance(text, str):
        return text
    for token in managed_secret_token_values():
        text = text.replace(token, "***")
    return text


_WEIXIN_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "bot_token",
        "encrypted_bot_token",
        "context_token",
        "weixin_context_token",
        "encrypted_weixin_context_token",
        "qrcode",
        "login_qrcode",
        "encrypted_qrcode",
        "qrcode_img_content",
        "login_qrcode_img_content",
        "verify_code",
        "aes_key",
        "aeskey",
        "encrypt_query_param",
        "cdn_key",
        "weixin_media_meta",
        "encrypted_weixin_media_meta",
        "access_token",
        "secret",
        "password",
    }
)

_WEIXIN_SECRET_KV_RE = re.compile(
    r"""(?i)(?:"|')?\b(bot_token|context_token|weixin_context_token|qrcode|login_qrcode|verify_code|aes_?key|encrypt_query_param|access_token|x-service-token)\b(?:"|')?(\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,&;}\]]+)"""
)


def get_weixin_max_concurrent_accounts() -> int:
    """Return the single-gateway max concurrent Weixin poller slot cap (default 200)."""
    raw = os.getenv("DALTON_WEIXIN_MAX_CONCURRENT_ACCOUNTS", "").strip()
    if raw:
        try:
            parsed = int(raw)
            if 1 <= parsed <= 5000:
                return parsed
        except ValueError:
            logger.warning(
                "Invalid DALTON_WEIXIN_MAX_CONCURRENT_ACCOUNTS=%r; falling back to config default.",
                raw,
            )
    return int(get_dalton_config().weixin.max_concurrent_accounts)


def redact_weixin_secrets(value: Any) -> Any:
    """Recursively redact Weixin credentials, QR tickets, verify codes, and CDN keys for logs."""
    if isinstance(value, str):
        scrubbed = redact_managed_tokens(value)
        return _WEIXIN_SECRET_KV_RE.sub(r"\1\2***", scrubbed)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if str(k).strip().lower() in _WEIXIN_SECRET_KEYS:
                out[k] = "***"
            else:
                out[k] = redact_weixin_secrets(v)
        return out
    if isinstance(value, list):
        return [redact_weixin_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_weixin_secrets(item) for item in value)
    return value


def sanitize_sandbox_env_vars(
    env_vars: dict[str, str],
    servers: Any = None,
) -> dict[str, str]:
    """Strip any sandbox environment variable whose key or value contains a
    management token (``DALTON_BRIDGE_ADMIN_TOKEN`` or, by value whatever the
    key names it, ``INTERNAL_SERVICE_TOKEN``) and mark any MCP server carrying
    one as unavailable (``enabled = False``). Each token is checked only when
    set in the environment.
    """
    tokens = managed_secret_token_values()
    if not tokens:
        return env_vars

    def _leaks(text: Any) -> bool:
        s = str(text)
        return any(t in s for t in tokens)

    if servers:
        for server in servers:
            srv_name = getattr(server, "name", None) or "<unknown>"
            leaked = False
            srv_url = getattr(server, "url", None)
            if isinstance(srv_url, str) and _leaks(srv_url):
                leaked = True
            srv_cmd = getattr(server, "command", None)
            if isinstance(srv_cmd, str) and _leaks(srv_cmd):
                leaked = True
            for arg_val in getattr(server, "args", None) or ():
                if _leaks(arg_val):
                    leaked = True
            for hk, hv in (getattr(server, "headers", None) or {}).items():
                if _leaks(hk) or _leaks(hv):
                    leaked = True
            for ek, ev in (getattr(server, "env", None) or {}).items():
                if _leaks(ek) or _leaks(ev):
                    leaked = True
                    env_vars.pop(str(ek), None)
                    logger.warning(
                        "Removed sandbox env var containing a management token and marked server unavailable: server=%s item=env.%s",
                        srv_name,
                        ek,
                    )
            if leaked:
                try:
                    server.enabled = False
                except Exception:
                    pass

    for k, v in list(env_vars.items()):
        if _leaks(k) or _leaks(v):
            env_vars.pop(k, None)
            logger.warning(
                "Removed sandbox env var containing a management token: item=%s",
                k,
            )

    return env_vars


def sanitize_mcp_client_config_server(
    server: Any,
    entry: dict[str, Any],
) -> dict[str, Any]:
    """Strip any MCP client config field (``url``, ``headers``, ``command``,
    ``args``, ``env``, ``env_keys``) whose string content contains a management
    token (``DALTON_BRIDGE_ADMIN_TOKEN`` or ``INTERNAL_SERVICE_TOKEN``, by
    value) and mark the server unavailable so it cannot start with one. Each
    token is checked only when set in the environment.
    """
    tokens = managed_secret_token_values()
    if not tokens:
        return entry

    def _leaks(text: Any) -> bool:
        s = str(text)
        return any(t in s for t in tokens)

    srv_name = getattr(server, "name", None) or "<unknown>"
    leaked_items: list[str] = []

    raw_url = getattr(server, "url", None)
    ent_url = entry.get("url")
    if (isinstance(raw_url, str) and _leaks(raw_url)) or (
        isinstance(ent_url, str) and _leaks(ent_url)
    ):
        entry.pop("url", None)
        leaked_items.append("url")

    raw_headers = dict(getattr(server, "headers", None) or {})
    ent_headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}
    for hk in list(dict.fromkeys([*raw_headers.keys(), *ent_headers.keys()])):
        hv_raw = raw_headers.get(hk, "")
        hv_ent = ent_headers.get(hk, "")
        if _leaks(hk) or _leaks(hv_raw) or _leaks(hv_ent):
            if isinstance(entry.get("headers"), dict):
                entry["headers"].pop(hk, None)
            leaked_items.append(f"headers.{hk}")

    raw_cmd = getattr(server, "command", None)
    ent_cmd = entry.get("command")
    if (isinstance(raw_cmd, str) and _leaks(raw_cmd)) or (
        isinstance(ent_cmd, str) and _leaks(ent_cmd)
    ):
        entry.pop("command", None)
        leaked_items.append("command")

    raw_args = list(getattr(server, "args", None) or [])
    for idx, arg_val in enumerate(raw_args):
        if _leaks(arg_val):
            leaked_items.append(f"args[{idx}]")
    if isinstance(entry.get("args"), list):
        for idx, arg_val in enumerate(entry["args"]):
            if _leaks(arg_val) and f"args[{idx}]" not in leaked_items:
                leaked_items.append(f"args[{idx}]")
        entry["args"] = [a for a in entry["args"] if not _leaks(a)]

    raw_env = dict(getattr(server, "env", None) or {})
    ent_env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    for ek in list(dict.fromkeys([*raw_env.keys(), *ent_env.keys()])):
        ev_raw = raw_env.get(ek, "")
        ev_ent = ent_env.get(ek, "")
        if _leaks(ek) or _leaks(ev_raw) or _leaks(ev_ent):
            if isinstance(entry.get("env"), dict):
                entry["env"].pop(ek, None)
            if isinstance(entry.get("env_keys"), list):
                entry["env_keys"] = [
                    k
                    for k in entry["env_keys"]
                    if k != ek and not _leaks(k)
                ]
            leaked_items.append(f"env.{ek}")
    if isinstance(entry.get("env_keys"), list):
        for ek in list(entry["env_keys"]):
            if _leaks(ek):
                entry["env_keys"].remove(ek)
                if f"env.{ek}" not in leaked_items:
                    leaked_items.append(f"env.{ek}")

    if leaked_items:
        for item_name in leaked_items:
            logger.warning(
                "Removed MCP client config item containing a management token and marked server unavailable: server=%s item=%s",
                srv_name,
                item_name,
            )
        try:
            server.enabled = False
        except Exception:
            pass
        entry["enabled"] = False
        entry["available"] = False
        entry.pop("url", None)
        if "command" in entry:
            entry["command"] = ""
        if "args" in entry:
            entry["args"] = []

    return entry


def has_explicit_transcription_service_url() -> bool:
    """Return True when ``DALTON_TRANSCRIPTION_SERVICE_URL`` is explicitly configured."""
    explicit = os.getenv("DALTON_TRANSCRIPTION_SERVICE_URL", "").strip()
    return bool(explicit and not (explicit.startswith("${") and explicit.endswith("}")))


def _raw_explicit_research_host_url(environ: Mapping[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    for key in ("DALTON_RESEARCH_HOST_URL", "DALTON_RESEARCH_HOST_BASE_URL"):
        val = str(env.get(key) or "").strip()
        if val and not (val.startswith("${") and val.endswith("}")):
            return val
    return ""


def has_explicit_research_host_url(environ: Mapping[str, str] | None = None) -> bool:
    """Return True when ``DALTON_RESEARCH_HOST_URL`` (or alias ``DALTON_RESEARCH_HOST_BASE_URL``) is explicitly configured."""
    return bool(_raw_explicit_research_host_url(environ))


def resolve_connector_env_defaults(
    profile: DeploymentProfile | None = None,
) -> dict[str, str]:
    """Return deployment-profile default service URLs for split connector services.

    - ``DeploymentProfile.CORE``: standalone Compose service DNS names on ``core_internal`` / ``sandbox-net``.
    - ``DeploymentProfile.EVERFLOW``: keeps legacy bridge defaults until explicit cutover sets
      ``DALTON_TRANSCRIPTION_SERVICE_URL`` / ``DALTON_RESEARCH_HOST_URL`` in ``.env`` (never hardcodes
      ``:8951`` in Everflow).
    """
    active = resolve_profile() if profile is None else profile
    if active is DeploymentProfile.CORE:
        return {
            "DALTON_TRANSCRIPTION_SERVICE_URL": "http://transcription:8965",
            "DALTON_RESEARCH_HOST_URL": "http://research-host:8951",
            "DALTON_RESEARCH_HOST_BASE_URL": "http://research-host:8951",
        }
    return {
        "DALTON_TRANSCRIPTION_SERVICE_URL": "http://host.docker.internal:8960",
    }


def get_dalton_research_host_base_url(host: str | None = None) -> str:
    """Return ``dalton-research-host`` base URL, or ``""`` when disabled or unconfigured in Everflow."""
    from urllib.parse import urlparse, urlunparse
    from src.dalton.capabilities import has_any_research_connector_capability

    if not has_any_research_connector_capability():
        return ""
    profile = resolve_profile()
    in_docker = os.path.exists("/.dockerenv")
    raw_url = _raw_explicit_research_host_url()
    if raw_url:
        default_host = host or (HOST_IP if in_docker else "127.0.0.1") or "127.0.0.1"
        expanded = raw_url.replace("{HOST_IP}", default_host).rstrip("/")
        parsed = urlparse(expanded)
        if parsed.scheme and parsed.hostname:
            if host is not None:
                target_host: str | None = host
            elif (
                profile is DeploymentProfile.EVERFLOW
                and in_docker
                and parsed.hostname.lower() in ("127.0.0.1", "localhost", "::1")
            ):
                target_host = HOST_IP or "host.docker.internal"
            else:
                target_host = None
            if target_host:
                host_part = (
                    f"[{target_host}]"
                    if ":" in target_host and not target_host.startswith("[")
                    else target_host
                )
                netloc = f"{host_part}:{parsed.port}" if parsed.port else host_part
                return urlunparse(
                    (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
                ).rstrip("/")
        return expanded

    if profile is DeploymentProfile.EVERFLOW:
        return ""

    default_url = resolve_connector_env_defaults(profile)["DALTON_RESEARCH_HOST_URL"]
    port = os.getenv("DALTON_RESEARCH_HOST_PORT", "8951").strip() or "8951"
    if host is not None:
        host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{host_part}:{port}"
    if port != "8951":
        parsed_def = urlparse(default_url)
        if parsed_def.scheme and parsed_def.hostname:
            return f"{parsed_def.scheme}://{parsed_def.hostname}:{port}"
    return default_url.rstrip("/")


def get_dalton_transcription_service_url(host: str | None = None) -> str:
    """Return the transcription service base URL, or ``""`` when transcription capability is off."""
    from urllib.parse import urlparse, urlunparse
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.TRANSCRIPTION):
        return ""
    profile = resolve_profile()
    in_docker = os.path.exists("/.dockerenv")
    def _normalize_endpoint_url(
        raw_url: str,
        *,
        default_host: str,
        rewrite_loopback_in_docker: bool,
    ) -> str:
        expanded = raw_url.replace("{HOST_IP}", default_host).rstrip("/")
        parsed = urlparse(expanded)
        if parsed.scheme and parsed.hostname:
            if host is not None:
                target_host = host
            elif (
                rewrite_loopback_in_docker
                and in_docker
                and parsed.hostname.lower() in ("127.0.0.1", "localhost", "::1")
            ):
                target_host = HOST_IP or "host.docker.internal"
            else:
                target_host = None
            if target_host:
                host_part = (
                    f"[{target_host}]"
                    if ":" in target_host and not target_host.startswith("[")
                    else target_host
                )
                netloc = f"{host_part}:{parsed.port}" if parsed.port else host_part
                return urlunparse(
                    (
                        parsed.scheme,
                        netloc,
                        parsed.path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                ).rstrip("/")
        return expanded

    if has_explicit_transcription_service_url():
        explicit = os.getenv("DALTON_TRANSCRIPTION_SERVICE_URL", "").strip()
        default_host = host or (HOST_IP if in_docker else "127.0.0.1") or "127.0.0.1"
        return _normalize_endpoint_url(
            explicit,
            default_host=default_host,
            rewrite_loopback_in_docker=profile is DeploymentProfile.EVERFLOW,
        )

    if profile is DeploymentProfile.EVERFLOW:
        # Before cutover, follow the exact legacy bridge URL resolution rules
        # (`DALTON_BRIDGE_URL` -> `DALTON_RESEARCHDATA_URL` origin -> `DALTON_BRIDGE_PORT`)
        # so deployments using a custom bridge port keep working without hardcoding `:8960`.
        fallback_host = host or HOST_IP or "host.docker.internal"
        explicit_bridge = os.getenv("DALTON_BRIDGE_URL", "").strip()
        if explicit_bridge and not (
            explicit_bridge.startswith("${") and explicit_bridge.endswith("}")
        ):
            bridge_default_host = (
                host or (HOST_IP if in_docker else "127.0.0.1") or "127.0.0.1"
            )
            return _normalize_endpoint_url(
                explicit_bridge,
                default_host=bridge_default_host,
                rewrite_loopback_in_docker=True,
            )
        raw_rd_url = os.getenv("DALTON_RESEARCHDATA_URL", "").strip()
        if raw_rd_url and not (raw_rd_url.startswith("${") and raw_rd_url.endswith("}")):
            expanded_rd = raw_rd_url.replace("{HOST_IP}", fallback_host)
            origin = _extract_bridge_origin_from_url(expanded_rd, host_override=host)
            if origin:
                return _normalize_endpoint_url(
                    origin,
                    default_host=fallback_host,
                    rewrite_loopback_in_docker=True,
                )

    defaults = resolve_connector_env_defaults(profile)
    default_url = defaults["DALTON_TRANSCRIPTION_SERVICE_URL"]
    if profile is DeploymentProfile.EVERFLOW:
        default_port = "8960"
        port = os.getenv("DALTON_BRIDGE_PORT", default_port).strip() or default_port
    else:
        default_port = "8965"
        port = os.getenv("DALTON_TRANSCRIPTION_PORT", default_port).strip() or default_port
    if host is not None:
        host_part = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{host_part}:{port}"
    if profile is DeploymentProfile.EVERFLOW and not in_docker and HOST_IP:
        return f"http://{HOST_IP}:{port}"
    if port != default_port:
        parsed_def = urlparse(default_url)
        if parsed_def.scheme and parsed_def.hostname:
            return f"{parsed_def.scheme}://{parsed_def.hostname}:{port}"
    return default_url.rstrip("/")


def get_dalton_bridge_base_url(host: str | None = None) -> str:
    """Return the host bridge base URL (e.g. ``http://127.0.0.1:8960``), or ``""`` when bridge capability is off."""
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.RESEARCHDATA_BRIDGE):
        return ""
    resolved_host = host or HOST_IP or "127.0.0.1"
    explicit = os.getenv("DALTON_BRIDGE_URL", "").strip()
    if explicit:
        return explicit.replace("{HOST_IP}", resolved_host).rstrip("/")
    rd_url = get_dalton_researchdata_url()
    origin = _extract_bridge_origin_from_url(rd_url, host_override=host)
    if origin:
        return origin
    port = os.getenv("DALTON_BRIDGE_PORT", "8960").strip() or "8960"
    return f"http://{resolved_host}:{port}"


def get_dalton_channel_gateway_url(host: str | None = None) -> str:
    """Return the Dalton channel gateway base URL, or ``""`` when channel gateway capability is off.

    ``dalton-channel-gateway`` binds host loopback (``127.0.0.1:8962`` by default), so a
    backend container whose ``.env`` sets ``CHANNEL_GATEWAY_URL=http://127.0.0.1:8962``
    reaches the host service through ``HOST_IP`` (``host.docker.internal``).
    """
    from urllib.parse import urlparse, urlunparse
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return ""
    in_docker = os.path.exists("/.dockerenv")
    default_host = host or (HOST_IP if in_docker else "127.0.0.1") or "127.0.0.1"
    explicit = os.getenv("CHANNEL_GATEWAY_URL", "").strip()
    if explicit:
        expanded = explicit.replace("{HOST_IP}", default_host).rstrip("/")
        parsed = urlparse(expanded)
        if parsed.scheme and parsed.hostname:
            if host is not None:
                target_host = host
            elif in_docker and parsed.hostname.lower() in ("127.0.0.1", "localhost", "::1"):
                target_host = HOST_IP or "host.docker.internal"
            else:
                target_host = None
            if target_host:
                host_part = f"[{target_host}]" if ":" in target_host and not target_host.startswith("[") else target_host
                netloc = f"{host_part}:{parsed.port}" if parsed.port else host_part
                return urlunparse(
                    (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
                ).rstrip("/")
        return expanded
    port = os.getenv("GATEWAY_PORT", "8962").strip() or "8962"
    return f"http://{default_host}:{port}"


def notify_via_gateway_enabled() -> bool:
    """Return whether generic automation notifications use the channel gateway.

    ``DALTON_NOTIFY_VIA_GATEWAY`` defaults to off. Off keeps ``post_bridge_notify``.
    On sends newly claimed completion and test rows through the gateway. Always
    False when ``channel_gateway`` capability is disabled.
    """
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return False
    raw = os.getenv("DALTON_NOTIFY_VIA_GATEWAY", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_eve_tracking_chat_id() -> str:
    """Return ``DALTON_EVE_TRACKING_CHAT_ID`` (a Feishu ``oc_`` chat id).

    Empty means completion plans do not intercept any chat. A value may be the
    bare chat id or ``feishu:chat:<id>``; the returned string is the bare id.
    The id is never hardcoded.
    """
    raw = os.getenv("DALTON_EVE_TRACKING_CHAT_ID", "").strip()
    if raw.lower().startswith("feishu:chat:"):
        raw = raw.split(":", 2)[-1].strip()
    return raw


FEISHU_SEND_CARD_TIMEOUT_S: float = 15.0


def get_feishu_send_card_timeout_s() -> float:
    """Return the Feishu card subprocess timeout in seconds from ``FEISHU_SEND_CARD_TIMEOUT_S`` (default 15s)."""
    raw = os.getenv("FEISHU_SEND_CARD_TIMEOUT_S", "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return max(1.0, float(FEISHU_SEND_CARD_TIMEOUT_S))


def get_notify_http_timeout_s() -> float:
    """Return the dispatch-to-bridge HTTP timeout (``FEISHU_SEND_CARD_TIMEOUT_S + 5``)."""
    return get_feishu_send_card_timeout_s() + 5.0


def get_dalton_frontend_url() -> str:
    """Return configured public frontend URL (``PUBLIC_APP_URL`` / ``DALTON_FRONTEND_URL``), or empty string if unconfigured.

    Never falls back to ``SERVER_BASE_URL`` (the backend API origin), and ignores
    any value that merely duplicates ``SERVER_BASE_URL``.
    """
    server_base = os.getenv("SERVER_BASE_URL", "").strip().rstrip("/")
    for key in ("DALTON_FRONTEND_URL", "PUBLIC_APP_URL", "FRONTEND_URL"):
        val = os.getenv(key, "").strip().rstrip("/")
        if val and (not server_base or val != server_base):
            return val
    return ""


def get_dalton_public_app_url() -> str:
    """Return configured public web origin (``DALTON_FRONTEND_URL`` / ``PUBLIC_APP_URL`` / ``FRONTEND_URL``).

    Unlike ``get_dalton_frontend_url()``, accepts a configured public URL even
    when it equals ``SERVER_BASE_URL`` (the single-origin Caddy edge layout used
    by Standalone Core deployments). Only ``http://`` and ``https://`` origins
    are returned; returns empty string if unconfigured.
    """
    for key in ("DALTON_FRONTEND_URL", "PUBLIC_APP_URL", "FRONTEND_URL"):
        val = os.getenv(key, "").strip().rstrip("/")
        if val.startswith(("http://", "https://")):
            return val
    return ""


def get_dalton_bridge_embed_url(host: str | None = None) -> str:
    """Return the host bridge `/embed` URL, or ``""`` when bridge capability is off."""
    from src.dalton.capabilities import CapabilityId, has_capability

    if not has_capability(CapabilityId.RESEARCHDATA_BRIDGE):
        return ""
    resolved_host = host or HOST_IP or "host.docker.internal"
    explicit = os.getenv("DALTON_BRIDGE_EMBED_URL", "").strip()
    if explicit:
        url = explicit.replace("{HOST_IP}", resolved_host).rstrip("/")
        if "/embed/" in url:
            url = url.split("/embed/", 1)[0] + "/embed"
        return url
    rd_url = get_dalton_researchdata_url()
    origin = _extract_bridge_origin_from_url(rd_url, host_override=host)
    if origin:
        return f"{origin}/embed"
    port = os.getenv("DALTON_BRIDGE_PORT", "8960").strip() or "8960"
    return f"http://{resolved_host}:{port}/embed"


def get_sec_user_agent() -> str:
    """Return ``SEC_USER_AGENT`` / ``EDGAR_IDENTITY`` from env, or a neutral placeholder."""
    return resolve_sec_user_agent()[0]


def parse_home_workspace_aliases(raw: str | None) -> dict[str, str]:
    """Parse ``DALTON_HOME_WORKSPACE_ALIASES`` (``user_id:workspace_id[,user_id:workspace_id]``).

    Malformed entries are logged at WARNING and ignored without raising.
    Valid workspace UUIDs are normalized to canonical lowercase strings.
    """
    if not raw or not raw.strip():
        return {}
    aliases: dict[str, str] = {}
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2:
            logger.warning(
                "Ignoring malformed DALTON_HOME_WORKSPACE_ALIASES entry %r: expected 'user_id:workspace_id'",
                item,
            )
            continue
        uid, wid = parts[0].strip(), parts[1].strip()
        if not uid or not wid:
            logger.warning(
                "Ignoring malformed DALTON_HOME_WORKSPACE_ALIASES entry %r: empty user_id or workspace_id",
                item,
            )
            continue
        try:
            normalized_wid = str(uuid.UUID(wid))
        except ValueError:
            logger.warning(
                "Ignoring malformed DALTON_HOME_WORKSPACE_ALIASES entry %r: invalid workspace_id UUID %r",
                item,
                wid,
            )
            continue
        aliases[uid] = normalized_wid
    return aliases


@lru_cache(maxsize=8)
def _cached_home_workspace_aliases(raw: str) -> dict[str, str]:
    return parse_home_workspace_aliases(raw)


def get_home_workspace_aliases() -> dict[str, str]:
    """Return cached ``{user_id: workspace_id}`` map from ``DALTON_HOME_WORKSPACE_ALIASES``."""
    raw = os.getenv("DALTON_HOME_WORKSPACE_ALIASES", "")
    return _cached_home_workspace_aliases(raw)


get_home_workspace_aliases.cache_clear = _cached_home_workspace_aliases.cache_clear  # type: ignore[attr-defined]


def get_home_workspace_alias(user_id: str) -> str | None:
    """Return the aliased Home ``workspace_id`` for ``user_id`` if configured, else ``None``."""
    if not user_id:
        return None
    return get_home_workspace_aliases().get(user_id)


HOME_WORKSPACE_ALIASES: dict[str, str] = get_home_workspace_aliases()

def get_dalton_audit_cache_hmac_key() -> bytes | None:
    """Derive the HMAC-SHA256 signing key for `.audit.md` cache blocks solely from ``DALTON_AUDIT_CACHE_KEY``."""
    seed = os.getenv("DALTON_AUDIT_CACHE_KEY", "").strip()
    if not seed:
        return None
    return hmac.new(seed.encode("utf-8"), b"dalton-audit-cache-v1", hashlib.sha256).digest()


_EPHEMERAL_WORKFLOW_DECISION_KEY: bytes = secrets.token_bytes(32)


def get_dalton_workflow_decision_hmac_key() -> bytes:
    """Derive the server-only HMAC-SHA256 key for signing workflow launch decisions.

    Uses a deployment server secret (never exposed to sandboxes) when configured so
    signatures survive process restarts and multi-worker routing, falling back to a
    process-ephemeral 32-byte key in local/test runs without env secrets.
    """
    for env_name in (
        "JWT_SIGNING_SECRET",
        "DALTON_AUDIT_CACHE_KEY",
        "SUPABASE_SECRET_KEY",
        "DALTON_RESEARCH_HOST_ADMIN_TOKEN",
    ):
        seed = os.getenv(env_name, "").strip()
        if seed:
            return hmac.new(
                seed.encode("utf-8"),
                b"dalton-workflow-decision-v1",
                hashlib.sha256,
            ).digest()
    return hmac.new(
        _EPHEMERAL_WORKFLOW_DECISION_KEY,
        b"dalton-workflow-decision-v1",
        hashlib.sha256,
    ).digest()


DEFAULT_INVITE_EMAIL_DOMAIN = "team.dalton.local"


def get_dalton_invite_email_domain() -> str:
    """Return ``DALTON_INVITE_EMAIL_DOMAIN`` (lowercase, default ``team.dalton.local``)."""
    raw = os.getenv("DALTON_INVITE_EMAIL_DOMAIN", "").strip().lower().lstrip("@")
    return raw or DEFAULT_INVITE_EMAIL_DOMAIN


def get_supabase_secret_key() -> str:
    """Return ``SUPABASE_SECRET_KEY`` for Supabase Admin API calls (never logged)."""
    return os.getenv("SUPABASE_SECRET_KEY", "").strip()


def get_supabase_url() -> str:
    """Return ``SUPABASE_URL`` without trailing slash."""
    env_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    if env_url:
        return env_url
    from src.config import settings as app_settings

    return (getattr(app_settings, "SUPABASE_URL", None) or "").strip().rstrip("/")


DEFAULT_TRUSTED_PROXIES: tuple[str, ...] = ("127.0.0.1", "::1")


def get_dalton_trusted_proxies() -> tuple[str, ...]:
    """Return configured trusted reverse-proxy IPs/CIDRs from ``DALTON_TRUSTED_PROXIES`` (default ``127.0.0.1,::1``)."""
    raw = os.getenv("DALTON_TRUSTED_PROXIES")
    if raw is None or not raw.strip():
        return DEFAULT_TRUSTED_PROXIES
    parsed = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parsed if parsed else DEFAULT_TRUSTED_PROXIES


def _positive_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        parsed = float(raw.strip())
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return float(default)
    if parsed <= 0:
        logger.warning("Ignoring non-positive %s=%r; using %s", name, raw, default)
        return float(default)
    return parsed


def get_chat_upload_part_idle_timeout_s() -> float:
    """Per-chunk body-read idle deadline in seconds for chat multipart part uploads."""
    cfg = get_dalton_config()
    return _positive_float_env(
        "DALTON_CHAT_UPLOAD_PART_IDLE_TIMEOUT_S",
        cfg.chat_uploads.part_read_idle_timeout_s,
    )


def get_chat_upload_part_total_timeout_s() -> float:
    """Total wall-clock body-read deadline in seconds for one chat upload part."""
    cfg = get_dalton_config()
    return _positive_float_env(
        "DALTON_CHAT_UPLOAD_PART_TOTAL_TIMEOUT_S",
        cfg.chat_uploads.part_read_total_timeout_s,
    )


def is_chat_upload_cleanup_enabled() -> bool:
    """Whether the periodic ``dalton_chat_file_uploads`` cleanup loop should run."""
    cfg = get_dalton_config()
    if not cfg.enabled or not cfg.chat_uploads.cleanup_enabled:
        return False
    raw = os.getenv("DALTON_CHAT_UPLOAD_CLEANUP_ENABLED")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def get_stop_backup_attach_timeout_s() -> float:
    """Timeout in seconds for temporary sandbox attach during stop-time backup."""
    cfg = get_dalton_config()
    return _positive_float_env(
        "DALTON_STOP_BACKUP_ATTACH_TIMEOUT_S",
        cfg.stop_backup_attach_timeout_s,
    )


def get_stop_backup_total_timeout_s() -> float:
    """Shared wall-clock deadline in seconds for one whole stop-time backup round."""
    cfg = get_dalton_config()
    return _positive_float_env(
        "DALTON_STOP_BACKUP_TOTAL_TIMEOUT_S",
        cfg.stop_backup_total_timeout_s,
    )


def get_stop_backup_close_timeout_s() -> float:
    """Independent short timeout in seconds for closing temporary stop-backup clients."""
    cfg = get_dalton_config()
    return _positive_float_env(
        "DALTON_STOP_BACKUP_CLOSE_TIMEOUT_S",
        cfg.stop_backup_close_timeout_s,
    )


def _parse_bool_env_or_section_yaml(
    env_var: str, section: str, yaml_key: str, default: bool = True
) -> bool:
    """Resolve a runtime switch from ``env_var`` -> merged YAML ``<section>.<yaml_key>`` -> ``default``.

    Kept outside ``DaltonConfig`` so ``DaltonConfig.model_dump()`` stays byte-identical
    for Everflow parity snapshots while honoring both ``dalton.yaml`` and ``dalton.core.yaml``.
    """
    raw = os.getenv(env_var)
    if raw is not None and raw.strip():
        cleaned = raw.strip().lower()
        if cleaned in ("0", "false", "no", "off"):
            return False
        if cleaned in ("1", "true", "yes", "on"):
            return True
    raw_yaml = _load_profile_dalton_raw(resolve_profile())
    section_cfg = raw_yaml.get(section)
    if isinstance(section_cfg, Mapping) and isinstance(section_cfg.get(yaml_key), bool):
        return bool(section_cfg[yaml_key])
    return bool(default)


def _parse_bool_env_or_channels_yaml(env_var: str, yaml_key: str, default: bool = True) -> bool:
    """Resolve a channel runtime switch from ``env_var`` -> merged YAML ``channels.<yaml_key>`` -> ``default``."""
    return _parse_bool_env_or_section_yaml(env_var, "channels", yaml_key, default=default)


def is_channel_interactive_actions_enabled() -> bool:
    """Static switch for channel interactive action buttons (``DALTON_CHANNEL_INTERACTIVE_ACTIONS_ENABLED``)."""
    return _parse_bool_env_or_channels_yaml(
        "DALTON_CHANNEL_INTERACTIVE_ACTIONS_ENABLED",
        "interactive_actions_enabled",
        default=True,
    )


def is_channel_interrupt_ledger_enabled() -> bool:
    """Static switch for channel HITL interrupt resume ledger (``DALTON_CHANNEL_INTERRUPT_LEDGER_ENABLED``)."""
    return _parse_bool_env_or_channels_yaml(
        "DALTON_CHANNEL_INTERRUPT_LEDGER_ENABLED",
        "interrupt_ledger_enabled",
        default=True,
    )


def is_channel_bt2_link_recovery_enabled() -> bool:
    """Static switch for BT2 deliverable link crash recovery enrollment (``DALTON_CHANNEL_BT2_LINK_RECOVERY_ENABLED``)."""
    return _parse_bool_env_or_channels_yaml(
        "DALTON_CHANNEL_BT2_LINK_RECOVERY_ENABLED",
        "bt2_link_recovery_enabled",
        default=True,
    )


def is_redis_diagnostics_enabled() -> bool:
    """Runtime switch for Redis write-timeout diagnostics & sampling (``DALTON_REDIS_DIAG_ENABLED``).

    Defaults to ``True``; disable via ``DALTON_REDIS_DIAG_ENABLED=0`` or YAML
    ``observability.redis_diagnostics_enabled: false``.
    """
    return _parse_bool_env_or_section_yaml(
        "DALTON_REDIS_DIAG_ENABLED", "observability", "redis_diagnostics_enabled", default=True
    )


def is_strict_deliverable_text_enabled() -> bool:
    """Switch for unified strict deliverable-text extraction (``DALTON_STRICT_DELIVERABLE_TEXT``)."""
    return _parse_bool_env_or_channels_yaml(
        "DALTON_STRICT_DELIVERABLE_TEXT",
        "strict_deliverable_text_enabled",
        default=True,
    )


def is_near_empty_retry_enabled() -> bool:
    """Switch for near-empty reply retry detection (``DALTON_NEAR_EMPTY_RETRY_ENABLED``)."""
    return _parse_bool_env_or_section_yaml(
        "DALTON_NEAR_EMPTY_RETRY_ENABLED",
        "llm",
        "near_empty_retry_enabled",
        default=True,
    )




def get_redis_diag_ops_dir() -> Path:
    """Return directory for per-worker/server Redis diagnostics JSON files (default ``var/dalton/ops``)."""
    raw = os.getenv("DALTON_REDIS_DIAG_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return REPO_ROOT / "var" / "dalton" / "ops"


def is_monitor_verify_readback_enabled() -> bool:
    """Switch for 3-stage monitor publish verification & readback (``DALTON_MONITOR_VERIFY_READBACK_ENABLED``)."""
    return _parse_bool_env_or_section_yaml(
        "DALTON_MONITOR_VERIFY_READBACK_ENABLED",
        "monitors",
        "verify_readback_enabled",
        default=True,
    )


def get_monitor_multi_binding_publish_mode() -> Literal["per_panel", "all_or_nothing"]:
    """Return multi-panel data-job cut strategy: ``'per_panel'`` (default) or ``'all_or_nothing'``."""
    raw = os.getenv("DALTON_MONITOR_MULTI_BINDING_PUBLISH_MODE")
    if raw is not None and raw.strip():
        cleaned = raw.strip().lower()
        if cleaned in ("per_panel", "all_or_nothing"):
            return cleaned  # type: ignore[return-value]
        logger.warning(
            "Ignoring invalid DALTON_MONITOR_MULTI_BINDING_PUBLISH_MODE=%r; using 'per_panel'",
            raw,
        )
    raw_yaml = _load_profile_dalton_raw(resolve_profile())
    mon_cfg = raw_yaml.get("monitors")
    if isinstance(mon_cfg, Mapping):
        val = str(mon_cfg.get("multi_binding_publish_mode") or "").strip().lower()
        if val in ("per_panel", "all_or_nothing"):
            return val  # type: ignore[return-value]
    return "per_panel"


def is_unattended_quota_fallback_enabled() -> bool:
    """Switch for unattended-turn subscription quota fallback & wakeup deferral (default ``False``).

    Reads ``DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED`` -> YAML
    ``autonomy.unattended_quota_fallback_enabled`` -> ``False``.
    """
    return _parse_bool_env_or_section_yaml(
        "DALTON_UNATTENDED_QUOTA_FALLBACK_ENABLED",
        "autonomy",
        "unattended_quota_fallback_enabled",
        default=False,
    )


def allow_cash_fallback_on_unattended() -> bool:
    """Switch allowing unattended quota fallback to spill into cash-billed models (default ``False``).

    When ``False`` (default), unattended fallback is strictly confined to ``$0``
    OAuth subscription models. When ``True``, cash fallback is gated atomically
    through ``acquire_cash_lease_tx``. Reads
    ``DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED`` -> YAML
    ``autonomy.allow_cash_fallback_on_unattended`` -> ``False``.
    """
    return _parse_bool_env_or_section_yaml(
        "DALTON_ALLOW_CASH_FALLBACK_ON_UNATTENDED",
        "autonomy",
        "allow_cash_fallback_on_unattended",
        default=False,
    )

