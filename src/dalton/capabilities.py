"""Deployment-scoped capability registry and gating helpers (Standalone Core §3.1).

Single source of truth for which optional Everflow / host-coupled capabilities
are active in the current deployment:

- ``DeploymentProfile.EVERFLOW``: all capabilities default on, except
  ``transcription`` which preserves its existing default-off semantics
  (requires ``DALTON_TRANSCRIPTION_ENABLED`` or ``DALTON_CAP_TRANSCRIPTION``
  plus configured ``DALTON_ASR_STORAGE_*``).
- ``DeploymentProfile.CORE``: all optional capabilities default off.

Evaluation is a pure function with **no mutable module-level cache**, so
multi-worker processes (``--workers N``) and unit tests that mutate env vars
always observe a consistent, deterministic view. Capabilities are static for
a deployment; changing them requires restarting backend processes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeVar

from src.dalton.profile import DeploymentProfile, resolve_profile

logger = logging.getLogger(__name__)
_ServerT = TypeVar("_ServerT")

# Sentinel marking core-universal tools, routes, and background loops that do
# not require any optional capability.
CORE_CAPABILITY: None = None

_TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSY_VALUES = frozenset({"0", "false", "no", "off"})

HOST_PLATFORM_MACOS = "macos"
HOST_PLATFORM_LINUX = "linux"
HOST_PLATFORM_UNKNOWN = "unknown"
VALID_HOST_PLATFORMS: frozenset[str] = frozenset({HOST_PLATFORM_MACOS, HOST_PLATFORM_LINUX})


def resolve_host_platform(
    *,
    profile: DeploymentProfile | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the deployment host platform (``macos`` | ``linux``) from ``DALTON_HOST_PLATFORM``.

    Container ``sys.platform`` has zero influence on the result:
    - Explicit ``DALTON_HOST_PLATFORM``: ``macos`` (or ``darwin``) -> ``"macos"``;
      ``linux`` -> ``"linux"``; any unrecognized non-empty value logs a warning
      and returns ``"unknown"`` (fail-closed for macOS-only capabilities).
    - When unset or blank: ``DeploymentProfile.EVERFLOW`` defaults to ``"macos"``
      and ``DeploymentProfile.CORE`` defaults to ``"linux"``.
    """
    env = os.environ if environ is None else environ
    raw = env.get("DALTON_HOST_PLATFORM")
    if raw is not None and raw.strip():
        cleaned = raw.strip().lower()
        if cleaned in (HOST_PLATFORM_MACOS, "darwin"):
            return HOST_PLATFORM_MACOS
        if cleaned == HOST_PLATFORM_LINUX:
            return HOST_PLATFORM_LINUX
        logger.warning(
            "Unrecognized DALTON_HOST_PLATFORM=%r; treating host platform as %r (fail-closed for macOS-only capabilities).",
            raw,
            HOST_PLATFORM_UNKNOWN,
        )
        return HOST_PLATFORM_UNKNOWN

    active_profile = (
        resolve_profile(
            app_env=env.get("APP_ENV", "") if environ is not None else None,
            dalton_profile=env.get("DALTON_PROFILE", "") if environ is not None else None,
        )
        if profile is None
        else profile
    )
    return HOST_PLATFORM_MACOS if active_profile is DeploymentProfile.EVERFLOW else HOST_PLATFORM_LINUX


class CapabilityId(StrEnum):
    """Optional Dalton capabilities gated at deployment time."""

    RESEARCHDATA_BRIDGE = "researchdata_bridge"  # 宿主机研究桥接总开关
    ALPHAENGINE = "alphaengine"  # 卖方研报与纪要（依赖 researchdata_bridge）
    GUIDEPOINT = "guidepoint"  # 专家访谈库（依赖 researchdata_bridge）
    COMPANY_WIKI = "company_wiki"  # 内部 Wiki 读写（依赖 researchdata_bridge）
    SALES_NOTES = "sales_notes"  # 销售笔记检索与入库（依赖 researchdata_bridge）
    TMTB = "tmtb"  # TMT 总结库（依赖 researchdata_bridge）
    FUNDA_AI = "funda_ai"  # FUNDA AI 研究库（依赖 researchdata_bridge）
    FEISHU_DOCS = "feishu_docs"  # 飞书文档/知识空间读写（依赖 researchdata_bridge）
    TRANSCRIPTION = "transcription"  # 语音转写（独立转写服务 + 转写专用存储）
    CHANNEL_GATEWAY = "channel_gateway"  # 多渠道网关总开关
    CHANNEL_FEISHU = "channel_feishu"  # 飞书卡片与群绑定（依赖 channel_gateway）
    CHANNEL_DISCORD = "channel_discord"  # Discord 频道与私信（依赖 channel_gateway）
    CHANNEL_WEIXIN = "channel_weixin"  # 微信扫码与机器人绑定（依赖 channel_gateway）
    CONVERSATION_ARCHIVE = "conversation_archive"  # 历史团队对话归档检索
    DATA_JOBS = "data_jobs"  # 确定性定时数据采集任务与看板源
    FUND_AUTOMATIONS = "fund_automations"  # 内部定时简报模板、市场摘要与播客周报工具
    PROBLEM_REPORTS = "problem_reports"  # 内部运行问题工单上报工具（report_issue）
    NATIVE_OAUTH_SYNC = "native_oauth_sync"  # 私有订阅 OAuth 模型目录与同步面板
    COLLEAGUE_SHARING = "colleague_sharing"  # 同事工作区共享与智能体跨区访问


@dataclass(frozen=True)
class CapabilitySpec:
    """Static metadata describing one optional capability."""

    id: CapabilityId
    description: str
    depends_on: tuple[CapabilityId, ...] = ()
    everflow_default: bool = True
    core_default: bool = False
    legacy_env_vars: tuple[str, ...] = ()
    yaml_keys: tuple[str, ...] = ()

    @property
    def env_var(self) -> str:
        """Canonical explicit override env var (``DALTON_CAP_<NAME>``)."""
        return f"DALTON_CAP_{self.id.value.upper()}"


_RAW_CAPABILITY_SPECS: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        id=CapabilityId.RESEARCHDATA_BRIDGE,
        description="宿主机研究桥接总开关",
        everflow_default=True,
        core_default=False,
        legacy_env_vars=("DALTON_RESEARCHDATA_URL", "DALTON_BRIDGE_URL"),
        yaml_keys=("capabilities.researchdata_bridge",),
    ),
    CapabilitySpec(
        id=CapabilityId.ALPHAENGINE,
        description="卖方研报与纪要（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.alphaengine",),
    ),
    CapabilitySpec(
        id=CapabilityId.GUIDEPOINT,
        description="专家访谈库（独立研究连接器）",
        depends_on=(),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.guidepoint",),
    ),
    CapabilitySpec(
        id=CapabilityId.COMPANY_WIKI,
        description="内部 Wiki 读写（开源研究宿主连接器）",
        depends_on=(),
        everflow_default=True,
        core_default=True,
        yaml_keys=("capabilities.company_wiki",),
    ),
    CapabilitySpec(
        id=CapabilityId.SALES_NOTES,
        description="销售笔记检索与入库（独立研究连接器）",
        depends_on=(),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.sales_notes",),
    ),
    CapabilitySpec(
        id=CapabilityId.TMTB,
        description="TMT 总结库（独立研究连接器）",
        depends_on=(),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.tmtb",),
    ),
    CapabilitySpec(
        id=CapabilityId.FUNDA_AI,
        description="FUNDA AI 研究库（独立研究连接器）",
        depends_on=(),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.funda_ai",),
    ),
    CapabilitySpec(
        id=CapabilityId.FEISHU_DOCS,
        description="飞书文档/知识空间读写（依赖 researchdata_bridge）",
        depends_on=(CapabilityId.RESEARCHDATA_BRIDGE,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.feishu_docs",),
    ),
    CapabilitySpec(
        id=CapabilityId.TRANSCRIPTION,
        description="语音转写（独立转写服务 + 转写专用存储）",
        depends_on=(),
        everflow_default=False,
        core_default=False,
        legacy_env_vars=("DALTON_TRANSCRIPTION_ENABLED",),
        yaml_keys=("capabilities.transcription",),
    ),
    CapabilitySpec(
        id=CapabilityId.CHANNEL_GATEWAY,
        description="多渠道网关总开关",
        everflow_default=True,
        core_default=False,
        legacy_env_vars=("CHANNEL_GATEWAY_URL",),
        yaml_keys=("capabilities.channel_gateway",),
    ),
    CapabilitySpec(
        id=CapabilityId.CHANNEL_FEISHU,
        description="飞书卡片与群绑定（依赖 channel_gateway）",
        depends_on=(CapabilityId.CHANNEL_GATEWAY,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.channel_feishu",),
    ),
    CapabilitySpec(
        id=CapabilityId.CHANNEL_DISCORD,
        description="Discord 频道与私信（依赖 channel_gateway）",
        depends_on=(CapabilityId.CHANNEL_GATEWAY,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.channel_discord",),
    ),
    CapabilitySpec(
        id=CapabilityId.CHANNEL_WEIXIN,
        description="微信扫码与机器人绑定（依赖 channel_gateway）",
        depends_on=(CapabilityId.CHANNEL_GATEWAY,),
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.channel_weixin",),
    ),
    CapabilitySpec(
        id=CapabilityId.CONVERSATION_ARCHIVE,
        description="历史团队对话归档检索",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.conversation_archive", "conversations.enabled"),
    ),
    CapabilitySpec(
        id=CapabilityId.DATA_JOBS,
        description="确定性定时数据采集任务与看板源",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.data_jobs", "data_jobs.enabled"),
    ),
    CapabilitySpec(
        id=CapabilityId.FUND_AUTOMATIONS,
        description="内部定时简报模板、市场摘要与播客周报工具",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.fund_automations",),
    ),
    CapabilitySpec(
        id=CapabilityId.PROBLEM_REPORTS,
        description="内部运行问题工单上报工具（report_issue）",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.problem_reports",),
    ),
    CapabilitySpec(
        id=CapabilityId.NATIVE_OAUTH_SYNC,
        description="私有订阅 OAuth 模型目录与同步面板",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.native_oauth_sync",),
    ),
    CapabilitySpec(
        id=CapabilityId.COLLEAGUE_SHARING,
        description="同事工作区共享与智能体跨区访问",
        everflow_default=True,
        core_default=False,
        yaml_keys=("capabilities.colleague_sharing",),
    ),
)

CAPABILITY_SPECS: Mapping[CapabilityId, CapabilitySpec] = MappingProxyType(
    {spec.id: spec for spec in _RAW_CAPABILITY_SPECS}
)


def _parse_explicit_cap_env(name: str, raw: str | None) -> bool | None:
    """Parse ``DALTON_CAP_<NAME>``; returns ``None`` when unset or blank."""
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    if cleaned in _TRUTHY_VALUES:
        return True
    if cleaned in _FALSY_VALUES:
        return False
    logger.warning(
        "Unrecognized boolean value for %s=%r; treating capability as disabled (fail-closed).",
        name,
        raw,
    )
    return False


def _is_concrete_url_env(raw: str | None) -> bool:
    """Return True when a URL env var holds a non-empty value (not an unexpanded ``${...}``)."""
    if raw is None:
        return False
    cleaned = raw.strip()
    if not cleaned:
        return False
    if cleaned.startswith("${") and cleaned.endswith("}"):
        return False
    return True


def _resolve_transcription_storage_configured() -> bool:
    """Check transcription dedicated storage, honoring test patches on ``services.transcriptions``."""
    svc_mod = sys.modules.get("src.dalton.services.transcriptions")
    if svc_mod is not None:
        fn = getattr(svc_mod, "is_transcription_storage_configured", None)
        if callable(fn):
            return bool(fn())
    from src.dalton.transcription_storage import is_transcription_storage_configured

    return bool(is_transcription_storage_configured())


def _legacy_env_override(
    cap: CapabilityId,
    env: Mapping[str, str],
) -> bool | None:
    """Evaluate backward-compatible dedicated env switches (priority 2)."""
    if cap is CapabilityId.TRANSCRIPTION:
        raw = env.get("DALTON_TRANSCRIPTION_ENABLED")
        if raw is None:
            return None
        cleaned = raw.strip().lower()
        if not cleaned:
            return False
        return cleaned in _TRUTHY_VALUES

    if cap is CapabilityId.RESEARCHDATA_BRIDGE:
        if _is_concrete_url_env(env.get("DALTON_RESEARCHDATA_URL")) or _is_concrete_url_env(
            env.get("DALTON_BRIDGE_URL")
        ):
            return True
        return None

    if cap is CapabilityId.CHANNEL_GATEWAY:
        if _is_concrete_url_env(env.get("CHANNEL_GATEWAY_URL")):
            return True
        return None

    return None


def effective_capabilities(
    *,
    profile: DeploymentProfile | None = None,
    environ: Mapping[str, str] | None = None,
    yaml_capabilities: Mapping[str, bool] | None = None,
    conversations_enabled: bool | None = None,
    data_jobs_enabled: bool | None = None,
    dalton_enabled: bool | None = None,
    transcription_storage_configured: bool | None = None,
    host_platform: str | None = None,
) -> frozenset[CapabilityId]:
    """Compute the active capability set as a pure function (no module-level mutable cache).

    Precedence (highest to lowest):
    1. Explicit ``DALTON_CAP_<CAPABILITY_ID>=true|false``
    2. Legacy dedicated env vars (``DALTON_TRANSCRIPTION_ENABLED``,
       ``DALTON_DATA_JOBS_ENABLED``, ``DALTON_RESEARCHDATA_URL`` /
       ``DALTON_BRIDGE_URL``, ``CHANNEL_GATEWAY_URL``)
    3. Profile-merged YAML config (``dalton.yaml`` / ``dalton.core.yaml``)
    4. Profile defaults (Everflow: on except ``transcription``; Core: all off)

    Dependency rules are applied after raw evaluation: any capability whose
    parent capability or runtime prerequisite (such as dedicated ASR storage
    for ``transcription``) is unsatisfied is forced off.
    """
    from src.dalton.settings import (
        get_dalton_config,
        get_profile_dalton_config,
        get_yaml_capabilities,
        is_dalton_enabled,
    )

    env = os.environ if environ is None else environ

    if dalton_enabled is None:
        if environ is not None and "DALTON_ENABLED" in env:
            dalton_on = env["DALTON_ENABLED"].strip().lower() in ("1", "true", "yes")
        else:
            dalton_on = is_dalton_enabled()
    else:
        dalton_on = bool(dalton_enabled)

    if not dalton_on:
        return frozenset()

    active_profile = (
        resolve_profile(
            app_env=env.get("APP_ENV", "") if environ is not None else None,
            dalton_profile=env.get("DALTON_PROFILE", "") if environ is not None else None,
        )
        if profile is None
        else profile
    )

    yaml_caps_map = (
        get_yaml_capabilities(active_profile)
        if yaml_capabilities is None
        else yaml_capabilities
    )

    cfg = None
    if conversations_enabled is None or data_jobs_enabled is None:
        cfg = (
            get_profile_dalton_config(active_profile)
            if profile is not None
            else get_dalton_config()
        )
    conv_yaml = bool(cfg.conversations.enabled) if conversations_enabled is None else bool(conversations_enabled)
    dj_yaml = bool(cfg.data_jobs.enabled) if data_jobs_enabled is None else bool(data_jobs_enabled)

    raw_state: dict[CapabilityId, bool] = {}
    for cap, spec in CAPABILITY_SPECS.items():
        # Priority 1: explicit DALTON_CAP_<NAME>
        explicit = _parse_explicit_cap_env(spec.env_var, env.get(spec.env_var))
        if explicit is not None:
            raw_state[cap] = explicit
            continue

        # Priority 2: legacy dedicated env switches
        legacy = _legacy_env_override(cap, env)
        if legacy is not None:
            raw_state[cap] = legacy
            continue

        # Priority 3: profile-merged YAML config
        if cap.value in yaml_caps_map:
            raw_state[cap] = bool(yaml_caps_map[cap.value])
            continue
        if cap is CapabilityId.CONVERSATION_ARCHIVE:
            raw_state[cap] = conv_yaml
            continue
        if cap is CapabilityId.DATA_JOBS:
            raw_state[cap] = dj_yaml
            continue

        # Priority 4: profile default
        raw_state[cap] = (
            spec.core_default
            if active_profile is DeploymentProfile.CORE
            else spec.everflow_default
        )

    # Dependency closure: disable any capability whose prerequisites are unsatisfied.
    changed = True
    while changed:
        changed = False
        for cap, spec in CAPABILITY_SPECS.items():
            if not raw_state[cap]:
                continue
            if any(not raw_state[dep] for dep in spec.depends_on):
                raw_state[cap] = False
                changed = True

    if raw_state[CapabilityId.ALPHAENGINE]:
        resolved_host_platform = (
            resolve_host_platform(profile=active_profile, environ=env)
            if host_platform is None
            else resolve_host_platform(
                profile=active_profile,
                environ={"DALTON_HOST_PLATFORM": host_platform},
            )
        )
        if resolved_host_platform != HOST_PLATFORM_MACOS:
            raw_state[CapabilityId.ALPHAENGINE] = False

    if raw_state[CapabilityId.TRANSCRIPTION]:
        storage_ok = (
            _resolve_transcription_storage_configured()
            if transcription_storage_configured is None
            else bool(transcription_storage_configured)
        )
        if not storage_ok:
            raw_state[CapabilityId.TRANSCRIPTION] = False

    return frozenset(cap for cap, enabled in raw_state.items() if enabled)


def has_capability(
    cap: CapabilityId | str,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff ``cap`` is enabled in the active deployment."""
    cap_id = cap if isinstance(cap, CapabilityId) else CapabilityId(str(cap).strip())
    active = effective_capabilities() if capabilities is None else capabilities
    return cap_id in active


def capabilities_fingerprint(
    capabilities: frozenset[CapabilityId] | None = None,
) -> str:
    """Return a deterministic 16-char hex digest of the enabled capability set."""
    active = effective_capabilities() if capabilities is None else capabilities
    canonical = ",".join(sorted(cap.value for cap in active))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def get_capabilities_payload() -> dict[str, Any]:
    """Build the read-only ``GET /api/v1/dalton/capabilities`` response payload."""
    from src.config.env import GINLIX_DATA_ENABLED
    from src.dalton.settings import get_brand_name

    profile = resolve_profile()
    caps = effective_capabilities(profile=profile)
    brand_name = get_brand_name()
    return {
        "profile": profile.value,
        "capabilities": sorted(cap.value for cap in caps),
        "fingerprint": capabilities_fingerprint(caps),
        "brand_name": brand_name,
        "brand": {
            "name": brand_name,
        },
        "ticker_logos": bool(GINLIX_DATA_ENABLED),
        "market_enabled": True,
    }


def require_capability(*caps: CapabilityId) -> None:
    """Raise HTTP 404 when Dalton or any required capability is disabled."""
    from fastapi import HTTPException
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    active = effective_capabilities()
    for cap in caps:
        if cap not in active:
            raise HTTPException(
                status_code=404,
                detail=f"Capability '{cap.value}' is not enabled",
            )


def require_subscription_oauth_enabled() -> None:
    """Raise HTTP 404 when Dalton is enabled and ``native_oauth_sync`` is disabled.

    Gates upstream subscription OAuth endpoints (``/api/v1/oauth/{codex,claude,grok}/*``)
    in Core deployments where subscription sign-ins are disabled and all ``*-oauth``
    models are pruned from the catalog. When Dalton is disabled (upstream OSS),
    returns without error so upstream behavior is unchanged.
    """
    from fastapi import HTTPException
    from src.dalton.settings import is_dalton_enabled

    if not is_dalton_enabled():
        return
    if not has_capability(CapabilityId.NATIVE_OAUTH_SYNC):
        raise HTTPException(
            status_code=404,
            detail="Subscription OAuth sign-in is disabled in this deployment",
        )


def require_capability_dep(*caps: CapabilityId) -> Callable[[], None]:
    """Return a FastAPI ``Depends(...)`` guard enforcing ``require_capability(*caps)``."""

    def _guard() -> None:
        require_capability(*caps)

    _guard.__name__ = "require_" + "_and_".join(c.value for c in caps)
    return _guard


def platform_to_channel_capability(platform: str | None) -> CapabilityId | None:
    """Map a channel platform identifier (``feishu`` / ``discord`` / ``weixin``) to its sub-capability."""
    cleaned = str(platform or "").strip().lower()
    if cleaned == "feishu":
        return CapabilityId.CHANNEL_FEISHU
    if cleaned == "discord":
        return CapabilityId.CHANNEL_DISCORD
    if cleaned in ("weixin", "wechat"):
        return CapabilityId.CHANNEL_WEIXIN
    return None


def require_channel_platform_capability(platform: str | None) -> None:
    """Enforce ``channel_gateway`` plus the platform's channel capability (raises HTTP 404 when off)."""
    cap = platform_to_channel_capability(platform)
    if cap is not None:
        require_capability(CapabilityId.CHANNEL_GATEWAY, cap)
    else:
        require_capability(CapabilityId.CHANNEL_GATEWAY)


def channel_target_capability(
    target: str,
    *,
    channel: str | None = None,
) -> CapabilityId | None:
    """Map a notification target prefix or channel name to its sub-capability."""
    lowered = str(target or "").strip().lower()
    if lowered.startswith("feishu:"):
        return CapabilityId.CHANNEL_FEISHU
    if lowered.startswith("discord:"):
        return CapabilityId.CHANNEL_DISCORD
    if lowered.startswith(("weixin:", "wechat:")):
        return CapabilityId.CHANNEL_WEIXIN
    return platform_to_channel_capability(channel)


_CHANNEL_CAPABILITY_BY_NAME: Mapping[str, CapabilityId] = MappingProxyType(
    {
        "feishu": CapabilityId.CHANNEL_FEISHU,
        "discord": CapabilityId.CHANNEL_DISCORD,
        "weixin": CapabilityId.CHANNEL_WEIXIN,
        "wechat": CapabilityId.CHANNEL_WEIXIN,
    }
)


def disabled_channel_names() -> list[str]:
    """Return sorted lowercase channel names whose capability is currently disabled."""
    caps = effective_capabilities()
    gateway_on = CapabilityId.CHANNEL_GATEWAY in caps
    return sorted(
        ch
        for ch, cap in _CHANNEL_CAPABILITY_BY_NAME.items()
        if not gateway_on or cap not in caps
    )


def is_channel_target_enabled(
    target: str,
    *,
    channel: str | None = None,
) -> bool:
    """Return True when channel gateway and (if applicable) the target's channel capability are enabled."""
    caps = effective_capabilities()
    if CapabilityId.CHANNEL_GATEWAY not in caps:
        return False
    sub = channel_target_capability(target, channel=channel)
    if sub is not None and sub not in caps:
        return False
    return True


def is_messaging_gateway_allowed() -> bool:
    """Return True when ``channel_gateway`` allows generic messaging tools and ``gateway_request()``."""
    return has_capability(CapabilityId.CHANNEL_GATEWAY)


# ---------------------------------------------------------------------------
# Built-in MCP server & data-map route capability bindings
# ---------------------------------------------------------------------------

CapabilityRequirement = CapabilityId | tuple[CapabilityId, ...] | None

# Internal research source capabilities that may power `dalton_research_all` (OR semantics).
INTERNAL_RESEARCH_CAPABILITIES: tuple[CapabilityId, ...] = (
    CapabilityId.COMPANY_WIKI,
    CapabilityId.SALES_NOTES,
    CapabilityId.TMTB,
    CapabilityId.FUNDA_AI,
    CapabilityId.GUIDEPOINT,
    CapabilityId.ALPHAENGINE,
)

# Private commercial connector bundles loaded only via `DALTON_EXTRA_PLUGINS_DIR`
# (never shipped under public `plugins/` or in Standalone Core releases).
EXTRA_ONLY_BUNDLED_PLUGINS: frozenset[str] = frozenset(
    {
        "dalton-guidepoint",
        "dalton-sales-notes",
        "dalton-tmtb",
        "dalton-funda-ai",
    }
)


def is_capability_requirement_satisfied(
    req: CapabilityRequirement,
    active: frozenset[CapabilityId],
) -> bool:
    """Evaluate a capability binding: ``None`` -> True; single -> AND; tuple -> OR (any enabled)."""
    if req is None:
        return True
    if isinstance(req, tuple):
        return any(cap in active for cap in req)
    return req in active


def has_any_research_connector_capability(
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff at least one internal research source capability is enabled."""
    active = effective_capabilities() if capabilities is None else capabilities
    return any(cap in active for cap in INTERNAL_RESEARCH_CAPABILITIES)


MCP_SERVER_CAPABILITY_MAP: Mapping[str, CapabilityRequirement] = MappingProxyType(
    {
        "researchdata": CapabilityId.RESEARCHDATA_BRIDGE,
        "dalton_sec": CORE_CAPABILITY,
        "dalton_macro": CORE_CAPABILITY,
        "dalton_company_wiki": CapabilityId.COMPANY_WIKI,
        "dalton_research_all": INTERNAL_RESEARCH_CAPABILITIES,
        "dalton_guidepoint": CapabilityId.GUIDEPOINT,
        "dalton_sales_notes": CapabilityId.SALES_NOTES,
        "dalton_tmtb": CapabilityId.TMTB,
        "dalton_funda_ai": CapabilityId.FUNDA_AI,
        "price_data": CORE_CAPABILITY,
        "fundamentals": CORE_CAPABILITY,
        "macro": CORE_CAPABILITY,
        "options": CORE_CAPABILITY,
        "yf_price": CORE_CAPABILITY,
        "yf_fundamentals": CORE_CAPABILITY,
        "yf_analysis": CORE_CAPABILITY,
        "yf_market": CORE_CAPABILITY,
        "tickertick": CORE_CAPABILITY,
        "x_api": CORE_CAPABILITY,
        "scrape": CORE_CAPABILITY,
        "firecrawl": CORE_CAPABILITY,
    }
)

# Canonical bundled plugin registry: every shipped package under `plugins/`
# (and commercial extra bundles loaded via `DALTON_EXTRA_PLUGINS_DIR`) must be
# explicitly classified as CORE_CAPABILITY (None), a CapabilityId, or an OR tuple.
BUNDLED_PLUGIN_CAPABILITY_MAP: Mapping[str, CapabilityRequirement] = MappingProxyType(
    {
        "alternative-data": CORE_CAPABILITY,
        "dalton": CORE_CAPABILITY,
        "dalton-data": CapabilityId.RESEARCHDATA_BRIDGE,
        "dalton-sec": CORE_CAPABILITY,
        "dalton-macro": CORE_CAPABILITY,
        "dalton-company-wiki": CapabilityId.COMPANY_WIKI,
        "dalton-research-all": INTERNAL_RESEARCH_CAPABILITIES,
        "dalton-guidepoint": CapabilityId.GUIDEPOINT,
        "dalton-sales-notes": CapabilityId.SALES_NOTES,
        "dalton-tmtb": CapabilityId.TMTB,
        "dalton-funda-ai": CapabilityId.FUNDA_AI,
        "firecrawl": CORE_CAPABILITY,
        "langalpha-deliverables": CORE_CAPABILITY,
        "langalpha-market-data": CORE_CAPABILITY,
        "langalpha-research": CORE_CAPABILITY,
        "langalpha-service": CORE_CAPABILITY,
        "yfinance": CORE_CAPABILITY,
    }
)


# Batch 3 split MCP servers / bundled plugins: enabled by default in Core
# (subject to their capability requirement), and opted-in in Everflow only once
# `DALTON_RESEARCH_HOST_URL` (or `DALTON_RESEARCH_HOST_BASE_URL`) is explicitly
# set in `.env` during cutover (§0.2 #6 single authoritative handler).
BATCH3_CONNECTOR_MCP_SERVERS: frozenset[str] = frozenset(
    {
        "dalton_macro",
        "dalton_company_wiki",
        "dalton_research_all",
        "dalton_guidepoint",
        "dalton_sales_notes",
        "dalton_tmtb",
        "dalton_funda_ai",
    }
)

BATCH3_CONNECTOR_BUNDLED_PLUGINS: frozenset[str] = frozenset(
    {
        "dalton-macro",
        "dalton-company-wiki",
        "dalton-research-all",
        "dalton-guidepoint",
        "dalton-sales-notes",
        "dalton-tmtb",
        "dalton-funda-ai",
    }
)

# Batch 3 legacy bridge routes migrated to split connectors / `dalton-research-host`
# once `is_batch3_connectors_enabled()` is True (§6.4b (1) #3).
BATCH3_CUTOVER_BRIDGE_ROUTES: frozenset[str] = frozenset(
    {
        "sec",
        "fred",
        "treasury",
        "company-wiki",
        "guidepoint",
        "sales-notes",
        "sales-mail",
        "tmtb",
        "funda-ai",
        "all",
    }
)

BATCH3_CUTOVER_INGEST_TARGETS: frozenset[str] = frozenset(
    {
        "wiki",
        "sales-notes",
    }
)

# Profile, capability, and cutover control keys that MUST reside exclusively in
# the shared repository `.env` (never in host service credential files).
CAPABILITY_CONTROL_ENV_KEYS: frozenset[str] = frozenset(
    {
        "APP_ENV",
        "DALTON_PROFILE",
        "DALTON_HOST_PLATFORM",
        "DALTON_ENABLED",
        "DALTON_RESEARCH_HOST_URL",
        "DALTON_RESEARCH_HOST_BASE_URL",
    }
)


def is_capability_control_env_key(key: str | None) -> bool:
    """Return True if ``key`` controls deployment profile, capabilities, or cutover state."""
    cleaned = str(key or "").strip()
    if not cleaned:
        return False
    return cleaned in CAPABILITY_CONTROL_ENV_KEYS or cleaned.startswith("DALTON_CAP_")


def load_deployment_capability_env(
    *,
    repo_env_file: Path | str | None = None,
    service_env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    allowed_service_keys: Collection[str] | None = None,
    loaded_files: list[Path] | None = None,
) -> dict[str, str]:
    """Merge deployment capability and host service environment in one canonical order.

    Precedence (highest to lowest, §6.4b (1) #1):
      1. Explicit process environment variables (``environ`` or ``os.environ``).
      2. Shared repository root ``.env`` via ``src.dalton.config_env.load_shared_dotenv``
         (supports ``DALTON_REPO_ENV_FILE`` / ``DALTON_DOTENV_PATH`` and ``PYTHON_DOTENV_DISABLED``).
      3. Optional host-side private service credential file (e.g.
         ``~/.config/dalton/research-host.env`` or ``~/.config/dalton/researchdata-bridge.env``).
         Profile, capability, and cutover control keys (``is_capability_control_env_key``)
         are ALWAYS ignored from ``service_env_file`` with a warning so services can never
         diverge from the shared repository ``.env``.
    """
    from src.dalton.config_env import load_shared_dotenv, parse_and_resolve_dotenv

    merged: dict[str, str] = dict(os.environ if environ is None else environ)
    shared_loaded = load_shared_dotenv(repo_env_file, environ=merged)
    if shared_loaded is not None and loaded_files is not None:
        loaded_files.append(shared_loaded)

    svc_raw = str(service_env_file).strip() if service_env_file is not None else ""
    if svc_raw:
        svc_path = Path(svc_raw).expanduser()
        if svc_path.is_file():
            resolved_svc = svc_path.resolve()
            parsed_svc = parse_and_resolve_dotenv(
                resolved_svc,
                base_env=merged,
                override=False,
            )
            allowed_set = (
                frozenset(str(k).strip() for k in allowed_service_keys if str(k).strip())
                if allowed_service_keys is not None
                else None
            )
            for raw_k, val in parsed_svc.items():
                key = str(raw_k or "").strip()
                if not key:
                    continue
                if is_capability_control_env_key(key):
                    logger.warning(
                        "Ignoring capability/profile/cutover control key %r in service env file %s "
                        "(control keys must be defined only in shared repository .env)",
                        key,
                        resolved_svc,
                    )
                    continue
                if allowed_set is not None and key not in allowed_set:
                    logger.warning(
                        "Ignoring non-allowlisted key %r in service env file %s",
                        key,
                        resolved_svc,
                    )
                    continue
                merged.setdefault(key, val)
            if loaded_files is not None:
                loaded_files.append(resolved_svc)

    return merged


def find_capability_env_conflicts(
    *,
    repo_env_file: Path | str | None = None,
    service_env_file: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
    extra_sources: Mapping[str, Mapping[str, str]] | None = None,
    expected_bridge_env_file: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Detect profile/capability/cutover env overrides and host routing conflicts (§6.4b (1) #1).

    Checks:
      1. Any capability/profile/cutover control key placed in ``service_env_file``.
      2. Process env (``environ``) or ``extra_sources`` (e.g. ``launchctl getenv``,
         loaded ``.plist`` ``EnvironmentVariables``, running host processes) overriding
         control keys relative to ``repo_env_file``.
      3. ``DALTON_BRIDGE_ENV_FILE`` resolving to an unexpected path (while allowing the
         canonical resolved ``~/.config/dalton/researchdata-bridge.env`` path).
      4. ``DALTON_GUIDEPOINT_MCP_URL`` injected in ``extra_sources`` or overriding
         ``service_env_file``.
    """
    from src.dalton.config_env import parse_and_resolve_dotenv, resolve_shared_dotenv_path

    conflicts: list[dict[str, Any]] = []
    proc_env: Mapping[str, str] = os.environ if environ is None else environ

    repo_vals: dict[str, str] = {}
    resolved_repo = resolve_shared_dotenv_path(repo_env_file, environ=proc_env)
    if resolved_repo is not None and resolved_repo.is_file():
        repo_vals = parse_and_resolve_dotenv(resolved_repo, base_env={}, override=True)

    svc_vals: dict[str, str] = {}
    resolved_svc: Path | None = None
    if service_env_file is not None and str(service_env_file).strip():
        cand_svc = Path(service_env_file).expanduser()
        if cand_svc.is_file():
            resolved_svc = cand_svc.resolve()
            svc_vals = parse_and_resolve_dotenv(resolved_svc, base_env={}, override=True)
            for key, val in sorted(svc_vals.items()):
                if is_capability_control_env_key(key):
                    conflicts.append(
                        {
                            "source": f"service_env:{resolved_svc}",
                            "key": key,
                            "value": val,
                            "repo_value": repo_vals.get(key),
                            "reason": "control_key_in_service_env",
                        }
                    )

    expected_bridge_path = (
        Path(expected_bridge_env_file).expanduser().resolve()
        if expected_bridge_env_file is not None and str(expected_bridge_env_file).strip()
        else (Path.home() / ".config" / "dalton" / "researchdata-bridge.env").resolve()
    )

    def _scan_mapping(source_name: str, mapping: Mapping[str, str], *, is_external: bool) -> None:
        for key, raw_val in sorted(mapping.items()):
            val = str(raw_val or "").strip()
            if is_capability_control_env_key(key):
                repo_val = repo_vals.get(key)
                if repo_val is not None and val != str(repo_val).strip():
                    conflicts.append(
                        {
                            "source": source_name,
                            "key": key,
                            "value": val,
                            "repo_value": str(repo_val).strip(),
                            "reason": "control_key_override",
                        }
                    )
                elif repo_val is None and val and is_external:
                    conflicts.append(
                        {
                            "source": source_name,
                            "key": key,
                            "value": val,
                            "repo_value": None,
                            "reason": "control_key_injected",
                        }
                    )
            elif key == "DALTON_BRIDGE_ENV_FILE" and val:
                actual_path = Path(val).expanduser().resolve()
                if actual_path != expected_bridge_path:
                    conflicts.append(
                        {
                            "source": source_name,
                            "key": key,
                            "value": str(actual_path),
                            "expected_value": str(expected_bridge_path),
                            "reason": "unexpected_bridge_env_file_path",
                        }
                    )
            elif key == "DALTON_GUIDEPOINT_MCP_URL" and val:
                svc_gp = str(svc_vals.get("DALTON_GUIDEPOINT_MCP_URL") or "").strip()
                if is_external or (svc_gp and val != svc_gp):
                    conflicts.append(
                        {
                            "source": source_name,
                            "key": key,
                            "value": val,
                            "service_value": svc_gp or None,
                            "reason": "guidepoint_url_env_override",
                        }
                    )

    _scan_mapping("process_env", proc_env, is_external=False)
    if extra_sources:
        for src_name, src_map in extra_sources.items():
            _scan_mapping(str(src_name), src_map, is_external=True)

    return conflicts


def is_batch3_connectors_enabled(
    *,
    profile: DeploymentProfile | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True when Batch 3 split MCP connectors should be mounted.

    - ``DeploymentProfile.CORE``: always True (Core ships and uses the split
      public connectors by default, subject to each connector's capability).
    - ``DeploymentProfile.EVERFLOW``: False by default so pre-cutover Everflow
      keeps legacy ``researchdata`` as the sole authoritative handler; becomes
      True once ``DALTON_RESEARCH_HOST_URL`` (or ``DALTON_RESEARCH_HOST_BASE_URL``)
      is explicitly set in ``.env`` by ``switch_research_host.sh --switch``.
    """
    from src.dalton.settings import has_explicit_research_host_url

    env = os.environ if environ is None else environ
    active_profile = (
        resolve_profile(
            app_env=env.get("APP_ENV", "") if environ is not None else None,
            dalton_profile=env.get("DALTON_PROFILE", "") if environ is not None else None,
        )
        if profile is None
        else profile
    )
    if active_profile is DeploymentProfile.CORE:
        return True
    return has_explicit_research_host_url(env)


def is_mcp_server_enabled_by_capability(
    server_name: str | None,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff the named MCP server is active under profile cutover and capabilities."""
    cleaned = str(server_name or "").strip()
    if not cleaned:
        return True
    if cleaned in BATCH3_CONNECTOR_MCP_SERVERS and not is_batch3_connectors_enabled():
        return False
    req = MCP_SERVER_CAPABILITY_MAP.get(cleaned)
    if req is None:
        return True
    active = effective_capabilities() if capabilities is None else capabilities
    return is_capability_requirement_satisfied(req, active)


def is_bundled_plugin_enabled_by_capability(
    bundle_name: str | None,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff the named bundled plugin is active under profile cutover and capabilities.

    Accepts both manifest names (``dalton-data``) and directory names
    (``dalton_data``) so raw directory reads and API lookups always agree.
    """
    cleaned = str(bundle_name or "").strip()
    if not cleaned:
        return True
    normalized = cleaned.replace("_", "-") if "_" in cleaned else cleaned
    if normalized in BATCH3_CONNECTOR_BUNDLED_PLUGINS and not is_batch3_connectors_enabled():
        return False
    req = BUNDLED_PLUGIN_CAPABILITY_MAP.get(cleaned)
    if req is None and "_" in cleaned:
        req = BUNDLED_PLUGIN_CAPABILITY_MAP.get(normalized)
    if req is None:
        return True
    active = effective_capabilities() if capabilities is None else capabilities
    return is_capability_requirement_satisfied(req, active)


def filter_mcp_servers_by_capability(
    servers: Iterable[_ServerT],
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> list[_ServerT]:
    """Filter MCPServerConfig objects to those whose required capability is enabled."""
    active = effective_capabilities() if capabilities is None else capabilities
    return [
        srv
        for srv in servers
        if is_mcp_server_enabled_by_capability(getattr(srv, "name", None), capabilities=active)
    ]


def filter_mcp_server_dicts_by_capability(
    servers: Iterable[Mapping[str, Any]] | None,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> list[Mapping[str, Any]]:
    """Filter raw YAML ``mcp.servers`` entries before merge/validation."""
    if not servers:
        return []
    active = effective_capabilities() if capabilities is None else capabilities
    return [
        srv
        for srv in servers
        if not isinstance(srv, Mapping)
        or is_mcp_server_enabled_by_capability(srv.get("name"), capabilities=active)
    ]


def filter_reconnectable_builtin_mcp(failed_servers: Iterable[str] | None) -> set[str]:
    """Filter ``failed_builtin_remote`` server names to those whose capability is enabled.

    Used by ``src/server/app/setup.py`` (`DALTON-HOOK(standalone-core)`) so a
    disabled server such as ``researchdata`` never triggers the background
    ``mcp-builtin-reconnect`` loop in Standalone Core deployments. Preserves
    the original server name string so exact matching against ``MCPConfig.servers``
    stays identical to upstream for names with leading/trailing whitespace.
    """
    if not failed_servers:
        return set()
    active = effective_capabilities()
    eligible: set[str] = set()
    for name in failed_servers:
        if not isinstance(name, str) or not name:
            continue
        if is_mcp_server_enabled_by_capability(name.strip(), capabilities=active):
            eligible.add(name)
    return eligible


def filter_reconnect_targets(
    failed_servers: Iterable[str] | None,
    requested: Collection[str] | None = None,
) -> set[str]:
    """Select failed built-in remote servers eligible for background reconnect.

    Applies capability filtering first and, when ``requested`` is provided,
    intersects with ``requested`` so ``reconnect_builtin_remote`` only spawns
    reconnect tasks for enabled servers.
    """
    eligible = filter_reconnectable_builtin_mcp(failed_servers)
    if requested is None:
        return eligible
    allowed = {name for name in requested if isinstance(name, str) and name}
    return eligible & allowed


def start_mcp_builtin_reconnect_task(
    log: logging.Logger | None = None,
) -> asyncio.Task[None] | None:
    """Start the background ``mcp-builtin-reconnect`` task only for enabled failed servers.

    Called from ``src/server/app/setup.py`` via a single ``DALTON-HOOK(standalone-core)``
    delegation line so startup gating, filtered target hand-off, and logging live in Dalton.
    """
    from ptc_agent.core.mcp_registry import get_global_registry, reconnect_builtin_remote

    booted_registry = get_global_registry()
    if booted_registry is None:
        return None
    failed_builtin = filter_reconnectable_builtin_mcp(booted_registry.failed_builtin_remote)
    if not failed_builtin:
        return None
    task = asyncio.create_task(
        reconnect_builtin_remote(frozenset(failed_builtin)),
        name="mcp-builtin-reconnect",
    )
    (log or logger).info(
        "Reconnecting built-in MCP servers in the background: %s",
        ", ".join(sorted(failed_builtin)),
    )
    return task


BRIDGE_ROUTE_CAPABILITY_MAP: Mapping[str, CapabilityRequirement] = MappingProxyType(
    {
        "all": INTERNAL_RESEARCH_CAPABILITIES,
        "alphaengine": CapabilityId.ALPHAENGINE,
        "transcript": CapabilityId.ALPHAENGINE,
        "guidepoint": CapabilityId.GUIDEPOINT,
        "company-wiki": CapabilityId.COMPANY_WIKI,
        "sales-notes": CapabilityId.SALES_NOTES,
        "sales-mail": (CapabilityId.SALES_NOTES, CapabilityId.TMTB),
        "tmtb": CapabilityId.TMTB,
        "funda-ai": CapabilityId.FUNDA_AI,
        "tmeet": CapabilityId.FUND_AUTOMATIONS,
        "gcal": CapabilityId.FUND_AUTOMATIONS,
        "discord-history": CapabilityId.CHANNEL_DISCORD,
    }
)


def is_research_source_enabled(
    source_id: str,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff the given research connector/source capability is enabled (independent of legacy bridge)."""
    active = effective_capabilities() if capabilities is None else capabilities
    req = BRIDGE_ROUTE_CAPABILITY_MAP.get(str(source_id or "").strip())
    if req is None:
        return False
    return is_capability_requirement_satisfied(req, active)


def is_bridge_route_enabled(
    route_id: str,
    *,
    capabilities: frozenset[CapabilityId] | None = None,
) -> bool:
    """Return True iff ``researchdata_bridge`` and the route's own capability are enabled."""
    active = effective_capabilities() if capabilities is None else capabilities
    if CapabilityId.RESEARCHDATA_BRIDGE not in active:
        return False
    req = BRIDGE_ROUTE_CAPABILITY_MAP.get(str(route_id or "").strip())
    if req is not None:
        return is_capability_requirement_satisfied(req, active)
    return True


def is_bridge_route_migrated_to_connector(
    route_id: str | None,
    *,
    profile: DeploymentProfile | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True iff legacy bridge MCP ``route_id`` has cut over to Batch 3 connectors (§6.4b (1) #3)."""
    cleaned = str(route_id or "").strip().lower()
    if cleaned not in BATCH3_CUTOVER_BRIDGE_ROUTES:
        return False
    return is_batch3_connectors_enabled(profile=profile, environ=environ)


def is_bridge_ingest_migrated_to_connector(
    target: str | None,
    *,
    profile: DeploymentProfile | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Return True iff legacy bridge ingest endpoint ``target`` has cut over to ``research-host`` (§6.4b (1) #3)."""
    cleaned = str(target or "").strip().lower().strip("/")
    alias_map = {
        "wiki": "wiki",
        "wiki/ingest": "wiki",
        "company-wiki": "wiki",
        "company_wiki": "wiki",
        "sales-notes": "sales-notes",
        "sales_notes": "sales-notes",
        "sales-notes/ingest": "sales-notes",
    }
    normalized = alias_map.get(cleaned, cleaned)
    if normalized not in BATCH3_CUTOVER_INGEST_TARGETS:
        return False
    return is_batch3_connectors_enabled(profile=profile, environ=environ)


# ---------------------------------------------------------------------------
# Declarative registries for anti-leakage gate tests (§3.4 #1)
# ---------------------------------------------------------------------------

# Every background task (`asyncio.create_task`) and `.start()` background loop
# launched in `src/server/app/setup.py` must be classified here as either
# `CORE_CAPABILITY` (None) or a specific `CapabilityId`.
BACKGROUND_LOOP_REGISTRY: Mapping[str, CapabilityId | None] = MappingProxyType(
    {
        # Dalton lifespan tasks
        "dalton-startup-data-source-health": CORE_CAPABILITY,
        "dalton-transcription-reconcile": CapabilityId.TRANSCRIPTION,
        "dalton-data-task-scheduler": CapabilityId.DATA_JOBS,
        "dalton-chat-upload-cleanup": CORE_CAPABILITY,
        "dalton-team-model-catalog-follow": CORE_CAPABILITY,
        "dalton-startup-watchlist-reconcile": CORE_CAPABILITY,
        # Filtered by filter_reconnectable_builtin_mcp (skips disabled servers like researchdata)
        "mcp-builtin-reconnect": CORE_CAPABILITY,
        # Upstream core platform background tasks & services
        "warm-brokerage-marks": CORE_CAPABILITY,
        "heap-settle": CORE_CAPABILITY,
        "PlatformSecretSweeper": CORE_CAPABILITY,
        "HookOutboxDrainer": CORE_CAPABILITY,
        "RecoveryScanner": CORE_CAPABILITY,
        "restart_drain": CORE_CAPABILITY,
        "TurnCancelListener": CORE_CAPABILITY,
        "RedisBackgroundSingletons": CORE_CAPABILITY,
        "MarketDataFeed": CORE_CAPABILITY,
        "AutomationScheduler": CORE_CAPABILITY,
        "PriceMonitorService": CORE_CAPABILITY,
        "WakeupSweeper": CORE_CAPABILITY,
        "IdleCompactionSweeper": CORE_CAPABILITY,
        "InsightService": CORE_CAPABILITY,
        "NewsRefreshService": CORE_CAPABILITY,
        "ProvenanceGCService": CORE_CAPABILITY,
        # Gated in setup.py by is_feature_enabled_system("drive") (default off in Everflow & Core)
        "DriveTrashSweep": CORE_CAPABILITY,
        "WorkspaceFileGCService": CORE_CAPABILITY,
        "SetupSettings": CORE_CAPABILITY,
    }
)

# Every route handler defined under `src/dalton/app/*.py` must be classified
# here as either `CORE_CAPABILITY` (None), a single `CapabilityId`, or a tuple
# of `CapabilityId` values (all required).
DALTON_ROUTE_CAPABILITY_REGISTRY: Mapping[
    tuple[str, str], CapabilityId | tuple[CapabilityId, ...] | None
] = MappingProxyType(
    {
        # --- public_og.py --- (share-preview OpenGraph fragment; brand-only fallback, no credentials)
        ("public_og.py", "get_public_og_meta"): CORE_CAPABILITY,
        # --- router.py ---
        ("router.py", "get_capabilities_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_openclaw_health_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_maintenance_admission_status_endpoint"): CORE_CAPABILITY,
        ("router.py", "probe_maintenance_admission_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_browser_memory_diagnostics_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_native_models_status_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "check_native_models_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "sync_native_models_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "retire_native_models_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("router.py", "restore_native_models_endpoint"): CapabilityId.NATIVE_OAUTH_SYNC,
        # Team Model Catalog (d051, CORE_CAPABILITY for both Everflow and Standalone Core)
        ("router.py", "get_team_models_catalog_endpoint"): CORE_CAPABILITY,
        ("router.py", "create_team_model_endpoint"): CORE_CAPABILITY,
        ("router.py", "create_team_provider_variant_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_team_provider_variant_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_team_curated_setup_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_team_model_revisions_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_team_model_revision_endpoint"): CORE_CAPABILITY,
        ("router.py", "rollback_team_models_endpoint"): CORE_CAPABILITY,
        ("router.py", "test_call_team_model_endpoint"): CORE_CAPABILITY,
        ("router.py", "resolve_team_legacy_import_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_team_model_endpoint"): CORE_CAPABILITY,
        ("router.py", "set_team_model_visibility_endpoint"): CORE_CAPABILITY,
        ("router.py", "retire_team_model_endpoint"): CORE_CAPABILITY,
        ("router.py", "restore_team_model_endpoint"): CORE_CAPABILITY,
        ("router.py", "restore_team_model_default_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_providers_status_endpoint"): CORE_CAPABILITY,
        ("router.py", "probe_provider_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_available_models_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_budget_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_budget_endpoint"): CORE_CAPABILITY,
        ("router.py", "upsert_task_cap_endpoint"): CORE_CAPABILITY,
        ("router.py", "delete_task_cap_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_budget_today_endpoint"): CORE_CAPABILITY,
        # Read-only sharing GET endpoints return 200 neutral payloads when colleague_sharing is off;
        # write endpoints (PUT) remain gated by CapabilityId.COLLEAGUE_SHARING (404 when off).
        ("router.py", "list_team_workspaces_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_workspace_sharing_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_workspace_sharing_endpoint"): CapabilityId.COLLEAGUE_SHARING,
        ("router.py", "get_workspace_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_workspace_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_user_sharing_settings_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_user_sharing_settings_endpoint"): CapabilityId.COLLEAGUE_SHARING,
        ("router.py", "get_user_settings_endpoint"): CapabilityId.CONVERSATION_ARCHIVE,
        ("router.py", "update_user_settings_endpoint"): CapabilityId.CONVERSATION_ARCHIVE,
        ("router.py", "get_team_credentials_status_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_knowledge_versions_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_knowledge_version_endpoint"): CORE_CAPABILITY,
        ("router.py", "diff_knowledge_versions_endpoint"): CORE_CAPABILITY,
        ("router.py", "import_knowledge_document_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_verdicts_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_publications_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_publication_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_publish_requests_endpoint"): CORE_CAPABILITY,
        ("router.py", "put_dalton_automation_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_coverage_board_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_coverage_units_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_coverage_unit_detail_endpoint"): CORE_CAPABILITY,
        ("router.py", "register_coverage_unit_endpoint"): CORE_CAPABILITY,
        ("router.py", "bind_coverage_unit_endpoint"): CORE_CAPABILITY,
        ("router.py", "unbind_coverage_unit_endpoint"): CORE_CAPABILITY,
        ("router.py", "archive_coverage_unit_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_autonomy_report_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_workspace_tracking_endpoint"): CORE_CAPABILITY,
        ("router.py", "update_workspace_tracking_endpoint"): CORE_CAPABILITY,
        # system-jobs endpoints are core for company/sector/home and gate ops/data internally
        ("router.py", "list_system_jobs_endpoint"): CORE_CAPABILITY,
        ("router.py", "toggle_system_job_endpoint"): CORE_CAPABILITY,
        ("router.py", "run_system_job_now_endpoint"): CORE_CAPABILITY,
        ("router.py", "rollback_system_job_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_user_coverage_endpoint"): CORE_CAPABILITY,
        ("router.py", "mutate_user_coverage_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_user_coverage_groups_endpoint"): CORE_CAPABILITY,
        ("router.py", "preview_user_coverage_group_endpoint"): CORE_CAPABILITY,
        ("router.py", "create_user_coverage_group_endpoint"): CORE_CAPABILITY,
        ("router.py", "delete_user_coverage_group_endpoint"): CORE_CAPABILITY,
        ("router.py", "resolve_user_coverage_group_endpoint"): CORE_CAPABILITY,
        ("router.py", "get_user_earnings_calendar_endpoint"): CORE_CAPABILITY,
        ("router.py", "put_user_earnings_calendar_mirror_endpoint"): CORE_CAPABILITY,
        ("router.py", "create_invite_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_invites_endpoint"): CORE_CAPABILITY,
        ("router.py", "revoke_invite_endpoint"): CORE_CAPABILITY,
        ("router.py", "check_invite_endpoint"): CORE_CAPABILITY,
        ("router.py", "redeem_invite_endpoint"): CORE_CAPABILITY,
        ("router.py", "list_feishu_read_scopes_endpoint"): CapabilityId.FEISHU_DOCS,
        ("router.py", "update_feishu_read_scopes_endpoint"): CapabilityId.FEISHU_DOCS,
        ("router.py", "list_feishu_spaces_endpoint"): CapabilityId.FEISHU_DOCS,
        ("router.py", "dalton_notify_feishu_bot_endpoint"): CapabilityId.CHANNEL_FEISHU,
        ("router.py", "dalton_notify_feishu_groups_endpoint"): CapabilityId.CHANNEL_FEISHU,
        ("router.py", "dalton_notify_discord_channel_verify_endpoint"): CapabilityId.CHANNEL_DISCORD,
        ("router.py", "dalton_notify_test_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("router.py", "report_delivery_precheck_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("router.py", "issue_report_delivery_confirmation_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("router.py", "global_search_endpoint"): CORE_CAPABILITY,
        # --- channels_router.py ---
        ("channels_router.py", "internal_gateway_lease_acquire_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_gateway_lease_heartbeat_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_gateway_lease_release_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_inbound_register_and_resolve_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_bind_code_consume_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_session_command_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_state_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_settings_put_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_deliveries_authorize_and_claim_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_deliveries_preflight_send_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_deliveries_verify_dm_recipient_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_deliveries_progress_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_topics_lookup_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_deliverables_download_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_snapshots_status_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_snapshots_download_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_thread_run_replay_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_inbound_progress_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_steers_reconcile_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_recover_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_action_tokens_mint_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_action_tokens_bind_card_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_action_tokens_inspect_gate_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_action_tokens_consume_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_interrupt_resumes_claim_text_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_interrupt_resumes_ack_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_interrupt_runtime_config_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_automation_runs_register_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_automation_runs_finish_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "internal_weixin_accounts_reconcile_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "internal_weixin_account_detail_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "internal_weixin_account_cursor_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "internal_weixin_account_status_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "internal_weixin_login_session_get_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "internal_weixin_login_session_patch_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_create_bind_code_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "web_get_bindings_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "web_put_channel_settings_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "web_mutate_discord_channel_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_DISCORD,
        ),
        ("channels_router.py", "web_delete_binding_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        ("channels_router.py", "web_weixin_qrcode_start_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_weixin_qrcode_wait_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_weixin_qrcode_verify_code_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_weixin_admin_overview_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_weixin_admin_config_put_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_weixin_admin_delete_account_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        ("channels_router.py", "web_weixin_admin_delete_user_weixin_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_WEIXIN,
        ),
        # --- automation_notifications_router.py ---
        ("automation_notifications_router.py", "internal_automation_notification_plan_endpoint"): CapabilityId.CHANNEL_GATEWAY,
        # --- data_task_notifications_router.py ---
        ("data_task_notifications_router.py", "internal_dedicated_feishu_vault_credentials_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.CHANNEL_FEISHU,
        ),
        ("data_task_notifications_router.py", "internal_data_task_notification_plan_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.DATA_JOBS,
        ),
        ("data_task_notifications_router.py", "internal_data_task_run_artifact_endpoint"): (
            CapabilityId.CHANNEL_GATEWAY,
            CapabilityId.DATA_JOBS,
        ),
        # --- transcriptions.py ---
        ("transcriptions.py", "create_or_submit_transcription_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "get_transcription_usage_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "start_transcription_upload_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "sign_transcription_upload_part_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "complete_transcription_upload_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "abort_transcription_upload_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "get_transcription_status_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "get_transcription_result_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "get_transcription_raw_result_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "retry_transcription_endpoint"): CapabilityId.TRANSCRIPTION,
        ("transcriptions.py", "cancel_transcription_endpoint"): CapabilityId.TRANSCRIPTION,
        # --- oauth_native.py ---
        ("oauth_native.py", "muse_device_initiate"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "muse_device_poll"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "muse_status"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "muse_disconnect"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_initiate"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_poll"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_callback"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_status"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "antigravity_disconnect"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "xai_device_initiate"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "xai_device_poll"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "xai_status"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("oauth_native.py", "xai_disconnect"): CapabilityId.NATIVE_OAUTH_SYNC,
        # --- internal_llm_router.py / internal_model_availability_router.py / internal_usage_router.py ---
        ("internal_llm_router.py", "internal_llm_complete"): CapabilityId.DATA_JOBS,
        ("internal_model_availability_router.py", "internal_model_availability_check"): CapabilityId.NATIVE_OAUTH_SYNC,
        ("internal_usage_router.py", "get_usage_by_model_endpoint"): CORE_CAPABILITY,
        # --- chat_uploads.py (core large-file multipart upload) ---
        ("chat_uploads.py", "create_chat_upload_endpoint"): CORE_CAPABILITY,
        ("chat_uploads.py", "upload_chat_part_endpoint"): CORE_CAPABILITY,
        ("chat_uploads.py", "complete_chat_upload_endpoint"): CORE_CAPABILITY,
        ("chat_uploads.py", "download_sandbox_chat_upload_endpoint"): CORE_CAPABILITY,
        ("workspace_file_download.py", "download_workspace_file_with_ticket"): CORE_CAPABILITY,  # one-time ticket from download-url
        ("chat_uploads.py", "get_chat_upload_endpoint"): CORE_CAPABILITY,
        ("chat_uploads.py", "cancel_chat_upload_endpoint"): CORE_CAPABILITY,
        # --- watchlist.py (Personal Watchlist three-tier & outbox reconciler) ---
        ("watchlist.py", "get_watchlist_endpoint"): CORE_CAPABILITY,
        ("watchlist.py", "get_watchlist_metrics_endpoint"): CORE_CAPABILITY,
        ("watchlist.py", "get_watchlist_tier_policies_endpoint"): CORE_CAPABILITY,
        ("watchlist.py", "preview_watchlist_tier_policy_endpoint"): CORE_CAPABILITY,
        ("watchlist.py", "update_watchlist_tier_policy_endpoint"): CORE_CAPABILITY,
        ("watchlist.py", "reset_watchlist_tier_policy_endpoint"): CORE_CAPABILITY,
        ("watchlist.py", "preview_watchlist_mutation_endpoint"): CORE_CAPABILITY,
        ("watchlist.py", "mutate_watchlist_endpoint"): CORE_CAPABILITY,
        ("watchlist.py", "reconcile_watchlist_endpoint"): CORE_CAPABILITY,
        # --- research_host/app.py (standalone research-host service) ---
        ("research_host/app.py", "healthz"): CORE_CAPABILITY,
        ("research_host/app.py", "get_drain"): CORE_CAPABILITY,
        ("research_host/app.py", "set_drain"): CORE_CAPABILITY,
        ("research_host/app.py", "acquire_connector_lock"): CORE_CAPABILITY,
        ("research_host/app.py", "get_wiki_pause_writes"): CapabilityId.COMPANY_WIKI,
        ("research_host/app.py", "set_wiki_pause_writes"): CapabilityId.COMPANY_WIKI,
        ("research_host/app.py", "mcp_endpoint"): INTERNAL_RESEARCH_CAPABILITIES,
        ("research_host/app.py", "ingest_endpoint"): (
            CapabilityId.COMPANY_WIKI,
            CapabilityId.SALES_NOTES,
        ),
    }
)


def drive_disabled_reasons() -> tuple[str, ...]:
    """Return active Dalton switches that forbid enabling upstream Drive.

    Drive remains forced off in Dalton whenever either colleague workspace
    access (``is_dalton_sharing_auth_enabled`` / ``is_colleague_dispatch_enabled``)
    or per-computer CLI tokens (``computer_cli_tokens``) is enabled, until
    cross-user mount unmount-before-handoff, caller-propagated ``converge_soon``,
    and caller-bound CLI token Drive permissions land in a dedicated batch.
    """
    from src.dalton.settings import (
        is_colleague_dispatch_enabled,
        is_computer_cli_tokens_enabled,
        is_dalton_enabled,
        is_dalton_sharing_auth_enabled,
    )

    if not is_dalton_enabled():
        return ()
    reasons: list[str] = []
    if is_dalton_sharing_auth_enabled() or is_colleague_dispatch_enabled():
        reasons.append("colleague_sharing")
    if is_computer_cli_tokens_enabled():
        reasons.append("computer_cli_tokens")
    return tuple(reasons)


def is_drive_blocked_by_dalton() -> bool:
    """Return True when active Dalton switches require Drive to stay off."""
    return bool(drive_disabled_reasons())


def check_drive_startup_guard(log: logging.Logger) -> bool:
    """Log an error at startup if ``features.drive.enabled`` is refused by Dalton.

    Returns ``True`` when Drive was configured enabled and forced off by active
    colleague access or ``computer_cli_tokens``; returns ``False`` otherwise.
    """
    from src.config import features as feature_catalog

    spec = feature_catalog.FEATURES.get("drive")
    if spec is None:
        return False
    override = feature_catalog.get_infrastructure_config().features.get("drive")
    configured_enabled = (
        override.enabled
        if override is not None and override.enabled is not None
        else spec.enabled
    )
    if not configured_enabled:
        return False
    reasons = drive_disabled_reasons()
    if not reasons:
        return False
    log.error(
        "Refusing to enable Drive (features.drive.enabled=true) while Dalton %s "
        "is enabled; forcing Drive off across all routes, mounts, and background "
        "tasks until cross-user unmount-before-handoff, caller-propagated "
        "converge_soon, and caller-bound CLI token Drive permissions are completed.",
        ", ".join(reasons),
    )
    return True
