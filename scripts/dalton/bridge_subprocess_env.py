#!/usr/bin/env python3
"""Subprocess environment filtering, archive allowlist enforcement, and atomic env-file merge.

Used by `researchdata_bridge.py`, `seed_asr_client.py`, `install_bridge.sh`,
`launchd/archive_daily.sh`, `conversation_archive.py`, `reextract_conversations.py`,
and the `company-wiki` / `market-digest` skill `run.sh` launchers.

Pure Python 3 standard library only so host launchd and skill virtualenvs can invoke
this module without third-party dependencies.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, MutableMapping
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse

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
        "DALTON_BROKER_TOKEN",
        "DALTON_BROKER_TOKEN_PATH",
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
)

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


class ArchiveEnvValidationError(ValueError):
    """Raised when required archive/re-extract environment keys are missing or invalid."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(issues))


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
                or _is_shell_injection_key(key)
            ):
                continue
            cleaned[key] = str(raw_val)
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


def _strip_yaml_inline_comment(val: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(val):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or val[idx - 1].isspace():
                return val[:idx].strip()
    return val.strip()


def _unquote_yaml_scalar(val: str) -> str:
    cleaned = _strip_yaml_inline_comment(val)
    if (
        len(cleaned) >= 2
        and cleaned[0] == cleaned[-1]
        and cleaned[0] in ("'", '"')
    ):
        return cleaned[1:-1].strip()
    return cleaned.strip()


def read_dalton_yaml_archive_defaults(repo_root: Path | None = None) -> dict[str, Any]:
    """Read `conversations.extractor_model`, `extractor_fallback_model`, and `models.assist_fallback_model`."""
    root = repo_root or _repo_root_default()
    yaml_path = root / "src" / "dalton" / "dalton.yaml"
    if not yaml_path.is_file():
        default_yaml = _repo_root_default() / "src" / "dalton" / "dalton.yaml"
        if default_yaml.is_file():
            yaml_path = default_yaml
    defaults: dict[str, Any] = {
        "extractor_model": "deepseek-flash",
        "extractor_fallback_model": "gemini-4-argon-medium-oauth",
        "assist_fallback_models": ["deepseek-flash"],
    }
    if not yaml_path.is_file():
        return defaults

    try:
        import yaml  # type: ignore[import-untyped]

        loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            conv = loaded.get("conversations")
            if isinstance(conv, dict):
                if isinstance(conv.get("extractor_model"), str) and conv["extractor_model"].strip():
                    defaults["extractor_model"] = conv["extractor_model"].strip()
                if "extractor_fallback_model" in conv:
                    fb = conv.get("extractor_fallback_model")
                    defaults["extractor_fallback_model"] = str(fb).strip() if fb else ""
            models_sec = loaded.get("models")
            if isinstance(models_sec, dict) and "assist_fallback_model" in models_sec:
                raw_af = models_sec.get("assist_fallback_model")
                if isinstance(raw_af, str):
                    defaults["assist_fallback_models"] = [
                        m.strip() for m in raw_af.split(",") if m.strip()
                    ]
                elif isinstance(raw_af, list):
                    defaults["assist_fallback_models"] = [
                        str(m).strip() for m in raw_af if str(m).strip()
                    ]
            return defaults
    except Exception:
        pass

    # Pure-stdlib indentation parser fallback when PyYAML is absent.
    try:
        lines = yaml_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return defaults

    top_section = ""
    in_assist_list = False
    assist_items: list[str] = []
    saw_assist_key = False
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0 and stripped.endswith(":"):
            top_section = stripped[:-1].strip()
            in_assist_list = False
            continue
        if top_section == "conversations" and indent == 2 and ":" in stripped:
            in_assist_list = False
            k, v = stripped.split(":", 1)
            k = k.strip()
            if k in ("extractor_model", "extractor_fallback_model"):
                defaults[k] = _unquote_yaml_scalar(v)
        elif top_section == "models":
            if indent == 2 and ":" in stripped:
                k, v = stripped.split(":", 1)
                k = k.strip()
                if k == "assist_fallback_model":
                    saw_assist_key = True
                    scalar = _unquote_yaml_scalar(v)
                    if scalar and not scalar.startswith("["):
                        assist_items = [m.strip() for m in scalar.split(",") if m.strip()]
                        in_assist_list = False
                    elif scalar.startswith("[") and scalar.endswith("]"):
                        inner = scalar[1:-1]
                        assist_items = [
                            _unquote_yaml_scalar(x)
                            for x in inner.split(",")
                            if _unquote_yaml_scalar(x)
                        ]
                        in_assist_list = False
                    else:
                        assist_items = []
                        in_assist_list = True
                else:
                    in_assist_list = False
            elif in_assist_list and indent >= 4 and stripped.startswith("-"):
                item = _unquote_yaml_scalar(stripped[1:].strip())
                if item:
                    assist_items.append(item)
    if saw_assist_key:
        defaults["assist_fallback_models"] = assist_items
    return defaults


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


def _extract_preference_model_from_mapping(
    user_preferences: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(user_preferences, Mapping):
        return None
    mp = user_preferences.get("model_preference")
    if isinstance(mp, Mapping):
        cand = mp.get("model")
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    elif isinstance(mp, str) and mp.strip():
        return mp.strip()
    direct = user_preferences.get("model")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    return None


def _extract_models_from_plan_file(
    plan_path: Path | str | None,
) -> tuple[str | None, str | None]:
    if plan_path is None:
        return None, None
    p = Path(plan_path).expanduser()
    if not p.is_file():
        return None, None
    doc = _read_json_dict(p)
    raw_model = doc.get("model")
    raw_fb = doc.get("fallback_model")
    p_model = str(raw_model).strip() if isinstance(raw_model, str) and raw_model.strip() else None
    p_fb = str(raw_fb).strip() if isinstance(raw_fb, str) else None
    return p_model, p_fb


_ARCHIVE_NON_PROVIDER_CANDIDATE_KEYS: frozenset[str] = frozenset(
    {"DALTON_ASSIST_FALLBACK_MODEL"}
    | set(ARCHIVE_BASE_REQUIRED_KEYS)
    | set(ARCHIVE_BASE_OPTIONAL_PROCESS_KEYS)
    | set(ARCHIVE_DB_REQUIRED_KEYS)
    | set(ARCHIVE_DB_OPTIONAL_KEYS)
    | {"BYOK_ENCRYPTION_KEY"}
    | set(ARCHIVE_OWNER_REQUIRED_KEYS)
    | set(ARCHIVE_OWNER_OPTIONAL_MODE_KEYS)
    | set(ARCHIVE_WORKSPACE_RUNTIME_KEYS)
    | {
        "DALTON_BRIDGE_ADMIN_TOKEN",
        "DALTON_BRIDGE_EMBED_URL",
        "no_proxy",
        "DALTON_OCW_AGENTS_ROOT",
        "DALTON_REEXTRACT_DSN",
    }
)


def _read_and_close_candidates_fd(
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Read and close one-shot `_DALTON_ARCHIVE_CANDIDATES_FD` pipe if present."""
    raw_fd = ""
    if isinstance(env, MutableMapping):
        raw_fd = str(env.pop("_DALTON_ARCHIVE_CANDIDATES_FD", "") or "").strip()
    elif env is not None:
        raw_fd = str(env.get("_DALTON_ARCHIVE_CANDIDATES_FD") or "").strip()
    env_fd = str(os.environ.pop("_DALTON_ARCHIVE_CANDIDATES_FD", "") or "").strip()
    if not raw_fd:
        raw_fd = env_fd
    if not raw_fd:
        return {}
    try:
        fd_num = int(raw_fd)
    except ValueError:
        return {}
    if fd_num < 0:
        return {}
    chunks: list[bytes] = []
    try:
        while True:
            buf = os.read(fd_num, 65536)
            if not buf:
                break
            chunks.append(buf)
    except OSError:
        return {}
    finally:
        try:
            os.close(fd_num)
        except OSError:
            pass
    try:
        parsed = json.loads(b"".join(chunks).decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in parsed.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            out[k] = v.strip()
    return out


def finalize_archive_subprocess_candidates(
    caller_candidates: Mapping[str, str] | None,
    final_env: Mapping[str, str],
    *,
    repo_root: Path | None = None,
) -> dict[str, str]:
    """Finalize candidate mapping against `final_env` before single-use pipe transfer.

    Preserves deferred provider candidates from `caller_candidates` (classified via
    `_load_catalog_maps` and `XAI_OAUTH_ALLOWED_ENV_KEYS`), updates transferable non-provider
    values from `final_env` (including `DALTON_BRIDGE_ADMIN_TOKEN` and `DALTON_ASSIST_FALLBACK_MODEL`),
    and removes non-provider values absent from `final_env`.
    """
    root = repo_root or _repo_root_default()
    _, _, all_provider_keys = _load_catalog_maps(root)
    provider_keys = (frozenset(all_provider_keys) | XAI_OAUTH_ALLOWED_ENV_KEYS) - {
        "DALTON_ASSIST_FALLBACK_MODEL"
    }
    out: dict[str, str] = {}

    if isinstance(caller_candidates, Mapping):
        for raw_key, raw_val in caller_candidates.items():
            key = str(raw_key)
            if (
                key not in provider_keys
                or _is_stripped_credential_key(key)
                or _is_shell_injection_key(key)
                or key == "DALTON_BRIDGE_TOKEN"
                or key == "_DALTON_ARCHIVE_CANDIDATES_FD"
            ):
                continue
            val = str(raw_val or "").strip()
            if val:
                out[key] = val

    for key in sorted(provider_keys | _ARCHIVE_NON_PROVIDER_CANDIDATE_KEYS):
        if (
            _is_stripped_credential_key(key)
            or _is_shell_injection_key(key)
            or key == "DALTON_BRIDGE_TOKEN"
            or key == "_DALTON_ARCHIVE_CANDIDATES_FD"
        ):
            out.pop(key, None)
            continue
        final_val = str(final_env.get(key) or "").strip()
        if final_val:
            out[key] = final_val
        elif key not in provider_keys:
            out.pop(key, None)

    return out


def attach_archive_candidates_pipe(
    target_env: MutableMapping[str, str],
    candidates: Mapping[str, str] | None,
) -> int | None:
    """Write `candidates` JSON to a nonblocking one-shot pipe and set `_DALTON_ARCHIVE_CANDIDATES_FD`.

    Requires the complete payload to be accepted in nonblocking mode; on `BlockingIOError`,
    partial write, or any `OSError`, closes both descriptors, clears `_DALTON_ARCHIVE_CANDIDATES_FD`,
    and raises `ArchiveEnvValidationError` containing no candidate values.
    """
    target_env.pop("_DALTON_ARCHIVE_CANDIDATES_FD", None)
    if not candidates:
        return None
    clean = {
        str(k): str(v).strip()
        for k, v in candidates.items()
        if isinstance(k, str)
        and isinstance(v, str)
        and v.strip()
        and not _is_stripped_credential_key(k)
        and not _is_shell_injection_key(k)
        and k != "DALTON_BRIDGE_TOKEN"
        and k != "_DALTON_ARCHIVE_CANDIDATES_FD"
    }
    if not clean:
        return None
    payload_bytes = json.dumps(clean, ensure_ascii=False).encode("utf-8")
    rfd, wfd = os.pipe()
    wfd_closed = False
    try:
        os.set_blocking(wfd, False)
        written = os.write(wfd, payload_bytes)
        if written != len(payload_bytes):
            raise ArchiveEnvValidationError(
                [
                    f"archive candidate transfer partial write ({written} of {len(payload_bytes)} bytes fit in pipe buffer)"
                ]
            )
        os.close(wfd)
        wfd_closed = True
        os.set_inheritable(rfd, True)
        target_env["_DALTON_ARCHIVE_CANDIDATES_FD"] = str(rfd)
        return rfd
    except BaseException as exc:
        target_env.pop("_DALTON_ARCHIVE_CANDIDATES_FD", None)
        if not wfd_closed:
            try:
                os.close(wfd)
            except OSError:
                pass
        try:
            os.close(rfd)
        except OSError:
            pass
        if isinstance(exc, ArchiveEnvValidationError):
            raise
        if isinstance(exc, BlockingIOError):
            raise ArchiveEnvValidationError(
                [
                    f"archive candidate transfer exceeded nonblocking pipe capacity ({len(payload_bytes)} bytes)"
                ]
            ) from None
        if isinstance(exc, OSError):
            raise ArchiveEnvValidationError(
                [
                    f"archive candidate transfer failed ({type(exc).__name__}: errno={exc.errno})"
                ]
            ) from None
        raise


def finalize_and_attach_archive_candidates(
    caller_candidates: Mapping[str, str] | None,
    final_env: MutableMapping[str, str],
    *,
    repo_root: Path | None = None,
) -> int | None:
    """Finalize `caller_candidates` against `final_env` and attach a checked one-shot pipe."""
    final_candidates = finalize_archive_subprocess_candidates(
        caller_candidates,
        final_env,
        repo_root=repo_root,
    )
    return attach_archive_candidates_pipe(final_env, final_candidates)


def capture_archive_provider_candidates(
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
    repo_env_path: Path | None = None,
    hydrate_from_dotenv: bool = True,
) -> dict[str, str]:
    """Capture allowlisted candidate values (`environment > dotenv`) locally for one invocation."""
    root = repo_root or _repo_root_default()
    src = env if env is not None else os.environ
    pipe_candidates = _read_and_close_candidates_fd(src)
    _, _, all_provider_keys = _load_catalog_maps(root)
    candidate_keys = (
        set(all_provider_keys)
        | set(XAI_OAUTH_ALLOWED_ENV_KEYS)
        | set(_ARCHIVE_NON_PROVIDER_CANDIDATE_KEYS)
    )
    captured: dict[str, str] = {}
    if pipe_candidates:
        for key in sorted(candidate_keys):
            if (
                _is_stripped_credential_key(key)
                or _is_shell_injection_key(key)
                or key == "DALTON_BRIDGE_TOKEN"
            ):
                continue
            pipe_val = str(pipe_candidates.get(key) or "").strip()
            if pipe_val:
                if key == "DALTON_BRIDGE_EMBED_URL" and "host.docker.internal" in pipe_val:
                    captured[key] = DEFAULT_HOST_EMBED_URL
                else:
                    captured[key] = pipe_val
        return captured

    dotenv_vars = _resolve_dotenv_vars(
        src,
        repo_root=root,
        repo_env_path=repo_env_path,
        hydrate_from_dotenv=hydrate_from_dotenv,
    )
    for key in sorted(candidate_keys):
        if (
            _is_stripped_credential_key(key)
            or _is_shell_injection_key(key)
            or key == "DALTON_BRIDGE_TOKEN"
        ):
            continue
        env_val = str(src.get(key) or "").strip()
        if env_val:
            if key == "DALTON_BRIDGE_EMBED_URL" and "host.docker.internal" in env_val:
                captured[key] = DEFAULT_HOST_EMBED_URL
            else:
                captured[key] = env_val
            continue
        file_val = str(dotenv_vars.get(key) or "").strip()
        if file_val:
            if key == "DALTON_BRIDGE_EMBED_URL" and "host.docker.internal" in file_val:
                captured[key] = DEFAULT_HOST_EMBED_URL
            else:
                captured[key] = file_val
    return captured


def resolve_selected_archive_models(
    *,
    explicit_model: str | None = None,
    explicit_fallback_model: str | None = None,
    plan_model: str | None = None,
    plan_fallback_model: str | None = None,
    plan_path: Path | str | None = None,
    preference_model: str | None = None,
    user_preferences: Mapping[str, Any] | None = None,
    assist_fallback_raw: str | None = None,
    repo_root: Path | None = None,
    include_primary: bool = True,
) -> list[str]:
    """Return deduplicated `[primary, fallback, *assist_fallbacks]` model IDs for archive/extract."""
    defaults = read_dalton_yaml_archive_defaults(repo_root)
    file_plan_model, file_plan_fb = _extract_models_from_plan_file(plan_path)
    eff_plan_model = (
        str(plan_model).strip()
        if plan_model and str(plan_model).strip()
        else file_plan_model
    )
    eff_pref_model = (
        str(preference_model).strip()
        if preference_model and str(preference_model).strip()
        else _extract_preference_model_from_mapping(user_preferences)
    )
    has_non_yaml_primary = bool(
        (explicit_model and str(explicit_model).strip())
        or eff_plan_model
        or eff_pref_model
    )
    if not include_primary:
        primary = ""
    elif explicit_model and str(explicit_model).strip():
        primary = str(explicit_model).strip()
    elif eff_plan_model:
        primary = eff_plan_model
    elif eff_pref_model:
        primary = eff_pref_model
    else:
        primary = str(defaults["extractor_model"])

    if explicit_fallback_model is not None:
        fallback = str(explicit_fallback_model or "").strip()
    elif plan_fallback_model is not None:
        fallback = str(plan_fallback_model or "").strip()
    elif file_plan_fb is not None:
        fallback = str(file_plan_fb or "").strip()
    else:
        fallback = str(defaults["extractor_fallback_model"] or "").strip()

    if assist_fallback_raw is not None and str(assist_fallback_raw).strip():
        assist_models = [
            item.strip() for item in str(assist_fallback_raw).split(",") if item.strip()
        ]
    elif include_primary and not has_non_yaml_primary:
        assist_models = list(defaults.get("assist_fallback_models") or [])
    else:
        assist_models = []

    ordered: list[str] = []
    for candidate in [primary, fallback, *assist_models]:
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    return ordered


def _extract_model_arg_from_argv(argv: Iterable[str]) -> str | None:
    tokens = list(argv)
    for idx, tok in enumerate(tokens):
        if tok == "--model" and idx + 1 < len(tokens):
            val = tokens[idx + 1].strip()
            return val or None
        if tok.startswith("--model="):
            val = tok.split("=", 1)[1].strip()
            return val or None
    return None


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


def archive_allowed_env_keys(
    *,
    models: Iterable[str | None],
    repo_root: Path | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return `(allowed_keys, conditionally_required_keys)` for `conversation_archive.py` (§2.6.1.4)."""
    provider_allowed, provider_required, _ = resolve_provider_env_keys_for_models(
        models, repo_root=repo_root
    )
    allowed = (
        set(ARCHIVE_BASE_REQUIRED_KEYS)
        | set(ARCHIVE_BASE_OPTIONAL_PROCESS_KEYS)
        | set(ARCHIVE_DB_REQUIRED_KEYS)
        | set(ARCHIVE_DB_OPTIONAL_KEYS)
        | {"BYOK_ENCRYPTION_KEY"}
        | set(ARCHIVE_OWNER_REQUIRED_KEYS)
        | set(ARCHIVE_OWNER_OPTIONAL_MODE_KEYS)
        | {"DALTON_ASSIST_FALLBACK_MODEL"}
        | set(provider_allowed)
        | {"DALTON_BRIDGE_ADMIN_TOKEN", "DALTON_BRIDGE_EMBED_URL", "no_proxy"}
        | {"DALTON_OCW_AGENTS_ROOT"}
    )
    return frozenset(allowed), provider_required


def _resolve_dotenv_vars(
    env: Mapping[str, str] | None = None,
    *,
    repo_root: Path | None = None,
    repo_env_path: Path | None = None,
    hydrate_from_dotenv: bool = False,
) -> dict[str, str]:
    """Load key-value pairs from an explicit dotenv file or `<repo>/.env` when requested."""
    src = env if env is not None else os.environ
    if repo_env_path is not None:
        return parse_env_file_dict(Path(repo_env_path).expanduser())
    explicit = (
        str(src.get("DALTON_DOTENV_PATH") or "").strip()
        or str(src.get("DALTON_REPO_ENV_FILE") or "").strip()
    )
    if explicit:
        return parse_env_file_dict(Path(explicit).expanduser())
    if hydrate_from_dotenv:
        root = repo_root or _repo_root_default()
        return parse_env_file_dict(root / ".env")
    return {}


def resolve_reextract_dsn(
    *,
    explicit_dsn: str | None = None,
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    repo_env_path: Path | None = None,
    hydrate_from_dotenv: bool = True,
) -> str | None:
    """Resolve `DALTON_REEXTRACT_DSN` from CLI `--dsn`, process environment, or `.env` before filtering."""
    if explicit_dsn and str(explicit_dsn).strip():
        return str(explicit_dsn).strip()
    src = env if env is not None else os.environ
    env_dsn = str(src.get("DALTON_REEXTRACT_DSN") or "").strip()
    if env_dsn:
        return env_dsn
    dotenv_vars = _resolve_dotenv_vars(
        src,
        repo_root=repo_root,
        repo_env_path=repo_env_path,
        hydrate_from_dotenv=hydrate_from_dotenv,
    )
    file_dsn = str(dotenv_vars.get("DALTON_REEXTRACT_DSN") or "").strip()
    return file_dsn or None


def build_archive_subprocess_env(
    *,
    repo_env_path: Path | None = None,
    bridge_env_path: Path | None = None,
    base_env: Mapping[str, str] | None = None,
    explicit_model: str | None = None,
    explicit_fallback_model: str | None = None,
    plan_model: str | None = None,
    plan_fallback_model: str | None = None,
    plan_path: Path | str | None = None,
    preference_model: str | None = None,
    user_preferences: Mapping[str, Any] | None = None,
    host_embed_url: str = DEFAULT_HOST_EMBED_URL,
    ocw_agents_root: str | None = None,
    repo_root: Path | None = None,
    validate_required: bool = False,
    defer_primary_model_validation: bool = False,
) -> dict[str, str]:
    """Construct the archive subprocess environment strictly following §2.6.1.4 Steps 1–4.

    Step 1: Initialize controlled base process vars (`PATH`, `HOME`, `LANG`, `TZ`, safe runtime paths).
    Step 2: Extract allowlisted DB, `BYOK_ENCRYPTION_KEY`, owner/auth mode, workspace runtime,
            and selected-model provider keys with `environment > dotenv` priority.
    Step 3: Extract ONLY `DALTON_BRIDGE_ADMIN_TOKEN` from `researchdata-bridge.env`.
    Step 4: Force host `DALTON_BRIDGE_EMBED_URL` + `no_proxy` and optional `DALTON_OCW_AGENTS_ROOT`,
            then strip all forbidden credentials and shell injection variables.
    """
    root = repo_root or _repo_root_default()
    parent = base_env if base_env is not None else os.environ
    eff_repo_env = repo_env_path
    if eff_repo_env is None:
        explicit_env_file = (
            str(parent.get("DALTON_DOTENV_PATH") or "").strip()
            or str(parent.get("DALTON_REPO_ENV_FILE") or "").strip()
        )
        if explicit_env_file:
            eff_repo_env = Path(explicit_env_file).expanduser()
    repo_vars = parse_env_file_dict(eff_repo_env) if eff_repo_env is not None else {}
    bridge_vars = parse_env_file_dict(bridge_env_path) if bridge_env_path is not None else {}

    # Step 1: Base process environment
    out: dict[str, str] = {
        "PATH": str(parent.get("PATH") or "").strip() or DEFAULT_SYSTEM_PATH,
        "HOME": str(parent.get("HOME") or "").strip() or os.path.expanduser("~"),
        "LANG": str(parent.get("LANG") or "").strip() or "en_US.UTF-8",
        "TZ": str(parent.get("TZ") or repo_vars.get("TZ") or "").strip() or "America/New_York",
    }
    for opt_key in ARCHIVE_BASE_OPTIONAL_PROCESS_KEYS:
        val = parent.get(opt_key)
        if isinstance(val, str) and val.strip():
            out[opt_key] = val
        elif opt_key in ARCHIVE_WORKSPACE_RUNTIME_KEYS and opt_key in repo_vars:
            rval = str(repo_vars.get(opt_key) or "").strip()
            if rval:
                out[opt_key] = rval
    if eff_repo_env is not None and "DALTON_DOTENV_PATH" not in out:
        out["DALTON_DOTENV_PATH"] = str(eff_repo_env)

    # Resolve selected models to determine exact provider key allowlist
    assist_raw = (
        str(parent.get("DALTON_ASSIST_FALLBACK_MODEL") or "").strip()
        or str(repo_vars.get("DALTON_ASSIST_FALLBACK_MODEL") or "").strip()
        or None
    )
    selected_models = resolve_selected_archive_models(
        explicit_model=explicit_model,
        explicit_fallback_model=explicit_fallback_model,
        plan_model=plan_model,
        plan_fallback_model=plan_fallback_model,
        plan_path=plan_path,
        preference_model=preference_model,
        user_preferences=user_preferences,
        assist_fallback_raw=assist_raw,
        repo_root=root,
        include_primary=not defer_primary_model_validation,
    )
    provider_allowed, provider_required, _ = resolve_provider_env_keys_for_models(
        selected_models, repo_root=root
    )

    # Step 2: Allowlisted keys with `parent` (environment) taking priority over `<repo>/.env`
    step2_keys = (
        list(ARCHIVE_DB_REQUIRED_KEYS)
        + list(ARCHIVE_DB_OPTIONAL_KEYS)
        + ["BYOK_ENCRYPTION_KEY"]
        + list(ARCHIVE_OWNER_REQUIRED_KEYS)
        + list(ARCHIVE_OWNER_OPTIONAL_MODE_KEYS)
        + ["DALTON_ASSIST_FALLBACK_MODEL"]
        + sorted(provider_allowed)
    )
    for key in step2_keys:
        if key in parent and str(parent[key]).strip():
            out[key] = str(parent[key]).strip()
        elif key in repo_vars and str(repo_vars[key]).strip():
            out[key] = str(repo_vars[key]).strip()

    # Step 3: Extract ONLY `DALTON_BRIDGE_ADMIN_TOKEN` from `researchdata-bridge.env`
    admin_tok = str(bridge_vars.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
    if not admin_tok:
        admin_tok = str(parent.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
    if not admin_tok:
        admin_tok = str(repo_vars.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip()
    if admin_tok:
        out["DALTON_BRIDGE_ADMIN_TOKEN"] = admin_tok

    # Step 4: Force host embed URL + no_proxy + optional DALTON_OCW_AGENTS_ROOT
    out["DALTON_BRIDGE_EMBED_URL"] = str(host_embed_url or DEFAULT_HOST_EMBED_URL).strip()
    out["no_proxy"] = _merge_no_proxy(parent.get("no_proxy"))

    eff_agents_root = (
        str(ocw_agents_root).strip()
        if ocw_agents_root is not None
        else str(
            parent.get("DALTON_OCW_AGENTS_ROOT")
            or repo_vars.get("DALTON_OCW_AGENTS_ROOT")
            or ""
        ).strip()
    )
    if eff_agents_root:
        out["DALTON_OCW_AGENTS_ROOT"] = eff_agents_root

    # Final defensive scrub of any stripped credential keys, shell injection vars, and non-admin bridge token
    out = strip_asr_credentials_from_env(out)
    out.pop("DALTON_BRIDGE_TOKEN", None)
    out.pop("_DALTON_ARCHIVE_CANDIDATES_FD", None)

    if validate_required:
        validate_archive_required_env(
            out,
            conditional_required_keys=provider_required,
        )
    return out


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


def _validate_ocw_agents_root_if_set(env: Mapping[str, str]) -> str | None:
    if "DALTON_OCW_AGENTS_ROOT" not in env:
        return None
    raw = str(env.get("DALTON_OCW_AGENTS_ROOT") or "").strip()
    if not raw:
        return "invalid key DALTON_OCW_AGENTS_ROOT (empty path)"
    p = Path(raw).expanduser()
    if not p.is_dir() or not os.access(p, os.R_OK):
        return "invalid key DALTON_OCW_AGENTS_ROOT (directory missing or unreadable)"
    return None


def validate_archive_required_env(
    env: Mapping[str, str],
    *,
    conditional_required_keys: Iterable[str] = (),
) -> None:
    """Validate all unconditional and model-conditional archive env keys without logging values."""
    issues: list[str] = []
    unconditional = (
        *ARCHIVE_BASE_REQUIRED_KEYS,
        *ARCHIVE_DB_REQUIRED_KEYS,
        "BYOK_ENCRYPTION_KEY",
        *ARCHIVE_OWNER_REQUIRED_KEYS,
        "DALTON_BRIDGE_ADMIN_TOKEN",
    )
    for key in unconditional:
        if not str(env.get(key) or "").strip():
            issues.append(f"missing required key {key}")

    if "DB_PORT" in env and str(env.get("DB_PORT") or "").strip():
        try:
            port = int(str(env["DB_PORT"]).strip())
            if not (1 <= port <= 65535):
                issues.append("invalid key DB_PORT (out of range 1..65535)")
        except ValueError:
            issues.append("invalid key DB_PORT (not an integer)")

    for key in sorted(set(conditional_required_keys)):
        if not str(env.get(key) or "").strip():
            issues.append(f"missing conditional model key {key}")

    embed_issue = _validate_host_embed_url(env.get("DALTON_BRIDGE_EMBED_URL", ""))
    if embed_issue:
        issues.append(embed_issue)

    no_proxy_val = str(env.get("no_proxy") or "").strip()
    if not no_proxy_val:
        issues.append("missing required key no_proxy")
    else:
        np_parts = {p.strip() for p in no_proxy_val.split(",") if p.strip()}
        if "127.0.0.1" not in np_parts or "localhost" not in np_parts:
            issues.append("invalid key no_proxy (must include localhost and 127.0.0.1)")

    agents_issue = _validate_ocw_agents_root_if_set(env)
    if agents_issue:
        issues.append(agents_issue)

    if issues:
        raise ArchiveEnvValidationError(issues)


def scrub_post_dotenv_import_env(
    target_env: MutableMapping[str, str],
    pre_import_env: Mapping[str, str] | None = None,
    *,
    explicit_model: str | None = None,
    explicit_fallback_model: str | None = None,
    extra_preserve_keys: Iterable[str] = (),
    repo_root: Path | None = None,
    include_reextract_dsn: bool = False,
) -> None:
    """Re-filter `target_env` after late config/dotenv loads (`load_from_files` / `load_dotenv_async`).

    Removes all stripped Feishu/gateway/ASR/broker credentials and shell injection vars,
    drops any non-allowlisted keys newly injected from `.env`, and preserves the caller's
    pre-load environment plus selected model/workspace keys.
    """
    root = repo_root or _repo_root_default()
    pre_env = pre_import_env if pre_import_env is not None else {}

    if explicit_model is not None or explicit_fallback_model is not None:
        assist_raw = target_env.get("DALTON_ASSIST_FALLBACK_MODEL") or pre_env.get(
            "DALTON_ASSIST_FALLBACK_MODEL"
        )
        selected_models = resolve_selected_archive_models(
            explicit_model=explicit_model,
            explicit_fallback_model=explicit_fallback_model,
            assist_fallback_raw=assist_raw,
            repo_root=root,
        )
        allowed_keys, _ = archive_allowed_env_keys(models=selected_models, repo_root=root)
    else:
        allowed_keys = frozenset(
            set(ARCHIVE_BASE_REQUIRED_KEYS)
            | set(ARCHIVE_BASE_OPTIONAL_PROCESS_KEYS)
            | set(ARCHIVE_DB_REQUIRED_KEYS)
            | set(ARCHIVE_DB_OPTIONAL_KEYS)
            | set(ARCHIVE_OWNER_REQUIRED_KEYS)
            | set(ARCHIVE_OWNER_OPTIONAL_MODE_KEYS)
            | set(ARCHIVE_WORKSPACE_RUNTIME_KEYS)
            | {"DALTON_BRIDGE_ADMIN_TOKEN", "DALTON_BRIDGE_EMBED_URL", "no_proxy", "DALTON_OCW_AGENTS_ROOT"}
        )

    if extra_preserve_keys:
        allowed_keys = frozenset(set(allowed_keys) | {str(k) for k in extra_preserve_keys if k})
    if include_reextract_dsn or "DALTON_REEXTRACT_DSN" in pre_env:
        allowed_keys = frozenset(set(allowed_keys) | {"DALTON_REEXTRACT_DSN"})

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


def enforce_archive_process_env_in_place(
    target_env: MutableMapping[str, str],
    *,
    explicit_model: str | None = None,
    explicit_fallback_model: str | None = None,
    plan_model: str | None = None,
    plan_fallback_model: str | None = None,
    plan_path: Path | str | None = None,
    preference_model: str | None = None,
    user_preferences: Mapping[str, Any] | None = None,
    provider_candidates: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    repo_env_path: Path | None = None,
    hydrate_from_dotenv: bool = False,
    preserve_pytest: bool = False,
    validate_required: bool = False,
    defer_primary_model_validation: bool = False,
) -> frozenset[str]:
    """Filter `target_env` in place to the exact archive allowlist for the selected models."""
    root = repo_root or _repo_root_default()
    dotenv_vars = _resolve_dotenv_vars(
        target_env,
        repo_root=root,
        repo_env_path=repo_env_path,
        hydrate_from_dotenv=hydrate_from_dotenv,
    )
    cand_vars: dict[str, str] = {}
    _, _, all_provider_keys = _load_catalog_maps(root)
    for p_key in set(all_provider_keys) | set(XAI_OAUTH_ALLOWED_ENV_KEYS) | {"DALTON_ASSIST_FALLBACK_MODEL"}:
        cur_p_val = str(target_env.get(p_key) or "").strip()
        if cur_p_val:
            cand_vars[p_key] = cur_p_val
    if isinstance(provider_candidates, Mapping):
        for p_key, p_val in provider_candidates.items():
            if str(p_val or "").strip():
                cand_vars[str(p_key)] = str(p_val).strip()
    assist_raw = (
        str(cand_vars.get("DALTON_ASSIST_FALLBACK_MODEL") or "").strip()
        or str(target_env.get("DALTON_ASSIST_FALLBACK_MODEL") or "").strip()
        or str(dotenv_vars.get("DALTON_ASSIST_FALLBACK_MODEL") or "").strip()
        or None
    )
    has_resolved_primary = bool(
        (explicit_model and str(explicit_model).strip())
        or (plan_model and str(plan_model).strip())
        or (preference_model and str(preference_model).strip())
        or user_preferences is not None
        or plan_path is not None
    )
    include_primary = not (defer_primary_model_validation and not has_resolved_primary)
    selected_models = resolve_selected_archive_models(
        explicit_model=explicit_model,
        explicit_fallback_model=explicit_fallback_model,
        plan_model=plan_model,
        plan_fallback_model=plan_fallback_model,
        plan_path=plan_path,
        preference_model=preference_model,
        user_preferences=user_preferences,
        assist_fallback_raw=assist_raw,
        repo_root=root,
        include_primary=include_primary,
    )
    allowed_keys, provider_required = archive_allowed_env_keys(
        models=selected_models, repo_root=root
    )

    for key in allowed_keys:
        if (
            isinstance(provider_candidates, Mapping)
            and key in provider_candidates
            and str(provider_candidates[key] or "").strip()
        ):
            target_env[key] = str(provider_candidates[key]).strip()
            continue
        if str(target_env.get(key) or "").strip():
            continue
        if key in cand_vars and str(cand_vars[key]).strip():
            target_env[key] = str(cand_vars[key]).strip()
            continue
        if dotenv_vars and key in dotenv_vars and str(dotenv_vars[key]).strip():
            target_env[key] = str(dotenv_vars[key]).strip()

    for key in list(target_env.keys()):
        if (
            _is_stripped_credential_key(key)
            or _is_shell_injection_key(key)
            or key == "DALTON_BRIDGE_TOKEN"
        ):
            target_env.pop(key, None)
            continue
        if preserve_pytest and key.startswith("PYTEST_"):
            continue
        if key not in allowed_keys:
            target_env.pop(key, None)

    if not str(target_env.get("PATH") or "").strip():
        target_env["PATH"] = DEFAULT_SYSTEM_PATH
    if not str(target_env.get("HOME") or "").strip():
        target_env["HOME"] = os.path.expanduser("~")
    if not str(target_env.get("LANG") or "").strip():
        target_env["LANG"] = "en_US.UTF-8"
    if not str(target_env.get("TZ") or "").strip():
        target_env["TZ"] = "America/New_York"

    cur_embed = str(target_env.get("DALTON_BRIDGE_EMBED_URL") or "").strip()
    if not cur_embed or "host.docker.internal" in cur_embed:
        target_env["DALTON_BRIDGE_EMBED_URL"] = DEFAULT_HOST_EMBED_URL
    target_env["no_proxy"] = _merge_no_proxy(target_env.get("no_proxy"))

    if validate_required:
        validate_archive_required_env(
            target_env, conditional_required_keys=provider_required
        )
    return provider_required


def reextract_allowed_env_keys(
    subcommand: str,
    *,
    models: Iterable[str | None] = (),
    repo_root: Path | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Return `(allowed_keys, conditionally_required_keys)` scoped to a `reextract_conversations.py` subcommand."""
    cmd = str(subcommand or "").strip()
    allowed: set[str] = (
        set(ARCHIVE_BASE_REQUIRED_KEYS)
        | set(ARCHIVE_BASE_OPTIONAL_PROCESS_KEYS)
        | set(ARCHIVE_DB_REQUIRED_KEYS)
        | set(ARCHIVE_DB_OPTIONAL_KEYS)
    )
    required_cond: frozenset[str] = frozenset()

    if cmd == "plan":
        allowed |= {"DALTON_OCW_AGENTS_ROOT", "DALTON_REEXTRACT_DSN"}
    elif cmd == "extract":
        p_allowed, p_required, _ = resolve_provider_env_keys_for_models(
            models, repo_root=repo_root
        )
        allowed |= (
            {"BYOK_ENCRYPTION_KEY", "DALTON_ASSIST_FALLBACK_MODEL"}
            | set(ARCHIVE_OWNER_REQUIRED_KEYS)
            | set(ARCHIVE_OWNER_OPTIONAL_MODE_KEYS)
            | set(p_allowed)
        )
        required_cond = p_required
    elif cmd in ("apply", "rollback"):
        allowed |= {"DALTON_REEXTRACT_DSN"}
    elif cmd == "render":
        allowed |= (
            set(ARCHIVE_OWNER_REQUIRED_KEYS)
            | set(ARCHIVE_OWNER_OPTIONAL_MODE_KEYS)
            | {"DALTON_BRIDGE_ADMIN_TOKEN", "DALTON_BRIDGE_EMBED_URL", "no_proxy"}
            | {"DALTON_REEXTRACT_DSN"}
        )
    else:
        raise ValueError(f"Unsupported reextract subcommand: {cmd!r}")

    return frozenset(allowed), required_cond


def enforce_reextract_process_env_in_place(
    target_env: MutableMapping[str, str],
    subcommand: str,
    *,
    explicit_model: str | None = None,
    explicit_fallback_model: str | None = None,
    plan_model: str | None = None,
    plan_fallback_model: str | None = None,
    plan_path: Path | str | None = None,
    preference_model: str | None = None,
    user_preferences: Mapping[str, Any] | None = None,
    provider_candidates: Mapping[str, str] | None = None,
    explicit_dsn: str | None = None,
    repo_root: Path | None = None,
    repo_env_path: Path | None = None,
    hydrate_from_dotenv: bool = False,
    preserve_pytest: bool = False,
    validate_required: bool = False,
) -> frozenset[str]:
    """Filter `target_env` in place to the exact least-privilege set for `reextract_conversations.py <subcommand>`."""
    root = repo_root or _repo_root_default()
    cmd = str(subcommand or "").strip()
    dotenv_vars = _resolve_dotenv_vars(
        target_env,
        repo_root=root,
        repo_env_path=repo_env_path,
        hydrate_from_dotenv=hydrate_from_dotenv,
    )
    cand_vars: dict[str, str] = {}
    _, _, all_provider_keys = _load_catalog_maps(root)
    for p_key in set(all_provider_keys) | set(XAI_OAUTH_ALLOWED_ENV_KEYS) | {"DALTON_ASSIST_FALLBACK_MODEL"}:
        cur_p_val = str(target_env.get(p_key) or "").strip()
        if cur_p_val:
            cand_vars[p_key] = cur_p_val
    if isinstance(provider_candidates, Mapping):
        for p_key, p_val in provider_candidates.items():
            if str(p_val or "").strip():
                cand_vars[str(p_key)] = str(p_val).strip()
    selected_models: list[str] = []
    if cmd == "extract":
        assist_raw = (
            str(cand_vars.get("DALTON_ASSIST_FALLBACK_MODEL") or "").strip()
            or str(target_env.get("DALTON_ASSIST_FALLBACK_MODEL") or "").strip()
            or str(dotenv_vars.get("DALTON_ASSIST_FALLBACK_MODEL") or "").strip()
            or None
        )
        selected_models = resolve_selected_archive_models(
            explicit_model=explicit_model,
            explicit_fallback_model=explicit_fallback_model,
            plan_model=plan_model,
            plan_fallback_model=plan_fallback_model,
            plan_path=plan_path,
            preference_model=preference_model,
            user_preferences=user_preferences,
            assist_fallback_raw=assist_raw,
            repo_root=root,
        )
    allowed_keys, cond_required = reextract_allowed_env_keys(
        cmd, models=selected_models, repo_root=root
    )

    for key in allowed_keys:
        if (
            isinstance(provider_candidates, Mapping)
            and key in provider_candidates
            and str(provider_candidates[key] or "").strip()
        ):
            target_env[key] = str(provider_candidates[key]).strip()
            continue
        if str(target_env.get(key) or "").strip():
            continue
        if key in cand_vars and str(cand_vars[key]).strip():
            target_env[key] = str(cand_vars[key]).strip()
            continue
        if dotenv_vars and key in dotenv_vars and str(dotenv_vars[key]).strip():
            target_env[key] = str(dotenv_vars[key]).strip()

    if (
        cmd in ("plan", "apply", "render", "rollback")
        and explicit_dsn
        and str(explicit_dsn).strip()
    ):
        target_env["DALTON_REEXTRACT_DSN"] = str(explicit_dsn).strip()

    for key in list(target_env.keys()):
        if (
            _is_stripped_credential_key(key)
            or _is_shell_injection_key(key)
            or key == "DALTON_BRIDGE_TOKEN"
        ):
            target_env.pop(key, None)
            continue
        if preserve_pytest and key.startswith("PYTEST_"):
            continue
        if key not in allowed_keys:
            target_env.pop(key, None)

    if not str(target_env.get("PATH") or "").strip():
        target_env["PATH"] = DEFAULT_SYSTEM_PATH
    if not str(target_env.get("HOME") or "").strip():
        target_env["HOME"] = os.path.expanduser("~")
    if not str(target_env.get("LANG") or "").strip():
        target_env["LANG"] = "en_US.UTF-8"
    if not str(target_env.get("TZ") or "").strip():
        target_env["TZ"] = "America/New_York"

    if cmd == "render":
        cur_embed = str(target_env.get("DALTON_BRIDGE_EMBED_URL") or "").strip()
        if not cur_embed or "host.docker.internal" in cur_embed:
            target_env["DALTON_BRIDGE_EMBED_URL"] = DEFAULT_HOST_EMBED_URL
        target_env["no_proxy"] = _merge_no_proxy(target_env.get("no_proxy"))

    if validate_required:
        validate_reextract_required_env(
            target_env,
            cmd,
            conditional_required_keys=cond_required,
            explicit_dsn=explicit_dsn,
        )
    return cond_required


def _is_valid_postgres_dsn(dsn: str) -> bool:
    s = str(dsn or "").strip()
    if not s:
        return False
    if s.startswith(("postgresql://", "postgres://")):
        parsed = urlparse(s)
        return bool(parsed.hostname or parsed.path.strip("/"))
    return "dbname=" in s or "host=" in s


def validate_reextract_required_env(
    env: Mapping[str, str],
    subcommand: str,
    *,
    conditional_required_keys: Iterable[str] = (),
    explicit_dsn: str | None = None,
) -> None:
    """Validate required keys for `reextract_conversations.py <subcommand>` without logging values."""
    cmd = str(subcommand or "").strip()
    issues: list[str] = []

    for key in ARCHIVE_BASE_REQUIRED_KEYS:
        if not str(env.get(key) or "").strip():
            issues.append(f"missing required key {key}")

    dsn_val = (
        str(explicit_dsn).strip()
        if explicit_dsn and str(explicit_dsn).strip()
        else str(env.get("DALTON_REEXTRACT_DSN") or "").strip()
    )
    has_dsn = bool(cmd in ("plan", "apply", "render", "rollback") and dsn_val)
    if has_dsn and not _is_valid_postgres_dsn(dsn_val):
        issues.append(
            "invalid key DALTON_REEXTRACT_DSN (must be a postgresql:// URI or libpq conninfo)"
        )
    if not has_dsn or (cmd == "render"):
        # Note: `render --yes` also opens the app pool for workspace resolution; dry-run with
        # explicit `--dsn` can skip DB_* only when `DALTON_REEXTRACT_DSN` / `--dsn` is given
        # and DB_HOST is unset, unless app pool is needed. Keep DB_* required whenever no DSN.
        if not has_dsn:
            for key in ARCHIVE_DB_REQUIRED_KEYS:
                if not str(env.get(key) or "").strip():
                    issues.append(f"missing required key {key}")

    if "DB_PORT" in env and str(env.get("DB_PORT") or "").strip():
        try:
            port = int(str(env["DB_PORT"]).strip())
            if not (1 <= port <= 65535):
                issues.append("invalid key DB_PORT (out of range 1..65535)")
        except ValueError:
            issues.append("invalid key DB_PORT (not an integer)")

    if cmd == "plan":
        agents_issue = _validate_ocw_agents_root_if_set(env)
        if agents_issue:
            issues.append(agents_issue)
    elif cmd == "extract":
        if not str(env.get("BYOK_ENCRYPTION_KEY") or "").strip():
            issues.append("missing required key BYOK_ENCRYPTION_KEY")
        for key in ARCHIVE_OWNER_REQUIRED_KEYS:
            if not str(env.get(key) or "").strip():
                issues.append(f"missing required key {key}")
        for key in sorted(set(conditional_required_keys)):
            if not str(env.get(key) or "").strip():
                issues.append(f"missing conditional model key {key}")
    elif cmd == "render":
        for key in ARCHIVE_OWNER_REQUIRED_KEYS:
            if not str(env.get(key) or "").strip():
                issues.append(f"missing required key {key}")
        if not str(env.get("DALTON_BRIDGE_ADMIN_TOKEN") or "").strip():
            issues.append("missing required key DALTON_BRIDGE_ADMIN_TOKEN")
        embed_issue = _validate_host_embed_url(env.get("DALTON_BRIDGE_EMBED_URL", ""))
        if embed_issue:
            issues.append(embed_issue)
        no_proxy_val = str(env.get("no_proxy") or "").strip()
        if not no_proxy_val:
            issues.append("missing required key no_proxy")
        else:
            np_parts = {p.strip() for p in no_proxy_val.split(",") if p.strip()}
            if "127.0.0.1" not in np_parts or "localhost" not in np_parts:
                issues.append("invalid key no_proxy (must include localhost and 127.0.0.1)")

    if issues:
        raise ArchiveEnvValidationError(issues)


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


def _fsync_directory(dir_path: Path) -> None:
    try:
        fd = os.open(str(dir_path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
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
) -> tuple[str, dict[str, str]]:
    """Preserve all existing lines/comments and existing non-empty values; fill missing or empty default keys."""
    effective = dict(existing_values)
    default_map = dict(defaults)
    out_lines: list[str] = []
    seen_default_keys: set[str] = set()

    for kind, key, raw_line in entries:
        if kind != "kv" or key is None:
            out_lines.append(raw_line)
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


def merge_bridge_and_repo_env_files(
    *,
    bridge_env_path: Path,
    repo_env_path: Path,
    default_port: str = "8960",
    default_ocw_root: str,
    default_python_bin: str,
) -> dict[str, Any]:
    """Validate and atomically merge defaults into `researchdata-bridge.env` and `<repo>/.env`.

    - Existing comments, ordering, extra keys (`FEISHU_*`, `SEED_ASR_*`, etc.), and all
      existing non-empty values are preserved verbatim.
    - Existing tokens are never rotated; existing `FEISHU_*` values are never overwritten.
    - Duplicate keys, malformed lines, partial Feishu credentials (`FEISHU_APP_ID` without
      `FEISHU_APP_SECRET` or vice versa), and invalid required values fail closed before
      modifying either target file.
    - Returns non-secret metadata only (`port`, `ocw_root`, `python_bin`).
    """
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
        ("DALTON_OCW_ROOT", str(default_ocw_root).strip()),
        ("DALTON_OCW_PYTHON", str(default_python_bin).strip()),
        ("DALTON_BRIDGE_MAX_INFLIGHT", "2"),
        ("DALTON_BRIDGE_CALLER_BUDGET_S", "625"),
    ]
    bridge_rendered, bridge_eff = _apply_env_defaults(
        bridge_entries, bridge_vals, bridge_defaults
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

    eff_ocw_root = str(bridge_eff.get("DALTON_OCW_ROOT") or "").strip()
    _validate_no_control_or_whitespace(
        path=bridge_env_path,
        key="DALTON_OCW_ROOT",
        value=eff_ocw_root,
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

    repo_defaults: list[tuple[str, str]] = [
        ("DALTON_BRIDGE_TOKEN", str(bridge_eff["DALTON_BRIDGE_TOKEN"]).strip()),
        ("DALTON_BRIDGE_ADMIN_TOKEN", str(bridge_eff["DALTON_BRIDGE_ADMIN_TOKEN"]).strip()),
        ("DALTON_RESEARCHDATA_URL", f"http://host.docker.internal:{port_int}/mcp"),
        ("SEC_USER_AGENT", "DaltonResearch research@dalton.local"),
    ]
    repo_rendered, repo_eff = _apply_env_defaults(repo_entries, repo_vals, repo_defaults)

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
        "ocw_root": eff_ocw_root,
        "python_bin": str(py_path),
    }


def render_plist_atomic(
    *,
    template_path: Path,
    dest_path: Path,
    bridge_env_path: Path,
    repo_root: Path,
    home_dir: Path,
    logs_dir: Path | None = None,
) -> int:
    """Render launchd plist atomically (`0644`) using non-secret paths from `bridge_env_path`."""
    validate_safe_regular_file_target(bridge_env_path, allow_missing=False)
    validate_safe_regular_file_target(template_path, allow_missing=False)
    validate_safe_regular_file_target(dest_path, allow_missing=True)
    _, bridge_vals = parse_env_file_strict(bridge_env_path)
    port_int = int(str(bridge_vals.get("DALTON_BRIDGE_PORT") or "8960").strip())
    ocw_root = str(bridge_vals.get("DALTON_OCW_ROOT") or "").strip()
    python_bin = str(bridge_vals.get("DALTON_OCW_PYTHON") or "").strip()
    tpl = template_path.read_text(encoding="utf-8")
    eff_logs = logs_dir if logs_dir is not None else (home_dir / "Library" / "Logs")
    validate_safe_directory_target(eff_logs)
    eff_logs.mkdir(parents=True, exist_ok=True)
    validate_safe_directory_target(eff_logs)
    rendered = (
        tpl.replace("__HOME__/.config/dalton/researchdata-bridge.env", str(bridge_env_path))
        .replace("__HOME__/Library/Logs", str(eff_logs))
        .replace("__PYTHON_BIN__", python_bin)
        .replace("__REPO_ROOT__", str(repo_root))
        .replace("__OCW_ROOT__", ocw_root)
        .replace("__HOME__", str(home_dir))
    )
    atomic_write_text(dest_path, rendered, mode=0o644)
    return port_int


# ---------------------------------------------------------------------------
# CLI entrypoints (`exec-nonarchive`, `exec-archive`, `merge-bridge-env`, etc.)
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


def _cmd_exec_archive(args: argparse.Namespace) -> int:
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("bridge_subprocess_env exec-archive: missing command", file=sys.stderr)
        return 2

    repo_root = Path(args.repo_root).resolve() if args.repo_root else _repo_root_default()
    repo_env = Path(args.repo_env).expanduser() if args.repo_env else (repo_root / ".env")
    bridge_env = (
        Path(args.bridge_env).expanduser()
        if args.bridge_env
        else Path(os.path.expanduser("~/.config/dalton/researchdata-bridge.env"))
    )
    explicit_model = args.model or _extract_model_arg_from_argv(cmd)
    defer_primary = not bool(explicit_model)
    caller_candidates = capture_archive_provider_candidates(
        os.environ,
        repo_root=repo_root,
        repo_env_path=repo_env,
        hydrate_from_dotenv=True,
    )
    try:
        env = build_archive_subprocess_env(
            repo_env_path=repo_env,
            bridge_env_path=bridge_env,
            base_env=os.environ,
            explicit_model=explicit_model,
            host_embed_url=args.embed_url or DEFAULT_HOST_EMBED_URL,
            repo_root=repo_root,
            validate_required=True,
            defer_primary_model_validation=defer_primary,
        )
        candidates_rfd = finalize_and_attach_archive_candidates(
            caller_candidates,
            env,
            repo_root=repo_root,
        )
    except ArchiveEnvValidationError as exc:
        print(f"[archive_env] ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        os.execvpe(cmd[0], cmd, env)
    except BaseException:
        if candidates_rfd is not None:
            try:
                os.close(candidates_rfd)
            except OSError:
                pass
        raise
    return 127


def _cmd_merge_bridge_env(args: argparse.Namespace) -> int:
    try:
        meta = merge_bridge_and_repo_env_files(
            bridge_env_path=Path(args.bridge_env).expanduser(),
            repo_env_path=Path(args.repo_env).expanduser(),
            default_port=str(args.default_port),
            default_ocw_root=str(args.default_ocw_root),
            default_python_bin=str(args.default_python),
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

    p_ar = sub.add_parser("exec-archive", help="Build archive allowlist env, validate, and exec")
    p_ar.add_argument("--repo-env", default=None)
    p_ar.add_argument("--bridge-env", default=None)
    p_ar.add_argument("--repo-root", default=None)
    p_ar.add_argument("--model", default=None)
    p_ar.add_argument("--embed-url", default=DEFAULT_HOST_EMBED_URL)
    p_ar.add_argument("command", nargs=argparse.REMAINDER)

    p_mg = sub.add_parser("merge-bridge-env", help="Atomically merge defaults into bridge and repo env files")
    p_mg.add_argument("--bridge-env", required=True)
    p_mg.add_argument("--repo-env", required=True)
    p_mg.add_argument("--default-port", default="8960")
    p_mg.add_argument("--default-ocw-root", required=True)
    p_mg.add_argument("--default-python", required=True)
    p_mg.add_argument("--print-port-only", action="store_true")

    p_pl = sub.add_parser("render-plist", help="Atomically render launchd plist")
    p_pl.add_argument("--template", required=True)
    p_pl.add_argument("--dest", required=True)
    p_pl.add_argument("--bridge-env", required=True)
    p_pl.add_argument("--repo-root", required=True)
    p_pl.add_argument("--home", required=True)
    p_pl.add_argument("--logs-dir", default=None)

    p_ck = sub.add_parser("check-env-keys", help="Output boolean presence map for named keys")
    p_ck.add_argument("keys", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_cli_parser().parse_args(argv)
    if args.subcommand == "exec-nonarchive":
        return _cmd_exec_nonarchive(args)
    if args.subcommand == "exec-archive":
        return _cmd_exec_archive(args)
    if args.subcommand == "merge-bridge-env":
        return _cmd_merge_bridge_env(args)
    if args.subcommand == "render-plist":
        return _cmd_render_plist(args)
    if args.subcommand == "check-env-keys":
        return _cmd_check_env_keys(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
