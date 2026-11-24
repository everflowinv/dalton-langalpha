"""Self-service configuration and permission enforcement for `metadata.report_profile`."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from psycopg.rows import dict_row

from src.dalton.access_tiers import get_user_tier
from src.dalton.bullbear_tickers import (
    bullbear_report_profiles,
    explain_unsupported_bullbear_ticker,
    supported_bullbear_tickers,
)
from src.dalton.database.automation_reports import (
    REPORT_PROFILES,
    automation_has_validated_report_profile,
    parse_report_profile,
)
from src.dalton.database.channel_gateway import (
    _verify_workspace_write_permission,
    bump_version_str,
    default_app_settings,
    get_feishu_open_id_for_user,
)
from src.dalton.llm.model_prefs import (
    DALTON_NOTIFY_OVERRIDES_KEY,
    DALTON_NOTIFY_ROUTES_KEY,
    apply_dalton_notify_preference_patch,
    resolve_dalton_notify_enabled,
    resolve_dalton_notify_overrides,
    resolve_dalton_notify_routes,
    resolve_dalton_notify_targets,
)
from src.dalton.notify.dispatch import (
    evaluate_user_notify_eligibility,
    send_dalton_gateway_notice,
)
from src.dalton.services.channel_vault_credentials import (
    ChannelVaultCredentialError,
    load_dedicated_feishu_vault_credentials,
)
from src.dalton.services.company_automations import (
    AutomationVersionConflictError,
    automation_history_context,
)
from src.server.database import automation as auto_db
from src.server.database.pool import get_db_connection
from src.server.database.user import (
    invalidate_user_prefs_cache,
    invalidate_user_profile_cache,
)

logger = logging.getLogger(__name__)

UNAUTHORIZED_GROUP_MESSAGE = "这个群还没有开通报告接收，请联系管理员开通"
REST_GROUP_TARGET_FORBIDDEN_MESSAGE = "非管理员不能通过 REST 接口设置群发送目标，请在对话里让 Dalton 设置。"
MISSING_REPORT_PROFILE_WARNING = (
    "这个任务目前发不出去：还没有设置发送对象，可以让我用 dalton_report_delivery 设置"
)
REPORT_PROFILE_MISSING_NOTIFIED_KEY = "report_profile_missing_notified_at"
REPORT_PROFILE_MISSING_NOTICE_KEY = "report_profile_missing_notice"

MISSING_NOTICE_MAX_ATTEMPTS = 3
MISSING_NOTICE_STALE_PENDING_S = 300
ADMIN_NOTIFY_COALESCE_WINDOW_S = 600

_FEISHU_TENANT_TOKEN_URL = (
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
)
_FEISHU_CHAT_MEMBERS_URL = (
    "https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members"
)
_FEISHU_HTTP_TIMEOUT_S = 10.0
_FEISHU_MAX_MEMBER_PAGES = 200
_FEISHU_CHECK_CONCURRENCY = 4
_FEISHU_GET_TOTAL_TIMEOUT_S = 15.0
_FEISHU_TOKEN_REFRESH_MARGIN_S = 300

# Process-local cache of Feishu tenant_access_token keyed by (app_id, secret_digest) -> (token, expires_at_mono)
_FEISHU_TENANT_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}

EXACT_SKILL_TO_PROFILE: dict[str, str] = {
    "dalton-ai-finance-daily": "ai-finance-daily",
    "dalton-us-weekly-movers": "us-weekly-movers",
    "dalton-x-accounts-daily": "x-accounts-daily",
    "dalton-humanoid-robot-weekly": "humanoid-robot-weekly",
    "dalton-weekly-trend-impact-scan": "weekly-trend-impact-scan",
    "dalton-ct-semi-sentiment": "ct-semi-sentiment",
    "dalton-china-consumer-weekly": "china-consumer-weekly",
    "dalton-global-consumer-weekly": "global-consumer-weekly",
    "dalton-daily-filing-alert": "daily-filing-alert",
    "dalton-tob-industry-weekly": "tob-industry-weekly",
    "dalton-vip-news-alert": "vip-news-alert",
    "dalton-mark-weekly-newenergy": "mark-weekly-newenergy",
    "dalton-tmeet-weekly-minutes": "tmeet-weekly-minutes",
    "dalton-ai-app-weekly-monitor": "ai-app-weekly",
    "dalton-richard-earnings": "richard-earnings",
    "dalton-richard-coverage-weekly": "richard-coverage-weekly",
    "dalton-wiki-auto-ingest": "wiki-auto-ingest",
    "dalton-podcast-weekly": "podcast-weekly",
}

_MARKET_DIGEST_PROFILES: tuple[str, ...] = (
    "market-digest-am",
    "market-digest-pm",
)


class _MultiProfileSkills(Mapping[str, tuple[str, ...]]):
    """Mapping from multi-profile skill name to its currently supported profiles."""

    def __getitem__(self, key: str) -> tuple[str, ...]:
        if key == "dalton-bullbear-tracker":
            return bullbear_report_profiles()
        if key == "dalton-market-digest":
            return _MARKET_DIGEST_PROFILES
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(("dalton-bullbear-tracker", "dalton-market-digest"))

    def __len__(self) -> int:
        return 2


MULTI_PROFILE_SKILLS: Mapping[str, tuple[str, ...]] = _MultiProfileSkills()

_ALL_REPORT_SKILLS = frozenset(EXACT_SKILL_TO_PROFILE) | frozenset(MULTI_PROFILE_SKILLS)
_SKILL_TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        skill,
        re.compile(rf"(?<![a-z0-9-]){re.escape(skill)}(?![a-z0-9-])", re.IGNORECASE),
    )
    for skill in sorted(_ALL_REPORT_SKILLS)
)
_DIRECT_AUTOMATION_REPORT_RE = re.compile(
    r"(?<![a-z0-9_])automation_report(?![a-z0-9_])",
    re.IGNORECASE,
)
_DISCORD_ID_RE = re.compile(r"^\d+$")
_FEISHU_CHAT_ID_RE = re.compile(r"^oc_[A-Za-z0-9_-]+$")
_FEISHU_USER_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]+$")


class ReportDeliveryError(ValueError):
    """Structured domain/permission error surfaced as a tool rejection or HTTP error."""

    def __init__(self, message: str, *, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def clear_feishu_tenant_token_cache() -> None:
    """Clear the process-local Feishu tenant token cache (mainly for tests)."""
    _FEISHU_TENANT_TOKEN_CACHE.clear()


def missing_profile_notice_id(automation_id: str) -> str:
    """Stable gateway notification identity for missing-profile rejection notices."""
    return f"report-profile-missing:{str(automation_id or '').strip()}"


def extract_report_skills(instruction: str | None) -> list[str]:
    """Return sorted report skill directory names referenced in ``instruction``."""
    text = str(instruction or "")
    if not text:
        return []
    return [skill for skill, pat in _SKILL_TOKEN_PATTERNS if pat.search(text)]


def instruction_uses_report_skill(instruction: str | None) -> bool:
    """True when ``instruction`` references a report skill or ``automation_report``."""
    text = str(instruction or "")
    if not text:
        return False
    return bool(extract_report_skills(text) or _DIRECT_AUTOMATION_REPORT_RE.search(text))


def allowed_profiles_for_instruction(instruction: str | None) -> list[str]:
    """Return valid ``REPORT_PROFILES`` compatible with skills named in ``instruction``."""
    skills = extract_report_skills(instruction)
    if not skills:
        return sorted(REPORT_PROFILES)
    profiles: set[str] = set()
    for skill in skills:
        if skill in EXACT_SKILL_TO_PROFILE:
            profiles.add(EXACT_SKILL_TO_PROFILE[skill])
        elif skill in MULTI_PROFILE_SKILLS:
            profiles.update(MULTI_PROFILE_SKILLS[skill])
    return sorted(profiles)


def _explicit_metadata_ticker(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    raw = str(metadata.get("ticker") or "").strip().upper()
    return raw or None


def _infer_bullbear_profile(
    instruction: str,
    metadata: Mapping[str, Any] | None,
    name: str | None,
) -> str | None:
    tickers = supported_bullbear_tickers()
    ticker_set = set(tickers)
    explicit = _explicit_metadata_ticker(metadata)
    if explicit is not None:
        if explicit in ticker_set:
            return f"{explicit.lower()}-tracker"
        return None
    ticker_candidates: set[str] = set()
    combined = f"{name or ''}\n{instruction}"
    for ticker in tickers:
        flags = 0 if len(ticker) <= 4 else re.IGNORECASE
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", combined, flags):
            ticker_candidates.add(ticker)
    if len(ticker_candidates) == 1:
        return f"{next(iter(ticker_candidates)).lower()}-tracker"
    return None


def _infer_market_digest_profile(instruction: str, name: str | None) -> str | None:
    combined = f"{name or ''}\n{instruction}"
    has_am = bool(
        re.search(r"(?<![A-Za-z])AM(?![A-Za-z])|morning|晨报|早报|盘前", combined, re.IGNORECASE)
    )
    has_pm = bool(
        re.search(r"(?<![A-Za-z])PM(?![A-Za-z])|evening|晚报|收盘|盘后", combined, re.IGNORECASE)
    )
    if has_am and not has_pm:
        return "market-digest-am"
    if has_pm and not has_am:
        return "market-digest-pm"
    return None


def infer_report_profile(
    instruction: str | None,
    *,
    metadata: Mapping[str, Any] | None = None,
    name: str | None = None,
) -> tuple[str | None, list[str]]:
    """Infer an unambiguous ``REPORT_PROFILES`` entry from ``instruction``, else candidates."""
    skills = extract_report_skills(instruction)
    if not skills:
        return None, sorted(REPORT_PROFILES)
    candidates = allowed_profiles_for_instruction(instruction)
    if "dalton-bullbear-tracker" in skills:
        explicit = _explicit_metadata_ticker(metadata)
        if explicit is not None:
            if explicit in set(supported_bullbear_tickers()):
                return f"{explicit.lower()}-tracker", candidates
            return None, candidates
    if len(candidates) == 1:
        return candidates[0], candidates
    if len(skills) == 1:
        skill = skills[0]
        text = str(instruction or "")
        if skill == "dalton-bullbear-tracker":
            return _infer_bullbear_profile(text, metadata, name), candidates
        if skill == "dalton-market-digest":
            return _infer_market_digest_profile(text, name), candidates
    return None, candidates


def resolve_report_profile_id(
    instruction: str | None,
    *,
    requested_profile: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    name: str | None = None,
) -> str:
    """Resolve and validate ``profile`` against ``REPORT_PROFILES`` and the task's skill."""
    allowed_for_skill = allowed_profiles_for_instruction(instruction)
    skills = extract_report_skills(instruction)
    cleaned = str(requested_profile or "").strip()
    if cleaned:
        if cleaned not in REPORT_PROFILES:
            raise ReportDeliveryError(
                f"无效的 profile {cleaned!r}。合法值为: {', '.join(sorted(REPORT_PROFILES))}",
                code="invalid_profile",
            )
        if skills and cleaned not in allowed_for_skill:
            raise ReportDeliveryError(
                f"profile {cleaned!r} 与任务指令中的技能 ({', '.join(skills)}) 不匹配，应为: {', '.join(allowed_for_skill)}",
                code="profile_skill_mismatch",
            )
        return cleaned

    if "dalton-bullbear-tracker" in skills:
        explicit = _explicit_metadata_ticker(metadata)
        if explicit is not None and explicit not in set(supported_bullbear_tickers()):
            raise ReportDeliveryError(
                explain_unsupported_bullbear_ticker(explicit),
                code="unsupported_ticker",
            )

    inferred, candidates = infer_report_profile(instruction, metadata=metadata, name=name)
    if inferred:
        return inferred
    if skills:
        raise ReportDeliveryError(
            f"无法从任务指令唯一推断 profile（匹配到技能 {', '.join(skills)}），请显式传入 profile，可选值: {', '.join(candidates)}",
            code="profile_required",
        )
    raise ReportDeliveryError(
        f"任务指令未引用已知报告技能，请显式传入 profile（必须是 REPORT_PROFILES 之一，如 {', '.join(candidates[:6])} ... 共 {len(candidates)} 个）",
        code="profile_required",
    )


def build_missing_report_profile_warning(
    instruction: str | None,
    metadata: Mapping[str, Any] | None,
) -> str | None:
    """Return creation/update warning when a report automation lacks a valid ``report_profile``."""
    if not instruction_uses_report_skill(instruction):
        return None
    if automation_has_validated_report_profile({"metadata": metadata}):
        return None
    return MISSING_REPORT_PROFILE_WARNING


def _format_dm_target(platform: str, platform_user_id: str) -> str | None:
    plat = str(platform or "").strip().lower()
    uid = str(platform_user_id or "").strip()
    if not uid:
        return None
    if plat in {"feishu", "discord", "weixin"}:
        return f"{plat}:user:{uid}"
    return None


def _binding_to_notify_target(platform: str, chat_address: str) -> str | None:
    plat = str(platform or "").strip().lower()
    addr = str(chat_address or "").strip()
    if plat == "feishu":
        if addr.startswith("feishu:chat:"):
            return addr
        if addr.startswith("feishu:"):
            ident = addr[len("feishu:") :]
            if _FEISHU_CHAT_ID_RE.fullmatch(ident):
                return f"feishu:chat:{ident}"
    elif plat == "discord":
        if addr.startswith("discord:channel:"):
            return addr
        if addr.startswith("discord:"):
            ident = addr[len("discord:") :]
            if _DISCORD_ID_RE.fullmatch(ident):
                return f"discord:channel:{ident}"
    return None


def parse_delivery_target(raw_target: str) -> tuple[str, str, str, str]:
    """Parse a user-supplied target into ``(canonical_target, platform, kind, raw_ident)``.

    ``kind`` is ``"dm"`` or ``"group"``. Raises ``ReportDeliveryError`` on malformed strings.
    """
    cleaned = str(raw_target or "").strip()
    if not cleaned:
        raise ReportDeliveryError("发送目标不能为空", code="invalid_target")

    # Normalize shorthand Feishu chat IDs (`feishu:oc_...` or `oc_...`) before matching.
    if cleaned.startswith("feishu:oc_"):
        cleaned = f"feishu:chat:{cleaned[len('feishu:'):]}"
    elif _FEISHU_CHAT_ID_RE.fullmatch(cleaned):
        cleaned = f"feishu:chat:{cleaned}"

    if cleaned.startswith("feishu:chat:"):
        ident = cleaned[len("feishu:chat:") :]
        if not _FEISHU_CHAT_ID_RE.fullmatch(ident):
            raise ReportDeliveryError(f"非法的飞书群目标格式: {raw_target!r}", code="invalid_target")
        return cleaned, "feishu", "group", ident
    if cleaned.startswith("feishu:user:"):
        ident = cleaned[len("feishu:user:") :]
        if not _FEISHU_USER_ID_RE.fullmatch(ident):
            raise ReportDeliveryError(f"非法的飞书私信目标格式: {raw_target!r}", code="invalid_target")
        return cleaned, "feishu", "dm", ident
    if cleaned.startswith("discord:channel:"):
        ident = cleaned[len("discord:channel:") :]
        if not _DISCORD_ID_RE.fullmatch(ident):
            raise ReportDeliveryError(f"非法的 Discord 频道目标格式: {raw_target!r}", code="invalid_target")
        return cleaned, "discord", "group", ident
    if cleaned.startswith("discord:user:"):
        ident = cleaned[len("discord:user:") :]
        if not _DISCORD_ID_RE.fullmatch(ident):
            raise ReportDeliveryError(f"非法的 Discord 私信目标格式: {raw_target!r}", code="invalid_target")
        return cleaned, "discord", "dm", ident
    if cleaned.startswith("weixin:user:"):
        ident = cleaned[len("weixin:user:") :]
        return cleaned, "weixin", "dm", ident

    # Group-shaped identifiers from unsupported or generic channels must not be
    # treated as authorized groups.
    if ":chat:" in cleaned or ":channel:" in cleaned or cleaned.endswith(":@me"):
        raise ReportDeliveryError(
            f"暂不支持该目标格式 {raw_target!r}；仅支持 feishu:chat:oc_* / feishu:user:ou_* / discord:channel:* / discord:user:*",
            code="invalid_target",
        )
    raise ReportDeliveryError(
        f"非法的发送目标格式 {raw_target!r}；应使用 feishu:chat:oc_* / feishu:user:ou_* / discord:user:* / discord:channel:*",
        code="invalid_target",
    )


async def list_caller_dm_identities(
    user_id: str,
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return all bound DM channel identities for ``user_id`` from ``dalton_channel_identities``."""
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        return []
    async with _use_conn(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT platform, platform_user_id
                FROM dalton_channel_identities
                WHERE user_id = %s
                ORDER BY platform, platform_user_id
                """,
                (cleaned_uid,),
            )
            rows = await cur.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        plat = str(row.get("platform") or "").strip().lower()
        puid = str(row.get("platform_user_id") or "").strip()
        target = _format_dm_target(plat, puid)
        if not target:
            continue
        result.append(
            {
                "platform": plat,
                "platform_user_id": puid,
                "target": target,
                "supports_report_cards": plat == "feishu",
                "supports_text_targets": plat in {"feishu", "discord"},
                "supports_failure_targets": plat == "discord",
            }
        )
    return result


def default_failure_targets_for_identities(
    identities: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Choose the default ``failure_targets`` from bound identities.

    ``parse_report_profile`` accepts only ``discord:user:<digits>`` in ``failure_targets``.
    When no Discord identity is bound, return ``[]``.
    """
    for item in identities:
        if str(item.get("platform") or "").strip().lower() == "discord":
            target = str(item.get("target") or "").strip()
            if target.startswith("discord:user:"):
                return [target]
    return []


async def list_authorized_group_bindings(
    *,
    platform: str | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """List authorized group/channel rows from ``dalton_channel_chat_bindings``."""
    cleaned_plat = str(platform or "").strip().lower() or None
    async with _use_conn(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            if cleaned_plat:
                await cur.execute(
                    """
                    SELECT platform, chat_address, chat_name, workspace_id, bound_by_user_id
                    FROM dalton_channel_chat_bindings
                    WHERE platform = %s
                    ORDER BY platform, chat_address
                    """,
                    (cleaned_plat,),
                )
            else:
                await cur.execute(
                    """
                    SELECT platform, chat_address, chat_name, workspace_id, bound_by_user_id
                    FROM dalton_channel_chat_bindings
                    ORDER BY platform, chat_address
                    """
                )
            rows = await cur.fetchall()

    bindings: list[dict[str, Any]] = []
    for row in rows:
        plat = str(row.get("platform") or "").strip().lower()
        chat_addr = str(row.get("chat_address") or "").strip()
        target = _binding_to_notify_target(plat, chat_addr)
        if not target:
            continue
        bindings.append(
            {
                "platform": plat,
                "chat_address": chat_addr,
                "chat_name": str(row.get("chat_name") or "").strip() or chat_addr,
                "workspace_id": (
                    str(row["workspace_id"]) if row.get("workspace_id") else None
                ),
                "bound_by_user_id": row.get("bound_by_user_id"),
                "target": target,
            }
        )
    return bindings


async def _get_feishu_tenant_token(
    client: Any,
    app_id: str,
    app_secret: str,
    *,
    use_cache: bool = True,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(token, status_code, error_message)`` using an expiry-aware cache."""
    secret_digest = hashlib.sha256(app_secret.encode("utf-8")).hexdigest()[:16]
    cache_key = (app_id, secret_digest)
    now_mono = time.monotonic()
    if use_cache:
        cached = _FEISHU_TENANT_TOKEN_CACHE.get(cache_key)
        if cached is not None and cached[1] > now_mono:
            return cached[0], None, None

    tok_resp = await client.post(
        _FEISHU_TENANT_TOKEN_URL,
        json={"app_id": app_id, "app_secret": app_secret},
    )
    if tok_resp.status_code != 200:
        return None, "token_failed", f"飞书获取访问令牌失败（HTTP {tok_resp.status_code}）"
    tok_data = tok_resp.json()
    if tok_data.get("code") != 0 or not tok_data.get("tenant_access_token"):
        return None, "token_failed", "飞书获取访问令牌被拒，无法核对群成员资格"

    token = str(tok_data["tenant_access_token"])
    if use_cache:
        raw_expire = tok_data.get("expire")
        try:
            expire_s = int(raw_expire) if raw_expire is not None else 7200
        except (TypeError, ValueError):
            expire_s = 7200
        # Keep a 5-minute safety margin (or half the TTL when shorter than 600s).
        margin = _FEISHU_TOKEN_REFRESH_MARGIN_S if expire_s > 600 else max(0, expire_s // 2)
        ttl_s = max(0, expire_s - margin)
        if ttl_s > 0:
            _FEISHU_TENANT_TOKEN_CACHE[cache_key] = (token, time.monotonic() + ttl_s)
    return token, None, None


async def _verify_feishu_chat_member_detailed(
    chat_id: str,
    open_id: str,
    *,
    get_credentials_fn: Callable[[], Awaitable[dict[str, str]]] | None = None,
    http_client_factory: Callable[..., Any] | None = None,
    use_token_cache: bool | None = None,
) -> tuple[bool, str, str | None]:
    """Check Feishu chat membership and return ``(is_member, status_code, message)``.

    ``status_code`` distinguishes confirmed ``"not_member"`` from transient
    ``"check_unavailable"`` / ``"credentials_unavailable"`` failures.
    """
    cleaned_chat = str(chat_id or "").strip()
    cleaned_open_id = str(open_id or "").strip()
    if not _FEISHU_CHAT_ID_RE.fullmatch(cleaned_chat):
        return False, "invalid_chat_id", "飞书群 ID 非法"
    if not _FEISHU_USER_ID_RE.fullmatch(cleaned_open_id):
        return False, "missing_bound_feishu_open_id", "未绑定飞书账号（open_id 缺失），无法核对群成员资格"

    creds_fn = get_credentials_fn or load_dedicated_feishu_vault_credentials
    try:
        creds = await creds_fn()
    except ChannelVaultCredentialError as exc:
        logger.warning("[REPORT_DELIVERY] Feishu Vault credentials unavailable: %s", exc.code)
        return False, "check_unavailable", "飞书应用凭据暂不可用，无法核对群成员资格"
    except Exception:
        logger.warning("[REPORT_DELIVERY] Feishu credentials lookup failed", exc_info=True)
        return False, "check_unavailable", "飞书应用凭据暂不可用，无法核对群成员资格"

    app_id = str((creds or {}).get("DALTON_FEISHU_APP_ID") or "").strip()
    app_secret = str((creds or {}).get("DALTON_FEISHU_APP_SECRET") or "").strip()
    if not app_id or not app_secret:
        return False, "check_unavailable", "飞书应用凭据未配置，无法核对群成员资格"

    eff_cache = (
        use_token_cache
        if use_token_cache is not None
        else (get_credentials_fn is None and http_client_factory is None)
    )
    client_factory = http_client_factory or httpx.AsyncClient
    try:
        async with client_factory(timeout=_FEISHU_HTTP_TIMEOUT_S) as client:
            tenant_token, tok_code, tok_err = await _get_feishu_tenant_token(
                client,
                app_id,
                app_secret,
                use_cache=eff_cache,
            )
            if not tenant_token:
                return False, tok_code or "check_unavailable", tok_err

            headers = {"Authorization": f"Bearer {tenant_token}"}
            page_token = ""
            seen_tokens: set[str] = set()
            for _ in range(_FEISHU_MAX_MEMBER_PAGES):
                params: dict[str, Any] = {
                    "member_id_type": "open_id",
                    "page_size": 100,
                }
                if page_token:
                    params["page_token"] = page_token
                mem_resp = await client.get(
                    _FEISHU_CHAT_MEMBERS_URL.format(chat_id=cleaned_chat),
                    headers=headers,
                    params=params,
                )
                if mem_resp.status_code != 200:
                    return False, "check_unavailable", f"飞书查询群成员失败（HTTP {mem_resp.status_code}）"
                mem_data = mem_resp.json()
                if mem_data.get("code") != 0:
                    return False, "check_unavailable", f"飞书查询群成员返回错误（code={mem_data.get('code')}）"
                data_obj = mem_data.get("data") or {}
                for item in data_obj.get("items") or []:
                    if isinstance(item, Mapping) and str(item.get("member_id") or "").strip() == cleaned_open_id:
                        return True, "member", None
                has_more = bool(data_obj.get("has_more"))
                next_token = str(data_obj.get("page_token") or "").strip()
                if not has_more or not next_token or next_token in seen_tokens:
                    break
                seen_tokens.add(next_token)
                page_token = next_token
            return False, "not_member", "你不是该飞书群成员，无法将其设为报告发送对象"
    except httpx.HTTPError:
        logger.warning("[REPORT_DELIVERY] Feishu chat member check HTTP error", exc_info=True)
        return False, "check_unavailable", "连接飞书开放平台失败，无法核对群成员资格"
    except Exception:
        logger.warning("[REPORT_DELIVERY] Feishu chat member check unexpected error", exc_info=True)
        return False, "check_unavailable", "核对飞书群成员资格失败"


async def verify_feishu_chat_member(
    chat_id: str,
    open_id: str,
    *,
    get_credentials_fn: Callable[[], Awaitable[dict[str, str]]] | None = None,
    http_client_factory: Callable[..., Any] | None = None,
    use_token_cache: bool | None = None,
) -> tuple[bool, str | None]:
    """Check via Feishu OpenAPI whether ``open_id`` belongs to ``chat_id``.

    Fails closed on missing credentials, network/HTTP error, or non-zero Feishu code.
    """
    ok, _status_code, msg = await _verify_feishu_chat_member_detailed(
        chat_id,
        open_id,
        get_credentials_fn=get_credentials_fn,
        http_client_factory=http_client_factory,
        use_token_cache=use_token_cache,
    )
    return ok, msg


_DEFAULT_VERIFY_FEISHU_CHAT_MEMBER = verify_feishu_chat_member


class _ConnContext:
    def __init__(self, conn: Any) -> None:
        self._conn = conn
        self._cm: Any = None

    async def __aenter__(self) -> Any:
        if self._conn is not None:
            return self._conn
        self._cm = get_db_connection()
        return await self._cm.__aenter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        if self._cm is not None:
            return await self._cm.__aexit__(exc_type, exc, tb)
        return False


def _use_conn(conn: Any = None) -> _ConnContext:
    return _ConnContext(conn)


def _is_valid_uuid(raw: Any) -> bool:
    try:
        UUID(str(raw or "").strip())
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _conn_has_cursor(conn: Any) -> bool:
    return conn is not None and callable(getattr(conn, "cursor", None))


async def sync_owner_group_chat_settings(
    owner_user_id: str,
    bindings: Sequence[Mapping[str, Any]],
    *,
    conn: Any = None,
) -> None:
    """Ensure pre-authorized group targets are enabled in owner's ``dalton_channel_user_settings``.

    Never writes to ``dalton_channel_chat_bindings`` (group authorization remains admin-only).
    """
    cleaned_uid = str(owner_user_id or "").strip()
    if not cleaned_uid or not bindings:
        return

    async with _use_conn(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_user_settings:{cleaned_uid}",),
                )
                await cur.execute(
                    """
                    SELECT version, apps FROM dalton_channel_user_settings
                    WHERE user_id = %s FOR UPDATE
                    """,
                    (cleaned_uid,),
                )
                row = await cur.fetchone()
                if row:
                    current_version = str(row.get("version") or "v1")
                    apps = json.loads(json.dumps(row.get("apps") or {}))
                else:
                    current_version = "v0"
                    apps = {}

                changed = False
                for binding in bindings:
                    plat = str(binding.get("platform") or "").strip().lower()
                    chat_addr = str(binding.get("chat_address") or "").strip()
                    chat_name = str(binding.get("chat_name") or "").strip() or chat_addr
                    ws_id = binding.get("workspace_id")
                    if not plat or not chat_addr:
                        continue
                    plat_cfg = apps.get(plat)
                    if not isinstance(plat_cfg, dict):
                        plat_cfg = default_app_settings(plat)
                        apps[plat] = plat_cfg
                        changed = True
                    chats = plat_cfg.get("chats")
                    if not isinstance(chats, dict):
                        chats = {}
                        plat_cfg["chats"] = chats
                        changed = True
                    if chat_addr not in chats:
                        norm_ws = str(ws_id).strip() if ws_id is not None else ""
                        chats[chat_addr] = {
                            "name": chat_name,
                            "workspace_id": norm_ws or None,
                        }
                        changed = True

                if not changed:
                    return

                new_version = bump_version_str(current_version)
                if row:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_user_settings
                        SET version = %s, apps = %s::jsonb, updated_at = NOW()
                        WHERE user_id = %s
                        """,
                        (new_version, json.dumps(apps), cleaned_uid),
                    )
                else:
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_user_settings (user_id, version, apps)
                        VALUES (%s, %s, %s::jsonb)
                        """,
                        (cleaned_uid, new_version, json.dumps(apps)),
                    )


def _group_targets_from_profile(profile: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(profile, Mapping):
        return []
    groups: list[str] = []
    seen: set[str] = set()
    for field_name in ("report_targets", "text_targets"):
        vals = profile.get(field_name)
        if not isinstance(vals, Sequence) or isinstance(vals, (str, bytes)):
            continue
        for item in vals:
            s = str(item or "").strip()
            if (s.startswith("feishu:chat:") or s.startswith("discord:channel:")) and s not in seen:
                seen.add(s)
                groups.append(s)
    return groups


async def _resolve_admin_target_map(
    pm_ids: Sequence[str],
    *,
    resolve_targets_fn: Callable[[str], Awaitable[list[str]]],
    list_dm_identities_fn: Callable[[str], Awaitable[list[dict[str, Any]]]],
) -> dict[str, list[str]]:
    """Map each admin user_id to their deduplicated delivery targets."""
    per_admin: dict[str, list[str]] = {}
    for admin_id in pm_ids:
        cleaned_aid = str(admin_id or "").strip()
        if not cleaned_aid:
            continue
        try:
            targets = list(await resolve_targets_fn(cleaned_aid) or [])
        except Exception:
            targets = []
        if not targets:
            try:
                identities = await list_dm_identities_fn(cleaned_aid)
                targets = [
                    str(item["target"])
                    for item in identities
                    if item.get("platform") in {"feishu", "discord"} and item.get("target")
                ]
            except Exception:
                targets = []
        deduped: list[str] = []
        seen: set[str] = set()
        for t in targets:
            cleaned_t = str(t or "").strip()
            if cleaned_t and cleaned_t not in seen:
                seen.add(cleaned_t)
                deduped.append(cleaned_t)
        if deduped:
            per_admin[cleaned_aid] = deduped
    return per_admin


def _format_admin_change_message(
    *,
    automation_id: str,
    automation_name: str,
    owner_user_id: str,
    actor_user_id: str,
    action: str,
    previous_groups: Sequence[str],
    new_groups: Sequence[str],
) -> tuple[str, str]:
    """Build ``(title, text)`` for a single group-target change."""
    before_groups = list(previous_groups or [])
    after_groups = list(new_groups or [])
    before_str = ", ".join(before_groups) if before_groups else "无"
    after_str = ", ".join(after_groups) if after_groups else "已清空"
    title = "定时报告群发送配置变更"
    lines = [
        f"[Report Delivery] 成员 {actor_user_id} {action} 了定时任务「{automation_name}」({automation_id}) 的群发送配置",
        f"属主: {owner_user_id}",
        f"原群目标: {before_str}",
        f"新群目标: {after_str}",
    ]
    return title, "\n".join(lines)


async def _dispatch_admin_gateway_notice(
    *,
    per_admin_targets: Mapping[str, Sequence[str]],
    title: str,
    text: str,
    automation_id: str,
    gateway_send_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    eff_send = gateway_send_fn or send_dalton_gateway_notice
    sent_targets: list[str] = []
    failed_targets: list[str] = []
    errors: dict[str, str] = {}
    per_target_results: list[dict[str, Any]] = []

    for admin_id, targets in per_admin_targets.items():
        if not targets:
            continue
        for single_target in targets:
            try:
                res = await eff_send(
                    source="report_delivery_admin_notice",
                    user_id=admin_id,
                    targets=[single_target],
                    title=title,
                    text=text,
                    automation_id=automation_id,
                    template="orange",
                )
            except Exception as exc:
                err_code = type(exc).__name__
                logger.warning(
                    "[REPORT_DELIVERY] Admin gateway notice raised for admin=%s target=%s: %s",
                    admin_id,
                    single_target,
                    err_code,
                    exc_info=True,
                )
                failed_targets.append(single_target)
                errors[single_target] = err_code
                per_target_results.append(
                    {
                        "admin_user_id": admin_id,
                        "target": single_target,
                        "ok": False,
                        "status": "failed",
                        "error_code": err_code,
                    }
                )
                continue

            res_items = res.get("results") if isinstance(res, Mapping) else None
            first_item = (
                res_items[0]
                if isinstance(res_items, list) and res_items and isinstance(res_items[0], Mapping)
                else None
            )
            ok = bool(res.get("ok") is True and (res.get("sent_targets") or []))
            if ok:
                sent_targets.append(single_target)
                per_target_results.append(
                    {
                        "admin_user_id": admin_id,
                        "target": single_target,
                        "ok": True,
                        "status": str((first_item or {}).get("status") or "sent"),
                        "error_code": None,
                    }
                )
            else:
                err_map = res.get("errors") if isinstance(res, Mapping) else None
                err_code = str(
                    (first_item or {}).get("error_code")
                    or (err_map.get(single_target) if isinstance(err_map, Mapping) else None)
                    or (next(iter(err_map.values())) if isinstance(err_map, Mapping) and err_map else None)
                    or "send_failed"
                )
                failed_targets.append(single_target)
                errors[single_target] = err_code
                per_target_results.append(
                    {
                        "admin_user_id": admin_id,
                        "target": single_target,
                        "ok": False,
                        "status": str((first_item or {}).get("status") or "failed"),
                        "error_code": err_code,
                    }
                )

    all_targets = [t for ts in per_admin_targets.values() for t in ts]
    if sent_targets:
        return {
            "notified": True,
            "partial_failure": bool(failed_targets),
            "targets": sent_targets,
            "failed_targets": failed_targets,
            "errors": errors,
            "results": per_target_results,
        }
    return {
        "notified": False,
        "reason": next(iter(errors.values()), "send_failed"),
        "targets": all_targets,
        "failed_targets": failed_targets or all_targets,
        "errors": errors,
        "results": per_target_results,
    }


def _parse_iso_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            dt = datetime.fromisoformat(raw.strip())
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


async def claim_admin_group_notice_slot(
    conn: Any,
    automation_id: str,
    *,
    window_s: int = ADMIN_NOTIFY_COALESCE_WINDOW_S,
) -> bool:
    """Inside an active config transaction, claim the per-automation 10-minute admin notify slot.

    Updates ``last_notified_at = NOW()`` inside the same transaction so rollbacks
    never advance the throttle timestamp and concurrent edits cannot double-send.
    """
    cleaned_aid = str(automation_id or "").strip()
    if not cleaned_aid:
        return False
    if conn is not None and not _conn_has_cursor(conn):
        return True

    async def _run_in_tx(db_conn: Any) -> bool:
        now = datetime.now(timezone.utc)
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_report_delivery_admin_notices (automation_id)
                VALUES (%s)
                ON CONFLICT (automation_id) DO NOTHING
                """,
                (cleaned_aid,),
            )
            await cur.execute(
                """
                SELECT last_notified_at
                FROM dalton_report_delivery_admin_notices
                WHERE automation_id = %s
                FOR UPDATE
                """,
                (cleaned_aid,),
            )
            row = await cur.fetchone() or {}
            last_dt = _parse_iso_dt(row.get("last_notified_at"))
            if (
                window_s > 0
                and last_dt is not None
                and (now - last_dt) < timedelta(seconds=window_s)
            ):
                return False
            await cur.execute(
                """
                UPDATE dalton_report_delivery_admin_notices
                SET last_notified_at = NOW(), updated_at = NOW()
                WHERE automation_id = %s
                """,
                (cleaned_aid,),
            )
            return True

    if conn is not None:
        return await _run_in_tx(conn)

    try:
        async with _use_conn(None) as db_conn, db_conn.transaction():
            return await _run_in_tx(db_conn)
    except RuntimeError as exc:
        if "pool is not open" in str(exc):
            return True
        raise


async def notify_admins_group_target_change(
    *,
    actor_user_id: str,
    owner_user_id: str,
    automation_id: str,
    automation_name: str,
    action: str,
    previous_groups: Sequence[str],
    new_groups: Sequence[str],
    coalesce_window_s: int = ADMIN_NOTIFY_COALESCE_WINDOW_S,
    skip_throttle_check: bool = False,
    conn: Any = None,
    get_pm_ids_fn: Callable[[], Sequence[str]] | None = None,
    resolve_targets_fn: Callable[[str], Awaitable[list[str]]] | None = None,
    list_dm_identities_fn: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    gateway_send_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Notify each configured Dalton admin target once via Dalton channel gateway (10m per-task throttle)."""
    if not skip_throttle_check:
        try:
            async with _use_conn(conn) as db_conn:
                if _conn_has_cursor(db_conn):
                    async with db_conn.transaction():
                        claimed = await claim_admin_group_notice_slot(
                            db_conn, automation_id, window_s=coalesce_window_s
                        )
                else:
                    claimed = True
        except RuntimeError as exc:
            if "pool is not open" not in str(exc):
                raise
            claimed = True
        if not claimed:
            return {
                "notified": False,
                "throttled": True,
                "reason": "rate_limited_10m",
                "targets": [],
                "results": [],
            }

    if get_pm_ids_fn is not None:
        pm_ids = list(get_pm_ids_fn())
    else:
        from src.dalton.access_tiers import list_admin_user_ids

        pm_ids = await list_admin_user_ids()
    if not pm_ids:
        return {"notified": False, "reason": "no_admins_configured", "targets": [], "results": []}

    eff_resolve = resolve_targets_fn or resolve_dalton_notify_targets
    eff_identities = list_dm_identities_fn or list_caller_dm_identities
    per_admin_targets = await _resolve_admin_target_map(
        pm_ids,
        resolve_targets_fn=eff_resolve,
        list_dm_identities_fn=eff_identities,
    )
    all_admin_targets = [t for ts in per_admin_targets.values() for t in ts]
    if not all_admin_targets:
        return {"notified": False, "reason": "no_admin_targets", "targets": [], "results": []}

    title, text = _format_admin_change_message(
        automation_id=automation_id,
        automation_name=automation_name,
        owner_user_id=owner_user_id,
        actor_user_id=actor_user_id,
        action=action,
        previous_groups=previous_groups,
        new_groups=new_groups,
    )
    return await _dispatch_admin_gateway_notice(
        per_admin_targets=per_admin_targets,
        title=title,
        text=text,
        automation_id=automation_id,
        gateway_send_fn=gateway_send_fn,
    )


def evaluate_missing_notice_claim(
    metadata: Any,
    *,
    now: datetime | None = None,
    max_attempts: int = MISSING_NOTICE_MAX_ATTEMPTS,
    stale_pending_s: int = MISSING_NOTICE_STALE_PENDING_S,
) -> tuple[bool, int]:
    """Return ``(should_claim, next_attempt_number)`` for missing-profile DM notice."""
    if not isinstance(metadata, Mapping):
        return True, 1
    if metadata.get(REPORT_PROFILE_MISSING_NOTIFIED_KEY) and REPORT_PROFILE_MISSING_NOTICE_KEY not in metadata:
        return False, 0

    notice = metadata.get(REPORT_PROFILE_MISSING_NOTICE_KEY)
    if not isinstance(notice, Mapping):
        return True, 1

    status = str(notice.get("status") or "").strip().lower()
    try:
        attempts = int(notice.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0

    if status == "sent":
        return False, attempts
    if attempts >= max_attempts:
        return False, attempts
    if status == "pending":
        last_dt = _parse_iso_dt(notice.get("last_attempt_at"))
        eff_now = now or datetime.now(timezone.utc)
        if last_dt is not None and (eff_now - last_dt) < timedelta(seconds=stale_pending_s):
            return False, attempts
    return True, attempts + 1


async def claim_missing_report_profile_notice(
    cur: Any,
    row: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> int | None:
    """Under an already-held ``automations`` ``FOR UPDATE`` lock, claim a missing-profile notice attempt.

    Returns the claimed ``attempt_number`` (1..3) or ``None`` if already sent / in-flight / exhausted.
    """
    eff_now = now or datetime.now(timezone.utc)
    should_claim, next_attempts = evaluate_missing_notice_claim(
        row.get("metadata"),
        now=eff_now,
    )
    if not should_claim:
        return None
    claimed_state = {
        "status": "pending",
        "attempts": next_attempts,
        "last_attempt_at": eff_now.isoformat(),
        "last_error": None,
    }
    await cur.execute(
        """
        UPDATE automations
        SET metadata = COALESCE(metadata, '{}'::jsonb)
            || jsonb_build_object('report_profile_missing_notice', %s::jsonb)
        WHERE automation_id = %s::uuid
        """,
        (json.dumps(claimed_state, ensure_ascii=False), row["automation_id"]),
    )
    return next_attempts


async def finalize_missing_report_profile_notice(
    *,
    automation_id: str,
    attempts: int,
    sent_ok: bool,
    error: str | None = None,
    conn: Any = None,
) -> None:
    """Persist ``sent`` (on gateway confirmation) or ``failed`` (eligible for retry up to 3 attempts)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    if sent_ok:
        next_state: dict[str, Any] = {
            "status": "sent",
            "attempts": attempts,
            "last_attempt_at": now_iso,
            "sent_at": now_iso,
            "last_error": None,
        }
    else:
        err_str = str(error or "send_failed")[:240]
        # Missing DM targets cannot deliver a notice, so do not burn retry budget.
        eff_attempts = max(0, int(attempts) - 1) if err_str == "no_dm_targets" else int(attempts)
        next_state = {
            "status": "failed",
            "attempts": eff_attempts,
            "last_attempt_at": now_iso,
            "last_error": err_str,
        }
    async with _use_conn(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE automations
                    SET metadata = COALESCE(metadata, '{}'::jsonb)
                        || jsonb_build_object('report_profile_missing_notice', %s::jsonb)
                    WHERE automation_id = %s::uuid
                      AND COALESCE(metadata -> 'report_profile_missing_notice' ->> 'status', '') = 'pending'
                      AND COALESCE((metadata -> 'report_profile_missing_notice' ->> 'attempts')::int, 0) = %s
                    """,
                    (
                        json.dumps(next_state, ensure_ascii=False),
                        automation_id,
                        int(attempts),
                    ),
                )


async def check_existing_missing_profile_gateway_notice(
    automation_id: str,
    *,
    conn: Any = None,
) -> str | None:
    """Check gateway delivery records for ``report-profile-missing:<automation_id>``.

    Returns ``"sent"`` if any target already succeeded, ``"in_flight"`` if any
    attempt is in-flight/uncertain on the gateway, or ``None`` otherwise.
    """
    notice_id = missing_profile_notice_id(automation_id)
    if conn is not None and not _conn_has_cursor(conn):
        return None
    try:
        async with _use_conn(conn) as db_conn:
            if not _conn_has_cursor(db_conn):
                return None
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT d.status, d.error_code, c.status AS channel_status
                    FROM dalton_notify_deliveries d
                    LEFT JOIN dalton_channel_deliveries c
                      ON c.delivery_key = 'automation_notify:' || d.delivery_id::text || ':item:0'
                    WHERE d.execution_id = %s
                    """,
                    (notice_id,),
                )
                rows = await cur.fetchall()
    except RuntimeError as exc:
        if "pool is not open" in str(exc):
            return None
        raise
    except Exception:
        return None

    in_flight = False
    for r in rows or []:
        if not isinstance(r, Mapping):
            continue
        d_status = str(r.get("status") or "").strip().lower()
        c_status = str(r.get("channel_status") or "").strip().lower()
        err_code = str(r.get("error_code") or "").strip().lower()
        if d_status == "sent" or c_status in ("sent", "partial"):
            return "sent"
        if c_status in ("in_flight", "unknown") or (
            d_status == "pending" and err_code in {"delivery_uncertain", "unknown", "timeout"}
        ):
            in_flight = True
    return "in_flight" if in_flight else None


async def reset_missing_profile_notice_deliveries(
    conn: Any,
    automation_id: str,
) -> None:
    """Archive prior ``report-profile-missing:<automation_id>`` delivery keys on ``clear``."""
    cleaned_aid = str(automation_id or "").strip()
    if not cleaned_aid or not _conn_has_cursor(conn):
        return
    notice_id = missing_profile_notice_id(cleaned_aid)
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_notify_deliveries
                SET execution_id = execution_id || ':cleared:' || delivery_id::text,
                    updated_at = NOW()
                WHERE execution_id = %s
                """,
                (notice_id,),
            )


async def notify_owner_missing_report_profile_once(
    *,
    owner_user_id: str,
    automation_id: str,
    automation_name: str | None = None,
    execution_id: str | None = None,
    conn: Any = None,
    resolve_targets_fn: Callable[[str], Awaitable[list[str]]] | None = None,
    list_dm_identities_fn: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    gateway_send_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> tuple[bool, str | None]:
    """Send a DM to ``owner_user_id`` via Dalton channel gateway on missing-profile rejection.

    Uses stable identity ``report-profile-missing:<automation_id>`` and checks
    existing gateway records before sending so uncertain or crashed attempts
    cannot duplicate. Only returns ``(True, None)`` when gateway delivery is
    confirmed.
    """
    cleaned_uid = str(owner_user_id or "").strip()
    if not cleaned_uid:
        return False, "missing_owner_user_id"

    existing_status = await check_existing_missing_profile_gateway_notice(
        automation_id, conn=conn
    )
    if existing_status == "sent":
        return True, None
    if existing_status == "in_flight":
        return False, "in_flight_or_uncertain"

    eff_resolve = resolve_targets_fn or resolve_dalton_notify_targets
    eff_identities = list_dm_identities_fn or list_caller_dm_identities
    dm_targets: list[str] = []
    seen: set[str] = set()

    def _collect(items: Sequence[str]) -> None:
        for item in items:
            s = str(item or "").strip()
            if (s.startswith("feishu:user:") or s.startswith("discord:user:")) and s not in seen:
                seen.add(s)
                dm_targets.append(s)

    try:
        _collect(list(await eff_resolve(cleaned_uid) or []))
    except Exception:
        pass
    if not dm_targets:
        try:
            identities = await eff_identities(cleaned_uid)
            _collect(
                [
                    str(item["target"])
                    for item in identities
                    if item.get("platform") in {"feishu", "discord"} and item.get("target")
                ]
            )
        except Exception:
            pass

    if not dm_targets:
        logger.info(
            "[REPORT_DELIVERY] Missing-profile DM has no DM targets for user_id=%s automation_id=%s",
            cleaned_uid,
            automation_id,
        )
        return False, "no_dm_targets"

    task_label = str(automation_name or "").strip() or automation_id
    lines = [
        f"定时任务「{task_label}」({automation_id}) 本次执行被拒：尚未配置有效的发送对象（report_profile）。",
        "你可以直接对 Dalton 说“帮我配置这个任务的报告发送对象”，由 Dalton 通过 dalton_report_delivery 设置（最多提醒 3 次）。",
    ]
    if execution_id:
        lines.append(f"execution_id: {execution_id}")

    eff_send = gateway_send_fn or send_dalton_gateway_notice
    notice_id = missing_profile_notice_id(automation_id)
    try:
        send_kwargs: dict[str, Any] = {
            "source": "report_delivery_missing_profile_notice",
            "user_id": cleaned_uid,
            "targets": dm_targets,
            "title": "定时报告缺少发送配置",
            "text": "\n".join(lines),
            "automation_id": automation_id,
            "execution_id": execution_id,
            "notice_id": notice_id,
            "stale_pending_minutes": 5,
            "template": "orange",
        }
        res = await eff_send(**send_kwargs)
    except Exception as exc:
        err = type(exc).__name__
        logger.warning(
            "[REPORT_DELIVERY] Failed sending missing-profile DM to user_id=%s automation_id=%s: %s",
            cleaned_uid,
            automation_id,
            err,
            exc_info=True,
        )
        return False, err

    if isinstance(res, Mapping) and res.get("ok") is True and res.get("sent_targets"):
        return True, None
    err_reason = "send_failed"
    if isinstance(res, Mapping):
        errs = res.get("errors")
        if isinstance(errs, Mapping) and errs:
            err_reason = str(next(iter(errs.values())))
        elif res.get("error_code"):
            err_reason = str(res["error_code"])
    return False, err_reason


async def handle_missing_report_profile_rejection(
    *,
    owner_user_id: str,
    automation_id: str,
    automation_name: str | None = None,
    execution_id: str | None = None,
    conn: Any = None,
    resolve_targets_fn: Callable[[str], Awaitable[list[str]]] | None = None,
    list_dm_identities_fn: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None,
    gateway_send_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> bool:
    """Claim under row lock, send via Dalton gateway, and persist sent/failed status."""
    cleaned_aid = str(automation_id or "").strip()
    if not cleaned_aid:
        return False

    claimed_attempt: int | None = None
    eff_owner = str(owner_user_id or "").strip()
    eff_name = automation_name

    async with _use_conn(conn) as db_conn:
        async with db_conn.transaction():
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT automation_id, user_id, name, metadata
                    FROM automations
                    WHERE automation_id = %s::uuid
                    FOR UPDATE
                    """,
                    (cleaned_aid,),
                )
                row = await cur.fetchone()
                if not row:
                    return False
                parsed, err = parse_report_profile(row.get("metadata"))
                if parsed is not None and err is None:
                    return False
                eff_owner = str(row.get("user_id") or eff_owner).strip()
                eff_name = row.get("name") or eff_name
                claimed_attempt = await claim_missing_report_profile_notice(cur, row)

    if claimed_attempt is None:
        return False

    sent_ok = False
    err_reason: str | None = None
    try:
        sent_ok, err_reason = await notify_owner_missing_report_profile_once(
            owner_user_id=eff_owner,
            automation_id=cleaned_aid,
            automation_name=eff_name,
            execution_id=execution_id,
            resolve_targets_fn=resolve_targets_fn,
            list_dm_identities_fn=list_dm_identities_fn,
            gateway_send_fn=gateway_send_fn,
        )
    except Exception as exc:
        sent_ok = False
        err_reason = type(exc).__name__

    await finalize_missing_report_profile_notice(
        automation_id=cleaned_aid,
        attempts=claimed_attempt,
        sent_ok=sent_ok,
        error=err_reason,
        conn=conn,
    )
    return sent_ok


async def _load_automation_for_actor(
    automation_id: str,
    caller_user_id: str,
    caller_tier: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_aid = str(automation_id or "").strip()
    if not cleaned_aid:
        raise ReportDeliveryError("automation_id 不能为空", code="invalid_request")

    row = await auto_db.get_automation(cleaned_aid, caller_user_id, conn=conn)
    if row is not None:
        return dict(row)

    if caller_tier != "admin":
        raise ReportDeliveryError(
            "未找到该定时任务，或你不是该任务的属主（仅任务属主本人或管理员可修改）",
            code="forbidden_or_not_found",
        )

    # Admin fallback: look up automation regardless of owner_user_id.
    async with _use_conn(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM automations WHERE automation_id = %s::uuid AND deleted_at IS NULL",
                (cleaned_aid,),
            )
            found = await cur.fetchone()
    if not found:
        raise ReportDeliveryError(f"未找到定时任务 {cleaned_aid}", code="not_found")
    return dict(found)


async def _require_internal_or_admin(
    caller_user_id: str,
    *,
    get_tier_fn: Callable[[str], Awaitable[str]] | None = None,
) -> str:
    tier = await (get_tier_fn or get_user_tier)(caller_user_id)
    if tier not in {"internal", "admin"}:
        raise ReportDeliveryError(
            "只有内部同事或管理员可以配置定时报告发送对象。",
            code="forbidden",
        )
    return str(tier)


@dataclass
class ValidatedReportProfileChange:
    """Validated ``report_profile`` / route change plan."""

    actor_user_id: str
    caller_tier: str
    owner_user_id: str
    automation_id: str
    automation_name: str
    action: str
    before_metadata: dict[str, Any]
    after_metadata: dict[str, Any]
    profile: dict[str, Any] | None
    previous_groups: list[str]
    new_groups: list[str]
    authorized_group_bindings: list[dict[str, Any]] = field(default_factory=list)
    route_targets: list[str] = field(default_factory=list)
    metadata_unchanged: bool = False
    audit_version: int | None = None
    admin_notify_claimed: bool | None = None
    _finalized: bool = field(default=False, init=False, repr=False)

    @property
    def requires_admin_group_notice(self) -> bool:
        return bool(
            (self.new_groups or self.previous_groups)
            and set(self.new_groups) != set(self.previous_groups)
        )

    def build_audit_entry(
        self,
        *,
        recorded_at: str | None = None,
        throttled: bool | None = None,
    ) -> dict[str, Any]:
        """Build the ``report_delivery_audit`` payload stored inside ``automations.audit_history``."""
        ts = recorded_at or datetime.now(timezone.utc).isoformat()
        eff_throttled = (
            bool(throttled)
            if throttled is not None
            else (self.admin_notify_claimed is False and self.requires_admin_group_notice)
        )
        entry: dict[str, Any] = {
            "action": self.action,
            "actor_user_id": self.actor_user_id,
            "owner_user_id": self.owner_user_id,
            "recorded_at": ts,
            "metadata_before": copy.deepcopy(dict(self.before_metadata or {})),
            "metadata_unchanged": bool(self.metadata_unchanged),
            "route_targets": list(self.route_targets),
            "previous_groups": list(self.previous_groups),
            "new_groups": list(self.new_groups),
            "admin_notify": {
                "required": self.requires_admin_group_notice,
                "status": (
                    "not_required"
                    if not self.requires_admin_group_notice
                    else ("throttled" if eff_throttled else "not_sent")
                ),
                "throttled": eff_throttled,
                "targets": [],
            },
        }
        if self.metadata_unchanged:
            entry["note"] = "路由变更、metadata 未变"
        else:
            entry["metadata_after"] = copy.deepcopy(dict(self.after_metadata or {}))
        return entry

    async def finalize(
        self,
        *,
        automation: Mapping[str, Any] | None = None,
        notify_admins_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        """Post-commit admin group notification and audit result update."""
        if self._finalized:
            return {"admin_notify_result": None}
        self._finalized = True

        eff_aid = (
            str(automation.get("automation_id") or "").strip()
            if isinstance(automation, Mapping)
            else ""
        ) or self.automation_id
        eff_name = (
            str(automation.get("name") or "").strip()
            if isinstance(automation, Mapping)
            else ""
        ) or self.automation_name or eff_aid
        eff_audit_version = self.audit_version
        if eff_audit_version is None and isinstance(automation, Mapping):
            raw_cv = automation.get("config_version")
            if isinstance(raw_cv, int) and not isinstance(raw_cv, bool):
                eff_audit_version = raw_cv

        admin_notify_result: dict[str, Any] | None = None
        if self.requires_admin_group_notice:
            if self.admin_notify_claimed is False:
                admin_notify_result = {
                    "notified": False,
                    "throttled": True,
                    "status": "throttled",
                    "reason": "rate_limited_10m",
                    "targets": [],
                    "results": [],
                }
            else:
                eff_notify_admins = notify_admins_fn or notify_admins_group_target_change
                kwargs: dict[str, Any] = {
                    "actor_user_id": self.actor_user_id,
                    "owner_user_id": self.owner_user_id,
                    "automation_id": eff_aid,
                    "automation_name": eff_name,
                    "action": self.action,
                    "previous_groups": self.previous_groups,
                    "new_groups": self.new_groups,
                }
                if self.admin_notify_claimed is True and notify_admins_fn is None:
                    kwargs["skip_throttle_check"] = True
                if conn is not None and notify_admins_fn is None:
                    kwargs["conn"] = conn
                try:
                    admin_notify_result = await eff_notify_admins(**kwargs)
                except Exception as exc:
                    err_code = f"finalize_error:{type(exc).__name__}"
                    logger.warning(
                        "[REPORT_DELIVERY] Post-commit admin group notification failed for automation_id=%s: %s",
                        eff_aid,
                        err_code,
                        exc_info=True,
                    )
                    admin_notify_result = {
                        "notified": False,
                        "throttled": False,
                        "status": "failed",
                        "reason": err_code,
                        "error": str(exc)[:240],
                        "targets": [],
                        "results": [],
                    }

            try:
                await record_admin_notify_results_in_audit(
                    automation_id=eff_aid,
                    audit_version=eff_audit_version,
                    notify_result=admin_notify_result,
                    conn=conn,
                )
            except Exception as exc:
                logger.warning(
                    "[REPORT_DELIVERY] Best-effort audit writeback failed in finalize for automation_id=%s: %s",
                    eff_aid,
                    type(exc).__name__,
                    exc_info=True,
                )

        return {
            "admin_notify_result": admin_notify_result,
        }


async def append_report_delivery_route_audit_in_tx(
    conn: Any,
    *,
    change: ValidatedReportProfileChange,
    throttled: bool = False,
    pref_row: Mapping[str, Any] | None = None,
) -> int | None:
    """Append an honest ``route_update`` entry into ``automations.audit_history`` inside ``conn``'s tx.

    Leaves ``automations.metadata`` untouched and omits ``metadata_after``
    (marking ``metadata_unchanged=True`` / ``"路由变更、metadata 未变"``).
    """
    if not _conn_has_cursor(conn) or not _is_valid_uuid(change.automation_id):
        return None
    from src.dalton.database.company_automations import prepare_update_history_columns
    from src.server.database.automation import AUTOMATION_COLUMNS

    now_iso = datetime.now(timezone.utc).isoformat()
    audit_payload = change.build_audit_entry(recorded_at=now_iso, throttled=throttled)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT {AUTOMATION_COLUMNS}, audit_history
            FROM automations
            WHERE automation_id = %s::uuid AND deleted_at IS NULL
            FOR UPDATE
            """,
            (change.automation_id,),
        )
        locked_row = await cur.fetchone()
        if not locked_row:
            return None
        audit_payload["metadata_before"] = copy.deepcopy(
            dict(locked_row.get("metadata") or {})
        )
        owner_uid = str(locked_row.get("user_id") or change.owner_user_id or "").strip()
        eff_pref_row: Mapping[str, Any] | None = None
        if (
            isinstance(pref_row, Mapping)
            and "model_preference" in pref_row
            and str(pref_row.get("user_id") or owner_uid).strip() == owner_uid
        ):
            eff_pref_row = pref_row
        with automation_history_context(
            actor_user_id=change.actor_user_id,
            action="route_update",
            report_delivery_audit=audit_payload,
        ):
            extra_hist_cols, is_noop = await prepare_update_history_columns(
                locked_row,
                {},
                actor_user_id=change.actor_user_id,
                action="route_update",
                skip_if_unchanged=False,
                pref_row=eff_pref_row,
                changed_at_iso=now_iso,
                conn=conn,
            )
        if is_noop or not extra_hist_cols:
            return None
        await cur.execute(
            """
            UPDATE automations
            SET config_version = %s,
                active_version = %s,
                updated_by = %s,
                audit_history = %s::jsonb,
                updated_at = NOW()
            WHERE automation_id = %s::uuid AND deleted_at IS NULL
            """,
            (
                extra_hist_cols["config_version"],
                extra_hist_cols["active_version"],
                extra_hist_cols["updated_by"],
                json.dumps(extra_hist_cols["audit_history"], ensure_ascii=False, default=str),
                change.automation_id,
            ),
        )
        new_ver = int(extra_hist_cols["config_version"])
        change.audit_version = new_ver
        return new_ver


async def _update_report_delivery_audit_field_in_history(
    *,
    automation_id: str,
    audit_version: int | None,
    field_name: str,
    field_value: Any,
    conn: Any = None,
) -> None:
    """Best-effort update of a field inside ``report_delivery_audit`` on the target history entry."""
    if not _is_valid_uuid(automation_id):
        return
    if conn is not None and not _conn_has_cursor(conn):
        return

    async def _update(db_conn: Any) -> None:
        if not _conn_has_cursor(db_conn):
            return
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT audit_history
                FROM automations
                WHERE automation_id = %s::uuid AND deleted_at IS NULL
                FOR UPDATE
                """,
                (automation_id,),
            )
            row = await cur.fetchone()
            if not row or not isinstance(row.get("audit_history"), list):
                return
            history = copy.deepcopy(list(row["audit_history"]))
            target_idx: int | None = None
            if audit_version is not None:
                for idx in range(len(history) - 1, -1, -1):
                    item = history[idx]
                    if isinstance(item, Mapping) and item.get("version") == audit_version:
                        target_idx = idx
                        break
            if target_idx is None:
                for idx in range(len(history) - 1, -1, -1):
                    item = history[idx]
                    if isinstance(item, Mapping) and isinstance(item.get("report_delivery_audit"), Mapping):
                        target_idx = idx
                        break
            if target_idx is None:
                return
            entry = dict(history[target_idx])
            rd_audit = (
                dict(entry.get("report_delivery_audit") or {})
                if isinstance(entry.get("report_delivery_audit"), Mapping)
                else {}
            )
            rd_audit[field_name] = field_value
            entry["report_delivery_audit"] = rd_audit
            history[target_idx] = entry
            await cur.execute(
                """
                UPDATE automations
                SET audit_history = %s::jsonb
                WHERE automation_id = %s::uuid
                """,
                (json.dumps(history, ensure_ascii=False, default=str), automation_id),
            )

    if conn is not None:
        await _update(conn)
        return
    try:
        async with _use_conn(None) as db_conn:
            if not _conn_has_cursor(db_conn):
                return
            async with db_conn.transaction():
                await _update(db_conn)
    except RuntimeError as exc:
        if "pool is not open" in str(exc):
            return
        raise


async def record_admin_notify_results_in_audit(
    *,
    automation_id: str,
    audit_version: int | None,
    notify_result: Mapping[str, Any],
    conn: Any = None,
) -> None:
    """Record per-admin-target delivery results into the change's ``audit_history`` entry."""
    now_iso = datetime.now(timezone.utc).isoformat()
    status_str = str(notify_result.get("status") or "").strip()
    if not status_str:
        if notify_result.get("throttled"):
            status_str = "throttled"
        elif notify_result.get("notified"):
            status_str = "sent"
        else:
            status_str = "failed"
    notify_block: dict[str, Any] = {
        "required": True,
        "status": status_str,
        "throttled": bool(notify_result.get("throttled")),
        "notified": bool(notify_result.get("notified")),
        "partial_failure": bool(notify_result.get("partial_failure")),
        "reason": notify_result.get("reason"),
        "recorded_at": now_iso,
        "targets": list(notify_result.get("results") or []),
    }
    if notify_result.get("errors"):
        notify_block["errors"] = dict(notify_result["errors"])
    if notify_result.get("error"):
        notify_block["error"] = str(notify_result["error"])[:240]

    await _update_report_delivery_audit_field_in_history(
        automation_id=automation_id,
        audit_version=audit_version,
        field_name="admin_notify",
        field_value=notify_block,
        conn=conn,
    )


async def record_route_warning_error_in_audit(
    *,
    automation_id: str,
    audit_version: int | None,
    error: str,
    conn: Any = None,
) -> None:
    """Best-effort record post-commit ``compute_route_warning_for_automation`` failure into ``audit_history``."""
    err_block = {
        "error": str(error)[:240],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    await _update_report_delivery_audit_field_in_history(
        automation_id=automation_id,
        audit_version=audit_version,
        field_name="route_warning_error",
        field_value=err_block,
        conn=conn,
    )


async def validate_report_profile_change(
    actor: str,
    automation: Mapping[str, Any] | None,
    new_profile: Any,
    *,
    via_rest: bool = False,
    user_confirmed: bool = False,
    action: str | None = None,
    after_metadata: Mapping[str, Any] | None = None,
    previous_groups_override: Sequence[str] | None = None,
    skip_skill_match: bool = False,
    conn: Any = None,
    verify_member_fn: Callable[[str, str], Awaitable[tuple[bool, str | None]]] | None = None,
    get_tier_fn: Callable[[str], Awaitable[str]] | None = None,
    list_dm_identities_fn: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    list_group_bindings_fn: Callable[..., Awaitable[list[dict[str, Any]]]] | None = None,
    get_feishu_open_id_fn: Callable[..., Awaitable[str]] | None = None,
) -> ValidatedReportProfileChange:
    """Single shared enforcement point for all ``report_profile`` / ``automation:<id>`` route writes.

    Checks caller tier, ownership, owner tier for group targets, profile schema & skill
    compatibility, DM binding ownership, REST group restrictions, authorized group
    bindings, dialogue confirmation, workspace write access, and Feishu membership.
    Returns a ``ValidatedReportProfileChange`` carrying the DB audit payload and
    post-commit admin notification plan.
    """
    eff_get_tier = get_tier_fn or get_user_tier
    caller_tier = await _require_internal_or_admin(actor, get_tier_fn=eff_get_tier)

    auto_map = dict(automation or {})
    owner_user_id = str(auto_map.get("user_id") or actor).strip()
    aid_str = str(auto_map.get("automation_id") or "pending").strip() or "pending"
    auto_name = str(auto_map.get("name") or "").strip() or aid_str

    if owner_user_id != actor and caller_tier != "admin":
        raise ReportDeliveryError(
            "只能修改自己的定时任务发送配置（管理员可代为修改）",
            code="forbidden",
        )

    old_meta = copy.deepcopy(dict(auto_map.get("metadata") or {}))
    prev_parsed, _ = parse_report_profile(old_meta)
    previous_groups = (
        list(previous_groups_override)
        if previous_groups_override is not None
        else _group_targets_from_profile(prev_parsed)
    )

    if new_profile is None:
        eff_action = action or "clear"
        if after_metadata is not None:
            next_meta = copy.deepcopy(dict(after_metadata))
        else:
            next_meta = copy.deepcopy(old_meta)
        next_meta.pop("report_profile", None)
        next_meta.pop(REPORT_PROFILE_MISSING_NOTIFIED_KEY, None)
        next_meta.pop(REPORT_PROFILE_MISSING_NOTICE_KEY, None)
        return ValidatedReportProfileChange(
            actor_user_id=actor,
            caller_tier=caller_tier,
            owner_user_id=owner_user_id,
            automation_id=aid_str,
            automation_name=auto_name,
            action=eff_action,
            before_metadata=old_meta,
            after_metadata=next_meta,
            profile=None,
            previous_groups=previous_groups,
            new_groups=[],
        )

    if not isinstance(new_profile, Mapping):
        raise ReportDeliveryError(
            "report_profile 必须是包含 profile、report_targets、failure_targets 的对象。",
            code="profile_invalid",
        )

    parsed_ok, parse_err = parse_report_profile({"report_profile": new_profile})
    if parse_err or parsed_ok is None:
        raise ReportDeliveryError(
            "发送目标格式与 report_profile 规则不兼容：report_targets 仅支持 feishu:chat:oc_* / feishu:user:ou_*，failure_targets 仅支持 discord:user:*，text_targets 支持飞书/Discord，且至少需有一个有效报告/文字目标。",
            code="profile_invalid",
        )

    if not skip_skill_match:
        instruction = auto_map.get("instruction")
        skills = extract_report_skills(instruction)
        allowed_for_skill = allowed_profiles_for_instruction(instruction)
        if skills and parsed_ok["profile"] not in allowed_for_skill:
            raise ReportDeliveryError(
                f"profile {parsed_ok['profile']!r} 与任务指令中的技能 ({', '.join(skills)}) 不匹配，应为: {', '.join(allowed_for_skill)}",
                code="profile_skill_mismatch",
            )

    eff_list_dm = list_dm_identities_fn or list_caller_dm_identities
    caller_identities = await eff_list_dm(actor)
    caller_dm_set = {item["target"] for item in caller_identities}
    owner_dm_set = (
        caller_dm_set
        if owner_user_id == actor
        else {item["target"] for item in await eff_list_dm(owner_user_id)}
    )

    eff_list_groups = list_group_bindings_fn or list_authorized_group_bindings
    bindings = await eff_list_groups()
    bindings_by_target = {b["target"]: b for b in bindings}
    authorized_group_bindings: list[dict[str, Any]] = []

    norm_report = list(parsed_ok.get("report_targets") or [])
    norm_text = list(parsed_ok["text_targets"]) if parsed_ok.get("text_targets") is not None else None
    norm_failure = list(parsed_ok.get("failure_targets") or [])
    all_raw_targets = norm_report + (norm_text or []) + norm_failure

    checked_targets: set[str] = set()
    caller_open_id: str | None = None
    owner_tier_checked: str | None = None
    eff_verify = verify_member_fn or verify_feishu_chat_member
    eff_get_open_id = get_feishu_open_id_fn or get_feishu_open_id_for_user

    for raw in all_raw_targets:
        canonical, plat, kind, ident = parse_delivery_target(raw)
        if canonical in checked_targets:
            continue
        checked_targets.add(canonical)

        if kind == "dm":
            if caller_tier != "admin" and canonical not in caller_dm_set:
                raise ReportDeliveryError(
                    f"私信目标 {canonical} 未绑定在你的账号下，只能发送到你本人已绑定的私信渠道。",
                    code="dm_not_bound",
                )
            if caller_tier == "admin" and canonical not in (caller_dm_set | owner_dm_set):
                raise ReportDeliveryError(
                    f"私信目标 {canonical} 未绑定在调用者或任务属主的账号下。",
                    code="dm_not_bound",
                )
            continue

        # Group / channel target checks:
        if via_rest and caller_tier != "admin":
            raise ReportDeliveryError(
                REST_GROUP_TARGET_FORBIDDEN_MESSAGE,
                code="rest_group_target_forbidden",
            )

        if caller_tier == "admin" and owner_user_id != actor:
            if owner_tier_checked is None:
                owner_tier_checked = await eff_get_tier(owner_user_id)
            if owner_tier_checked not in {"internal", "admin"}:
                raise ReportDeliveryError(
                    "外部成员的任务不能设置群发送目标。",
                    code="owner_tier_forbidden",
                )

        binding = bindings_by_target.get(canonical)
        if not binding:
            raise ReportDeliveryError(UNAUTHORIZED_GROUP_MESSAGE, code="unauthorized_group")

        if not via_rest and not user_confirmed:
            raise ReportDeliveryError(
                f"设置群发送目标 ({canonical}) 必须先在当前对话向用户确认，并以 user_confirmed=true 重新调用。",
                code="confirmation_required",
            )

        if binding.get("workspace_id"):
            async with _use_conn(conn) as db_conn:
                async with db_conn.cursor(row_factory=dict_row) as cur:
                    ok_ws, _, _ = await _verify_workspace_write_permission(
                        cur, str(binding["workspace_id"]), owner_user_id, conn=db_conn
                    )
                    if not ok_ws:
                        raise ReportDeliveryError(
                            f"任务属主对群 {binding['chat_name']} 绑定的工作区没有写权限。",
                            code="workspace_forbidden",
                        )

        if caller_tier != "admin":
            if plat != "feishu":
                raise ReportDeliveryError(
                    "该渠道类型的群/频道暂无法核对成员资格，目前仅允许管理员设置。",
                    code="admin_only_channel",
                )
            if caller_open_id is None:
                caller_open_id = await eff_get_open_id(actor)
            if not caller_open_id:
                raise ReportDeliveryError(
                    "你尚未绑定飞书账号（open_id），无法核对飞书群成员资格。",
                    code="feishu_not_bound",
                )
            is_member, member_err = await eff_verify(ident, caller_open_id)
            if not is_member:
                raise ReportDeliveryError(
                    member_err or "你不是该飞书群成员，不能设置发送到此群。",
                    code="not_group_member",
                )

        authorized_group_bindings.append(binding)

    stored_profile: dict[str, Any] = {
        "profile": parsed_ok["profile"],
        "report_targets": norm_report,
        "failure_targets": norm_failure,
    }
    if norm_text is not None:
        stored_profile["text_targets"] = norm_text

    is_route_only = action == "route_update"
    if is_route_only:
        next_meta = {}
    else:
        if after_metadata is not None:
            next_meta = copy.deepcopy(dict(after_metadata))
        else:
            next_meta = copy.deepcopy(old_meta)
        next_meta["report_profile"] = stored_profile
        next_meta.pop(REPORT_PROFILE_MISSING_NOTIFIED_KEY, None)
        next_meta.pop(REPORT_PROFILE_MISSING_NOTICE_KEY, None)

    route_targets: list[str] = []
    for t in norm_report + (norm_text or []) + norm_failure:
        if t not in route_targets:
            route_targets.append(t)

    return ValidatedReportProfileChange(
        actor_user_id=actor,
        caller_tier=caller_tier,
        owner_user_id=owner_user_id,
        automation_id=aid_str,
        automation_name=auto_name,
        action=action or "set",
        before_metadata=old_meta,
        after_metadata=next_meta,
        profile=stored_profile,
        previous_groups=previous_groups,
        new_groups=_group_targets_from_profile(parsed_ok),
        authorized_group_bindings=authorized_group_bindings,
        route_targets=route_targets,
        metadata_unchanged=is_route_only,
    )


async def _binding_accessible_for_caller(
    binding: Mapping[str, Any],
    caller_user_id: str,
    *,
    conn: Any = None,
) -> bool:
    ws_id = binding.get("workspace_id")
    if not ws_id:
        return True
    try:
        async with _use_conn(conn) as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                ok_ws, _, _ = await _verify_workspace_write_permission(
                    cur, str(ws_id), caller_user_id, conn=db_conn
                )
                return bool(ok_ws)
    except Exception:
        return False


async def _check_feishu_bindings_bounded(
    feishu_items: Sequence[tuple[int, dict[str, Any]]],
    caller_open_id: str,
    *,
    verify_member_fn: Callable[[str, str], Awaitable[tuple[bool, str | None]]] | None = None,
    concurrency: int = _FEISHU_CHECK_CONCURRENCY,
    total_timeout_s: float = _FEISHU_GET_TOTAL_TIMEOUT_S,
) -> dict[int, tuple[bool, str, str | None]]:
    """Verify multiple Feishu groups concurrently with bounded concurrency and total timeout."""
    if not feishu_items:
        return {}

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    results: dict[int, tuple[bool, str, str | None]] = {}

    async def _check_one(idx: int, binding: dict[str, Any]) -> None:
        ident = binding["chat_address"].split(":", 1)[1]
        eff_fn = verify_member_fn or (
            verify_feishu_chat_member
            if verify_feishu_chat_member is not _DEFAULT_VERIFY_FEISHU_CHAT_MEMBER
            else None
        )
        async with sem:
            if eff_fn is not None:
                try:
                    ok, reason = await eff_fn(ident, caller_open_id)
                except Exception:
                    results[idx] = (False, "check_unavailable", "核对飞书群成员资格失败")
                    return
                if ok:
                    results[idx] = (True, "member", None)
                elif reason and any(
                    marker in reason
                    for marker in ("HTTP", "错误", "失败", "令牌", "凭据", "连接")
                ):
                    results[idx] = (False, "check_unavailable", reason)
                else:
                    results[idx] = (False, "not_member", reason or "你不是该飞书群成员，无法将其设为报告发送对象")
                return

            ok, status_code, msg = await _verify_feishu_chat_member_detailed(ident, caller_open_id)
            results[idx] = (ok, status_code, msg)

    tasks = [asyncio.create_task(_check_one(idx, b)) for idx, b in feishu_items]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=total_timeout_s)
    except asyncio.TimeoutError:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for idx, _ in feishu_items:
            results.setdefault(idx, (False, "check_timeout", "飞书群成员核对超时，请稍后重试或直接指定目标群再试"))
    return results


async def get_report_delivery_config(
    *,
    caller_user_id: str,
    automation_id: str | None = None,
    check_group_membership: bool = True,
    verify_member_fn: Callable[[str, str], Awaitable[tuple[bool, str | None]]] | None = None,
    feishu_check_concurrency: int = _FEISHU_CHECK_CONCURRENCY,
    feishu_total_timeout_s: float = _FEISHU_GET_TOTAL_TIMEOUT_S,
) -> dict[str, Any]:
    """Read an automation's report_profile + notify eligibility and caller's selectable targets."""
    caller_tier = await _require_internal_or_admin(caller_user_id)

    identities = await list_caller_dm_identities(caller_user_id)
    bindings = await list_authorized_group_bindings()

    auto: dict[str, Any] | None = None
    if automation_id:
        auto = await _load_automation_for_actor(automation_id, caller_user_id, caller_tier)

    caller_open_id = ""
    should_check_feishu = bool(
        automation_id and check_group_membership and caller_tier != "admin"
    )
    if should_check_feishu:
        caller_open_id = await get_feishu_open_id_for_user(caller_user_id)

    available_groups: list[dict[str, Any]] = []
    feishu_to_verify: list[tuple[int, dict[str, Any]]] = []
    for binding in bindings:
        plat = binding["platform"]
        entry: dict[str, Any] = {
            "target": binding["target"],
            "platform": plat,
            "chat_name": binding["chat_name"],
            "workspace_id": binding["workspace_id"],
        }
        if caller_tier == "admin":
            entry["selectable"] = True
            entry["membership_status"] = "admin_override"
        elif plat == "feishu":
            if not automation_id:
                # Overview listing avoids fanning out Feishu OpenAPI calls across every group.
                entry["selectable"] = False
                entry["membership_status"] = "unchecked_in_list"
            elif not check_group_membership:
                entry["selectable"] = bool(caller_open_id)
                entry["membership_status"] = "unchecked"
            elif not caller_open_id:
                entry["selectable"] = False
                entry["membership_status"] = "missing_bound_feishu_open_id"
            else:
                has_ws_access = await _binding_accessible_for_caller(binding, caller_user_id)
                if not has_ws_access:
                    entry["selectable"] = False
                    entry["membership_status"] = "workspace_forbidden"
                else:
                    idx = len(available_groups)
                    feishu_to_verify.append((idx, binding))
        else:
            entry["selectable"] = False
            entry["membership_status"] = "admin_only_channel"
        available_groups.append(entry)

    if feishu_to_verify:
        checked = await _check_feishu_bindings_bounded(
            feishu_to_verify,
            caller_open_id,
            verify_member_fn=verify_member_fn,
            concurrency=feishu_check_concurrency,
            total_timeout_s=feishu_total_timeout_s,
        )
        for idx, (ok, status_code, err_msg) in checked.items():
            available_groups[idx]["selectable"] = ok
            available_groups[idx]["membership_status"] = status_code
            if not ok and err_msg and status_code != "not_member":
                available_groups[idx]["membership_error"] = err_msg

    result: dict[str, Any] = {
        "ok": True,
        "action": "get",
        "caller_tier": caller_tier,
        "default_failure_targets": default_failure_targets_for_identities(identities),
        "available_dm_targets": identities,
        "available_group_targets": available_groups,
    }

    if not automation_id:
        automations = await auto_db.list_all_automations(caller_user_id)
        summaries = []
        for item in automations:
            meta = item.get("metadata")
            parsed, err = parse_report_profile(meta)
            if parsed is not None or instruction_uses_report_skill(item.get("instruction")):
                summaries.append(
                    {
                        "automation_id": str(item.get("automation_id")),
                        "name": item.get("name"),
                        "report_profile": parsed,
                        "profile_valid": parsed is not None and err is None,
                        "inferred_profile": infer_report_profile(
                            item.get("instruction"),
                            metadata=meta if isinstance(meta, Mapping) else None,
                            name=item.get("name"),
                        )[0],
                    }
                )
        result["automations"] = summaries
        return result

    assert auto is not None
    owner_user_id = str(auto.get("user_id") or "").strip()
    meta = auto.get("metadata")
    parsed, profile_err = parse_report_profile(meta)
    inferred_profile, candidate_profiles = infer_report_profile(
        auto.get("instruction"),
        metadata=meta if isinstance(meta, Mapping) else None,
        name=auto.get("name"),
    )

    aid_str = str(auto.get("automation_id"))
    route_key = f"automation:{aid_str}"
    master_enabled = await resolve_dalton_notify_enabled(owner_user_id)
    overrides = await resolve_dalton_notify_overrides(owner_user_id)
    routes = await resolve_dalton_notify_routes(owner_user_id)
    allowed_now, skip_reason, effective_route_targets = await evaluate_user_notify_eligibility(
        owner_user_id,
        auto,
    )

    intended_targets: list[str] = []
    if parsed:
        for k in ("report_targets", "text_targets", "failure_targets"):
            for t in parsed.get(k) or []:
                if t not in intended_targets:
                    intended_targets.append(t)

    effective_set = {str(t).strip() for t in effective_route_targets}
    missing_from_route = [t for t in intended_targets if t not in effective_set]
    blocked_reasons: list[str] = []
    if not master_enabled:
        blocked_reasons.append("master_switch_off")
    if not allowed_now and skip_reason and skip_reason not in blocked_reasons:
        blocked_reasons.append(skip_reason)
    if parsed and missing_from_route:
        blocked_reasons.append(f"route_missing_targets:{','.join(missing_from_route)}")

    result["automation"] = {
        "automation_id": aid_str,
        "name": auto.get("name"),
        "owner_user_id": owner_user_id,
        "uses_report_skill": instruction_uses_report_skill(auto.get("instruction")),
        "inferred_profile": inferred_profile,
        "candidate_profiles": candidate_profiles,
        "report_profile": parsed,
        "profile_status": "valid" if parsed else (profile_err or "missing"),
        "notify_state": {
            "route_key": route_key,
            "master_switch_enabled": master_enabled,
            "route_override": overrides.get(route_key),
            "route_targets": routes.get(route_key),
            "eligibility_allowed": allowed_now,
            "eligibility_skip_reason": skip_reason,
            "effective_route_targets": effective_route_targets,
            "missing_from_route": missing_from_route,
            "blocked_by_switch_or_route": bool(blocked_reasons),
            "blocked_reasons": blocked_reasons,
        },
    }
    return result


async def sync_report_delivery_routes_in_tx(
    db_conn: Any,
    *,
    owner_user_id: str,
    automation_id: str,
    change: ValidatedReportProfileChange,
) -> dict[str, Any] | None:
    """Sync ``automation:<id>`` route/override (or clear it) inside the caller's DB transaction."""
    route_key = f"automation:{automation_id}"
    if change.profile is not None:
        updated_prefs = await apply_dalton_notify_preference_patch(
            owner_user_id,
            {
                DALTON_NOTIFY_OVERRIDES_KEY: {route_key: True},
                DALTON_NOTIFY_ROUTES_KEY: {route_key: change.route_targets},
            },
            conn=db_conn,
            skip_report_profile_validation=True,
        )
        if change.authorized_group_bindings:
            await sync_owner_group_chat_settings(
                owner_user_id,
                change.authorized_group_bindings,
                conn=db_conn,
            )
        return updated_prefs if isinstance(updated_prefs, dict) else None

    updated_prefs = await apply_dalton_notify_preference_patch(
        owner_user_id,
        {
            DALTON_NOTIFY_OVERRIDES_KEY: {route_key: None},
            DALTON_NOTIFY_ROUTES_KEY: {route_key: None},
        },
        conn=db_conn,
        skip_report_profile_validation=True,
    )
    await reset_missing_profile_notice_deliveries(db_conn, automation_id)
    return updated_prefs if isinstance(updated_prefs, dict) else None


async def refresh_report_audit_snapshot_in_tx(
    db_conn: Any,
    *,
    automation_id: str,
    audit_version: int | None = None,
    pref_row: Mapping[str, Any] | None = None,
) -> None:
    """Recompute the version's ``audit_history`` snapshot & diff from post-sync preferences in tx."""
    if not _conn_has_cursor(db_conn) or not _is_valid_uuid(automation_id):
        return
    from src.dalton.database.company_automations import (
        fetch_user_preferences_unlocked,
        resolve_snapshot_notify_fields,
    )
    from src.dalton.services.company_automations import (
        compute_snapshot_changes,
        sanitize_automation_snapshot,
    )
    from src.server.database.automation import AUTOMATION_COLUMNS

    async with db_conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""
            SELECT {AUTOMATION_COLUMNS}, audit_history
            FROM automations
            WHERE automation_id = %s::uuid AND deleted_at IS NULL
            FOR UPDATE
            """,
            (automation_id,),
        )
        locked_row = await cur.fetchone()
        if not locked_row or not isinstance(locked_row.get("audit_history"), list):
            return
        if str(locked_row.get("notify_storage_mode") or "legacy").strip() == "task_row":
            return
        history = copy.deepcopy(list(locked_row["audit_history"]))
        if not history:
            return

        target_idx: int | None = None
        if audit_version is not None:
            for idx in range(len(history) - 1, -1, -1):
                item = history[idx]
                if isinstance(item, Mapping) and item.get("version") == audit_version:
                    target_idx = idx
                    break
        if target_idx is None:
            target_idx = len(history) - 1

        target_entry = history[target_idx]
        if not isinstance(target_entry, Mapping):
            return
        old_snap = target_entry.get("snapshot")
        if not isinstance(old_snap, Mapping):
            return

        owner_uid = str(locked_row.get("user_id") or "").strip()
        eff_pref_row: Mapping[str, Any] | None = None
        if (
            isinstance(pref_row, Mapping)
            and "model_preference" in pref_row
            and str(pref_row.get("user_id") or owner_uid).strip() == owner_uid
        ):
            eff_pref_row = pref_row
        else:
            fetched = await fetch_user_preferences_unlocked(owner_uid, conn=db_conn)
            if isinstance(fetched, Mapping) and "model_preference" in fetched:
                eff_pref_row = fetched
        if eff_pref_row is None:
            return

        eff_enabled, eff_routes = await resolve_snapshot_notify_fields(
            locked_row,
            pref_row=eff_pref_row,
            conn=db_conn,
        )
        new_snap = sanitize_automation_snapshot(
            {
                **dict(old_snap),
                "notify_enabled": bool(eff_enabled),
                "notify_routes": list(eff_routes),
            }
        )
        entry = dict(target_entry)
        entry["snapshot"] = new_snap

        if str(entry.get("action") or "") != "create":
            prev_snap: Mapping[str, Any] | None = None
            prev_ver = entry.get("prev_version")
            if isinstance(prev_ver, int) and not isinstance(prev_ver, bool):
                for item in reversed(history[:target_idx]):
                    if isinstance(item, Mapping) and item.get("version") == prev_ver:
                        cand = item.get("snapshot")
                        if isinstance(cand, Mapping):
                            prev_snap = cand
                        break
            if prev_snap is None and target_idx > 0:
                prev_item = history[target_idx - 1]
                cand = prev_item.get("snapshot") if isinstance(prev_item, Mapping) else None
                if isinstance(cand, Mapping):
                    prev_snap = cand
            if prev_snap is not None:
                entry["changes"] = compute_snapshot_changes(prev_snap, new_snap)

        history[target_idx] = entry
        await cur.execute(
            """
            UPDATE automations
            SET audit_history = %s::jsonb
            WHERE automation_id = %s::uuid
            """,
            (json.dumps(history, ensure_ascii=False, default=str), automation_id),
        )


async def sync_report_profile_after_write_in_tx(
    db_conn: Any,
    *,
    change: ValidatedReportProfileChange,
    saved_row: Mapping[str, Any],
    default_owner_user_id: str | None = None,
    is_company: bool = False,
) -> str | None:
    """Sync routes, refresh audit snapshot, reset missing-profile notice, and claim 10m notify slot in tx."""
    from src.dalton.company_identity import is_company_system_user

    aid_str = str(saved_row.get("automation_id") or change.automation_id).strip()
    owner_uid = str(
        saved_row.get("user_id") or default_owner_user_id or change.owner_user_id
    ).strip()
    change.automation_id = aid_str
    change.owner_user_id = owner_uid
    row_is_company = (
        is_company
        or saved_row.get("scope") == "company"
        or is_company_system_user(owner_uid)
    )
    raw_cv = saved_row.get("config_version")
    audit_ver = (
        raw_cv
        if isinstance(raw_cv, int) and not isinstance(raw_cv, bool)
        else None
    )
    if audit_ver is not None and change.audit_version is None:
        change.audit_version = audit_ver

    sync_failed_warning: str | None = None
    if not row_is_company and _is_valid_uuid(aid_str):
        synced_pref_row = await sync_report_delivery_routes_in_tx(
            db_conn,
            owner_user_id=owner_uid,
            automation_id=aid_str,
            change=change,
        )
        await refresh_report_audit_snapshot_in_tx(
            db_conn,
            automation_id=aid_str,
            audit_version=audit_ver,
            pref_row=synced_pref_row,
        )
    else:
        if change.profile is None and aid_str:
            await reset_missing_profile_notice_deliveries(db_conn, aid_str)
        if not row_is_company:
            sync_failed_warning = (
                "已写入 report_profile，但任务 ID 非标准 UUID，未能同步通知路由。"
            )

    if change.requires_admin_group_notice and aid_str:
        change.admin_notify_claimed = await claim_admin_group_notice_slot(
            db_conn, aid_str
        )
        if change.admin_notify_claimed is False:
            await record_admin_notify_results_in_audit(
                automation_id=aid_str,
                audit_version=audit_ver,
                notify_result={
                    "notified": False,
                    "throttled": True,
                    "status": "throttled",
                    "reason": "rate_limited_10m",
                    "results": [],
                },
                conn=db_conn,
            )
    return sync_failed_warning


async def compute_route_warning_for_automation(
    owner_user_id: str,
    automation: Mapping[str, Any] | None,
    change: ValidatedReportProfileChange,
) -> tuple[bool, bool, str | None, list[str], str | None]:
    """Evaluate notify eligibility after route sync and return ``(master_enabled, allowed, skip_reason, missing, route_warning)``."""
    if change.profile is None:
        return True, True, None, [], None
    from src.dalton.company_identity import is_company_system_user

    auto_map = dict(automation or {})
    if auto_map.get("scope") == "company" or is_company_system_user(owner_user_id):
        notify_enabled = auto_map.get("notify_enabled")
        raw_routes = auto_map.get("notify_routes")
        route_set = (
            {str(t).strip() for t in raw_routes if isinstance(t, str) and str(t).strip()}
            if isinstance(raw_routes, Sequence) and not isinstance(raw_routes, (str, bytes))
            else set()
        )
        missing = [t for t in change.route_targets if t not in route_set]
        if notify_enabled is False or missing:
            reason = "company_notify_disabled" if notify_enabled is False else "company_route_unsynced"
            return (
                True,
                False if notify_enabled is False else True,
                reason,
                missing,
                f"已写入 report_profile，但公司任务行路由未能同步覆盖部分目标（reason={reason}, missing={missing}）。",
            )
        return True, True, None, [], None

    master_enabled = await resolve_dalton_notify_enabled(owner_user_id)
    allowed_now, skip_reason, effective_route_targets = await evaluate_user_notify_eligibility(
        owner_user_id,
        automation,
    )
    effective_set = {str(t).strip() for t in effective_route_targets}
    missing_from_route = [t for t in change.route_targets if t not in effective_set]
    route_warning: str | None = None
    if master_enabled and (not allowed_now or missing_from_route):
        route_warning = (
            f"已写入 report_profile，但当前通知资格或路由仍拦截部分目标（skip_reason={skip_reason}, missing={missing_from_route}）。"
        )
    return master_enabled, allowed_now, skip_reason, missing_from_route, route_warning


async def evaluate_route_warning_best_effort(
    owner_user_id: str,
    automation: Mapping[str, Any] | None,
    change: ValidatedReportProfileChange,
) -> str | None:
    """Best-effort post-commit ``compute_route_warning_for_automation``; logs and audits on error."""
    try:
        _, _, _, _, route_warning = await compute_route_warning_for_automation(
            owner_user_id,
            automation,
            change,
        )
        return route_warning
    except Exception as exc:
        err_code = f"{type(exc).__name__}: {str(exc)[:180]}"
        aid_str = (
            str(automation.get("automation_id") or "").strip()
            if isinstance(automation, Mapping)
            else ""
        ) or change.automation_id
        audit_ver = change.audit_version
        if audit_ver is None and isinstance(automation, Mapping):
            raw_cv = automation.get("config_version")
            if isinstance(raw_cv, int) and not isinstance(raw_cv, bool):
                audit_ver = raw_cv
        logger.warning(
            "[REPORT_DELIVERY] Post-commit route_warning evaluation failed for automation_id=%s: %s",
            aid_str,
            type(exc).__name__,
            exc_info=True,
        )
        try:
            await record_route_warning_error_in_audit(
                automation_id=aid_str,
                audit_version=audit_ver,
                error=err_code,
            )
        except Exception:
            logger.warning(
                "[REPORT_DELIVERY] Best-effort route_warning audit writeback failed for automation_id=%s",
                aid_str,
                exc_info=True,
            )
        return f"已写入 report_profile，但路由状态检查失败（{type(exc).__name__}），请稍后核对通知资格。"


async def execute_report_profile_rest_mutation(
    *,
    change: ValidatedReportProfileChange,
    actor_user_id: str,
    mutate_fn: Callable[[Any], Awaitable[dict[str, Any] | None]],
    expected_config_version: int | None = None,
    is_company: bool = False,
    conn: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run REST create/update + route sync + DB audit + 10m notify slot in one transaction."""
    from src.dalton.company_identity import is_company_system_user

    audit_entry = change.build_audit_entry()
    saved_row: dict[str, Any] | None = None
    sync_failed_warning: str | None = None

    async with _use_conn(conn) as db_conn:
        async with db_conn.transaction():
            with automation_history_context(
                actor_user_id=actor_user_id,
                action=change.action if change.action in {"create", "update"} else "update",
                expected_config_version=expected_config_version,
                report_delivery_audit=audit_entry,
            ):
                saved_row = await mutate_fn(db_conn)
            if saved_row is None:
                return None, None

            sync_failed_warning = await sync_report_profile_after_write_in_tx(
                db_conn,
                change=change,
                saved_row=saved_row,
                is_company=is_company,
            )

    if not is_company and not is_company_system_user(change.owner_user_id):
        try:
            await invalidate_user_prefs_cache(change.owner_user_id)
            await invalidate_user_profile_cache(change.owner_user_id)
        except Exception:
            pass

    await change.finalize(automation=saved_row)
    if sync_failed_warning:
        return saved_row, sync_failed_warning
    route_warning = await evaluate_route_warning_best_effort(
        change.owner_user_id,
        saved_row,
        change,
    )
    return saved_row, route_warning


async def set_report_delivery_config(
    *,
    caller_user_id: str,
    automation_id: str,
    report_targets: Sequence[str],
    profile: str | None = None,
    failure_targets: Sequence[str] | None = None,
    text_targets: Sequence[str] | None = None,
    user_confirmed: bool = False,
    conn: Any = None,
    verify_member_fn: Callable[[str, str], Awaitable[tuple[bool, str | None]]] | None = None,
    notify_admins_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Validate permissions/targets and atomically save ``metadata.report_profile`` + notify route."""
    caller_tier = await _require_internal_or_admin(caller_user_id)
    auto = await _load_automation_for_actor(automation_id, caller_user_id, caller_tier, conn=conn)
    owner_user_id = str(auto.get("user_id") or "").strip()
    aid_str = str(auto.get("automation_id"))
    auto_name = str(auto.get("name") or "").strip() or aid_str

    if owner_user_id != caller_user_id and caller_tier != "admin":
        raise ReportDeliveryError(
            "只能修改自己的定时任务发送配置（管理员可代为修改）",
            code="forbidden",
        )

    old_meta = copy.deepcopy(dict(auto.get("metadata") or {}))
    resolved_profile = resolve_report_profile_id(
        auto.get("instruction"),
        requested_profile=profile,
        metadata=old_meta,
        name=auto.get("name"),
    )

    def _dedup_parse(raw_list: Sequence[str] | None) -> list[str]:
        seen_t: set[str] = set()
        out: list[str] = []
        for raw in raw_list or []:
            parsed_t = parse_delivery_target(raw)
            if parsed_t[0] not in seen_t:
                seen_t.add(parsed_t[0])
                out.append(parsed_t[0])
        return out

    norm_report = _dedup_parse(report_targets)
    norm_text = _dedup_parse(text_targets) if text_targets is not None else None
    if failure_targets is None:
        owner_identities = await list_caller_dm_identities(owner_user_id)
        norm_failure = default_failure_targets_for_identities(owner_identities)
    else:
        norm_failure = _dedup_parse(failure_targets)

    candidate_profile: dict[str, Any] = {
        "profile": resolved_profile,
        "report_targets": norm_report,
        "failure_targets": norm_failure,
    }
    if norm_text is not None:
        candidate_profile["text_targets"] = norm_text

    validated = await validate_report_profile_change(
        caller_user_id,
        auto,
        candidate_profile,
        via_rest=False,
        user_confirmed=user_confirmed,
        action="set",
        conn=conn,
        verify_member_fn=verify_member_fn,
    )

    route_key = f"automation:{aid_str}"
    updated: dict[str, Any] | None = None
    audit_entry = validated.build_audit_entry()
    async with _use_conn(conn) as db_conn:
        async with db_conn.transaction():
            try:
                with automation_history_context(
                    actor_user_id=caller_user_id,
                    action="update",
                    expected_config_version=auto.get("config_version"),
                    report_delivery_audit=audit_entry,
                ):
                    updated = await auto_db.update_automation(
                        aid_str,
                        owner_user_id,
                        metadata=validated.after_metadata,
                        expected_config_version=auto.get("config_version"),
                        actor_user_id=caller_user_id,
                        history_action="update",
                        conn=db_conn,
                    )
            except AutomationVersionConflictError as exc:
                raise ReportDeliveryError(
                    "更新任务 metadata 失败（可能发生并发修改，请重试）",
                    code="conflict",
                ) from exc
            if updated is None:
                raise ReportDeliveryError(
                    "更新任务 metadata 失败（可能发生并发修改，请重试）",
                    code="conflict",
                )

            synced_pref_row = await sync_report_delivery_routes_in_tx(
                db_conn,
                owner_user_id=owner_user_id,
                automation_id=aid_str,
                change=validated,
            )
            audit_ver = (
                updated.get("config_version")
                if isinstance(updated.get("config_version"), int)
                and not isinstance(updated.get("config_version"), bool)
                else None
            )
            if audit_ver is not None and validated.audit_version is None:
                validated.audit_version = audit_ver
            await refresh_report_audit_snapshot_in_tx(
                db_conn,
                automation_id=aid_str,
                audit_version=audit_ver,
                pref_row=synced_pref_row,
            )

            if validated.requires_admin_group_notice:
                validated.admin_notify_claimed = await claim_admin_group_notice_slot(
                    db_conn, aid_str
                )
                if validated.admin_notify_claimed is False:
                    await record_admin_notify_results_in_audit(
                        automation_id=aid_str,
                        audit_version=audit_ver,
                        notify_result={
                            "notified": False,
                            "throttled": True,
                            "reason": "rate_limited_10m",
                            "results": [],
                        },
                        conn=db_conn,
                    )

    try:
        await invalidate_user_prefs_cache(owner_user_id)
        await invalidate_user_profile_cache(owner_user_id)
    except Exception:
        pass

    finalized = await validated.finalize(
        automation=updated,
        notify_admins_fn=notify_admins_fn,
    )
    admin_notify_result = finalized.get("admin_notify_result") or {}

    try:
        (
            master_enabled,
            allowed_now,
            skip_reason,
            missing_from_route,
            route_warning,
        ) = await compute_route_warning_for_automation(owner_user_id, updated, validated)
    except Exception as exc:  # config is already committed; report it saved with a warning
        logger.warning(
            "[REPORT_DELIVERY] Post-commit route_warning evaluation failed for automation_id=%s: %s",
            aid_str,
            type(exc).__name__,
            exc_info=True,
        )
        cv = updated.get("config_version") if isinstance(updated, Mapping) else None
        try:
            await record_route_warning_error_in_audit(
                automation_id=aid_str,
                audit_version=cv if isinstance(cv, int) and not isinstance(cv, bool) else None,
                error=type(exc).__name__,
            )
        except Exception:
            logger.warning(
                "[REPORT_DELIVERY] Best-effort route_warning audit writeback failed for automation_id=%s",
                aid_str,
                exc_info=True,
            )
        master_enabled = allowed_now = None
        skip_reason = "route_warning_unavailable"
        missing_from_route = []
        route_warning = f"已保存发送配置，但路由状态检查失败（{type(exc).__name__}），请稍后核对通知资格。"

    return {
        "ok": True,
        "action": "set",
        "automation_id": aid_str,
        "name": auto_name,
        "report_profile": validated.profile,
        "route_key": route_key,
        "route_targets": validated.route_targets,
        "audit_version": (
            updated.get("config_version")
            if isinstance(updated, Mapping)
            else None
        ),
        "master_switch_enabled": master_enabled,
        "eligibility_allowed": allowed_now,
        "eligibility_skip_reason": skip_reason,
        "missing_from_route": missing_from_route,
        "route_warning": route_warning,
        "admin_notified": bool(admin_notify_result.get("notified")),
        "admin_notify_throttled": bool(admin_notify_result.get("throttled")),
        "admin_notify_error": (
            admin_notify_result.get("reason")
            if admin_notify_result
            and not admin_notify_result.get("notified")
            and not admin_notify_result.get("throttled")
            else None
        ),
        "master_switch_warning": (
            None
            if master_enabled
            else "属主的推送总开关目前关闭（请在 设置 → 模型 开启推送总开关），开启后报告才会实际发出。"
        ),
    }


async def clear_report_delivery_config(
    *,
    caller_user_id: str,
    automation_id: str,
    conn: Any = None,
    notify_admins_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Record DB audit, remove ``metadata.report_profile``, clear ``automation:<id>`` route, and reset notice state atomically."""
    caller_tier = await _require_internal_or_admin(caller_user_id)
    auto = await _load_automation_for_actor(automation_id, caller_user_id, caller_tier, conn=conn)
    owner_user_id = str(auto.get("user_id") or "").strip()
    aid_str = str(auto.get("automation_id"))
    auto_name = str(auto.get("name") or "").strip() or aid_str

    if owner_user_id != caller_user_id and caller_tier != "admin":
        raise ReportDeliveryError(
            "只能修改自己的定时任务发送配置（管理员可代为修改）",
            code="forbidden",
        )

    old_meta = dict(auto.get("metadata") or {})
    had_profile = "report_profile" in old_meta

    validated = await validate_report_profile_change(
        caller_user_id,
        auto,
        None,
        via_rest=False,
        user_confirmed=True,
        action="clear",
        conn=conn,
    )

    route_key = f"automation:{aid_str}"
    updated: dict[str, Any] | None = None
    audit_entry = validated.build_audit_entry()
    async with _use_conn(conn) as db_conn:
        async with db_conn.transaction():
            try:
                with automation_history_context(
                    actor_user_id=caller_user_id,
                    action="update",
                    expected_config_version=auto.get("config_version"),
                    report_delivery_audit=audit_entry,
                ):
                    updated = await auto_db.update_automation(
                        aid_str,
                        owner_user_id,
                        metadata=validated.after_metadata,
                        expected_config_version=auto.get("config_version"),
                        actor_user_id=caller_user_id,
                        history_action="update",
                        conn=db_conn,
                    )
            except AutomationVersionConflictError as exc:
                raise ReportDeliveryError(
                    "清除任务 metadata 失败（可能发生并发修改，请重试）",
                    code="conflict",
                ) from exc
            if updated is None:
                raise ReportDeliveryError(
                    "清除任务 metadata 失败（可能发生并发修改，请重试）",
                    code="conflict",
                )

            synced_pref_row = await sync_report_delivery_routes_in_tx(
                db_conn,
                owner_user_id=owner_user_id,
                automation_id=aid_str,
                change=validated,
            )
            audit_ver = (
                updated.get("config_version")
                if isinstance(updated.get("config_version"), int)
                and not isinstance(updated.get("config_version"), bool)
                else None
            )
            if audit_ver is not None and validated.audit_version is None:
                validated.audit_version = audit_ver
            await refresh_report_audit_snapshot_in_tx(
                db_conn,
                automation_id=aid_str,
                audit_version=audit_ver,
                pref_row=synced_pref_row,
            )

            if validated.requires_admin_group_notice:
                validated.admin_notify_claimed = await claim_admin_group_notice_slot(
                    db_conn, aid_str
                )
                if validated.admin_notify_claimed is False:
                    await record_admin_notify_results_in_audit(
                        automation_id=aid_str,
                        audit_version=audit_ver,
                        notify_result={
                            "notified": False,
                            "throttled": True,
                            "reason": "rate_limited_10m",
                            "results": [],
                        },
                        conn=db_conn,
                    )

    try:
        await invalidate_user_prefs_cache(owner_user_id)
        await invalidate_user_profile_cache(owner_user_id)
    except Exception:
        pass

    finalized = await validated.finalize(
        automation=updated,
        notify_admins_fn=notify_admins_fn,
    )
    admin_notify_result = finalized.get("admin_notify_result") or {}

    return {
        "ok": True,
        "action": "clear",
        "automation_id": aid_str,
        "name": auto_name,
        "cleared": had_profile,
        "route_key": route_key,
        "audit_version": (
            updated.get("config_version")
            if isinstance(updated, Mapping)
            else None
        ),
        "admin_notified": bool(admin_notify_result.get("notified")),
        "admin_notify_throttled": bool(admin_notify_result.get("throttled")),
        "admin_notify_error": (
            admin_notify_result.get("reason")
            if admin_notify_result
            and not admin_notify_result.get("notified")
            and not admin_notify_result.get("throttled")
            else None
        ),
    }
