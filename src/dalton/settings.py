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
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

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
    assist_model: str = ""
    assist_fallback_model: str | list[str] = Field(default_factory=list)
    probe_models: dict[str, str] = Field(default_factory=dict)


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
    preflight_timeout_s: float = Field(default=45.0, gt=0)
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
    models: DaltonModelsConfig = Field(default_factory=DaltonModelsConfig)
    budget: DaltonBudgetDefaults = Field(default_factory=DaltonBudgetDefaults)
    knowledge: DaltonKnowledgeConfig = Field(default_factory=DaltonKnowledgeConfig)
    publish: DaltonPublishConfig = Field(default_factory=DaltonPublishConfig)
    autonomy: DaltonAutonomyConfig = Field(default_factory=DaltonAutonomyConfig)
    conversations: DaltonConversationsConfig = Field(
        default_factory=DaltonConversationsConfig
    )
    network: DaltonNetworkConfig = Field(default_factory=DaltonNetworkConfig)


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
        return base.model_copy(
            update={"pm_user_ids": env_pm, "configured_pm_user_ids": env_pm}
        )
    return base


get_dalton_config.cache_clear = _load_base_dalton_config.cache_clear  # type: ignore[attr-defined]


get_dalton_settings = get_dalton_config


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


def get_dalton_auth_mode() -> str:
    """Return ``DALTON_AUTH_MODE`` normalized to lowercase (default ``oss``, optional ``supabase``)."""
    raw = os.getenv("DALTON_AUTH_MODE", "oss").strip().lower()
    return raw or "oss"


def get_dalton_allowed_emails() -> frozenset[str]:
    """Return lowercase email allowlist parsed from ``DALTON_ALLOWED_EMAILS`` (comma-separated)."""
    raw = os.getenv("DALTON_ALLOWED_EMAILS", "")
    return frozenset(e.strip().lower() for e in raw.split(",") if e.strip())


def is_dalton_supabase_auth_enabled() -> bool:
    """Whether Dalton Supabase authentication + email allowlist is active in OSS mode.

    Returns True whenever ``HOST_MODE == 'oss'`` and ``DALTON_AUTH_MODE == 'supabase'``
    so a missing ``SUPABASE_URL`` enters the Dalton auth path and fails closed with 503
    instead of falling back to passwordless ``local-dev-user``.
    """
    from src.config import settings as app_settings

    return app_settings.HOST_MODE == "oss" and get_dalton_auth_mode() == "supabase"


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


def get_xai_grok_client_version() -> str:
    """Return the Grok CLI version sent as ``x-grok-client-version``."""
    raw = os.getenv("DALTON_XAI_GROK_CLIENT_VERSION", "").strip()
    return raw or DEFAULT_XAI_GROK_CLIENT_VERSION


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


def get_dalton_web_search_bridge_token() -> str:
    """Return ``DALTON_WEB_SEARCH_BRIDGE_TOKEN`` from environment (empty string if unset).

    Uses a dedicated token rather than ``INTERNAL_SERVICE_TOKEN`` so the host-side
    launchd bridge never receives the backend's internal service-dispatch credential.
    """
    return os.getenv("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "").strip()


def get_dalton_web_search_bridge_url(host: str | None = None) -> str:
    """Return the resolved host-side OpenClaw web search HTTP bridge base URL."""
    resolved_host = host or HOST_IP
    explicit = os.getenv("DALTON_WEB_SEARCH_BRIDGE_URL", "").strip()
    if explicit:
        return explicit.replace("{HOST_IP}", resolved_host).rstrip("/")
    return f"http://{resolved_host}:8961"


def get_dalton_web_search_bridge_queue_timeout_seconds(default: float = 30.0) -> float:
    """Return ``DALTON_WEB_SEARCH_BRIDGE_QUEUE_TIMEOUT_S`` from environment."""
    raw = os.getenv("DALTON_WEB_SEARCH_BRIDGE_QUEUE_TIMEOUT_S", "").strip()
    if not raw:
        return float(default)
    try:
        return max(0.001, float(raw))
    except ValueError:
        return float(default)


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


def get_internal_service_token() -> str:
    """Return ``INTERNAL_SERVICE_TOKEN`` from environment (empty string if unset)."""
    return os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()


def managed_secret_token_values() -> tuple[str, ...]:
    """Non-empty values of the management tokens that must never reach a sandbox.

    ``DALTON_BRIDGE_ADMIN_TOKEN`` (the host bridge's admin credential) and
    ``INTERNAL_SERVICE_TOKEN`` (the backend's service-to-service credential).
    Either may be unset; only the non-empty ones are checked, so one missing
    never disables the other's scrub.
    """
    return tuple(
        t
        for t in (get_dalton_bridge_admin_token(), get_internal_service_token())
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


def get_dalton_bridge_base_url(host: str | None = None) -> str:
    """Return the host bridge base URL (e.g. ``http://127.0.0.1:8960``) for host job management."""
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


def get_dalton_bridge_embed_url(host: str | None = None) -> str:
    """Return the host bridge `/embed` URL for container-to-host embedding calls."""
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
    """Return ``SEC_USER_AGENT`` from environment with a compliant fallback."""
    return (
        os.getenv("SEC_USER_AGENT", "").strip()
        or os.getenv("EDGAR_IDENTITY", "").strip()
        or "DaltonResearch research@everflowinv.com"
    )


def get_openclaw_gateway_token() -> str:
    """Return ``OPENCLAW_GATEWAY_TOKEN`` (used only by P0.5 --target gateway control group)."""
    return os.getenv("OPENCLAW_GATEWAY_TOKEN", "").strip()


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





