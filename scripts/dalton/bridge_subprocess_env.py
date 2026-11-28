#!/usr/bin/env python3
"""Subprocess environment filtering and atomic env-file/plist merge.

Used by `researchdata_bridge.py`, `seed_asr_client.py`, `install_bridge.sh`,
and the `company-wiki` / `market-digest` skill `run.sh` launchers.

Pure Python 3 standard library only so host launchd and skill virtualenvs can invoke
this module without third-party dependencies.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
import errno
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import stat
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

DEFAULT_SYSTEM_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
DEFAULT_HOST_EMBED_URL = "http://127.0.0.1:8960/embed"
DEFAULT_NO_PROXY_HOSTS: tuple[str, ...] = (
    "localhost",
    "127.0.0.1",
    "::1",
    "host.docker.internal",
)

# Host-only ASR/TOS, Feishu, OpenClaw gateway, broker, and web-search bridge credentials.
ASR_CREDENTIAL_ENV_KEYS: frozenset[str] = frozenset(
    {
        "SEED_ASR_APP_ID",
        "SEED_ASR_ACCESS_KEY",
        "VOLC_TOS_AK",
        "VOLC_TOS_SK",
        "VOLC_TOS_BUCKET",
        "VOLC_TOS_REGION",
        "VOLC_TOS_ENDPOINT",
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_DEFAULT_GRANT_CHAT_ID",
        "DALTON_FEISHU_APP_ID",
        "DALTON_FEISHU_APP_SECRET",
        "DALTON_FEISHU_DEFAULT_GRANT_CHAT_ID",
        "OPENCLAW_FEISHU_APP_ID",
        "OPENCLAW_FEISHU_APP_SECRET",
        "LARK_APP_ID",
        "LARK_APP_SECRET",
        "OPENCLAW_GATEWAY_TOKEN",
        "DALTON_OPENCLAW_GATEWAY_TOKEN",
        "DALTON_WEB_SEARCH_BRIDGE_TOKEN",
        "DALTON_DISCORD_BOT_TOKEN",
    }
)

ASR_STORAGE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_ASR_STORAGE_ENDPOINT_URL",
        "DALTON_ASR_STORAGE_BUCKET_NAME",
        "DALTON_ASR_STORAGE_ACCESS_KEY_ID",
        "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY",
        "DALTON_ASR_STORAGE_REGION",
        "DALTON_ASR_STORAGE_ADDRESSING_STYLE",
        "DALTON_ASR_STORAGE_KEY_PREFIX",
    }
)

STRIPPED_CREDENTIAL_ENV_PREFIXES: tuple[str, ...] = (
    "SEED_ASR_",
    "VOLC_TOS_",
    "DALTON_ASR_STORAGE_",
    "FEISHU_",
    "LARK_",
    "OPENCLAW_FEISHU_",
    "DALTON_FEISHU_",
)

BRIDGE_TOKEN_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "DALTON_BRIDGE_TOKEN",
        "DALTON_RESEARCH_ADMIN_TOKEN",
        "DALTON_TRANSCRIPTION_TOKEN",
    }
)

SHELL_INJECTION_ENV_KEYS: frozenset[str] = frozenset(
    {
        "BASH_ENV",
        "ENV",
        "ZDOTDIR",
        "PROMPT_COMMAND",
        "SHELLOPTS",
        "BASHOPTS",
        "PYTHONSTARTUP",
        "PYTHONINSPECT",
        "CDPATH",
        "GLOBIGNORE",
    }
)

SHELL_INJECTION_ENV_PREFIXES: tuple[str, ...] = ("BASH_FUNC_",)

NONARCHIVE_STRIPPED_PROVIDER_AND_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "DEEPSEEK_API_KEY",
        "XAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "ZAI_API_KEY",
        "MOONSHOT_API_KEY",
        "MINIMAX_API_KEY",
        "KIMI_API_KEY",
        "OPENROUTER_API_KEY",
        "CEREBRAS_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "COHERE_API_KEY",
        "TOGETHER_API_KEY",
        "FIREWORKS_API_KEY",
        "PERPLEXITY_API_KEY",
        "DALTON_XAI_OAUTH_CLIENT_ID",
        "DALTON_XAI_GROK_CLIENT_VERSION",
        "TAVILY_API_KEY",
        "BRAVE_API_KEY",
        "EXA_API_KEY",
        "SERPER_API_KEY",
        "FMP_API_KEY",
        "FINNHUB_API_KEY",
        "POLYGON_API_KEY",
        "TIINGO_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "FIRECRAWL_API_KEY",
        "BYOK_ENCRYPTION_KEY",
        "DB_PASSWORD",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "DALTON_REEXTRACT_DSN",
        "DATABASE_URL",
        "INTERNAL_SERVICE_TOKEN",
        "DAYTONA_API_KEY",
        "E2B_API_KEY",
    }
)

CARD_SEND_EXPLICIT_FEISHU_KEYS: tuple[str, ...] = (
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_DEFAULT_GRANT_CHAT_ID",
)

# Explicit non-secret runtime environment keys passed through to research_runtime subprocesses.
RESEARCH_RUNTIME_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_RESEARCH_CODE",
        "DALTON_RESEARCH_HOME",
        "DALTON_EMBED_PYTHON",
        "DALTON_HOST_PLATFORM",
        "EDGAR_IDENTITY",
        "DALTON_BRIDGE_ENV_FILE",
        "DALTON_GUIDEPOINT_MCP_URL",
        "DALTON_BRIDGE_INTERNAL_CALL",
        "DALTON_REQUEST_DEADLINE_EPOCH_MS",
        "DALTON_ENABLED_LIBRARY_SOURCES",
    }
)

# Internal bridge/probe markers that MUST be stripped from runner=host data task subprocesses.
HOST_TASK_STRIPPED_INTERNAL_ENV_KEYS: tuple[str, ...] = (
    "DALTON_BRIDGE_INTERNAL_CALL",
    "DALTON_REQUEST_DEADLINE_EPOCH_MS",
    "DALTON_OPS_CONTROLLED_PROBE",
)

# Per-route secret keys explicitly allowed for research_runtime skills that require them
# (instead of sourcing ~/.zshrc or ~/.bash_profile inside run.sh).
RESEARCH_EXPLICIT_SECRET_ALLOWLIST: frozenset[str] = frozenset(
    {
        "GEMINI_API_KEY",
        "FIRECRAWL_API_KEY",
    }
)

ARCHIVE_BASE_REQUIRED_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG", "TZ")

ARCHIVE_WORKSPACE_RUNTIME_KEYS: frozenset[str] = frozenset(
    {
        "SANDBOX_PROVIDER",
        "DOCKER_SANDBOX_IMAGE",
        "DOCKER_SANDBOX_DEV_MODE",
        "DOCKER_SANDBOX_HOST_DIR",
        "DOCKER_SANDBOX_VOLUMES",
        "DAYTONA_API_KEY",
        "DAYTONA_SERVER_URL",
        "E2B_API_KEY",
        "PTC_CONFIG_FILE",
        "DALTON_DOTENV_PATH",
        "DALTON_REPO_ENV_FILE",
    }
)

ARCHIVE_BASE_OPTIONAL_PROCESS_KEYS: frozenset[str] = frozenset(
    {
        "TMPDIR",
        "TEMP",
        "TMP",
        "LC_ALL",
        "LC_CTYPE",
        "USER",
        "LOGNAME",
        "SYSTEMROOT",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "DALTON_ARCHIVE_MEM_TRACE",
        "DALTON_ARCHIVE_PID_FILE",
        "DALTON_ARCHIVE_STATE_FILE",
        *ARCHIVE_WORKSPACE_RUNTIME_KEYS,
    }
)

ARCHIVE_DB_REQUIRED_KEYS: tuple[str, ...] = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
)
ARCHIVE_DB_OPTIONAL_KEYS: tuple[str, ...] = ("DB_SSLMODE",)

ARCHIVE_OWNER_REQUIRED_KEYS: tuple[str, ...] = (
    "AUTH_USER_ID",
    "DALTON_PM_USER_IDS",
    "DALTON_ENABLED",
)
ARCHIVE_OWNER_OPTIONAL_MODE_KEYS: tuple[str, ...] = (
    "HOST_MODE",
    "DALTON_AUTH_MODE",
    "APP_ENV",
    "DALTON_PROFILE",
    "DALTON_HOST_PLATFORM",
)

DIRECT_EMBEDDING_CONFIG_KEYS: tuple[str, ...] = (
    "DALTON_EMBEDDING_PROVIDER",
    "DALTON_EMBEDDING_MODEL",
    "DALTON_EMBEDDING_API_MODEL",
    "DALTON_EMBEDDING_DIMENSIONS",
    "DALTON_EMBEDDING_BASE_URL",
    "DALTON_EMBEDDING_TASK_TYPE_DOC",
    "DALTON_EMBEDDING_TASK_TYPE_QUERY",
)

DIRECT_EMBEDDING_CREDENTIAL_KEYS: tuple[str, ...] = (
    "DALTON_EMBEDDING_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
)

VALID_HOST_PLATFORMS: frozenset[str] = frozenset({"macos", "linux"})

XAI_OAUTH_ALLOWED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_XAI_OAUTH_CLIENT_ID",
        "DALTON_XAI_GROK_CLIENT_VERSION",
    }
)
XAI_OAUTH_REQUIRED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_XAI_OAUTH_CLIENT_ID",
    }
)

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileValidationError(ValueError):
    """Raised when an env file contains duplicate keys, syntax errors, or invalid values."""

    def __init__(
        self,
        *,
        path: Path,
        reason: str,
        key: str | None = None,
        lineno: int | None = None,
    ) -> None:
        self.path = path
        self.key = key
        self.lineno = lineno
        self.reason = reason
        loc = f"{path}"
        if lineno is not None:
            loc = f"{loc}:{lineno}"
        if key:
            msg = f"{loc}: key {key}: {reason}"
        else:
            msg = f"{loc}: {reason}"
        super().__init__(msg)


def _is_shell_injection_key(key: str) -> bool:
    upper = key.upper()
    return (
        key in SHELL_INJECTION_ENV_KEYS
        or upper in SHELL_INJECTION_ENV_KEYS
        or any(upper.startswith(prefix) for prefix in SHELL_INJECTION_ENV_PREFIXES)
    )


def _is_stripped_credential_key(key: str) -> bool:
    upper = key.upper()
    return (
        key in ASR_CREDENTIAL_ENV_KEYS
        or key in ASR_STORAGE_ENV_KEYS
        or upper in ASR_CREDENTIAL_ENV_KEYS
        or upper in ASR_STORAGE_ENV_KEYS
        or any(upper.startswith(prefix) for prefix in STRIPPED_CREDENTIAL_ENV_PREFIXES)
    )


def has_asr_credentials_in_env(env: Mapping[str, str] | None = None) -> bool:
    """Return True if `env` contains any stripped ASR, TOS, Feishu, gateway, or broker key."""
    src = env if env is not None else os.environ
    return any(_is_stripped_credential_key(k) for k in src)


def strip_asr_credentials_from_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a copy of `env` with ASR/TOS, Feishu, OpenClaw gateway, broker, and shell injection keys removed."""
    src = env if env is not None else os.environ
    return {
        k: str(v)
        for k, v in src.items()
        if not _is_stripped_credential_key(k) and not _is_shell_injection_key(k)
    }


def _nonarchive_stripped_provider_keys(repo_root: Path | None = None) -> frozenset[str]:
    _, _, catalog_provider_keys = _load_catalog_maps(repo_root)
    return frozenset(NONARCHIVE_STRIPPED_PROVIDER_AND_SECRET_KEYS | catalog_provider_keys)


def build_nonarchive_subprocess_env(
    base_env: Mapping[str, str] | None = None,
    *,
    extra_env: Mapping[str, str] | None = None,
    drop_keys: Iterable[str] = (),
    allow_keys: Iterable[str] = (),
    repo_root: Path | None = None,
) -> dict[str, str]:
    """Build filtered environment for the 8 non-archive Bridge subprocess entrypoints and skills.

    Strips all ASR/TOS, Feishu (`FEISHU_*`, `LARK_*`, `OPENCLAW_FEISHU_*`, `DALTON_FEISHU_*`),
    gateway/broker tokens, shell startup injection variables (`BASH_ENV`, `ENV`, `ZDOTDIR`,
    `PROMPT_COMMAND`, `PYTHONSTARTUP`, etc.), both `DALTON_BRIDGE_ADMIN_TOKEN` and
    `DALTON_BRIDGE_TOKEN`, and all unrelated LLM/search/market/DB/encryption secrets unless
    explicitly listed in `allow_keys`.
    """
    merged: dict[str, str] = {}
    src = base_env if base_env is not None else os.environ
    for k, v in src.items():
        if v is not None:
            merged[str(k)] = str(v)
    if extra_env:
        for k, v in extra_env.items():
            if v is not None:
                merged[str(k)] = str(v)

    cleaned = strip_asr_credentials_from_env(merged)
    for token_key in BRIDGE_TOKEN_ENV_KEYS:
        cleaned.pop(token_key, None)

    explicitly_allowed = {str(k).strip() for k in allow_keys if str(k).strip()}
    for secret_key in _nonarchive_stripped_provider_keys(repo_root):
        if secret_key not in explicitly_allowed:
            cleaned.pop(secret_key, None)

    for key in drop_keys:
        cleaned.pop(str(key), None)
    if not cleaned.get("PATH", "").strip():
        cleaned["PATH"] = DEFAULT_SYSTEM_PATH
    return cleaned


# DALTON-HOOK(host-job-secrets): controlled Vault secret + plain env injection for runner=host
def build_host_task_subprocess_env(
    base_env: Mapping[str, str] | None = None,
    *,
    allowed_secrets: Mapping[str, str] | None = None,
    plain_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> dict[str, str]:
    """Build subprocess env for `runner: host` tasks with Vault-supplied secrets.

    Always starts from `build_nonarchive_subprocess_env` so no credentials leak from
    the bridge process's own environment, then injects manifest-validated `plain_env`
    and `allowed_secrets` (never shell-injection or bridge-token keys).
    """
    cleaned = build_nonarchive_subprocess_env(
        base_env,
        extra_env=extra_env,
        drop_keys=HOST_TASK_STRIPPED_INTERNAL_ENV_KEYS,
        repo_root=repo_root,
    )
    stripped_provider_keys = _nonarchive_stripped_provider_keys(repo_root)
    if plain_env:
        for raw_key, raw_val in plain_env.items():
            key = str(raw_key or "").strip()
            if (
                not key
                or raw_val is None
                or key in BRIDGE_TOKEN_ENV_KEYS
                or key in HOST_TASK_STRIPPED_INTERNAL_ENV_KEYS
                or _is_shell_injection_key(key)
                or _is_stripped_credential_key(key)
                or key in stripped_provider_keys
            ):
                continue
            cleaned[key] = str(raw_val)
    if allowed_secrets:
        for raw_key, raw_val in allowed_secrets.items():
            key = str(raw_key or "").strip()
            if (
                not key
                or raw_val is None
                or key in BRIDGE_TOKEN_ENV_KEYS
                or key in HOST_TASK_STRIPPED_INTERNAL_ENV_KEYS
                or _is_shell_injection_key(key)
            ):
                continue
            cleaned[key] = str(raw_val)
    for stripped_k in HOST_TASK_STRIPPED_INTERNAL_ENV_KEYS:
        cleaned.pop(stripped_k, None)
    return cleaned


def build_feishu_card_send_subprocess_env(
    base_env: Mapping[str, str] | None = None,
    *,
    feishu_app_id: str,
    feishu_app_secret: str,
    feishu_default_grant_chat_id: str = "",
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build environment for the Batch 1 card-send subprocess (`feishu_send_card.py`).

    Starts from the scrubbed non-archive baseline (removing ASR/TOS, gateway/broker,
    `DALTON_FEISHU_*`, `OPENCLAW_FEISHU_*`, `DALTON_BRIDGE_ADMIN_TOKEN`, `DALTON_BRIDGE_TOKEN`,
    and `DALTON_NO_DIRECT_SEND`), then injects only the explicitly supplied Bridge Feishu
    sending credentials.
    """
    cleaned = build_nonarchive_subprocess_env(
        base_env,
        extra_env=extra_env,
        drop_keys=("DALTON_NO_DIRECT_SEND",),
    )
    app_id = str(feishu_app_id or "").strip()
    app_secret = str(feishu_app_secret or "").strip()
    grant_chat_id = str(feishu_default_grant_chat_id or "").strip()
    if app_id:
        cleaned["FEISHU_APP_ID"] = app_id
    if app_secret:
        cleaned["FEISHU_APP_SECRET"] = app_secret
    if grant_chat_id:
        cleaned["FEISHU_DEFAULT_GRANT_CHAT_ID"] = grant_chat_id
    return cleaned


def build_research_runtime_subprocess_env(
    base_env: Mapping[str, str] | None = None,
    *,
    runtime_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    allow_secret_keys: Iterable[str] = (),
    drop_keys: Iterable[str] = (),
    repo_root: Path | None = None,
) -> dict[str, str]:
    """Build filtered env for `research_runtime` children using explicit runtime/secret allowlists."""
    allowed_secrets = tuple(
        sorted(
            {
                str(k).strip()
                for k in allow_secret_keys
                if str(k).strip() in RESEARCH_EXPLICIT_SECRET_ALLOWLIST
            }
        )
    )
    merged_extra: dict[str, str] = {}
    if extra_env:
        for raw_k, raw_v in extra_env.items():
            key = str(raw_k or "").strip()
            if key and raw_v is not None:
                merged_extra[key] = str(raw_v)
    if runtime_env:
        for raw_k, raw_v in runtime_env.items():
            key = str(raw_k or "").strip()
            if raw_v is None or not key:
                continue
            if key in RESEARCH_RUNTIME_ENV_KEYS or key in allowed_secrets:
                merged_extra[key] = str(raw_v)
    return build_nonarchive_subprocess_env(
        base_env,
        extra_env=merged_extra,
        drop_keys=drop_keys,
        allow_keys=allowed_secrets,
        repo_root=repo_root,
    )


def parse_env_file_dict(path: Path) -> dict[str, str]:
    """Parse `KEY=VALUE` lines using the same unquoting rule as `researchdata_bridge.py`."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip("'").strip('"')
            if k:
                out[k] = v
    except OSError:
        return out
    return out


def _repo_root_default() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_catalog_maps(
    repo_root: Path | None = None,
) -> tuple[dict[str, str], dict[str, dict[str, Any]], set[str]]:
    """Return `(model_to_provider, provider_info, all_known_provider_env_keys)`."""
    root = repo_root or _repo_root_default()
    if not (root / "src" / "llms" / "manifest" / "providers.json").is_file():
        default_root = _repo_root_default()
        if (default_root / "src" / "llms" / "manifest" / "providers.json").is_file():
            root = default_root
    model_to_provider: dict[str, str] = {}
    provider_info: dict[str, dict[str, Any]] = {}
    all_provider_env_keys: set[str] = set(XAI_OAUTH_ALLOWED_ENV_KEYS)

    def _ingest_provider_config(cfg_map: Mapping[str, Any]) -> None:
        for p_name, p_cfg in cfg_map.items():
            if not isinstance(p_cfg, dict):
                continue
            p_env = p_cfg.get("env_key")
            p_access = p_cfg.get("access_type")
            valid_p_env = (
                str(p_env).strip()
                if isinstance(p_env, str)
                and _ENV_KEY_RE.fullmatch(p_env.strip())
                and p_env.strip() == p_env.strip().upper()
                else None
            )
            if valid_p_env:
                all_provider_env_keys.add(valid_p_env)
            provider_info[str(p_name)] = {
                "env_key": valid_p_env,
                "access_type": p_access,
            }
            variants = p_cfg.get("variants")
            if isinstance(variants, dict):
                for v_name, v_cfg in variants.items():
                    if not isinstance(v_cfg, dict):
                        continue
                    if "env_key" in v_cfg:
                        raw_ve = v_cfg.get("env_key")
                        v_env = (
                            str(raw_ve).strip()
                            if isinstance(raw_ve, str)
                            and _ENV_KEY_RE.fullmatch(raw_ve.strip())
                            and raw_ve.strip() == raw_ve.strip().upper()
                            else None
                        )
                    else:
                        v_env = valid_p_env
                    if v_env:
                        all_provider_env_keys.add(v_env)
                    provider_info[str(v_name)] = {
                        "env_key": v_env,
                        "access_type": v_cfg.get("access_type", p_access),
                    }

    providers_doc = _read_json_dict(root / "src" / "llms" / "manifest" / "providers.json")
    if isinstance(providers_doc.get("provider_config"), dict):
        _ingest_provider_config(providers_doc["provider_config"])
    if isinstance(providers_doc.get("models"), dict):
        for p_name, items in providers_doc["models"].items():
            if not isinstance(items, list):
                continue
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                mid = str(entry.get("id") or "").strip()
                if mid:
                    model_to_provider.setdefault(mid, str(p_name))
                for alias in entry.get("alias") or []:
                    if isinstance(alias, str) and alias.strip():
                        model_to_provider.setdefault(alias.strip(), str(p_name))

    models_doc = _read_json_dict(root / "src" / "llms" / "manifest" / "models.json")
    for m_key, m_cfg in models_doc.items():
        if isinstance(m_cfg, dict) and isinstance(m_cfg.get("provider"), str):
            model_to_provider[str(m_key)] = m_cfg["provider"].strip()

    for overlay_rel in (
        Path("src/dalton/llm/native_overlay.json"),
        Path("var/dalton/catalog/native_sync_overlay.json"),
    ):
        ov = _read_json_dict(root / overlay_rel)
        ov_providers = ov.get("providers")
        if isinstance(ov_providers, dict) and isinstance(ov_providers.get("provider_config"), dict):
            _ingest_provider_config(ov_providers["provider_config"])
        ov_models = ov.get("models")
        if isinstance(ov_models, dict):
            for m_key, m_cfg in ov_models.items():
                if isinstance(m_cfg, dict) and isinstance(m_cfg.get("provider"), str):
                    model_to_provider[str(m_key)] = m_cfg["provider"].strip()

    return model_to_provider, provider_info, all_provider_env_keys


def _infer_provider_from_model_name(model: str) -> str | None:
    m = model.strip()
    if not m:
        return None
    lower = m.lower()
    if lower == "grok-oauth" or (lower.startswith("grok-") and lower.endswith("-oauth")):
        return "xai-oauth"
    if lower.endswith("-oauth"):
        if lower.startswith("muse-"):
            return "muse-oauth"
        if lower.startswith("gemini-") or lower.startswith("gpt-oss-"):
            return "antigravity-oauth"
        if lower.startswith("claude-") and any(
            lower.endswith(sfx) for sfx in ("-low-oauth", "-medium-oauth", "-high-oauth")
        ):
            return "antigravity-oauth"
        if lower.startswith("claude-"):
            return "claude-oauth"
        if lower.startswith("gpt-"):
            return "codex-oauth"
        return "oauth"
    if lower.startswith("deepseek-"):
        return "deepseek"
    if lower.startswith("gemini-"):
        return "gemini"
    if lower.startswith("grok-"):
        return "xai"
    if lower.startswith("claude-"):
        return "anthropic"
    if lower.startswith("gpt-"):
        return "openai"
    if lower.startswith("glm-") and lower.endswith("-plan"):
        return "z-ai-cn-coding"
    if lower.startswith("glm-") and lower.endswith("-cn"):
        return "z-ai-cn"
    if lower.startswith("glm-"):
        return "z-ai"
    if lower.startswith("qwen") and lower.endswith("-intl"):
        return "dashscope-intl"
    if lower.startswith("qwen"):
        return "dashscope"
    if lower.startswith("kimi-") and lower.endswith("-coding"):
        return "moonshot-coding"
    if lower.startswith("kimi-"):
        return "moonshot"
    if lower.startswith("minimax-"):
        return "minimax"
    return None


def resolve_provider_env_keys_for_models(
    models: Iterable[str | None],
    *,
    repo_root: Path | None = None,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Resolve `(allowed_provider_keys, conditionally_required_keys, all_known_provider_keys)` for `models`."""
    model_to_provider, provider_info, all_provider_keys = _load_catalog_maps(repo_root)
    allowed: set[str] = set()
    required: set[str] = set()

    for raw_model in models:
        m = str(raw_model or "").strip()
        if not m:
            continue
        provider = model_to_provider.get(m) or _infer_provider_from_model_name(m)
        if not provider:
            continue
        if provider == "xai-oauth":
            allowed.update(XAI_OAUTH_ALLOWED_ENV_KEYS)
            required.update(XAI_OAUTH_REQUIRED_ENV_KEYS)
            continue
        if provider.endswith("-oauth") or provider == "oauth":
            continue
        p_meta = provider_info.get(provider) or {}
        env_key = p_meta.get("env_key")
        if (
            isinstance(env_key, str)
            and env_key
            and not _is_stripped_credential_key(env_key)
            and env_key not in BRIDGE_TOKEN_ENV_KEYS
        ):
            allowed.add(env_key)

    return frozenset(allowed), frozenset(required), frozenset(all_provider_keys)


def _merge_no_proxy(existing: str | None) -> str:
    items: list[str] = []
    if existing:
        for part in str(existing).split(","):
            cleaned = part.strip()
            if cleaned and cleaned not in items:
                items.append(cleaned)
    for req in DEFAULT_NO_PROXY_HOSTS:
        if req not in items:
            items.append(req)
    return ",".join(items)


def _validate_host_embed_url(url_val: str) -> str | None:
    raw = str(url_val or "").strip()
    if not raw:
        return "missing required key DALTON_BRIDGE_EMBED_URL"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "invalid key DALTON_BRIDGE_EMBED_URL (malformed URL)"
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 8960:
        return "invalid key DALTON_BRIDGE_EMBED_URL (must point to http://127.0.0.1:8960)"
    return None


def scrub_post_dotenv_import_env(
    target_env: MutableMapping[str, str],
    pre_import_env: Mapping[str, str] | None = None,
    *,
    extra_preserve_keys: Iterable[str] = (),
    repo_root: Path | None = None,
) -> None:
    """Re-filter `target_env` after late config/dotenv loads (`load_from_files` / `load_dotenv_async`).

    Removes all stripped Feishu/gateway/ASR credentials and shell injection vars,
    drops any non-allowlisted keys newly injected from `.env`, and preserves the caller's
    pre-load environment plus selected workspace keys.
    """
    _ = repo_root
    pre_env = pre_import_env if pre_import_env is not None else {}

    allowed_keys = frozenset(
        set(ARCHIVE_BASE_REQUIRED_KEYS)
        | set(ARCHIVE_BASE_OPTIONAL_PROCESS_KEYS)
        | set(ARCHIVE_DB_REQUIRED_KEYS)
        | set(ARCHIVE_DB_OPTIONAL_KEYS)
        | set(ARCHIVE_OWNER_REQUIRED_KEYS)
        | set(ARCHIVE_OWNER_OPTIONAL_MODE_KEYS)
        | set(ARCHIVE_WORKSPACE_RUNTIME_KEYS)
        | set(DIRECT_EMBEDDING_CONFIG_KEYS)
        | set(DIRECT_EMBEDDING_CREDENTIAL_KEYS)
        | {"DALTON_BRIDGE_ADMIN_TOKEN", "DALTON_BRIDGE_EMBED_URL", "no_proxy", "DALTON_OCW_AGENTS_ROOT"}
    )

    if extra_preserve_keys:
        allowed_keys = frozenset(set(allowed_keys) | {str(k) for k in extra_preserve_keys if k})

    for key in list(target_env.keys()):
        if (
            _is_stripped_credential_key(key)
            or _is_shell_injection_key(key)
            or key == "DALTON_BRIDGE_TOKEN"
        ):
            target_env.pop(key, None)
            continue
        if key not in pre_env and key not in allowed_keys:
            target_env.pop(key, None)

    for key, pre_raw in pre_env.items():
        if (
            _is_stripped_credential_key(key)
            or _is_shell_injection_key(key)
            or key == "DALTON_BRIDGE_TOKEN"
        ):
            continue
        pre_val = str(pre_raw or "").strip()
        if pre_val:
            target_env[key] = pre_val

    pre_embed = str(pre_env.get("DALTON_BRIDGE_EMBED_URL") or "").strip()
    if pre_embed and _validate_host_embed_url(pre_embed) is None:
        target_env["DALTON_BRIDGE_EMBED_URL"] = pre_embed
    elif "DALTON_BRIDGE_EMBED_URL" in target_env:
        cur_embed = str(target_env.get("DALTON_BRIDGE_EMBED_URL") or "").strip()
        if not cur_embed or "host.docker.internal" in cur_embed:
            target_env["DALTON_BRIDGE_EMBED_URL"] = DEFAULT_HOST_EMBED_URL

    if "no_proxy" in pre_env or "no_proxy" in target_env:
        target_env["no_proxy"] = _merge_no_proxy(
            pre_env.get("no_proxy") or target_env.get("no_proxy")
        )


def inspect_env_key_presence(
    keys: Iterable[str],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, bool]:
    """Return boolean key-presence map ONLY (never exposing any environment value)."""
    src = env if env is not None else os.environ
    return {str(k): (str(k) in src and bool(str(src.get(str(k)) or ""))) for k in keys}


# ---------------------------------------------------------------------------
# Strict `.env` parsing & secure atomic installer merge (`install_bridge.sh`)
# ---------------------------------------------------------------------------


_MACOS_SYSTEM_ROOT_SYMLINKS: frozenset[str] = frozenset({"/var", "/tmp", "/etc"})


def _validate_safe_parent_chain(target_path: Path, *, report_path: Path) -> None:
    """Reject any symlinked or non-directory ancestor in `target_path` (excluding OS root links)."""
    abs_parent = Path(os.path.abspath(str(target_path)))
    for candidate in (abs_parent, *abs_parent.parents):
        cand_str = str(candidate)
        if cand_str in _MACOS_SYSTEM_ROOT_SYMLINKS:
            continue
        if candidate.is_symlink():
            raise EnvFileValidationError(
                path=report_path,
                reason=f"unsafe symlink parent directory ({candidate})",
            )
        try:
            st = candidate.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise EnvFileValidationError(
                path=report_path,
                reason=f"parent path is not a directory ({candidate})",
            )


def validate_safe_directory_target(dir_path: Path) -> None:
    """Validate that `dir_path` and its ancestors are safe real directories (never symlinks)."""
    _validate_safe_parent_chain(dir_path, report_path=dir_path)


def validate_safe_regular_file_target(
    path: Path,
    *,
    allow_missing: bool = True,
) -> None:
    """Validate that `path` has safe real parent directories and is a regular file (never a symlink)."""
    _validate_safe_parent_chain(path.parent, report_path=path)
    if path.is_symlink():
        raise EnvFileValidationError(
            path=path,
            reason="target path must not be a symlink",
        )
    try:
        st = path.lstat()
    except FileNotFoundError as exc:
        if not allow_missing:
            raise EnvFileValidationError(
                path=path,
                reason="required file does not exist",
            ) from exc
        return
    if stat.S_ISDIR(st.st_mode):
        raise EnvFileValidationError(path=path, reason="target path is a directory")
    if not stat.S_ISREG(st.st_mode):
        raise EnvFileValidationError(path=path, reason="target path is not a regular file")


def _fsync_directory(dir_path: Path, *, strict: bool = False) -> None:
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        if strict:
            raise
        return
    try:
        os.fsync(fd)
    except OSError:
        if strict:
            raise
    finally:
        os.close(fd)


def atomic_write_text(
    path: Path,
    content: str,
    *,
    mode: int = 0o600,
    parent_mode: int | None = None,
) -> None:
    """Atomically write `content` to `path` with exact `mode` permissions via temp file + `os.replace`."""
    validate_safe_regular_file_target(path, allow_missing=True)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    validate_safe_directory_target(path.parent)
    if parent_mode is not None:
        os.chmod(path.parent, parent_mode)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    closed = False
    try:
        os.fchmod(fd, mode)
        payload = content.encode("utf-8")
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
        os.close(fd)
        closed = True
        os.chmod(tmp_path, mode)
        validate_safe_regular_file_target(path, allow_missing=True)
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def parse_env_file_strict(
    path: Path,
) -> tuple[list[tuple[str, str | None, str]], dict[str, str]]:
    """Strictly parse an env file, rejecting symlinks, malformed `KEY=VALUE` lines, and duplicate keys.

    Returns `(entries, unquoted_values)` where each entry is `(kind, key_or_none, raw_line)`
    and `unquoted_values` applies `v.strip().strip("'").strip('"')` matching `_read_env_file_dict`.
    Never includes raw values in raised error messages.
    """
    validate_safe_regular_file_target(path, allow_missing=True)
    if not path.exists():
        return [], {}

    raw_text = path.read_text(encoding="utf-8")
    entries: list[tuple[str, str | None, str]] = []
    values: dict[str, str] = {}
    seen_lines: dict[str, int] = {}

    for lineno, raw_line in enumerate(raw_text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            entries.append(("verbatim", None, raw_line))
            continue
        if "=" not in stripped:
            raise EnvFileValidationError(
                path=path,
                lineno=lineno,
                reason="malformed line (expected KEY=VALUE)",
            )
        raw_key, raw_val = stripped.split("=", 1)
        key = raw_key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            raise EnvFileValidationError(
                path=path,
                lineno=lineno,
                reason="invalid environment variable name syntax",
            )
        if key in seen_lines:
            raise EnvFileValidationError(
                path=path,
                key=key,
                lineno=lineno,
                reason=f"duplicate key (first defined at line {seen_lines[key]})",
            )
        seen_lines[key] = lineno
        unquoted = raw_val.strip().strip("'").strip('"').strip()
        values[key] = unquoted
        entries.append(("kv", key, raw_line))

    return entries, values


def _apply_env_defaults(
    entries: list[tuple[str, str | None, str]],
    existing_values: Mapping[str, str],
    defaults: list[tuple[str, str]],
    *,
    remove_keys: Iterable[str] = (),
) -> tuple[str, dict[str, str]]:
    """Preserve all existing lines/comments and existing non-empty values; fill missing or empty default keys."""
    remove_set = {str(k) for k in remove_keys if k}
    effective = {k: v for k, v in existing_values.items() if k not in remove_set}
    default_map = dict(defaults)
    out_lines: list[str] = []
    seen_default_keys: set[str] = set()

    for kind, key, raw_line in entries:
        if kind != "kv" or key is None:
            out_lines.append(raw_line)
            continue
        if key in remove_set:
            continue
        if key in default_map:
            seen_default_keys.add(key)
            cur_val = str(existing_values.get(key) or "").strip()
            if cur_val:
                out_lines.append(raw_line)
                effective[key] = cur_val
            else:
                new_val = default_map[key]
                out_lines.append(f"{key}={new_val}")
                effective[key] = new_val
        else:
            out_lines.append(raw_line)

    for key, def_val in defaults:
        if key not in seen_default_keys:
            out_lines.append(f"{key}={def_val}")
            effective[key] = def_val

    return "\n".join(out_lines) + "\n", effective


def _validate_no_control_or_whitespace(
    *, path: Path, key: str, value: str, allow_spaces: bool = False
) -> None:
    if not value:
        raise EnvFileValidationError(path=path, key=key, reason="required value is empty")
    if any(ord(ch) < 32 for ch in value):
        raise EnvFileValidationError(
            path=path, key=key, reason="contains control characters"
        )
    if not allow_spaces and any(ch.isspace() for ch in value):
        raise EnvFileValidationError(
            path=path, key=key, reason="must not contain whitespace"
        )


def _read_plist_env_vars(plist_path: Path | None) -> dict[str, str]:
    """Safely read `EnvironmentVariables` from an existing launchd plist if present."""
    if plist_path is None:
        return {}
    validate_safe_regular_file_target(plist_path, allow_missing=True)
    if not plist_path.exists():
        return {}
    try:
        parsed = plistlib.loads(plist_path.read_bytes())
    except Exception:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    env_map = parsed.get("EnvironmentVariables")
    if not isinstance(env_map, Mapping):
        return {}
    return {
        str(k): str(v)
        for k, v in env_map.items()
        if isinstance(k, str) and v is not None
    }


def merge_bridge_and_repo_env_files(
    *,
    bridge_env_path: Path,
    repo_env_path: Path,
    default_port: str = "8960",
    default_python_bin: str,
    default_research_code: str = "",
    default_research_home: str = "",
    existing_plist_path: Path | None = None,
    research_runtime: str = "new",
    default_host_platform: str = "macos",
) -> dict[str, Any]:
    """Validate and atomically merge defaults into `researchdata-bridge.env` and `<repo>/.env`.

    - Existing comments, ordering, extra keys (`FEISHU_*`, `SEED_ASR_*`, etc.), and all
      existing non-empty values are preserved verbatim.
    - Existing tokens are never rotated; existing `FEISHU_*` values are never overwritten.
    - Only the decoupled `DALTON_RESEARCH_CODE` / `DALTON_RESEARCH_HOME` mode is supported;
      any legacy `DALTON_OCW_ROOT` entry is removed.
    - Duplicate keys, malformed lines, partial Feishu credentials (`FEISHU_APP_ID` without
      `FEISHU_APP_SECRET` or vice versa), and invalid required values fail closed before
      modifying either target file.
    - Returns non-secret metadata only (`port`, `research_code`, `research_home`, `python_bin`).
    """
    if research_runtime not in ("auto", "new"):
        raise ValueError(f"unsupported research_runtime mode: {research_runtime!r}")
    _ = existing_plist_path
    bridge_entries, bridge_vals = parse_env_file_strict(bridge_env_path)
    repo_entries, repo_vals = parse_env_file_strict(repo_env_path)

    # Validate partial Feishu configuration in bridge env before generating or writing anything.
    feishu_id = str(bridge_vals.get("FEISHU_APP_ID") or "").strip()
    feishu_secret = str(bridge_vals.get("FEISHU_APP_SECRET") or "").strip()
    if bool(feishu_id) != bool(feishu_secret):
        missing_key = "FEISHU_APP_SECRET" if feishu_id else "FEISHU_APP_ID"
        raise EnvFileValidationError(
            path=bridge_env_path,
            key=missing_key,
            reason="incomplete Feishu app credentials (both FEISHU_APP_ID and FEISHU_APP_SECRET must be set together)",
        )

    seed_token = (
        str(bridge_vals.get("DALTON_BRIDGE_TOKEN") or "").strip()
        or str(repo_vals.get("DALTON_BRIDGE_TOKEN") or "").strip()
        or secrets.token_hex(32)
    )
    seed_admin_token = (
        str(bridge_vals.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
        or str(repo_vals.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
        or secrets.token_hex(32)
    )

    bridge_defaults: list[tuple[str, str]] = [
        ("DALTON_BRIDGE_HOST", "127.0.0.1"),
        ("DALTON_BRIDGE_PORT", str(default_port).strip() or "8960"),
        ("DALTON_BRIDGE_TOKEN", seed_token),
        ("DALTON_BRIDGE_ADMIN_TOKEN", seed_admin_token),
        ("DALTON_OCW_PYTHON", str(default_python_bin).strip()),
        ("DALTON_BRIDGE_MAX_INFLIGHT", "2"),
        ("DALTON_BRIDGE_CALLER_BUDGET_S", "625"),
    ]
    if str(default_research_code).strip():
        bridge_defaults.append(("DALTON_RESEARCH_CODE", str(default_research_code).strip()))
    if str(default_research_home).strip():
        bridge_defaults.append(("DALTON_RESEARCH_HOME", str(default_research_home).strip()))
    bridge_rendered, bridge_eff = _apply_env_defaults(
        bridge_entries, bridge_vals, bridge_defaults, remove_keys=("DALTON_OCW_ROOT",)
    )

    # Validate all required bridge effective values before touching disk.
    _validate_no_control_or_whitespace(
        path=bridge_env_path,
        key="DALTON_BRIDGE_HOST",
        value=str(bridge_eff.get("DALTON_BRIDGE_HOST") or "").strip(),
    )
    raw_port = str(bridge_eff.get("DALTON_BRIDGE_PORT") or "").strip()
    try:
        port_int = int(raw_port)
    except ValueError as exc:
        raise EnvFileValidationError(
            path=bridge_env_path,
            key="DALTON_BRIDGE_PORT",
            reason="must be an integer in 1..65535",
        ) from exc
    if not (1 <= port_int <= 65535):
        raise EnvFileValidationError(
            path=bridge_env_path,
            key="DALTON_BRIDGE_PORT",
            reason="must be in range 1..65535",
        )

    _validate_no_control_or_whitespace(
        path=bridge_env_path,
        key="DALTON_BRIDGE_TOKEN",
        value=str(bridge_eff.get("DALTON_BRIDGE_TOKEN") or "").strip(),
    )
    _validate_no_control_or_whitespace(
        path=bridge_env_path,
        key="DALTON_BRIDGE_ADMIN_TOKEN",
        value=str(bridge_eff.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip(),
    )

    eff_research_code = str(bridge_eff.get("DALTON_RESEARCH_CODE") or "").strip()
    if eff_research_code:
        _validate_no_control_or_whitespace(
            path=bridge_env_path,
            key="DALTON_RESEARCH_CODE",
            value=eff_research_code,
            allow_spaces=True,
        )

    eff_research_home = str(bridge_eff.get("DALTON_RESEARCH_HOME") or "").strip()
    if eff_research_home:
        _validate_no_control_or_whitespace(
            path=bridge_env_path,
            key="DALTON_RESEARCH_HOME",
            value=eff_research_home,
            allow_spaces=True,
        )

    eff_python = str(bridge_eff.get("DALTON_OCW_PYTHON") or "").strip()
    _validate_no_control_or_whitespace(
        path=bridge_env_path,
        key="DALTON_OCW_PYTHON",
        value=eff_python,
        allow_spaces=True,
    )
    py_path = Path(eff_python).expanduser()
    if not (py_path.is_file() and os.access(py_path, os.X_OK)):
        raise EnvFileValidationError(
            path=bridge_env_path,
            key="DALTON_OCW_PYTHON",
            reason="executable Python binary does not exist or is not executable",
        )

    for int_key in ("DALTON_BRIDGE_MAX_INFLIGHT", "DALTON_BRIDGE_CALLER_BUDGET_S"):
        raw_iv = str(bridge_eff.get(int_key) or "").strip()
        try:
            iv = int(raw_iv)
        except ValueError as exc:
            raise EnvFileValidationError(
                path=bridge_env_path,
                key=int_key,
                reason="must be a positive integer",
            ) from exc
        if iv < 1:
            raise EnvFileValidationError(
                path=bridge_env_path,
                key=int_key,
                reason="must be >= 1",
            )

    eff_default_host_platform = str(default_host_platform or "macos").strip().lower()
    if eff_default_host_platform not in VALID_HOST_PLATFORMS:
        raise EnvFileValidationError(
            path=repo_env_path,
            key="DALTON_HOST_PLATFORM",
            reason="must be 'macos' or 'linux'",
        )

    repo_defaults: list[tuple[str, str]] = [
        ("DALTON_BRIDGE_TOKEN", str(bridge_eff["DALTON_BRIDGE_TOKEN"]).strip()),
        ("DALTON_BRIDGE_ADMIN_TOKEN", str(bridge_eff["DALTON_BRIDGE_ADMIN_TOKEN"]).strip()),
        ("DALTON_RESEARCHDATA_URL", f"http://host.docker.internal:{port_int}/mcp"),
        ("DALTON_HOST_PLATFORM", eff_default_host_platform),
        ("SEC_USER_AGENT", "DaltonResearch research@dalton.local"),
    ]
    repo_rendered, repo_eff = _apply_env_defaults(repo_entries, repo_vals, repo_defaults)

    eff_host_platform = str(repo_eff.get("DALTON_HOST_PLATFORM") or "").strip().lower()
    if eff_host_platform not in VALID_HOST_PLATFORMS:
        raise EnvFileValidationError(
            path=repo_env_path,
            key="DALTON_HOST_PLATFORM",
            reason="must be 'macos' or 'linux'",
        )

    _validate_no_control_or_whitespace(
        path=repo_env_path,
        key="DALTON_BRIDGE_TOKEN",
        value=str(repo_eff.get("DALTON_BRIDGE_TOKEN") or "").strip(),
    )
    _validate_no_control_or_whitespace(
        path=repo_env_path,
        key="DALTON_BRIDGE_ADMIN_TOKEN",
        value=str(repo_eff.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip(),
    )
    eff_rd_url = str(repo_eff.get("DALTON_RESEARCHDATA_URL") or "").strip()
    _validate_no_control_or_whitespace(
        path=repo_env_path,
        key="DALTON_RESEARCHDATA_URL",
        value=eff_rd_url,
    )
    parsed_rd = urlparse(eff_rd_url)
    if parsed_rd.scheme not in ("http", "https") or not parsed_rd.netloc:
        raise EnvFileValidationError(
            path=repo_env_path,
            key="DALTON_RESEARCHDATA_URL",
            reason="must be a valid http(s) URL",
        )
    _validate_no_control_or_whitespace(
        path=repo_env_path,
        key="SEC_USER_AGENT",
        value=str(repo_eff.get("SEC_USER_AGENT") or "").strip(),
        allow_spaces=True,
    )

    atomic_write_text(bridge_env_path, bridge_rendered, mode=0o600, parent_mode=0o700)
    atomic_write_text(repo_env_path, repo_rendered, mode=0o600)

    return {
        "port": port_int,
        "research_code": eff_research_code,
        "research_home": eff_research_home,
        "python_bin": str(py_path),
        "research_runtime_mode": "new",
    }


_PLIST_PLACEHOLDER_RE = re.compile(r"__[A-Z][A-Z0-9_]*__")
_DEFAULT_OWNERSHIP_REL_PATH = Path(
    "var/dalton/cutover-snapshots/research-host/handoff-journal.json"
)


def _validate_rendered_plist(rendered: str, *, template_path: Path) -> None:
    leftover = sorted(set(_PLIST_PLACEHOLDER_RE.findall(rendered)))
    if leftover:
        raise EnvFileValidationError(
            path=template_path,
            reason=f"unreplaced template placeholder(s): {', '.join(leftover)}",
        )
    try:
        plistlib.loads(rendered.encode("utf-8"))
    except Exception as exc:
        raise EnvFileValidationError(
            path=template_path,
            reason=f"rendered plist failed plistlib validation: {exc}",
        ) from exc


def _resolve_repo_venv_python(repo_root: Path) -> Path:
    """Return `<repo>/.venv/bin/python` without dereferencing the venv symlink."""
    venv_py = repo_root.expanduser().resolve() / ".venv" / "bin" / "python"
    if not (venv_py.is_file() and os.access(venv_py, os.X_OK)):
        raise EnvFileValidationError(
            path=venv_py,
            reason="repository virtualenv Python not found or not executable (run `uv sync --frozen` first)",
        )
    return venv_py


def _resolve_abs_without_symlink_deref(path: Path) -> Path:
    return Path(os.path.abspath(str(path.expanduser())))


_SHELL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHELL_ASSIGN_LINE_RE = re.compile(r"^(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_SHELL_UNQUOTED_META_CHARS: frozenset[str] = frozenset({";", "|", "&", "<", ">", "(", ")", "`"})
_KNOWN_PROXY_OPTIONAL_ENV_KEYS: frozenset[str] = frozenset(
    {
        "DALTON_RESEARCH_HOME",
        "DALTON_OCW_ROOT",
        "GUIDEPOINT_ENV_FILE",
        "GUIDEPOINT_PYTHON",
        "GUIDEPOINT_PROXY_PORT",
        "GUIDEPOINT_TOKENS_FILE",
        "DALTON_GUIDEPOINT_TOKENS_FILE",
        "DALTON_GUIDEPOINT_OWNERSHIP_FILE",
        "DALTON_HOST_OWNERSHIP_FILE",
        "GUIDEPOINT_PROXY_ADMIN_TOKEN",
        "GUIDEPOINT_UPSTREAM_URL",
        "GUIDEPOINT_TOKEN_URL",
    }
)


class StageAPlistRollbackError(OSError):
    """Raised when Stage A plist commit fails AND rollback cannot fully restore targets."""

    def __init__(
        self,
        *,
        backup_dir: Path,
        affected_files: Sequence[Path],
        commit_error: BaseException,
        rollback_errors: Sequence[str],
        err_no: int | None = None,
    ) -> None:
        self.backup_dir = backup_dir
        self.affected_files = tuple(affected_files)
        self.commit_error = commit_error
        self.rollback_errors = tuple(rollback_errors)
        affected_text = ", ".join(str(p) for p in self.affected_files) or "(directory fsync)"
        rb_text = "; ".join(self.rollback_errors)
        commit_desc = f"{type(commit_error).__name__}: {commit_error}"
        msg = (
            f"Stage A plist commit failed ({commit_desc}) AND rollback was incomplete "
            f"for [{affected_text}] ({rb_text}); original backups preserved at {backup_dir}"
        )
        super().__init__(err_no if err_no is not None else errno.EIO, msg)


def _expand_shell_dollar(
    raw: str,
    idx: int,
    env: Mapping[str, str],
    *,
    path: Path,
    lineno: int,
    key: str,
) -> tuple[str, int]:
    """Expand `$VAR`, `${VAR}`, `${VAR:-default}`, or `${VAR-default}` at `raw[idx] == '$'`.

    Rejects command substitution `$(...)`, arithmetic `$((...))`, unsupported parameter
    operators, special shell parameters, and any undefined/indeterminate variable reference
    so unparseable or runtime-dependent shell syntax fails closed.
    """
    n = len(raw)
    if idx + 1 >= n:
        return "$", idx + 1
    nxt = raw[idx + 1]
    if nxt == "(":
        raise EnvFileValidationError(
            path=path,
            key=key,
            reason=f"line {lineno}: command/arithmetic substitution '$(' is not reliably parseable",
        )
    if nxt == "{":
        # Find matching '}' respecting nested single/double quotes and nested ${...}
        depth = 1
        pos = idx + 2
        in_sq = False
        in_dq = False
        while pos < n and depth > 0:
            ch = raw[pos]
            if in_sq:
                if ch == "'":
                    in_sq = False
                pos += 1
                continue
            if in_dq:
                if ch == "\\" and pos + 1 < n:
                    pos += 2
                    continue
                if ch == '"':
                    in_dq = False
                pos += 1
                continue
            if ch == "\\" and pos + 1 < n:
                pos += 2
                continue
            if ch == "'":
                in_sq = True
                pos += 1
                continue
            if ch == '"':
                in_dq = True
                pos += 1
                continue
            if ch == "$" and pos + 1 < n and raw[pos + 1] == "{":
                depth += 1
                pos += 2
                continue
            if ch == "}":
                depth -= 1
                if depth == 0:
                    break
                pos += 1
                continue
            pos += 1
        if depth != 0:
            raise EnvFileValidationError(
                path=path,
                key=key,
                reason=f"line {lineno}: unclosed '${{...}}' parameter expansion",
            )
        inner = raw[idx + 2 : pos]
        if not inner:
            raise EnvFileValidationError(
                path=path,
                key=key,
                reason=f"line {lineno}: empty '${{}}' parameter expansion",
            )
        if _SHELL_IDENT_RE.fullmatch(inner):
            if inner not in env:
                raise EnvFileValidationError(
                    path=path,
                    key=key,
                    reason=(
                        f"line {lineno}: cannot deterministically resolve undefined/runtime "
                        f"variable '${{{inner}}}' in sourced env file"
                    ),
                )
            return str(env[inner]), pos + 1
        m_def = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(:-|-)([\s\S]*)$", inner)
        if not m_def:
            raise EnvFileValidationError(
                path=path,
                key=key,
                reason=f"line {lineno}: unsupported parameter expansion syntax in '${{...}}'",
            )
        var_name, op, default_expr = m_def.group(1), m_def.group(2), m_def.group(3)
        if op == ":-":
            if var_name in env and env[var_name] != "":
                return str(env[var_name]), pos + 1
        else:
            if var_name in env:
                return str(env[var_name]), pos + 1
        if var_name not in env and var_name not in _KNOWN_PROXY_OPTIONAL_ENV_KEYS:
            raise EnvFileValidationError(
                path=path,
                key=key,
                reason=(
                    f"line {lineno}: cannot deterministically resolve unknown variable "
                    f"'${{{var_name}{op}...}}' in sourced env file"
                ),
            )
        expanded_default = _parse_shell_rhs_value(
            default_expr,
            env,
            path=path,
            lineno=lineno,
            key=key,
            allow_inline_comment=False,
        )
        return expanded_default, pos + 1

    if nxt.isalpha() or nxt == "_":
        end = idx + 2
        while end < n and (raw[end].isalnum() or raw[end] == "_"):
            end += 1
        var_name = raw[idx + 1 : end]
        if var_name not in env:
            raise EnvFileValidationError(
                path=path,
                key=key,
                reason=(
                    f"line {lineno}: cannot deterministically resolve undefined/runtime "
                    f"variable '${var_name}' in sourced env file"
                ),
            )
        return str(env[var_name]), end

    if nxt in ("'", '"', " ", "\t"):
        return "$", idx + 1

    raise EnvFileValidationError(
        path=path,
        key=key,
        reason=f"line {lineno}: unsupported shell parameter expansion '${nxt}'",
    )


def _parse_shell_rhs_value(
    rhs: str,
    env: Mapping[str, str],
    *,
    path: Path,
    lineno: int,
    key: str,
    allow_inline_comment: bool = True,
) -> str:
    """Parse a single bash `source` assignment RHS with quotes, `$VAR`/`${VAR}`, and inline `#` comments."""
    out: list[str] = []
    n = len(rhs)
    i = 0

    # Leading unquoted tilde expansion (`~` or `~/...`)
    if rhs == "~" or rhs.startswith("~/"):
        home_val = str(env.get("HOME") or "")
        if not home_val:
            raise EnvFileValidationError(
                path=path,
                key=key,
                reason=f"line {lineno}: cannot expand '~' without HOME",
            )
        out.append(home_val)
        i = 1
    elif rhs.startswith("~"):
        raise EnvFileValidationError(
            path=path,
            key=key,
            reason=f"line {lineno}: unsupported user-specific tilde expansion",
        )

    while i < n:
        ch = rhs[i]
        if ch == "'":
            end = rhs.find("'", i + 1)
            if end < 0:
                raise EnvFileValidationError(
                    path=path,
                    key=key,
                    reason=f"line {lineno}: unclosed single quote",
                )
            out.append(rhs[i + 1 : end])
            i = end + 1
            continue

        if ch == '"':
            i += 1
            closed = False
            while i < n:
                c = rhs[i]
                if c == '"':
                    closed = True
                    i += 1
                    break
                if c == "\\":
                    if i + 1 >= n:
                        raise EnvFileValidationError(
                            path=path,
                            key=key,
                            reason=f"line {lineno}: trailing backslash inside double quote",
                        )
                    nxt = rhs[i + 1]
                    if nxt in ('"', "\\", "$", "`"):
                        out.append(nxt)
                    else:
                        out.append("\\" + nxt)
                    i += 2
                    continue
                if c == "`":
                    raise EnvFileValidationError(
                        path=path,
                        key=key,
                        reason=f"line {lineno}: backtick command substitution is not reliably parseable",
                    )
                if c == "$":
                    val, i = _expand_shell_dollar(rhs, i, env, path=path, lineno=lineno, key=key)
                    out.append(val)
                    continue
                out.append(c)
                i += 1
            if not closed:
                raise EnvFileValidationError(
                    path=path,
                    key=key,
                    reason=f"line {lineno}: unclosed double quote",
                )
            continue

        if ch in (" ", "\t"):
            rest = rhs[i:].lstrip(" \t")
            if not rest:
                break
            if allow_inline_comment and rest.startswith("#"):
                break
            raise EnvFileValidationError(
                path=path,
                key=key,
                reason=f"line {lineno}: unquoted whitespace / trailing tokens in sourced shell assignment",
            )

        if ch == "\\":
            if i + 1 >= n:
                raise EnvFileValidationError(
                    path=path,
                    key=key,
                    reason=f"line {lineno}: multiline line continuation is not reliably parseable",
                )
            out.append(rhs[i + 1])
            i += 2
            continue

        if ch in _SHELL_UNQUOTED_META_CHARS:
            raise EnvFileValidationError(
                path=path,
                key=key,
                reason=f"line {lineno}: unsupported shell metacharacter '{ch}' in sourced env file",
            )

        if ch == "$":
            val, i = _expand_shell_dollar(rhs, i, env, path=path, lineno=lineno, key=key)
            out.append(val)
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def parse_sourced_shell_env_strict(
    path: Path,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Strictly parse a bash-sourced env file (e.g. `guidepoint_mcp.env`) in real `source` order.

    Supports `KEY=VALUE` / `export KEY=VALUE`, single/double quotes, `$VAR` / `${VAR}` /
    `${VAR:-default}` / `${VAR-default}` expansion using inherited + previously assigned
    variables, `~/` expansion, and inline `#` comments preceded by unquoted whitespace.
    Rejects any shell construct that cannot be reliably evaluated statically (command
    substitution, conditionals, `source`, multiline continuations, unclosed quotes, etc.).
    """
    validate_safe_regular_file_target(path, allow_missing=False)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EnvFileValidationError(
            path=path,
            reason=f"failed reading sourced shell env file ({type(exc).__name__})",
        ) from exc

    env: dict[str, str] = {str(k): str(v) for k, v in (base_env or {}).items()}
    assigned: dict[str, str] = {}

    for lineno, raw_line in enumerate(raw_text.splitlines(), start=1):
        line = raw_line.rstrip("\r")
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        m = _SHELL_ASSIGN_LINE_RE.match(stripped)
        if not m:
            raise EnvFileValidationError(
                path=path,
                reason=f"line {lineno}: unsupported non-assignment shell syntax in sourced env file",
            )
        key, rhs = m.group(1), m.group(2)
        val = _parse_shell_rhs_value(
            rhs,
            env,
            path=path,
            lineno=lineno,
            key=key,
            allow_inline_comment=True,
        )
        env[key] = val
        assigned[key] = val

    return assigned


def _read_plist_launch_spec_strict(plist_path: Path) -> tuple[dict[str, str], list[str]]:
    """Strictly read `EnvironmentVariables` and `ProgramArguments` from a launchd plist."""
    validate_safe_regular_file_target(plist_path, allow_missing=False)
    try:
        parsed = plistlib.loads(plist_path.read_bytes())
    except Exception as exc:
        raise EnvFileValidationError(
            path=plist_path,
            reason=f"failed parsing plist ({type(exc).__name__})",
        ) from exc
    if not isinstance(parsed, Mapping):
        raise EnvFileValidationError(path=plist_path, reason="plist root is not a dict")
    env_map = parsed.get("EnvironmentVariables")
    if env_map is None:
        env_vars: dict[str, str] = {}
    elif not isinstance(env_map, Mapping):
        raise EnvFileValidationError(
            path=plist_path,
            key="EnvironmentVariables",
            reason="EnvironmentVariables is not a dict",
        )
    else:
        env_vars = {str(k): str(v) for k, v in env_map.items() if isinstance(k, str) and v is not None}

    raw_args = parsed.get("ProgramArguments")
    if raw_args is None:
        prog_args: list[str] = []
    elif not isinstance(raw_args, list) or not all(isinstance(item, str) for item in raw_args):
        raise EnvFileValidationError(
            path=plist_path,
            key="ProgramArguments",
            reason="ProgramArguments is not a list of strings",
        )
    else:
        prog_args = list(raw_args)
    return env_vars, prog_args


def _read_plist_env_vars_strict(plist_path: Path) -> dict[str, str]:
    """Strictly read `EnvironmentVariables` from an existing launchd plist (fail closed on corruption)."""
    env_vars, _ = _read_plist_launch_spec_strict(plist_path)
    return env_vars


def _extract_launcher_script_dir(
    program_args: Sequence[str],
    *,
    existing_plist_found: bool,
    repo_abs: Path,
) -> str | None:
    """Resolve `SCRIPT_DIR` matching `guidepoint_mcp_proxy_run.sh` (`dirname ${BASH_SOURCE[0]}`)."""
    if not existing_plist_found:
        return str(repo_abs / "scripts" / "dalton" / "research_runtime" / "scripts")
    if not program_args:
        return None
    idx = 0
    if Path(program_args[0]).name == "env":
        idx = 1
        while idx < len(program_args):
            tok = program_args[idx]
            if tok == "--":
                idx += 1
                break
            if tok.startswith("-") or ("=" in tok and not tok.startswith("/")):
                idx += 1
                continue
            break
    if idx >= len(program_args):
        return None
    cmd = program_args[idx]
    script_arg: str | None = None
    if Path(cmd).name in ("bash", "sh", "zsh"):
        j = idx + 1
        while j < len(program_args):
            tok = program_args[j]
            if tok == "--":
                j += 1
                break
            if tok == "-c" or tok.startswith("-c"):
                return None
            if tok in ("-o", "+o"):
                j += 2
                continue
            if tok.startswith("-") or tok.startswith("+"):
                j += 1
                continue
            break
        if j < len(program_args):
            script_arg = program_args[j]
    else:
        script_arg = cmd
    if not script_arg:
        return None
    sp = Path(script_arg)
    if not sp.is_absolute():
        return None
    return str(Path(os.path.abspath(str(sp))).parent)


def _require_abs_proxy_path(raw: str, *, path: Path, key: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        raise EnvFileValidationError(
            path=path,
            key=key,
            reason=f"resolved path {raw!r} is not absolute and cannot be deterministically resolved",
        )
    return Path(os.path.abspath(str(p)))


def resolve_legacy_proxy_effective_paths(
    *,
    repo_root: Path,
    home_dir: Path,
    existing_proxy_plist_path: Path | None = None,
    bridge_env_vals: Mapping[str, str] | None = None,
    guidepoint_env_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve legacy `com.dalton.guidepoint-mcp-proxy` effective paths in real runtime order.

    Single source of truth shared by Stage A rendering, post-render verification, and `preflight.py`.
    Actual launcher order (`guidepoint_mcp_proxy_run.sh` + `guidepoint_lock.py`):
      1. Inherit `EnvironmentVariables` from `com.dalton.guidepoint-mcp-proxy.plist`.
      2. Initialize launcher shell variables before `source "$ENV_FILE"`:
         - `SCRIPT_DIR` (directory of the launcher script)
         - `RESEARCH_HOME` (`DALTON_RESEARCH_HOME` > `DALTON_OCW_ROOT` > `$HOME/Library/Application Support/Everflow/research`)
         - `ENV_FILE` (`GUIDEPOINT_ENV_FILE` > `$RESEARCH_HOME/config/guidepoint_mcp.env`)
         - `PYTHON_BIN` (`GUIDEPOINT_PYTHON` > `/opt/homebrew/bin/python3.12`)
      3. `source "$ENV_FILE"` (overrides any keys inherited from the plist, with variable
         expansion against plist + launcher variables and fail-closed on undefined/unknown vars).
      4. Resolve paths via `guidepoint_lock.py` priority:
         - tokens: `GUIDEPOINT_TOKENS_FILE` > `DALTON_GUIDEPOINT_TOKENS_FILE` > `$RESEARCH_HOME/config/guidepoint_mcp_tokens.json`
         - ownership: `DALTON_GUIDEPOINT_OWNERSHIP_FILE` > `DALTON_HOST_OWNERSHIP_FILE` > canonical repo journal.
    """
    repo_abs = repo_root.expanduser().resolve()
    home_abs = home_dir.expanduser().resolve()
    bvals = dict(bridge_env_vals or {})

    plist_env: dict[str, str] = {}
    plist_prog_args: list[str] = []
    existing_plist_found = False
    plist_src_path = existing_proxy_plist_path.expanduser() if existing_proxy_plist_path is not None else repo_abs
    if existing_proxy_plist_path is not None:
        p = existing_proxy_plist_path.expanduser()
        if p.exists() or p.is_symlink():
            existing_plist_found = True
            plist_env, plist_prog_args = _read_plist_launch_spec_strict(p)

    eff_env: dict[str, str] = {"HOME": str(home_abs)}
    eff_env.update(plist_env)
    launcher_home = _require_abs_proxy_path(
        str(eff_env.get("HOME") or str(home_abs)),
        path=plist_src_path,
        key="HOME",
    )

    # Match `guidepoint_mcp_proxy_run.sh`: when an existing plist is present, launcher only reads
    # plist env (`DALTON_RESEARCH_HOME` > `DALTON_OCW_ROOT` > default); bridge_env_vals is only a
    # fallback default when rendering fresh without an existing plist.
    if existing_plist_found:
        raw_rh = (
            str(eff_env.get("DALTON_RESEARCH_HOME") or "").strip()
            or str(eff_env.get("DALTON_OCW_ROOT") or "").strip()
        )
    else:
        raw_rh = (
            str(eff_env.get("DALTON_RESEARCH_HOME") or "").strip()
            or str(eff_env.get("DALTON_OCW_ROOT") or "").strip()
            or str(bvals.get("DALTON_RESEARCH_HOME") or "").strip()
        )
    research_home_abs = (
        _require_abs_proxy_path(raw_rh, path=plist_src_path, key="DALTON_RESEARCH_HOME")
        if raw_rh
        else (launcher_home / "Library" / "Application Support" / "Everflow" / "research")
    )

    if guidepoint_env_path is not None:
        gp_env_abs = _resolve_abs_without_symlink_deref(guidepoint_env_path)
    elif str(eff_env.get("GUIDEPOINT_ENV_FILE") or "").strip():
        gp_env_abs = _require_abs_proxy_path(
            str(eff_env["GUIDEPOINT_ENV_FILE"]).strip(),
            path=plist_src_path,
            key="GUIDEPOINT_ENV_FILE",
        )
    else:
        gp_env_abs = research_home_abs / "config" / "guidepoint_mcp.env"

    python_bin_val = str(eff_env.get("GUIDEPOINT_PYTHON") or "").strip() or "/opt/homebrew/bin/python3.12"
    script_dir_val = _extract_launcher_script_dir(
        plist_prog_args,
        existing_plist_found=existing_plist_found,
        repo_abs=repo_abs,
    )

    # Launcher shell variables set in `guidepoint_mcp_proxy_run.sh` before `set -a; source "$ENV_FILE"`.
    source_base_env = dict(eff_env)
    source_base_env["RESEARCH_HOME"] = str(research_home_abs)
    source_base_env["ENV_FILE"] = str(gp_env_abs)
    source_base_env["PYTHON_BIN"] = python_bin_val
    if script_dir_val is not None:
        source_base_env["SCRIPT_DIR"] = script_dir_val

    sourced_vals: dict[str, str] = {}
    if gp_env_abs.exists() or gp_env_abs.is_symlink():
        sourced_vals = parse_sourced_shell_env_strict(gp_env_abs, base_env=source_base_env)
        # `source "$ENV_FILE"` overwrites same-named plist variables!
        eff_env.update(sourced_vals)

    if not eff_env.get("DALTON_RESEARCH_HOME") and not eff_env.get("DALTON_OCW_ROOT"):
        eff_env["DALTON_RESEARCH_HOME"] = str(research_home_abs)

    # Effective tokens path follows exact `guidepoint_lock.resolve_guidepoint_tokens_file()` priority:
    # primary `GUIDEPOINT_TOKENS_FILE` ALWAYS beats alias `DALTON_GUIDEPOINT_TOKENS_FILE`, even if
    # primary came from plist and alias came from sourced `guidepoint_mcp.env`.
    final_tokens_raw = (
        str(eff_env.get("GUIDEPOINT_TOKENS_FILE") or "").strip()
        or str(eff_env.get("DALTON_GUIDEPOINT_TOKENS_FILE") or "").strip()
    )
    if final_tokens_raw:
        tokens_abs = _require_abs_proxy_path(
            final_tokens_raw,
            path=gp_env_abs if gp_env_abs.exists() else plist_src_path,
            key="GUIDEPOINT_TOKENS_FILE",
        )
    else:
        final_rh_raw = (
            str(eff_env.get("DALTON_RESEARCH_HOME") or "").strip()
            or str(eff_env.get("DALTON_OCW_ROOT") or "").strip()
        )
        final_rh = (
            _require_abs_proxy_path(
                final_rh_raw,
                path=gp_env_abs if gp_env_abs.exists() else plist_src_path,
                key="DALTON_RESEARCH_HOME",
            )
            if final_rh_raw
            else research_home_abs
        )
        tokens_abs = final_rh / "config" / "guidepoint_mcp_tokens.json"

    final_own_raw = (
        str(eff_env.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "").strip()
        or str(eff_env.get("DALTON_HOST_OWNERSHIP_FILE") or "").strip()
    )
    ownership_abs = (
        _require_abs_proxy_path(
            final_own_raw,
            path=gp_env_abs if gp_env_abs.exists() else plist_src_path,
            key="DALTON_GUIDEPOINT_OWNERSHIP_FILE",
        )
        if final_own_raw
        else (repo_abs / _DEFAULT_OWNERSHIP_REL_PATH)
    )

    sourced_primary_tokens = str(sourced_vals.get("GUIDEPOINT_TOKENS_FILE") or "").strip()
    sourced_alias_tokens = str(sourced_vals.get("DALTON_GUIDEPOINT_TOKENS_FILE") or "").strip()
    sourced_primary_own = str(sourced_vals.get("DALTON_GUIDEPOINT_OWNERSHIP_FILE") or "").strip()
    sourced_alias_own = str(sourced_vals.get("DALTON_HOST_OWNERSHIP_FILE") or "").strip()
    sourced_tokens_raw = sourced_primary_tokens or sourced_alias_tokens
    sourced_ownership_raw = sourced_primary_own or sourced_alias_own

    return {
        "existing_plist_found": existing_plist_found,
        "env_file_exists": gp_env_abs.is_file(),
        "research_home": str(research_home_abs),
        "guidepoint_env_file": str(gp_env_abs),
        "tokens_file": str(tokens_abs),
        "ownership_file": str(ownership_abs),
        "effective_env": eff_env,
        "sourced_vals": sourced_vals,
        "env_file_primary_tokens_override": (
            str(_require_abs_proxy_path(sourced_primary_tokens, path=gp_env_abs, key="GUIDEPOINT_TOKENS_FILE"))
            if sourced_primary_tokens
            else ""
        ),
        "env_file_tokens_override": (
            str(_require_abs_proxy_path(sourced_tokens_raw, path=gp_env_abs, key="GUIDEPOINT_TOKENS_FILE"))
            if sourced_tokens_raw
            else ""
        ),
        "env_file_ownership_override": (
            str(
                _require_abs_proxy_path(
                    sourced_ownership_raw,
                    path=gp_env_abs,
                    key="DALTON_GUIDEPOINT_OWNERSHIP_FILE",
                )
            )
            if sourced_ownership_raw
            else ""
        ),
        "plist_tokens_override": (
            str(
                _require_abs_proxy_path(
                    str(plist_env.get("GUIDEPOINT_TOKENS_FILE") or "").strip()
                    or str(plist_env.get("DALTON_GUIDEPOINT_TOKENS_FILE") or "").strip(),
                    path=plist_src_path,
                    key="GUIDEPOINT_TOKENS_FILE",
                )
            )
            if (
                str(plist_env.get("GUIDEPOINT_TOKENS_FILE") or "").strip()
                or str(plist_env.get("DALTON_GUIDEPOINT_TOKENS_FILE") or "").strip()
            )
            else ""
        ),
    }


def _build_bridge_plist_xml(
    *,
    template_path: Path,
    bridge_env_path: Path,
    repo_root: Path,
    home_dir: Path,
    logs_dir: Path | None = None,
    repo_env_path: Path | None = None,
    ownership_file_path: Path | None = None,
    research_runtime: str = "new",
) -> tuple[str, int, Path]:
    if research_runtime not in ("auto", "new"):
        raise ValueError(f"unsupported research_runtime mode: {research_runtime!r}")
    validate_safe_regular_file_target(bridge_env_path, allow_missing=False)
    validate_safe_regular_file_target(template_path, allow_missing=False)
    _, bridge_vals = parse_env_file_strict(bridge_env_path)

    repo_abs = repo_root.expanduser().resolve()
    home_abs = home_dir.expanduser().resolve()
    bridge_env_abs = _resolve_abs_without_symlink_deref(bridge_env_path)
    repo_env_abs = (
        _resolve_abs_without_symlink_deref(repo_env_path)
        if repo_env_path is not None
        else (repo_abs / ".env")
    )
    ownership_abs = (
        _resolve_abs_without_symlink_deref(ownership_file_path)
        if ownership_file_path is not None
        else (repo_abs / _DEFAULT_OWNERSHIP_REL_PATH)
    )

    port_int = int(str(bridge_vals.get("DALTON_BRIDGE_PORT") or "8960").strip())
    env_research_code = str(bridge_vals.get("DALTON_RESEARCH_CODE") or "").strip()
    env_research_home = str(bridge_vals.get("DALTON_RESEARCH_HOME") or "").strip()

    research_code = (
        str(_resolve_abs_without_symlink_deref(Path(env_research_code)))
        if env_research_code
        else str(repo_abs / "scripts" / "dalton" / "research_runtime")
    )
    research_home = (
        str(_resolve_abs_without_symlink_deref(Path(env_research_home)))
        if env_research_home
        else str(home_abs / "Library" / "Application Support" / "Everflow" / "research")
    )
    python_bin = str(_resolve_repo_venv_python(repo_abs))
    tpl = template_path.read_text(encoding="utf-8")
    eff_logs = (
        _resolve_abs_without_symlink_deref(logs_dir)
        if logs_dir is not None
        else (home_abs / "Library" / "Logs")
    )

    rendered = (
        tpl.replace(
            "__HOME__/.config/dalton/researchdata-bridge.env",
            xml_escape(str(bridge_env_abs)),
        )
        .replace("__HOME__/Library/Logs", xml_escape(str(eff_logs)))
        .replace("__PYTHON_BIN__", xml_escape(python_bin))
        .replace("__REPO_ROOT__", xml_escape(str(repo_abs)))
        .replace("__REPO_ENV_FILE__", xml_escape(str(repo_env_abs)))
        .replace("__GUIDEPOINT_OWNERSHIP_FILE__", xml_escape(str(ownership_abs)))
        .replace("__RESEARCH_CODE__", xml_escape(research_code))
        .replace("__RESEARCH_HOME__", xml_escape(research_home))
        .replace("__HOME__", xml_escape(str(home_abs)))
    )

    _validate_rendered_plist(rendered, template_path=template_path)
    return rendered, port_int, eff_logs


def render_plist_atomic(
    *,
    template_path: Path,
    dest_path: Path,
    bridge_env_path: Path,
    repo_root: Path,
    home_dir: Path,
    logs_dir: Path | None = None,
    repo_env_path: Path | None = None,
    ownership_file_path: Path | None = None,
    research_runtime: str = "new",
    create_logs_dir: bool = True,
) -> int:
    """Render bridge launchd plist atomically (`0644` + fsync) using `<repo>/.venv/bin/python`."""
    validate_safe_regular_file_target(dest_path, allow_missing=True)
    rendered, port_int, eff_logs = _build_bridge_plist_xml(
        template_path=template_path,
        bridge_env_path=bridge_env_path,
        repo_root=repo_root,
        home_dir=home_dir,
        logs_dir=logs_dir,
        repo_env_path=repo_env_path,
        ownership_file_path=ownership_file_path,
        research_runtime=research_runtime,
    )
    if create_logs_dir:
        validate_safe_directory_target(eff_logs)
        eff_logs.mkdir(parents=True, exist_ok=True)
        validate_safe_directory_target(eff_logs)
    atomic_write_text(dest_path, rendered, mode=0o644)
    return port_int


def _build_research_host_plist_xml(
    *,
    template_path: Path,
    repo_root: Path,
    home_dir: Path,
    research_host_env_path: Path | None = None,
    repo_env_path: Path | None = None,
    logs_dir: Path | None = None,
    research_code: Path | str | None = None,
    research_home: Path | str | None = None,
    ownership_file_path: Path | None = None,
    tokens_file_path: Path | None = None,
) -> tuple[str, dict[str, str], Path]:
    validate_safe_regular_file_target(template_path, allow_missing=False)

    repo_abs = repo_root.expanduser().resolve()
    home_abs = home_dir.expanduser().resolve()
    host_env_abs = (
        _resolve_abs_without_symlink_deref(research_host_env_path)
        if research_host_env_path is not None
        else (home_abs / ".config" / "dalton" / "research-host.env")
    )
    host_vals: dict[str, str] = {}
    if host_env_abs.is_file():
        validate_safe_regular_file_target(host_env_abs, allow_missing=False)
        _, host_vals = parse_env_file_strict(host_env_abs)

    repo_env_abs = (
        _resolve_abs_without_symlink_deref(repo_env_path)
        if repo_env_path is not None
        else (repo_abs / ".env")
    )
    eff_research_code = (
        str(_resolve_abs_without_symlink_deref(Path(research_code)))
        if research_code is not None and str(research_code).strip()
        else str(
            _resolve_abs_without_symlink_deref(Path(host_vals["DALTON_RESEARCH_CODE"]))
            if str(host_vals.get("DALTON_RESEARCH_CODE") or "").strip()
            else (repo_abs / "scripts" / "dalton" / "research_runtime")
        )
    )
    eff_research_home = (
        str(_resolve_abs_without_symlink_deref(Path(research_home)))
        if research_home is not None and str(research_home).strip()
        else str(
            _resolve_abs_without_symlink_deref(Path(host_vals["DALTON_RESEARCH_HOME"]))
            if str(host_vals.get("DALTON_RESEARCH_HOME") or "").strip()
            else (home_abs / "Library" / "Application Support" / "Everflow" / "research")
        )
    )
    ownership_abs = (
        _resolve_abs_without_symlink_deref(ownership_file_path)
        if ownership_file_path is not None
        else (repo_abs / _DEFAULT_OWNERSHIP_REL_PATH)
    )
    raw_tokens = (
        str(tokens_file_path)
        if tokens_file_path is not None and str(tokens_file_path).strip()
        else (
            str(host_vals.get("GUIDEPOINT_TOKENS_FILE") or "").strip()
            or str(host_vals.get("DALTON_GUIDEPOINT_TOKENS_FILE") or "").strip()
        )
    )
    tokens_abs = (
        _resolve_abs_without_symlink_deref(Path(raw_tokens))
        if raw_tokens
        else (Path(eff_research_home) / "config" / "guidepoint_mcp_tokens.json")
    )
    eff_logs = (
        _resolve_abs_without_symlink_deref(logs_dir)
        if logs_dir is not None
        else (home_abs / "Library" / "Logs")
    )

    python_bin = str(_resolve_repo_venv_python(repo_abs))
    tpl = template_path.read_text(encoding="utf-8")
    rendered = (
        tpl.replace("__PYTHON_BIN__", xml_escape(python_bin))
        .replace("__RESEARCH_HOST_ENV_FILE__", xml_escape(str(host_env_abs)))
        .replace("__REPO_ENV_FILE__", xml_escape(str(repo_env_abs)))
        .replace("__REPO__", xml_escape(str(repo_abs)))
        .replace("__HOME__", xml_escape(str(home_abs)))
        .replace("__RESEARCH_CODE__", xml_escape(eff_research_code))
        .replace("__RESEARCH_HOME__", xml_escape(eff_research_home))
        .replace("__GUIDEPOINT_OWNERSHIP_FILE__", xml_escape(str(ownership_abs)))
        .replace("__GUIDEPOINT_TOKENS_FILE__", xml_escape(str(tokens_abs)))
        .replace("__LOG_DIR__", xml_escape(str(eff_logs)))
    )

    _validate_rendered_plist(rendered, template_path=template_path)
    meta = {
        "python_bin": python_bin,
        "ownership_file": str(ownership_abs),
        "tokens_file": str(tokens_abs),
    }
    return rendered, meta, eff_logs


def render_research_host_plist_atomic(
    *,
    template_path: Path,
    dest_path: Path,
    repo_root: Path,
    home_dir: Path,
    research_host_env_path: Path | None = None,
    repo_env_path: Path | None = None,
    logs_dir: Path | None = None,
    research_code: Path | str | None = None,
    research_home: Path | str | None = None,
    ownership_file_path: Path | None = None,
    tokens_file_path: Path | None = None,
    create_logs_dir: bool = False,
) -> dict[str, str]:
    """Render `com.dalton.research-host.plist` atomically (`0644` + fsync) with literal absolute paths."""
    validate_safe_regular_file_target(dest_path, allow_missing=True)
    rendered, meta, eff_logs = _build_research_host_plist_xml(
        template_path=template_path,
        repo_root=repo_root,
        home_dir=home_dir,
        research_host_env_path=research_host_env_path,
        repo_env_path=repo_env_path,
        logs_dir=logs_dir,
        research_code=research_code,
        research_home=research_home,
        ownership_file_path=ownership_file_path,
        tokens_file_path=tokens_file_path,
    )
    if create_logs_dir:
        validate_safe_directory_target(eff_logs)
        eff_logs.mkdir(parents=True, exist_ok=True)
        validate_safe_directory_target(eff_logs)
    atomic_write_text(dest_path, rendered, mode=0o644)
    return {
        "dest": str(dest_path),
        **meta,
    }


def _build_launchd_service_plist_xml(
    *,
    template_path: Path,
    repo_root: Path,
    home_dir: Path,
    logs_dir: Path | None = None,
    runtime_env_key: str = "DALTON_RESEARCH_HOME",
    runtime_env_val: str | None = None,
    node_bin: str | None = None,
    python_bin: Path | str | None = None,
    guidepoint_env_path: Path | None = None,
    tokens_file_path: Path | None = None,
    ownership_file_path: Path | None = None,
    bridge_env_path: Path | None = None,
    research_home: Path | str | None = None,
) -> tuple[str, dict[str, str], Path]:
    """Unified LaunchAgent plist template renderer for host services (including guidepoint-mcp-proxy)."""
    validate_safe_regular_file_target(template_path, allow_missing=False)

    repo_abs = repo_root.expanduser().resolve()
    home_abs = home_dir.expanduser().resolve()
    bridge_vals: dict[str, str] = {}
    if bridge_env_path is not None and bridge_env_path.expanduser().is_file():
        validate_safe_regular_file_target(bridge_env_path, allow_missing=False)
        _, bridge_vals = parse_env_file_strict(bridge_env_path)

    if runtime_env_val is not None and str(runtime_env_val).strip():
        eff_research_home = str(_resolve_abs_without_symlink_deref(Path(runtime_env_val)))
    elif research_home is not None and str(research_home).strip():
        eff_research_home = str(_resolve_abs_without_symlink_deref(Path(research_home)))
    elif str(bridge_vals.get("DALTON_RESEARCH_HOME") or "").strip():
        eff_research_home = str(
            _resolve_abs_without_symlink_deref(Path(bridge_vals["DALTON_RESEARCH_HOME"]))
        )
    else:
        eff_research_home = str(home_abs / "Library" / "Application Support" / "Everflow" / "research")

    gp_env_abs = (
        _resolve_abs_without_symlink_deref(guidepoint_env_path)
        if guidepoint_env_path is not None
        else (Path(eff_research_home) / "config" / "guidepoint_mcp.env")
    )
    tokens_abs = (
        _resolve_abs_without_symlink_deref(tokens_file_path)
        if tokens_file_path is not None
        else (Path(eff_research_home) / "config" / "guidepoint_mcp_tokens.json")
    )
    ownership_abs = (
        _resolve_abs_without_symlink_deref(ownership_file_path)
        if ownership_file_path is not None
        else (repo_abs / _DEFAULT_OWNERSHIP_REL_PATH)
    )
    eff_logs = (
        _resolve_abs_without_symlink_deref(logs_dir)
        if logs_dir is not None
        else (home_abs / "Library" / "Logs")
    )

    if python_bin is not None and str(python_bin).strip():
        eff_py = str(_resolve_abs_without_symlink_deref(Path(python_bin)))
    else:
        eff_py = str(_resolve_repo_venv_python(repo_abs))

    eff_node = (
        str(_resolve_abs_without_symlink_deref(Path(node_bin)))
        if node_bin is not None and str(node_bin).strip()
        else "/opt/homebrew/bin/node"
    )

    tpl = template_path.read_text(encoding="utf-8")
    rendered = (
        tpl.replace("__REPO__", xml_escape(str(repo_abs)))
        .replace("__HOME__", xml_escape(str(home_abs)))
        .replace("__LOG_DIR__", xml_escape(str(eff_logs)))
        .replace("__RUNTIME_ENV_KEY__", xml_escape(str(runtime_env_key)))
        .replace("__RUNTIME_ENV_VAL__", xml_escape(eff_research_home))
        .replace("__NODE_BIN__", xml_escape(eff_node))
        .replace("__PYTHON_BIN__", xml_escape(eff_py))
        .replace("__GUIDEPOINT_ENV_FILE__", xml_escape(str(gp_env_abs)))
        .replace("__GUIDEPOINT_TOKENS_FILE__", xml_escape(str(tokens_abs)))
        .replace("__GUIDEPOINT_OWNERSHIP_FILE__", xml_escape(str(ownership_abs)))
    )

    _validate_rendered_plist(rendered, template_path=template_path)
    meta = {
        "python_bin": eff_py,
        "ownership_file": str(ownership_abs),
        "tokens_file": str(tokens_abs),
        "guidepoint_env_file": str(gp_env_abs),
    }
    return rendered, meta, eff_logs


def render_guidepoint_proxy_plist_atomic(
    *,
    template_path: Path,
    dest_path: Path,
    repo_root: Path,
    home_dir: Path,
    bridge_env_path: Path | None = None,
    logs_dir: Path | None = None,
    research_home: Path | str | None = None,
    guidepoint_env_path: Path | None = None,
    tokens_file_path: Path | None = None,
    ownership_file_path: Path | None = None,
    python_bin: Path | str | None = None,
    runtime_env_key: str = "DALTON_RESEARCH_HOME",
    runtime_env_val: str | None = None,
    node_bin: str | None = None,
    create_logs_dir: bool = False,
) -> dict[str, str]:
    """Render a host-service LaunchAgent plist (e.g. `guidepoint-mcp-proxy`) atomically (`0644` + fsync)."""
    validate_safe_regular_file_target(dest_path, allow_missing=True)
    rendered, meta, eff_logs = _build_launchd_service_plist_xml(
        template_path=template_path,
        repo_root=repo_root,
        home_dir=home_dir,
        logs_dir=logs_dir,
        runtime_env_key=runtime_env_key,
        runtime_env_val=runtime_env_val,
        node_bin=node_bin,
        python_bin=python_bin,
        guidepoint_env_path=guidepoint_env_path,
        tokens_file_path=tokens_file_path,
        ownership_file_path=ownership_file_path,
        bridge_env_path=bridge_env_path,
        research_home=research_home,
    )
    if create_logs_dir:
        validate_safe_directory_target(eff_logs)
        eff_logs.mkdir(parents=True, exist_ok=True)
        validate_safe_directory_target(eff_logs)
    atomic_write_text(dest_path, rendered, mode=0o644)
    return {
        "dest": str(dest_path),
        **meta,
    }


def render_stage_a_plists_atomic(
    *,
    output_dir: Path,
    repo_root: Path,
    home_dir: Path,
    bridge_env_path: Path | None = None,
    research_host_env_path: Path | None = None,
    repo_env_path: Path | None = None,
    logs_dir: Path | None = None,
    ownership_file_path: Path | None = None,
    guidepoint_python: Path | str | None = None,
    existing_proxy_plist_path: Path | None = None,
) -> dict[str, str]:
    """Render all three Stage A LaunchAgent plists atomically inside `output_dir` only.

    - Reconciles live legacy proxy paths (plist -> `source guidepoint_mcp.env`) before rendering.
    - Validates all three plists in a staging directory inside `output_dir` with `fsync`
      before committing any of them, and never creates directories outside `output_dir`.
    """
    repo_abs = repo_root.expanduser().resolve()
    home_abs = home_dir.expanduser().resolve()
    out_abs = _resolve_abs_without_symlink_deref(output_dir)
    validate_safe_directory_target(out_abs)
    out_abs.mkdir(parents=True, exist_ok=True)
    validate_safe_directory_target(out_abs)

    tpl_dir = repo_abs / "scripts" / "dalton" / "launchd"
    eff_bridge_env = (
        _resolve_abs_without_symlink_deref(bridge_env_path)
        if bridge_env_path is not None
        else (home_abs / ".config" / "dalton" / "researchdata-bridge.env")
    )
    eff_host_env = (
        _resolve_abs_without_symlink_deref(research_host_env_path)
        if research_host_env_path is not None
        else (home_abs / ".config" / "dalton" / "research-host.env")
    )
    target_ownership_abs = (
        _resolve_abs_without_symlink_deref(ownership_file_path)
        if ownership_file_path is not None
        else (repo_abs / _DEFAULT_OWNERSHIP_REL_PATH)
    )

    bridge_vals: dict[str, str] = {}
    if eff_bridge_env.is_file():
        validate_safe_regular_file_target(eff_bridge_env, allow_missing=False)
        _, bridge_vals = parse_env_file_strict(eff_bridge_env)

    host_vals: dict[str, str] = {}
    if eff_host_env.is_file():
        validate_safe_regular_file_target(eff_host_env, allow_missing=False)
        _, host_vals = parse_env_file_strict(eff_host_env)

    # Reconcile live legacy proxy actual runtime paths (plist -> source guidepoint_mcp.env)
    eff_existing_proxy_plist = (
        _resolve_abs_without_symlink_deref(existing_proxy_plist_path)
        if existing_proxy_plist_path is not None
        else (home_abs / "Library" / "LaunchAgents" / "com.dalton.guidepoint-mcp-proxy.plist")
    )
    live_proxy = resolve_legacy_proxy_effective_paths(
        repo_root=repo_abs,
        home_dir=home_abs,
        existing_proxy_plist_path=eff_existing_proxy_plist,
        bridge_env_vals=bridge_vals,
    )

    # If guidepoint_mcp.env explicitly overrides ownership to a different file, fail closed
    # because `guidepoint_mcp_proxy_run.sh` sources it AFTER the plist environment.
    if live_proxy["env_file_ownership_override"]:
        env_own_resolved = Path(live_proxy["env_file_ownership_override"]).resolve()
        if env_own_resolved != target_ownership_abs.resolve():
            raise EnvFileValidationError(
                path=Path(live_proxy["guidepoint_env_file"]),
                key="DALTON_GUIDEPOINT_OWNERSHIP_FILE",
                reason=(
                    f"sourced guidepoint env overrides ownership file to {env_own_resolved}, "
                    f"conflicting with canonical {target_ownership_abs}"
                ),
            )

    host_explicit_tokens_raw = (
        str(host_vals.get("GUIDEPOINT_TOKENS_FILE") or "").strip()
        or str(host_vals.get("DALTON_GUIDEPOINT_TOKENS_FILE") or "").strip()
    )
    host_explicit_tokens = (
        _resolve_abs_without_symlink_deref(Path(host_explicit_tokens_raw))
        if host_explicit_tokens_raw
        else None
    )
    live_proxy_tokens = _resolve_abs_without_symlink_deref(Path(live_proxy["tokens_file"]))
    live_proxy_has_explicit = bool(
        live_proxy["existing_plist_found"]
        or live_proxy["env_file_tokens_override"]
        or live_proxy["env_file_exists"]
    )

    if host_explicit_tokens is not None and live_proxy_has_explicit:
        if host_explicit_tokens.resolve() != live_proxy_tokens.resolve():
            raise EnvFileValidationError(
                path=eff_host_env,
                key="GUIDEPOINT_TOKENS_FILE",
                reason=(
                    f"research-host tokens path {host_explicit_tokens} conflicts with live "
                    f"legacy proxy effective tokens path {live_proxy_tokens}"
                ),
            )

    # Choose effective tokens path using the EXACT live runtime resolution (`live_proxy["tokens_file"]`)
    # so primary `GUIDEPOINT_TOKENS_FILE` in the existing plist is NEVER silently superseded by a
    # secondary alias `DALTON_GUIDEPOINT_TOKENS_FILE` in `guidepoint_mcp.env`.
    if live_proxy_has_explicit:
        eff_tokens_abs = live_proxy_tokens
    elif host_explicit_tokens is not None:
        eff_tokens_abs = host_explicit_tokens
    else:
        eff_tokens_abs = live_proxy_tokens

    # If `guidepoint_mcp.env` sets primary `GUIDEPOINT_TOKENS_FILE`, verify it won't override
    # our chosen `eff_tokens_abs` on restart (when live_proxy_has_explicit is True they already match).
    if live_proxy["env_file_primary_tokens_override"]:
        env_primary_tok = Path(live_proxy["env_file_primary_tokens_override"]).resolve()
        if env_primary_tok != eff_tokens_abs.resolve():
            raise EnvFileValidationError(
                path=Path(live_proxy["guidepoint_env_file"]),
                key="GUIDEPOINT_TOKENS_FILE",
                reason=(
                    f"sourced guidepoint env overrides GUIDEPOINT_TOKENS_FILE to {env_primary_tok}, "
                    f"conflicting with {eff_tokens_abs}"
                ),
            )

    bridge_dest = out_abs / "com.dalton.researchdata-bridge.plist"
    host_dest = out_abs / "com.dalton.research-host.plist"
    proxy_dest = out_abs / "com.dalton.guidepoint-mcp-proxy.plist"
    for dest in (bridge_dest, host_dest, proxy_dest):
        validate_safe_regular_file_target(dest, allow_missing=True)

    # Phase 1: Render and validate all three XML payloads in memory (zero disk writes yet).
    bridge_xml, _, _ = _build_bridge_plist_xml(
        template_path=tpl_dir / "com.dalton.researchdata-bridge.plist.template",
        bridge_env_path=eff_bridge_env,
        repo_root=repo_abs,
        home_dir=home_abs,
        logs_dir=logs_dir,
        repo_env_path=repo_env_path,
        ownership_file_path=target_ownership_abs,
    )
    host_xml, _, _ = _build_research_host_plist_xml(
        template_path=tpl_dir / "com.dalton.research-host.plist.template",
        repo_root=repo_abs,
        home_dir=home_abs,
        research_host_env_path=eff_host_env,
        repo_env_path=repo_env_path,
        logs_dir=logs_dir,
        ownership_file_path=target_ownership_abs,
        tokens_file_path=eff_tokens_abs,
    )
    proxy_xml, _, _ = _build_launchd_service_plist_xml(
        template_path=tpl_dir / "com.dalton.guidepoint-mcp-proxy.plist.template",
        repo_root=repo_abs,
        home_dir=home_abs,
        bridge_env_path=eff_bridge_env,
        logs_dir=logs_dir,
        research_home=live_proxy["research_home"],
        guidepoint_env_path=Path(live_proxy["guidepoint_env_file"]),
        tokens_file_path=eff_tokens_abs,
        ownership_file_path=target_ownership_abs,
        python_bin=guidepoint_python,
    )

    # Phase 2: Write all three staged files inside output_dir, fsync, and re-parse from disk.
    stage_dir = Path(tempfile.mkdtemp(prefix=".stage-a-plists.", dir=str(out_abs)))
    backup_dir = stage_dir / "backup"
    staged_pairs: list[tuple[Path, Path]] = [
        (stage_dir / bridge_dest.name, bridge_dest),
        (stage_dir / host_dest.name, host_dest),
        (stage_dir / proxy_dest.name, proxy_dest),
    ]
    payloads = [bridge_xml, host_xml, proxy_xml]
    commit_started = False
    commit_succeeded = False
    rollback_succeeded = False
    try:
        for (staged_path, _dest), xml_text in zip(staged_pairs, payloads):
            fd = os.open(str(staged_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            closed = False
            try:
                os.fchmod(fd, 0o644)
                raw = xml_text.encode("utf-8")
                view = memoryview(raw)
                written = 0
                while written < len(view):
                    written += os.write(fd, view[written:])
                os.fsync(fd)
                os.close(fd)
                closed = True
            except BaseException:
                if not closed:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                raise
            os.chmod(staged_path, 0o644)
            # Re-verify written bytes on disk via plistlib before committing any file
            plistlib.loads(staged_path.read_bytes())

        # Post-render runtime check: verify newly rendered proxy plist -> `source guidepoint_mcp.env`
        # resolves directly from the rendered plist to the exact expected env/tokens/ownership paths.
        staged_proxy_path = stage_dir / proxy_dest.name
        post_proxy = resolve_legacy_proxy_effective_paths(
            repo_root=repo_abs,
            home_dir=home_abs,
            existing_proxy_plist_path=staged_proxy_path,
            bridge_env_vals=bridge_vals,
        )
        if Path(post_proxy["guidepoint_env_file"]).resolve() != Path(live_proxy["guidepoint_env_file"]).resolve():
            raise EnvFileValidationError(
                path=staged_proxy_path,
                key="GUIDEPOINT_ENV_FILE",
                reason=(
                    f"post-render proxy runtime env path {post_proxy['guidepoint_env_file']} "
                    f"diverges from rendered {live_proxy['guidepoint_env_file']}"
                ),
            )
        if Path(post_proxy["tokens_file"]).resolve() != eff_tokens_abs.resolve():
            raise EnvFileValidationError(
                path=Path(live_proxy["guidepoint_env_file"]),
                key="GUIDEPOINT_TOKENS_FILE",
                reason=(
                    f"post-render proxy runtime tokens path {post_proxy['tokens_file']} "
                    f"diverges from rendered {eff_tokens_abs}"
                ),
            )
        if Path(post_proxy["ownership_file"]).resolve() != target_ownership_abs.resolve():
            raise EnvFileValidationError(
                path=Path(live_proxy["guidepoint_env_file"]),
                key="DALTON_GUIDEPOINT_OWNERSHIP_FILE",
                reason=(
                    f"post-render proxy runtime ownership path {post_proxy['ownership_file']} "
                    f"diverges from canonical {target_ownership_abs}"
                ),
            )

        # Snapshot pre-existing destination files into stage_dir/backup before committing any file
        # so a mid-commit failure cleanly rolls back all previously replaced destinations.
        backup_dir.mkdir(mode=0o700)
        commit_plan: list[tuple[Path, Path, bool, Path | None]] = []
        for staged_path, dest_path in staged_pairs:
            validate_safe_regular_file_target(dest_path, allow_missing=True)
            if dest_path.exists():
                bkp_path = backup_dir / dest_path.name
                old_mode = stat.S_IMODE(dest_path.stat().st_mode)
                old_bytes = dest_path.read_bytes()
                bfd = os.open(str(bkp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, old_mode)
                bclosed = False
                try:
                    os.fchmod(bfd, old_mode)
                    bview = memoryview(old_bytes)
                    bwritten = 0
                    while bwritten < len(bview):
                        bwritten += os.write(bfd, bview[bwritten:])
                    os.fsync(bfd)
                    os.close(bfd)
                    bclosed = True
                except BaseException:
                    if not bclosed:
                        try:
                            os.close(bfd)
                        except OSError:
                            pass
                    raise
                os.chmod(bkp_path, old_mode)
                commit_plan.append((staged_path, dest_path, True, bkp_path))
            else:
                commit_plan.append((staged_path, dest_path, False, None))

        _fsync_directory(backup_dir, strict=True)
        _fsync_directory(stage_dir, strict=True)

        # Phase 3: Commit all three validated files; roll back all committed files on ANY failure.
        # If rollback encounters any OSError, preserve `backup_dir` intact and report all affected files.
        committed: list[tuple[Path, bool, Path | None]] = []
        commit_started = True
        try:
            for staged_path, dest_path, existed, bkp_path in commit_plan:
                os.replace(staged_path, dest_path)
                committed.append((dest_path, existed, bkp_path))
            _fsync_directory(out_abs, strict=True)
            commit_succeeded = True
        except BaseException as commit_exc:
            if not committed:
                rollback_succeeded = True
                raise
            affected_files: list[Path] = []
            rollback_failures: list[str] = []
            first_errno: int | None = None
            for dest_path, existed, bkp_path in reversed(committed):
                try:
                    if existed:
                        if bkp_path is None or not bkp_path.exists():
                            raise OSError(errno.ENOENT, "backup file missing", str(bkp_path))
                        os.replace(bkp_path, dest_path)
                    else:
                        dest_path.unlink(missing_ok=True)
                except OSError as rb_exc:
                    affected_files.append(dest_path)
                    if first_errno is None and rb_exc.errno is not None:
                        first_errno = rb_exc.errno
                    rollback_failures.append(
                        f"{dest_path} ({type(rb_exc).__name__}: errno={rb_exc.errno} {rb_exc.strerror or rb_exc})"
                    )
            if not rollback_failures:
                try:
                    _fsync_directory(out_abs, strict=True)
                except OSError as fsync_rb_exc:
                    if first_errno is None and fsync_rb_exc.errno is not None:
                        first_errno = fsync_rb_exc.errno
                    rollback_failures.append(
                        f"fsync {out_abs} ({type(fsync_rb_exc).__name__}: errno={fsync_rb_exc.errno} "
                        f"{fsync_rb_exc.strerror or fsync_rb_exc})"
                    )
            if rollback_failures:
                _fsync_directory(out_abs, strict=False)
                raise StageAPlistRollbackError(
                    backup_dir=backup_dir,
                    affected_files=affected_files,
                    commit_error=commit_exc,
                    rollback_errors=rollback_failures,
                    err_no=first_errno,
                ) from commit_exc
            rollback_succeeded = True
            raise
    finally:
        if (not commit_started) or commit_succeeded or rollback_succeeded:
            shutil.rmtree(stage_dir, ignore_errors=True)

    return {
        "com.dalton.guidepoint-mcp-proxy": str(proxy_dest),
        "com.dalton.researchdata-bridge": str(bridge_dest),
        "com.dalton.research-host": str(host_dest),
    }


# ---------------------------------------------------------------------------
# CLI entrypoints (`exec-nonarchive`, `merge-bridge-env`, etc.)
# ---------------------------------------------------------------------------


def _cmd_exec_nonarchive(args: argparse.Namespace) -> int:
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("bridge_subprocess_env exec-nonarchive: missing command", file=sys.stderr)
        return 2
    env = build_nonarchive_subprocess_env(
        os.environ,
        drop_keys=(
            "DALTON_BRIDGE_ENV_FILTER_PY",
            "DALTON_WIKI_EXPLICIT_RUN_SH",
            "DALTON_MARKET_DIGEST_EXPLICIT_RUN_SH",
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "PYTHONSTARTUP",
            "PYTHONINSPECT",
            "PYTHONEXECUTABLE",
        ),
        allow_keys=getattr(args, "allow_env", None) or (),
    )
    os.execvpe(cmd[0], cmd, env)
    return 127


def _cmd_merge_bridge_env(args: argparse.Namespace) -> int:
    try:
        meta = merge_bridge_and_repo_env_files(
            bridge_env_path=Path(args.bridge_env).expanduser(),
            repo_env_path=Path(args.repo_env).expanduser(),
            default_port=str(args.default_port),
            default_python_bin=str(args.default_python),
            default_research_code=str(args.default_research_code),
            default_research_home=str(args.default_research_home),
            existing_plist_path=Path(args.existing_plist).expanduser() if args.existing_plist else None,
            research_runtime=str(args.research_runtime),
        )
    except EnvFileValidationError as exc:
        print(f"[install_bridge] ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"[install_bridge] ERROR: failed writing configuration ({type(exc).__name__}: errno={exc.errno})",
            file=sys.stderr,
        )
        return 1
    if args.print_port_only:
        print(meta["port"])
    else:
        print(json.dumps(meta, ensure_ascii=False))
    return 0


def _cmd_render_plist(args: argparse.Namespace) -> int:
    try:
        port = render_plist_atomic(
            template_path=Path(args.template).expanduser(),
            dest_path=Path(args.dest).expanduser(),
            bridge_env_path=Path(args.bridge_env).expanduser(),
            repo_root=Path(args.repo_root).expanduser(),
            home_dir=Path(args.home).expanduser(),
            logs_dir=Path(args.logs_dir).expanduser() if args.logs_dir else None,
            repo_env_path=Path(args.repo_env).expanduser() if args.repo_env else None,
            ownership_file_path=Path(args.ownership_file).expanduser() if args.ownership_file else None,
            research_runtime=str(args.research_runtime),
        )
    except EnvFileValidationError as exc:
        print(f"[install_bridge] ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"[install_bridge] ERROR: failed writing launchd plist ({type(exc).__name__}: errno={exc.errno})",
            file=sys.stderr,
        )
        return 1
    print(port)
    return 0


def _cmd_render_research_host_plist(args: argparse.Namespace) -> int:
    try:
        meta = render_research_host_plist_atomic(
            template_path=Path(args.template).expanduser(),
            dest_path=Path(args.dest).expanduser(),
            repo_root=Path(args.repo_root).expanduser(),
            home_dir=Path(args.home).expanduser(),
            research_host_env_path=Path(args.research_host_env).expanduser() if args.research_host_env else None,
            repo_env_path=Path(args.repo_env).expanduser() if args.repo_env else None,
            logs_dir=Path(args.logs_dir).expanduser() if args.logs_dir else None,
            ownership_file_path=Path(args.ownership_file).expanduser() if args.ownership_file else None,
            tokens_file_path=Path(args.tokens_file).expanduser() if args.tokens_file else None,
        )
    except EnvFileValidationError as exc:
        print(f"[render_research_host_plist] ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"[render_research_host_plist] ERROR: failed writing launchd plist ({type(exc).__name__}: errno={exc.errno})",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(meta, ensure_ascii=False))
    return 0


def _cmd_render_host_service_plist(args: argparse.Namespace) -> int:
    try:
        meta = render_guidepoint_proxy_plist_atomic(
            template_path=Path(args.template).expanduser(),
            dest_path=Path(args.dest).expanduser(),
            repo_root=Path(args.repo_root).expanduser(),
            home_dir=Path(args.home).expanduser(),
            bridge_env_path=Path(args.bridge_env).expanduser() if args.bridge_env else None,
            logs_dir=Path(args.logs_dir).expanduser() if args.logs_dir else None,
            runtime_env_key=str(args.runtime_env_key or "DALTON_RESEARCH_HOME"),
            runtime_env_val=args.runtime_env_val,
            node_bin=args.node_bin,
            python_bin=args.python_bin,
            guidepoint_env_path=Path(args.guidepoint_env).expanduser() if args.guidepoint_env else None,
            tokens_file_path=Path(args.tokens_file).expanduser() if args.tokens_file else None,
            ownership_file_path=Path(args.ownership_file).expanduser() if args.ownership_file else None,
            create_logs_dir=False,
        )
    except EnvFileValidationError as exc:
        print(f"[render_host_service_plist] ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"[render_host_service_plist] ERROR: failed writing launchd plist ({type(exc).__name__}: errno={exc.errno})",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(meta, ensure_ascii=False))
    return 0


def _cmd_render_stage_a_plists(args: argparse.Namespace) -> int:
    try:
        rendered = render_stage_a_plists_atomic(
            output_dir=Path(args.output_dir).expanduser(),
            repo_root=Path(args.repo_root).expanduser(),
            home_dir=Path(args.home).expanduser(),
            bridge_env_path=Path(args.bridge_env).expanduser() if args.bridge_env else None,
            research_host_env_path=Path(args.research_host_env).expanduser() if args.research_host_env else None,
            repo_env_path=Path(args.repo_env).expanduser() if args.repo_env else None,
            logs_dir=Path(args.logs_dir).expanduser() if args.logs_dir else None,
            ownership_file_path=Path(args.ownership_file).expanduser() if args.ownership_file else None,
            guidepoint_python=args.guidepoint_python,
            existing_proxy_plist_path=(
                Path(args.existing_proxy_plist).expanduser() if args.existing_proxy_plist else None
            ),
        )
    except EnvFileValidationError as exc:
        print(f"[render_stage_a_plists] ERROR: {exc}", file=sys.stderr)
        return 1
    except StageAPlistRollbackError as exc:
        print(f"[render_stage_a_plists] ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"[render_stage_a_plists] ERROR: failed writing launchd plists ({type(exc).__name__}: errno={exc.errno})",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(rendered, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_check_env_keys(args: argparse.Namespace) -> int:
    presence = inspect_env_key_presence(args.keys)
    print(json.dumps(presence, sort_keys=True))
    return 0


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dalton Bridge subprocess environment filter and config installer helper"
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_na = sub.add_parser("exec-nonarchive", help="Scrub credentials and exec command")
    p_na.add_argument(
        "--allow-env",
        action="append",
        default=[],
        dest="allow_env",
        help="Explicitly allow a specific credential variable (repeatable)",
    )
    p_na.add_argument("command", nargs=argparse.REMAINDER)

    p_mg = sub.add_parser("merge-bridge-env", help="Atomically merge defaults into bridge and repo env files")
    p_mg.add_argument("--bridge-env", required=True)
    p_mg.add_argument("--repo-env", required=True)
    p_mg.add_argument("--default-port", default="8960")
    p_mg.add_argument("--default-research-code", default="")
    p_mg.add_argument("--default-research-home", default="")
    p_mg.add_argument("--default-python", required=True)
    p_mg.add_argument("--existing-plist", default="")
    p_mg.add_argument("--research-runtime", choices=("auto", "new"), default="new")
    p_mg.add_argument("--print-port-only", action="store_true")

    p_pl = sub.add_parser("render-plist", help="Atomically render bridge launchd plist")
    p_pl.add_argument("--template", required=True)
    p_pl.add_argument("--dest", required=True)
    p_pl.add_argument("--bridge-env", required=True)
    p_pl.add_argument("--repo-root", required=True)
    p_pl.add_argument("--home", required=True)
    p_pl.add_argument("--logs-dir", default=None)
    p_pl.add_argument("--repo-env", default=None)
    p_pl.add_argument("--ownership-file", default=None)
    p_pl.add_argument("--research-runtime", choices=("auto", "new"), default="new")

    p_rh = sub.add_parser("render-research-host-plist", help="Atomically render com.dalton.research-host plist")
    p_rh.add_argument("--template", required=True)
    p_rh.add_argument("--dest", required=True)
    p_rh.add_argument("--repo-root", required=True)
    p_rh.add_argument("--home", required=True)
    p_rh.add_argument("--research-host-env", default=None)
    p_rh.add_argument("--repo-env", default=None)
    p_rh.add_argument("--logs-dir", default=None)
    p_rh.add_argument("--ownership-file", default=None)
    p_rh.add_argument("--tokens-file", default=None)

    p_hs = sub.add_parser(
        "render-host-service-plist",
        help="Atomically render a host-service LaunchAgent plist (e.g. guidepoint-mcp-proxy) with fsync",
    )
    p_hs.add_argument("--template", required=True)
    p_hs.add_argument("--dest", required=True)
    p_hs.add_argument("--repo-root", required=True)
    p_hs.add_argument("--home", required=True)
    p_hs.add_argument("--bridge-env", default=None)
    p_hs.add_argument("--logs-dir", default=None)
    p_hs.add_argument("--runtime-env-key", default="DALTON_RESEARCH_HOME")
    p_hs.add_argument("--runtime-env-val", default=None)
    p_hs.add_argument("--node-bin", default=None)
    p_hs.add_argument("--python-bin", default=None)
    p_hs.add_argument("--guidepoint-env", default=None)
    p_hs.add_argument("--tokens-file", default=None)
    p_hs.add_argument("--ownership-file", default=None)

    p_sa = sub.add_parser(
        "render-stage-a-plists",
        help="Atomically render all three Stage A LaunchAgent plists into an output directory",
    )
    p_sa.add_argument("--output-dir", required=True)
    p_sa.add_argument("--repo-root", required=True)
    p_sa.add_argument("--home", required=True)
    p_sa.add_argument("--bridge-env", default=None)
    p_sa.add_argument("--research-host-env", default=None)
    p_sa.add_argument("--repo-env", default=None)
    p_sa.add_argument("--logs-dir", default=None)
    p_sa.add_argument("--ownership-file", default=None)
    p_sa.add_argument("--guidepoint-python", default=None)
    p_sa.add_argument("--existing-proxy-plist", default=None)

    p_ck = sub.add_parser("check-env-keys", help="Output boolean presence map for named keys")
    p_ck.add_argument("keys", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    if args.subcommand == "exec-nonarchive":
        return _cmd_exec_nonarchive(args)
    if args.subcommand == "merge-bridge-env":
        return _cmd_merge_bridge_env(args)
    if args.subcommand == "render-plist":
        return _cmd_render_plist(args)
    if args.subcommand == "render-research-host-plist":
        return _cmd_render_research_host_plist(args)
    if args.subcommand == "render-host-service-plist":
        return _cmd_render_host_service_plist(args)
    if args.subcommand == "render-stage-a-plists":
        return _cmd_render_stage_a_plists(args)
    if args.subcommand == "check-env-keys":
        return _cmd_check_env_keys(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
