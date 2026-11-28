"""Main-bucket consumer reference audit: warning-only startup check & strict operator CLI.

Shared by Everflow and Standalone Core (``CORE_CAPABILITY = None``).

Two modes with clear separation of responsibilities:

1. **Backend startup advisory check** (:func:`warn_main_bucket_references_on_startup`):
   - No-op when ``is_storage_enabled()`` is True.
   - Runs fast, index-backed probes over exact pointer tables (no recent-N windows,
     no plain-string checkpoint scans, no URL-shape avatar false positives).
   - Logs a ``WARNING`` listing hit tables and row counts (never object keys, URLs,
     or row contents). **Never refuses or blocks backend startup** on any result or
     error.

2. **Operator pre-shutdown strict audit CLI**::

       python -m src.dalton.storage_refs check --strict

   Run manually by operators **before disabling the main bucket** (closing the main
   bucket requires exit code ``0``):
   - Batched full-table scans across all 17 main-bucket consumers (no recent-N
     windows), with progress output to ``stderr`` and clean ``SIGINT`` interruption.
   - Stops immediately on the first confirmed reference hit (fail-fast).
   - Structurally verifies widget events and ``data_ref.key`` using the exact
     replay contract (string hits are only a candidate pre-filter; plain message
     text never triggers false positives).
   - Avatars treat only an upload-recorded object key column as a confirmed hit;
     URL-shape-only evidence logs a suspected ``WARNING`` and does NOT count as a
     confirmed hit.
   - Exit codes: ``0`` = no confirmed references, ``1`` = confirmed reference found,
     ``2`` = check failed or interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import tarfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import unquote, urlsplit, urlunsplit

from src.dalton.storage_bucket_rules import (
    ROLE_MAIN,
    ROLE_SECONDARY,
    RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE,
    TRANSCRIPTION_AUDIO_EXPIRE_DAYS,
    is_key_covered_by_transcription_audio_expire_rule,
)
from src.dalton.storage_keys import storage_user_id
from src.server.database.pool import get_db_connection, open_session_connection
from src.utils.storage import is_storage_enabled

logger = logging.getLogger(__name__)

STRICT_MAIN_BUCKET_NAME = "dalton-main"

STARTUP_STATEMENT_TIMEOUT = "5s"
STARTUP_COUNT_CAP = 1000
STRICT_BATCH_SIZE = 500
STRICT_BATCH_TIMEOUT = "30s"

EXIT_CLEAN = 0
EXIT_REFERENCES_FOUND = 1
EXIT_CHECK_FAILED = 2

_ERE_SPECIAL_RE = re.compile(r"([\\.^$*+?()[\]{}|])")
_AVATAR_KEY_TAIL_RE = re.compile(r"^[^/?#]+\.[A-Za-z0-9]+$")
_AVATAR_OBJECT_KEY_COLUMNS = ("avatar_object_key", "avatar_key")
_RESPONSE_IMAGE_STRICT_TAIL_RE = re.compile(r"^(?:[^/?#]+/)?[0-9a-f]{12,64}/[^/?#]+$")
_MARKDOWN_IMAGE_OR_LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)]+)\)")
_HTML_IMG_SRC_RE = re.compile(
    r"""<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)


def _origin_from_split(parsed: Any) -> str | None:
    """Return normalized ``scheme://host[:port]`` for an http(s) ``urlsplit`` result, or None."""
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    host = parsed.hostname
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    host_fmt = f"[{host}]" if ":" in host else host
    default_port = 80 if scheme == "http" else 443
    if port is not None and port != default_port:
        return f"{scheme}://{host_fmt}:{port}"
    return f"{scheme}://{host_fmt}"


def _escape_ere(literal: str) -> str:
    return _ERE_SPECIAL_RE.sub(r"\\\1", literal)


def _normalize_storage_key_prefix(env: Mapping[str, str] | None = None) -> str:
    em = os.environ if env is None else env
    raw = (em.get("STORAGE_KEY_PREFIX") or "").strip()
    if not raw or raw.startswith("/") or ".." in raw.split("/"):
        return ""
    return raw if raw.endswith("/") else f"{raw}/"


def site_avatar_url_rules(
    *, env: Mapping[str, str] | None = None
) -> list[tuple[str, str, str]]:
    """Return ``(origin, full_avatar_path_prefix, pg_ere_pattern)`` rules for this site.

    Matches ONLY URLs shaped like this deployment's main-bucket
    ``get_public_url("avatars/<id>.<ext>")``:
    1. Origin (case-insensitive ``scheme://host[:port]``) and configured public base path
    2. Configured ``STORAGE_KEY_PREFIX`` (if any)
    3. Single-segment object key ``avatars/<name>.<ext>``

    Used solely for suspected-avatar warnings when no upload-recorded object key
    column exists; external avatar URLs (OAuth/Gravatar/third-party CDNs) never match.
    """
    em = os.environ if env is None else env
    candidates: list[str] = []

    explicit_base = (
        em.get("STORAGE_PUBLIC_URL_BASE") or em.get("S3_PUBLIC_URL_BASE") or ""
    ).strip()
    if explicit_base:
        candidates.append(explicit_base)
    else:
        if "STORAGE_BUCKET_NAME" in em:
            s3_bucket = (em.get("STORAGE_BUCKET_NAME") or "").strip()
        else:
            s3_bucket = (em.get("S3_BUCKET_NAME") or "").strip()
        if s3_bucket:
            region = (em.get("STORAGE_REGION") or em.get("S3_REGION") or "us-east-1").strip()
            candidates.append(f"https://{s3_bucket}.s3.{region}.amazonaws.com")

        oss_bucket = (em.get("OSS_BUCKET_NAME") or "").strip()
        oss_endpoint = (em.get("OSS_ENDPOINT") or "").strip()
        if oss_bucket and oss_endpoint:
            candidates.append(f"https://{oss_bucket}.{oss_endpoint}")

        public_app_url = (em.get("PUBLIC_APP_URL") or "").strip()
        if public_app_url:
            candidates.append(f"{public_app_url.rstrip('/')}/storage")

    key_prefix = _normalize_storage_key_prefix(em)
    rules: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_base in candidates:
        try:
            parsed = urlsplit(raw_base.rstrip("/"))
        except ValueError:
            continue
        if not parsed.scheme or not parsed.netloc:
            continue
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        base_path = parsed.path.rstrip("/")
        full_prefix = f"{base_path}/{key_prefix}avatars/"
        pair = (origin, full_prefix)
        if pair in seen:
            continue
        seen.add(pair)
        path_ere = f"^{_escape_ere(full_prefix)}[^/?#]+\\.[A-Za-z0-9]+([?#].*)?$"
        rules.append((origin, full_prefix, path_ere))
    return rules


def is_site_avatar_url(
    avatar_url: str | None, *, env: Mapping[str, str] | None = None
) -> bool:
    """Return True iff ``avatar_url`` matches this deployment's main-bucket avatar URL shape."""
    raw = (avatar_url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    if not parsed.scheme or not parsed.netloc:
        return False
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    path = parsed.path or ""
    for rule_origin, full_prefix, _path_ere in site_avatar_url_rules(env=env):
        if origin != rule_origin:
            continue
        if not path.startswith(full_prefix):
            continue
        tail = path[len(full_prefix) :]
        if _AVATAR_KEY_TAIL_RE.match(tail):
            return True
    return False


# ---------------------------------------------------------------------------
# Structural widget verification (aligned with replay/widgets.py & projector.py)
# ---------------------------------------------------------------------------


def _widget_payload_has_data_ref(payload: Any) -> bool:
    """Match ``replay.widgets._resolve_widget_data_refs`` (widgets.py:33-38).

    A widget payload references object storage iff it is a dict without an
    inlined ``data`` key and carries ``data_ref: {"key": "<non-empty str>"}``.
    """
    if not isinstance(payload, dict) or "data" in payload:
        return False
    ref = payload.get("data_ref")
    if not isinstance(ref, dict):
        return False
    key = ref.get("key")
    return isinstance(key, str) and bool(key.strip())


def turn_slice_has_widget_data_ref(lines_text: str | None) -> bool:
    """Return True iff ``turn_slices.lines`` contains a replayable html_widget ``data_ref.key``."""
    if not lines_text or '"data_ref"' not in lines_text:
        return False
    for raw in lines_text.split("\n"):
        if "\t" not in raw:
            continue
        parts = raw.split("\t", 2)
        if len(parts) != 3:
            continue
        event, _flags, data_json = parts
        if event != "artifact":
            continue
        try:
            data = json.loads(data_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("artifact_type") != "html_widget":
            continue
        if _widget_payload_has_data_ref(data.get("payload")):
            return True
    return False


def _is_show_widget_tool_artifact(tool_name: Any, artifact: Any) -> bool:
    """Match ``history.projector._build_artifact_event`` for ``ShowWidget``."""
    if not isinstance(artifact, dict) or not artifact:
        return False
    if isinstance(tool_name, str) and tool_name:
        if tool_name != "ShowWidget":
            return False
    else:
        art_type = artifact.get("type") or artifact.get("artifact_type")
        if art_type != "html_widget":
            return False
    return _widget_payload_has_data_ref(artifact)


def checkpoint_value_has_widget_data_ref(value: Any) -> bool:
    """Return True iff a deserialized ``messages`` checkpoint value has a ShowWidget ``data_ref.key``.

    Inspects ONLY ``ToolMessage.artifact`` for ``ShowWidget`` (never message
    ``content`` text), so ordinary user/assistant/tool messages discussing
    ``data_ref`` or ``widgets/`` never trigger false positives.
    """
    if value is None:
        return False

    # Unwrap LangGraph DeltaChannel snapshot container without hard-importing private types at module import.
    if type(value).__name__ == "_DeltaSnapshot" and hasattr(value, "value"):
        return checkpoint_value_has_widget_data_ref(value.value)

    if isinstance(value, (list, tuple)):
        return any(checkpoint_value_has_widget_data_ref(item) for item in value)

    # LangChain ToolMessage instance (check type == 'tool' and .artifact, ignoring .content)
    msg_type = getattr(value, "type", None)
    if msg_type == "tool" and hasattr(value, "artifact"):
        return _is_show_widget_tool_artifact(
            getattr(value, "name", None), getattr(value, "artifact", None)
        )

    # Serialized dict representations (plain dict or LangChain constructor dict)
    if isinstance(value, dict):
        if value.get("type") == "tool" and "artifact" in value:
            return _is_show_widget_tool_artifact(value.get("name"), value.get("artifact"))
        if (
            value.get("lc") == 1
            and value.get("type") == "constructor"
            and isinstance(value.get("id"), list)
            and value["id"]
            and value["id"][-1] == "ToolMessage"
            and isinstance(value.get("kwargs"), dict)
        ):
            kw = value["kwargs"]
            return _is_show_widget_tool_artifact(kw.get("name"), kw.get("artifact"))

    return False


_CHECKPOINT_SERDE: Any = None


def _get_checkpoint_serde() -> Any:
    global _CHECKPOINT_SERDE
    if _CHECKPOINT_SERDE is None:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        _CHECKPOINT_SERDE = JsonPlusSerializer()
    return _CHECKPOINT_SERDE


def _site_main_bucket_url_bases(env: Mapping[str, str] | None = None) -> list[str]:
    """Return all configured URL bases that this site or main bucket can emit."""
    em = os.environ if env is None else env
    candidates: list[str] = []

    for key in ("STORAGE_PUBLIC_URL_BASE", "S3_PUBLIC_URL_BASE"):
        val = (em.get(key) or "").strip()
        if val:
            candidates.append(val)

    public_app_url = (em.get("PUBLIC_APP_URL") or "").strip()
    if public_app_url:
        candidates.append(f"{public_app_url.rstrip('/')}/storage")

    if "STORAGE_BUCKET_NAME" in em:
        s3_bucket = (em.get("STORAGE_BUCKET_NAME") or "").strip()
    else:
        s3_bucket = (em.get("S3_BUCKET_NAME") or "").strip()
    if s3_bucket:
        region = (em.get("STORAGE_REGION") or em.get("S3_REGION") or "us-east-1").strip()
        candidates.append(f"https://{s3_bucket}.s3.{region}.amazonaws.com")
        for ep_raw in (
            (em.get("STORAGE_ENDPOINT_URL") or "").strip()
            or (em.get("S3_ENDPOINT_URL") or "").strip(),
            (em.get("STORAGE_BROWSER_ENDPOINT_URL") or "").strip(),
        ):
            if not ep_raw:
                continue
            candidates.append(f"{ep_raw.rstrip('/')}/{s3_bucket}")
            try:
                ep = urlsplit(ep_raw.rstrip("/"))
            except ValueError:
                continue
            ep_origin = _origin_from_split(ep)
            if ep_origin is not None and ep.hostname and not ep.hostname.lower().startswith(
                f"{s3_bucket.lower()}."
            ):
                ep_scheme, _, ep_hostport = ep_origin.partition("://")
                candidates.append(f"{ep_scheme}://{s3_bucket}.{ep_hostport}{ep.path.rstrip('/')}")

    oss_bucket = (em.get("OSS_BUCKET_NAME") or "").strip()
    oss_endpoint = (em.get("OSS_ENDPOINT") or "").strip()
    if oss_bucket and oss_endpoint:
        if "://" in oss_endpoint:
            try:
                ep = urlsplit(oss_endpoint.rstrip("/"))
            except ValueError:
                ep = None
            ep_origin = _origin_from_split(ep) if ep is not None else None
            if ep is not None and ep_origin is not None:
                ep_scheme, _, ep_hostport = ep_origin.partition("://")
                candidates.append(f"{ep_scheme}://{oss_bucket}.{ep_hostport}{ep.path.rstrip('/')}")
        else:
            candidates.append(f"https://{oss_bucket}.{oss_endpoint.strip('/')}")
    return candidates


def _site_prefix_rules(
    sub_prefixes: Sequence[str], *, env: Mapping[str, str] | None = None
) -> list[tuple[str | None, str, str]]:
    """Build ``(origin_or_none, full_url_path_prefix, logical_prefix)`` rules for main-bucket paths."""
    em = os.environ if env is None else env
    candidates = _site_main_bucket_url_bases(em)
    key_prefix = _normalize_storage_key_prefix(em)
    rules: list[tuple[str | None, str, str]] = []
    seen: set[tuple[str | None, str, str]] = set()
    for raw_base in candidates:
        if raw_base.startswith("/") and not raw_base.startswith("//"):
            base_path = raw_base.rstrip("/")
            for sub in sub_prefixes:
                full_prefix = f"{base_path}/{key_prefix}{sub}"
                triple: tuple[str | None, str, str] = (None, full_prefix, sub)
                if triple not in seen:
                    seen.add(triple)
                    rules.append(triple)
            continue
        try:
            parsed = urlsplit(raw_base.rstrip("/"))
        except ValueError:
            continue
        origin = _origin_from_split(parsed)
        if origin is None:
            continue
        base_path = parsed.path.rstrip("/")
        for sub in sub_prefixes:
            full_prefix = f"{base_path}/{key_prefix}{sub}"
            triple = (origin, full_prefix, sub)
            if triple not in seen:
                seen.add(triple)
                rules.append(triple)
    return rules


_RESPONSE_IMAGE_LOGICAL_PREFIXES: tuple[str, ...] = (
    "private-media/response-images/",
    "response-images/",
)
_ATTACHMENT_LOGICAL_PREFIXES: tuple[str, ...] = (
    "private-media/attachments/",
    "attachments/",
)


def site_response_image_url_rules(
    *, env: Mapping[str, str] | None = None
) -> list[tuple[str | None, str]]:
    """Return ``(origin_or_none, full_response_images_path_prefix)`` rules for this site/bucket.

    Covers both current ``private-media/response-images/`` and legacy ``response-images/``
    prefixes across every URL base that this deployment's main bucket or site proxy can emit:
    - ``STORAGE_PUBLIC_URL_BASE`` / ``S3_PUBLIC_URL_BASE`` (absolute or site-relative ``/...``)
    - ``PUBLIC_APP_URL`` + ``/storage``
    - Configured S3 bucket (AWS default virtual host + ``STORAGE_ENDPOINT_URL`` /
      ``S3_ENDPOINT_URL`` / ``STORAGE_BROWSER_ENDPOINT_URL`` in path and virtual-hosted styles)
    - Configured OSS bucket (``OSS_BUCKET_NAME`` + ``OSS_ENDPOINT``)
    """
    return [
        (origin, full_prefix)
        for origin, full_prefix, _logical in _site_prefix_rules(
            _RESPONSE_IMAGE_LOGICAL_PREFIXES, env=env
        )
    ]


def _is_valid_response_image_tail(tail: str, *, require_hash: bool) -> bool:
    if not tail or tail.startswith("/"):
        return False
    segments = tail.split("/")
    if any(not seg or seg in (".", "..") for seg in segments):
        return False
    if require_hash:
        return bool(_RESPONSE_IMAGE_STRICT_TAIL_RE.match(tail))
    return True


def is_main_bucket_response_image_url(
    url: str | None, *, env: Mapping[str, str] | None = None
) -> bool:
    """Return True iff ``url`` points to a main-bucket ``response-images/`` object.

    Checks ONLY the parsed URL origin and path (never the query string or fragment):
    - When site or main-bucket URL bases are configured in ``env``, requires a matching
      origin (or site-relative path) AND matching object path prefix ending in
      ``{key_prefix}response-images/``.
    - When no site or main-bucket URL base is configured in ``env`` (e.g. an operator
      already cleared ``STORAGE_*`` env vars before running ``check --strict``), falls back
      to requiring the exact ``/{key_prefix}response-images/`` or
      ``/storage/{key_prefix}response-images/`` object path prefix with a content-addressed
      or legacy-batch hex key segment (``<hash>/<file>`` or ``<thread>/<hash>/<file>``).
    - External URLs that merely contain ``response-images/`` in a query parameter never match.
    - Malformed URLs return False without raising.
    """
    raw = (url or "").strip()
    if not raw or "response-images/" not in raw:
        return False
    for logical_prefix in _RESPONSE_IMAGE_LOGICAL_PREFIXES:
        if raw.startswith(logical_prefix):
            tail = unquote(raw[len(logical_prefix) :])
            return _is_valid_response_image_tail(tail, require_hash=True)
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    path = parsed.path or ""
    if "response-images/" not in path:
        return False

    rules = site_response_image_url_rules(env=env)
    if rules:
        if parsed.scheme:
            origin = _origin_from_split(parsed)
            if origin is None:
                return False
            for rule_origin, full_prefix in rules:
                if rule_origin == origin and path.startswith(full_prefix):
                    if _is_valid_response_image_tail(
                        path[len(full_prefix) :], require_hash=False
                    ):
                        return True
            return False
        if parsed.netloc or not path.startswith("/"):
            return False
        for _rule_origin, full_prefix in rules:
            if path.startswith(full_prefix) and _is_valid_response_image_tail(
                path[len(full_prefix) :], require_hash=False
            ):
                return True
        return False

    # Fallback when no site/main-bucket URL base is configured in env:
    # still enforce valid http(s)/relative URL + exact root or /storage/ object prefix + hex key shape.
    if parsed.scheme:
        if _origin_from_split(parsed) is None:
            return False
    elif parsed.netloc or not path.startswith("/"):
        return False
    key_prefix = _normalize_storage_key_prefix(env)
    for prefix in (
        f"/{key_prefix}private-media/response-images/",
        f"/storage/{key_prefix}private-media/response-images/",
        f"/{key_prefix}response-images/",
        f"/storage/{key_prefix}response-images/",
    ):
        if path.startswith(prefix) and _is_valid_response_image_tail(
            path[len(prefix) :], require_hash=True
        ):
            return True
    return False


def _captured_images_map_has_ref(
    captured: Any, *, env: Mapping[str, str] | None = None
) -> bool:
    """Return True iff a ``captured_images``, ``path_to_url``, or ``images`` map names a main-bucket response image."""
    if not isinstance(captured, dict):
        return False
    return any(
        bool(k)
        and isinstance(v, str)
        and is_main_bucket_response_image_url(v, env=env)
        for k, v in captured.items()
    )


def _extract_markdown_target_url(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<"):
        end = target.find(">")
        if end > 1:
            return target[1:end].strip()
    return target.split(None, 1)[0] if target else ""


def _text_has_main_bucket_response_image_url(
    text: str, *, env: Mapping[str, str] | None = None
) -> bool:
    """Return True iff markdown embeds/links or HTML ``<img>`` tags in ``text`` reference a main-bucket response image."""
    if not text or "response-images/" not in text:
        return False
    for match in _MARKDOWN_IMAGE_OR_LINK_RE.finditer(text):
        target = _extract_markdown_target_url(match.group(3))
        if is_main_bucket_response_image_url(target, env=env):
            return True
    for match in _HTML_IMG_SRC_RE.finditer(text):
        src = match.group(1) or match.group(2) or match.group(3) or ""
        if is_main_bucket_response_image_url(src, env=env):
            return True
    return False


def _legacy_verbatim_has_response_image_ref(
    verbatim: Any, *, env: Mapping[str, str] | None = None
) -> bool:
    """Return True iff ``replay_facts.legacy.verbatim`` plays back any main-bucket response image URL.

    Matches ``src/server/services/history/replay/legacy.py:230-236``: joins streamed
    ``message_chunk`` text pieces per ``(agent, id)`` before extracting markdown image/link
    and HTML ``<img>`` URLs, and also checks any archived ``image_capture`` records.
    """
    if not isinstance(verbatim, list):
        return False

    grouped_texts: dict[tuple[Any, Any], list[str]] = {}
    for stored in verbatim:
        if not isinstance(stored, dict):
            continue
        event = stored.get("event")
        data = stored.get("data")
        if not isinstance(data, dict):
            continue
        if event == "message_chunk":
            content_type = data.get("content_type")
            content = data.get("content")
            if content_type in ("text", None) and isinstance(content, str) and content:
                grouped_texts.setdefault((data.get("agent"), data.get("id")), []).append(content)
        elif event == "image_capture" or data.get("name") == "image_capture":
            props = data.get("props")
            if isinstance(props, dict) and _captured_images_map_has_ref(
                props.get("path_to_url"), env=env
            ):
                return True
            if _captured_images_map_has_ref(data.get("path_to_url"), env=env):
                return True

    for pieces in grouped_texts.values():
        if _text_has_main_bucket_response_image_url("".join(pieces), env=env):
            return True
    return False


def replay_facts_has_response_image_ref(
    replay_facts: Any,
    *,
    include_legacy: bool = True,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Structurally verify ``replay_facts`` for main-bucket response-image references.

    Checks:
    - ``replay_facts["images"]``
    - (when ``include_legacy``) ``replay_facts["legacy"]["images"]`` and
      ``replay_facts["legacy"]["verbatim"]``
    """
    facts = replay_facts
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except (ValueError, TypeError):
            return False
    if not isinstance(facts, dict):
        return False
    if _captured_images_map_has_ref(facts.get("images"), env=env):
        return True
    if include_legacy:
        legacy_obj = facts.get("legacy")
        if isinstance(legacy_obj, dict):
            if _captured_images_map_has_ref(legacy_obj.get("images"), env=env):
                return True
            if _legacy_verbatim_has_response_image_ref(legacy_obj.get("verbatim"), env=env):
                return True
    return False


def checkpoint_value_has_captured_images(
    value: Any, *, env: Mapping[str, str] | None = None
) -> bool:
    """Return True iff a deserialized ``messages`` checkpoint value has ``AIMessage.response_metadata.captured_images``.

    Matches ``ptc_agent.agent.middleware.image_capture.recorded_images`` and
    ``replay.turn.project_turn`` / ``replay.run_lane._run_items``: inspects ONLY
    ``AIMessage.response_metadata["captured_images"]`` (never message text).
    """
    if value is None:
        return False

    if type(value).__name__ == "_DeltaSnapshot" and hasattr(value, "value"):
        return checkpoint_value_has_captured_images(value.value, env=env)

    if isinstance(value, (list, tuple)):
        return any(checkpoint_value_has_captured_images(item, env=env) for item in value)

    msg_type = getattr(value, "type", None)
    if msg_type in ("ai", "AIMessageChunk") and hasattr(value, "response_metadata"):
        meta = getattr(value, "response_metadata", None)
        if isinstance(meta, dict):
            return _captured_images_map_has_ref(meta.get("captured_images"), env=env)
        return False

    if isinstance(value, dict):
        if value.get("type") in ("ai", "AIMessageChunk") and isinstance(
            value.get("response_metadata"), dict
        ):
            return _captured_images_map_has_ref(
                value["response_metadata"].get("captured_images"), env=env
            )
        if (
            value.get("lc") == 1
            and value.get("type") == "constructor"
            and isinstance(value.get("id"), list)
            and value["id"]
            and value["id"][-1] in ("AIMessage", "AIMessageChunk")
            and isinstance(value.get("kwargs"), dict)
        ):
            meta = value["kwargs"].get("response_metadata")
            if isinstance(meta, dict):
                return _captured_images_map_has_ref(meta.get("captured_images"), env=env)

    return False


def checkpoint_value_has_image_capture_ui(
    value: Any, *, env: Mapping[str, str] | None = None
) -> bool:
    """Return True iff a deserialized ``ui`` checkpoint value has a legacy ``image_capture`` record.

    Matches ``replay.turn._collect_image_url_map`` (``turn.py:31,232-240``).
    """
    if value is None:
        return False

    if type(value).__name__ == "_DeltaSnapshot" and hasattr(value, "value"):
        return checkpoint_value_has_image_capture_ui(value.value, env=env)

    if isinstance(value, (list, tuple)):
        return any(checkpoint_value_has_image_capture_ui(item, env=env) for item in value)

    if isinstance(value, dict) and value.get("name") == "image_capture":
        props = value.get("props")
        if isinstance(props, dict):
            return _captured_images_map_has_ref(props.get("path_to_url"), env=env)

    return False


def decode_and_check_checkpoint_widget_ref(
    blob_type: str | None,
    blob: bytes | bytearray | memoryview | None,
    *,
    serde: Any | None = None,
) -> bool:
    """Deserialize one candidate ``checkpoint_blobs`` / ``checkpoint_writes`` row and verify widget ``data_ref``."""
    if not blob_type or blob_type == "empty" or blob is None:
        return False
    raw = bytes(blob)
    if b"data_ref" not in raw:
        return False
    ser = serde if serde is not None else _get_checkpoint_serde()
    value = ser.loads_typed((blob_type, raw))
    return checkpoint_value_has_widget_data_ref(value)


def decode_and_check_checkpoint_ref(
    blob_type: str | None,
    blob: bytes | bytearray | memoryview | None,
    *,
    channel: str | None = None,
    serde: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Deserialize one candidate checkpoint row and verify widget ``data_ref`` or response-image refs.

    Covers every main-bucket reference replay reads from checkpoints:
    - ``channel == "messages"``: ``ShowWidget`` ``ToolMessage.artifact.data_ref.key``
      and ``AIMessage.response_metadata["captured_images"]``
    - ``channel == "ui"``: legacy ``image_capture`` UI records (``props.path_to_url``)
    """
    if not blob_type or blob_type == "empty" or blob is None:
        return False
    raw = bytes(blob)
    has_data_ref = b"data_ref" in raw
    has_captured = b"captured_images" in raw
    has_ui_capture = b"image_capture" in raw

    if channel == "messages":
        if not (has_data_ref or has_captured):
            return False
    elif channel == "ui":
        if not has_ui_capture:
            return False
    elif not (has_data_ref or has_captured or has_ui_capture):
        return False

    ser = serde if serde is not None else _get_checkpoint_serde()
    value = ser.loads_typed((blob_type, raw))
    if channel == "ui":
        return checkpoint_value_has_image_capture_ui(value, env=env)
    if channel == "messages":
        return (has_data_ref and checkpoint_value_has_widget_data_ref(value)) or (
            has_captured and checkpoint_value_has_captured_images(value, env=env)
        )
    return (
        (has_data_ref and checkpoint_value_has_widget_data_ref(value))
        or (has_captured and checkpoint_value_has_captured_images(value, env=env))
        or (has_ui_capture and checkpoint_value_has_image_capture_ui(value, env=env))
    )


# ---------------------------------------------------------------------------
# Fast startup probes (warning-only, index-backed, zero false positives)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartupConsumerProbe:
    """One fast, index-backed main-bucket consumer probe for warning-only startup checks."""

    name: str
    table: str
    count_sql: str
    where_summary: str
    source_ref: str
    required_tables: tuple[str, ...] = ()

    def tables_to_check(self) -> tuple[str, ...]:
        return self.required_tables or (self.table,)


STARTUP_MAIN_BUCKET_PROBES: tuple[StartupConsumerProbe, ...] = (
    StartupConsumerProbe(
        name="workspace_file_blobs",
        table="public.workspace_file_blobs",
        count_sql=(
            f"SELECT COUNT(*) FROM ("
            f"SELECT 1 FROM workspace_file_blobs LIMIT {STARTUP_COUNT_CAP}"
            f") sub"
        ),
        where_summary=f"any row (PK index, capped at {STARTUP_COUNT_CAP})",
        source_ref="migrations/versions/039_workspace_file_blobs.py:105; src/server/database/blob_keys.py:158-175",
    ),
    StartupConsumerProbe(
        name="workspace_files",
        table="public.workspace_files",
        count_sql=(
            "SELECT COUNT(*) FROM ("
            f"(SELECT workspace_file_id FROM workspace_files WHERE blob_sha256 IS NOT NULL LIMIT {STARTUP_COUNT_CAP}) "
            "UNION "
            f"(SELECT workspace_file_id FROM workspace_files WHERE pack_sha256 IS NOT NULL LIMIT {STARTUP_COUNT_CAP}) "
            f"LIMIT {STARTUP_COUNT_CAP}"
            ") sub"
        ),
        where_summary=f"blob_sha256 IS NOT NULL OR pack_sha256 IS NOT NULL (partial indexes, capped at {STARTUP_COUNT_CAP})",
        source_ref="migrations/versions/039_workspace_file_blobs.py:172-181; src/server/database/blob_keys.py:163-168,181",
    ),
    StartupConsumerProbe(
        name="thread_transcript_files",
        table="public.thread_transcript_files",
        count_sql=(
            "SELECT COUNT(*) FROM ("
            "SELECT 1 FROM workspace_file_blobs b "
            "JOIN thread_transcript_files t ON t.sha256 = b.sha256 AND t.user_id = b.user_id "
            f"WHERE t.content IS NULL LIMIT {STARTUP_COUNT_CAP}"
            ") sub"
        ),
        where_summary=f"blob-backed row (joined via workspace_file_blobs) with content IS NULL (capped at {STARTUP_COUNT_CAP})",
        source_ref="migrations/versions/056_thread_transcripts.py:43-60; src/server/database/blob_keys.py:169-170",
        required_tables=("public.workspace_file_blobs", "public.thread_transcript_files"),
    ),
    StartupConsumerProbe(
        name="artifact_version_files",
        table="public.artifact_version_files",
        count_sql=(
            "SELECT COUNT(*) FROM ("
            "SELECT 1 FROM workspace_file_blobs b "
            "JOIN artifact_version_files a ON a.sha256 = b.sha256 AND a.user_id = b.user_id "
            f"WHERE a.content IS NULL LIMIT {STARTUP_COUNT_CAP}"
            ") sub"
        ),
        where_summary=f"blob-backed row (joined via workspace_file_blobs) with content IS NULL (capped at {STARTUP_COUNT_CAP})",
        source_ref="migrations/versions/101_artifacts.py:163-187; src/server/database/blob_keys.py:171-172",
        required_tables=("public.workspace_file_blobs", "public.artifact_version_files"),
    ),
    StartupConsumerProbe(
        name="drive_contents",
        table="public.drive_contents",
        count_sql=(
            "SELECT COUNT(*) FROM ("
            f"SELECT 1 FROM drive_contents WHERE blob_user_id IS NOT NULL LIMIT {STARTUP_COUNT_CAP}"
            ") sub"
        ),
        where_summary=f"blob_user_id IS NOT NULL (partial index, capped at {STARTUP_COUNT_CAP})",
        source_ref="migrations/versions/150_drive.py:156-184; src/server/database/blob_keys.py:173-174",
    ),
    StartupConsumerProbe(
        name="user_skills",
        table="public.user_skills",
        count_sql=(
            "SELECT COUNT(*) FROM ("
            f"SELECT 1 FROM user_skills WHERE archive_key IS NOT NULL LIMIT {STARTUP_COUNT_CAP}"
            ") sub"
        ),
        where_summary=f"archive_key IS NOT NULL (partial index, capped at {STARTUP_COUNT_CAP})",
        source_ref="migrations/versions/026_user_skills.py:56-120; src/server/services/skill_archive_storage.py:54-65,94-117",
    ),
    StartupConsumerProbe(
        name="org_library_items",
        table="public.org_library_items",
        count_sql=(
            "SELECT COUNT(*) FROM ("
            f"SELECT 1 FROM org_library_items WHERE kind = 'skill' AND archive_key IS NOT NULL LIMIT {STARTUP_COUNT_CAP}"
            ") sub"
        ),
        where_summary=f"kind = 'skill' AND archive_key IS NOT NULL (partial index, capped at {STARTUP_COUNT_CAP})",
        source_ref="migrations/versions/140_org_library_skills.py:35-59; src/server/services/skill_archive_storage.py:67-91",
    ),
    StartupConsumerProbe(
        name="chat_attachments",
        table="public.chat_attachments",
        count_sql=(
            "SELECT COUNT(*) FROM ("
            f"SELECT 1 FROM chat_attachments WHERE object_key IS NOT NULL AND cleaned_at IS NULL LIMIT {STARTUP_COUNT_CAP}"
            ") sub"
        ),
        where_summary=f"object_key IS NOT NULL AND cleaned_at IS NULL (inline rows excluded, capped at {STARTUP_COUNT_CAP})",
        source_ref="migrations/versions/123_chat_attachments.py:49-117; src/server/services/chat_attachments.py:365-379",
    ),
)

# Compatibility alias for callers inspecting registered startup probes
MAIN_BUCKET_CONSUMER_PROBES = STARTUP_MAIN_BUCKET_PROBES


@dataclass(frozen=True)
class ConsumerCheckOutcome:
    """Result of checking one main-bucket consumer (startup or strict CLI)."""

    name: str
    table: str
    has_references: bool
    hit_count: int = 0
    hit_count_capped: bool = False
    suspected_count: int = 0
    scanned_rows: int = 0
    skipped_missing_table: bool = False
    error: str | None = None
    elapsed_ms: float = 0.0

    def formatted_hit_count(self) -> str:
        return f"{self.hit_count}+" if self.hit_count_capped else str(self.hit_count)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table": self.table,
            "has_references": self.has_references,
            "hit_count": self.hit_count,
            "hit_count_capped": self.hit_count_capped,
            "suspected_count": self.suspected_count,
            "scanned_rows": self.scanned_rows,
            "skipped_missing_table": self.skipped_missing_table,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


# Backwards-compatible name
ConsumerProbeOutcome = ConsumerCheckOutcome


def _extract_first_value(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def _extract_tuple(row: Any, width: int) -> tuple[Any, ...]:
    if isinstance(row, dict):
        vals = tuple(row.values())
        return vals[:width]
    return tuple(row[:width])


async def _check_tables_exist(cur: Any, tables: tuple[str, ...]) -> str | None:
    """Return the first missing table name, or None if all exist."""
    for req_table in tables:
        await cur.execute("SELECT to_regclass(%s)", (req_table,))
        reg_row = await cur.fetchone()
        if _extract_first_value(reg_row) is None:
            return req_table
    return None


async def _probe_single_startup_consumer(
    conn: Any,
    consumer: StartupConsumerProbe,
    *,
    statement_timeout: str,
    target_logger: logging.Logger,
) -> ConsumerCheckOutcome:
    started = time.monotonic()
    try:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (statement_timeout,),
                )
                missing = await _check_tables_exist(cur, consumer.tables_to_check())
                if missing is not None:
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    target_logger.info(
                        "Main-storage startup check skipped %s: table %s does not exist",
                        consumer.name,
                        missing,
                    )
                    return ConsumerCheckOutcome(
                        name=consumer.name,
                        table=consumer.table,
                        has_references=False,
                        skipped_missing_table=True,
                        elapsed_ms=elapsed_ms,
                    )

                await cur.execute(consumer.count_sql)
                row = await cur.fetchone()
                raw_val = _extract_first_value(row)
                if isinstance(raw_val, bool):
                    count = 1 if raw_val else 0
                else:
                    count = int(raw_val or 0)
                elapsed_ms = (time.monotonic() - started) * 1000.0
                return ConsumerCheckOutcome(
                    name=consumer.name,
                    table=consumer.table,
                    has_references=count > 0,
                    hit_count=count,
                    hit_count_capped=count >= STARTUP_COUNT_CAP,
                    elapsed_ms=elapsed_ms,
                )
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        err_text = f"{type(exc).__name__}: {exc}"
        target_logger.warning(
            "Main-storage startup check failed or timed out for %s (%s): %s "
            "(warning only; startup continues)",
            consumer.name,
            consumer.table,
            err_text,
        )
        return ConsumerCheckOutcome(
            name=consumer.name,
            table=consumer.table,
            has_references=False,
            error=err_text,
            elapsed_ms=elapsed_ms,
        )


async def probe_main_bucket_references(
    *,
    conn: Any | None = None,
    probes: tuple[StartupConsumerProbe, ...] = STARTUP_MAIN_BUCKET_PROBES,
    statement_timeout: str = STARTUP_STATEMENT_TIMEOUT,
    target_logger: logging.Logger | None = None,
) -> tuple[list[ConsumerCheckOutcome], float]:
    """Run startup advisory probes sequentially, each in its own timed transaction."""
    log = target_logger or logger
    total_start = time.monotonic()
    outcomes: list[ConsumerCheckOutcome] = []

    if conn is not None:
        for consumer in probes:
            outcomes.append(
                await _probe_single_startup_consumer(
                    conn,
                    consumer,
                    statement_timeout=statement_timeout,
                    target_logger=log,
                )
            )
    else:
        async with get_db_connection() as db_conn:
            for consumer in probes:
                outcomes.append(
                    await _probe_single_startup_consumer(
                        db_conn,
                        consumer,
                        statement_timeout=statement_timeout,
                        target_logger=log,
                    )
                )

    total_ms = (time.monotonic() - total_start) * 1000.0
    return outcomes, total_ms


async def warn_main_bucket_references_on_startup(
    *,
    conn: Any | None = None,
    probes: tuple[StartupConsumerProbe, ...] = STARTUP_MAIN_BUCKET_PROBES,
    statement_timeout: str = STARTUP_STATEMENT_TIMEOUT,
    target_logger: logging.Logger | None = None,
) -> list[ConsumerCheckOutcome]:
    """Advisory startup check when main storage is disabled: WARNING only, never blocks boot.

    - No-op when ``is_storage_enabled()`` is True.
    - Runs in both Everflow and Standalone Core profiles.
    - Lists only hit table names and counts (never object keys, URLs, or row content).
    - Any probe hit, timeout, or DB error logs ``WARNING`` and allows startup to proceed.
    """
    if is_storage_enabled():
        return []

    log = target_logger or logger
    try:
        outcomes, total_ms = await probe_main_bucket_references(
            conn=conn,
            probes=probes,
            statement_timeout=statement_timeout,
            target_logger=log,
        )
    except Exception as exc:
        log.warning(
            "Main-storage startup reference check could not run (%s); "
            "allowing startup to proceed. Run `python -m src.dalton.storage_refs check --strict` "
            "manually before disabling the main bucket.",
            type(exc).__name__,
        )
        return []

    hits = [o for o in outcomes if o.has_references]
    errors = [o for o in outcomes if o.error]
    skipped = sum(1 for o in outcomes if o.skipped_missing_table)

    log.info(
        "Main-storage startup advisory check probed %d consumer(s) in %.1f ms "
        "(hits=%d, errors=%d, skipped_missing_tables=%d)",
        len(outcomes),
        total_ms,
        len(hits),
        len(errors),
        skipped,
    )

    if hits:
        hit_summary = ", ".join(
            f"{o.table} ({o.name}): {o.formatted_hit_count()}" for o in hits
        )
        log.warning(
            "Main storage is disabled (`is_storage_enabled()=False`), but existing "
            "main-bucket references were detected — %s. Startup is NOT blocked; "
            "keep `STORAGE_PROVIDER=s3` (or set `DALTON_WSFILES_WRITE_INLINE_ONLY=1` "
            "to route new workspace files inline) and run "
            "`python -m src.dalton.storage_refs check --strict` before shutting down "
            "the main bucket.",
            hit_summary,
        )
    elif errors:
        err_summary = ", ".join(f"{o.table} ({o.name})" for o in errors)
        log.warning(
            "Main-storage startup advisory check encountered %d probe error/timeout(s) (%s); "
            "allowing startup to proceed.",
            len(errors),
            err_summary,
        )

    return outcomes


# Backwards-compatible alias — never raises DaltonStartupConfigError.
enforce_storage_off_startup_gate = warn_main_bucket_references_on_startup


# ---------------------------------------------------------------------------
# Strict full-table batched audit for `python -m src.dalton.storage_refs check --strict`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrictConsumerSpec:
    """Declarative specification for one of the 17 strict main-bucket consumers."""

    name: str
    table: str
    mode: str  # "indexed_exists" | "batched_bool" | "turn_slices" | "checkpoint_blobs" | "checkpoint_writes" | "users_avatars"
    where_summary: str
    source_ref: str
    required_tables: tuple[str, ...] = ()
    exists_sql: str = ""
    pk_columns: tuple[str, ...] = ()
    batch_sql_first: str = ""
    batch_sql_next: str = ""

    def tables_to_check(self) -> tuple[str, ...]:
        return self.required_tables or (self.table,)


def _build_batched_bool_spec(
    *,
    name: str,
    table: str,
    table_ident: str,
    pk_columns: tuple[str, ...],
    hit_expr: str,
    where_summary: str,
    source_ref: str,
    required_tables: tuple[str, ...] = (),
) -> StrictConsumerSpec:
    pk_select = ", ".join(pk_columns)
    order_by = ", ".join(f"{col} ASC" for col in pk_columns)
    if len(pk_columns) == 1:
        cursor_cond = f"{pk_columns[0]} > %s"
    else:
        placeholders = ", ".join(["%s"] * len(pk_columns))
        cursor_cond = f"({pk_select}) > ({placeholders})"
    return StrictConsumerSpec(
        name=name,
        table=table,
        mode="batched_bool",
        where_summary=where_summary,
        source_ref=source_ref,
        required_tables=required_tables,
        pk_columns=pk_columns,
        batch_sql_first=(
            f"SELECT {pk_select}, ({hit_expr}) AS is_hit "
            f"FROM {table_ident} ORDER BY {order_by} LIMIT %s"
        ),
        batch_sql_next=(
            f"SELECT {pk_select}, ({hit_expr}) AS is_hit "
            f"FROM {table_ident} WHERE {cursor_cond} ORDER BY {order_by} LIMIT %s"
        ),
    )


STRICT_MAIN_BUCKET_CONSUMERS: tuple[StrictConsumerSpec, ...] = (
    # --- Indexed full-table exact checks (1-7) ---
    StrictConsumerSpec(
        name="workspace_file_blobs",
        table="public.workspace_file_blobs",
        mode="indexed_exists",
        exists_sql="SELECT EXISTS (SELECT 1 FROM workspace_file_blobs LIMIT 1)",
        where_summary="any row (PK index full-table)",
        source_ref="migrations/versions/039_workspace_file_blobs.py:105; src/server/database/blob_keys.py:158-175",
    ),
    StrictConsumerSpec(
        name="workspace_files",
        table="public.workspace_files",
        mode="indexed_exists",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM workspace_files WHERE blob_sha256 IS NOT NULL LIMIT 1) "
            "OR EXISTS (SELECT 1 FROM workspace_files WHERE pack_sha256 IS NOT NULL LIMIT 1)"
        ),
        where_summary="blob_sha256 IS NOT NULL OR pack_sha256 IS NOT NULL (partial indexes)",
        source_ref="migrations/versions/039_workspace_file_blobs.py:172-181; src/server/database/blob_keys.py:163-168,181",
    ),
    StrictConsumerSpec(
        name="thread_transcript_files",
        table="public.thread_transcript_files",
        mode="indexed_exists",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM workspace_file_blobs b "
            "JOIN thread_transcript_files t ON t.sha256 = b.sha256 AND t.user_id = b.user_id "
            "WHERE t.content IS NULL LIMIT 1"
            ")"
        ),
        where_summary="blob-backed row (joined via workspace_file_blobs) with content IS NULL",
        source_ref="migrations/versions/056_thread_transcripts.py:43-60; src/server/database/blob_keys.py:169-170",
        required_tables=("public.workspace_file_blobs", "public.thread_transcript_files"),
    ),
    StrictConsumerSpec(
        name="artifact_version_files",
        table="public.artifact_version_files",
        mode="indexed_exists",
        exists_sql=(
            "SELECT EXISTS ("
            "SELECT 1 FROM workspace_file_blobs b "
            "JOIN artifact_version_files a ON a.sha256 = b.sha256 AND a.user_id = b.user_id "
            "WHERE a.content IS NULL LIMIT 1"
            ")"
        ),
        where_summary="blob-backed row (joined via workspace_file_blobs) with content IS NULL",
        source_ref="migrations/versions/101_artifacts.py:163-187; src/server/database/blob_keys.py:171-172",
        required_tables=("public.workspace_file_blobs", "public.artifact_version_files"),
    ),
    StrictConsumerSpec(
        name="drive_contents",
        table="public.drive_contents",
        mode="indexed_exists",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM drive_contents WHERE blob_user_id IS NOT NULL LIMIT 1)"
        ),
        where_summary="blob_user_id IS NOT NULL (partial index)",
        source_ref="migrations/versions/150_drive.py:156-184; src/server/database/blob_keys.py:173-174",
    ),
    StrictConsumerSpec(
        name="user_skills",
        table="public.user_skills",
        mode="indexed_exists",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM user_skills WHERE archive_key IS NOT NULL LIMIT 1)"
        ),
        where_summary="archive_key IS NOT NULL (partial index)",
        source_ref="migrations/versions/026_user_skills.py:56-120; src/server/services/skill_archive_storage.py:54-65,94-117",
    ),
    StrictConsumerSpec(
        name="org_library_items",
        table="public.org_library_items",
        mode="indexed_exists",
        exists_sql=(
            "SELECT EXISTS (SELECT 1 FROM org_library_items WHERE kind = 'skill' AND archive_key IS NOT NULL LIMIT 1)"
        ),
        where_summary="kind = 'skill' AND archive_key IS NOT NULL (partial index)",
        source_ref="migrations/versions/140_org_library_skills.py:35-59; src/server/services/skill_archive_storage.py:67-91",
    ),
    # --- Full-table batched PK scans (8-13) ---
    _build_batched_bool_spec(
        name="chat_attachments",
        table="public.chat_attachments",
        table_ident="chat_attachments",
        pk_columns=("attachment_id",),
        hit_expr="object_key IS NOT NULL AND cleaned_at IS NULL",
        where_summary="full-table batched scan: object_key IS NOT NULL AND cleaned_at IS NULL (inline rows excluded)",
        source_ref="migrations/versions/123_chat_attachments.py:49-117; src/server/services/chat_attachments.py:365-379",
    ),
    _build_batched_bool_spec(
        name="provenance_result_bodies",
        table="public.provenance_result_bodies",
        table_ident="provenance_result_bodies",
        pk_columns=("result_sha256",),
        hit_expr="object_key IS NOT NULL AND object_key <> ''",
        where_summary="full-table batched scan: object_key IS NOT NULL AND object_key <> ''",
        source_ref="migrations/versions/015_add_provenance_result_bodies.py:38-58; src/server/database/provenance_bodies.py:107-115",
    ),
    _build_batched_bool_spec(
        name="memo_binaries",
        table="public.store",
        table_ident="store",
        pk_columns=("prefix", "key"),
        hit_expr=(
            "prefix LIKE '%%.memos' AND value ? 'binary_ref' "
            "AND jsonb_typeof(value -> 'binary_ref') = 'object' "
            "AND COALESCE(value -> 'binary_ref' ->> 'key', '') <> ''"
        ),
        where_summary="full-table batched scan: store memo namespaces with non-empty binary_ref.key",
        source_ref="migrations/versions/001_initial_schema.py:535-549; src/ptc_agent/agent/backends/langgraph_store.py:175-176; src/server/app/memo.py:566-575",
    ),
    StrictConsumerSpec(
        name="response_images_turns",
        table="public.conversation_responses",
        mode="response_images_turns",
        pk_columns=("conversation_response_id",),
        batch_sql_first=(
            "SELECT conversation_response_id, "
            "CASE WHEN replay_facts IS NOT NULL AND ("
            "(replay_facts ? 'images' AND jsonb_typeof(replay_facts -> 'images') = 'object' "
            "AND position('response-images/' in (replay_facts -> 'images')::text) > 0) "
            "OR (replay_facts ? 'legacy' AND jsonb_typeof(replay_facts -> 'legacy') = 'object' AND ("
            "((replay_facts -> 'legacy') ? 'images' "
            "AND jsonb_typeof(replay_facts -> 'legacy' -> 'images') = 'object' "
            "AND position('response-images/' in (replay_facts -> 'legacy' -> 'images')::text) > 0) "
            "OR ((replay_facts -> 'legacy') ? 'verbatim' "
            "AND jsonb_typeof(replay_facts -> 'legacy' -> 'verbatim') = 'array')"
            "))) THEN replay_facts ELSE NULL END AS cand_facts "
            "FROM conversation_responses ORDER BY conversation_response_id ASC LIMIT %s"
        ),
        batch_sql_next=(
            "SELECT conversation_response_id, "
            "CASE WHEN replay_facts IS NOT NULL AND ("
            "(replay_facts ? 'images' AND jsonb_typeof(replay_facts -> 'images') = 'object' "
            "AND position('response-images/' in (replay_facts -> 'images')::text) > 0) "
            "OR (replay_facts ? 'legacy' AND jsonb_typeof(replay_facts -> 'legacy') = 'object' AND ("
            "((replay_facts -> 'legacy') ? 'images' "
            "AND jsonb_typeof(replay_facts -> 'legacy' -> 'images') = 'object' "
            "AND position('response-images/' in (replay_facts -> 'legacy' -> 'images')::text) > 0) "
            "OR ((replay_facts -> 'legacy') ? 'verbatim' "
            "AND jsonb_typeof(replay_facts -> 'legacy' -> 'verbatim') = 'array')"
            "))) THEN replay_facts ELSE NULL END AS cand_facts "
            "FROM conversation_responses WHERE conversation_response_id > %s "
            "ORDER BY conversation_response_id ASC LIMIT %s"
        ),
        where_summary=(
            "full-table batched scan: replay_facts->'images', replay_facts->'legacy'->'images', "
            "and replay_facts->'legacy'->'verbatim' structurally verified against site/main-bucket origin "
            "and response-images/ object path prefix"
        ),
        source_ref=(
            "migrations/versions/065_replay_facts.py:25-28; src/server/services/history/replay/facts.py:48-49,181; "
            "src/server/services/history/replay/legacy.py:97-99,217-237; src/ptc_agent/agent/middleware/image_capture.py:88-91"
        ),
    ),
    StrictConsumerSpec(
        name="response_images_subagents",
        table="public.subagent_runs",
        mode="response_images_subagents",
        pk_columns=("task_run_id",),
        batch_sql_first=(
            "SELECT task_run_id, "
            "CASE WHEN replay_facts IS NOT NULL AND replay_facts ? 'images' "
            "AND jsonb_typeof(replay_facts -> 'images') = 'object' "
            "AND position('response-images/' in (replay_facts -> 'images')::text) > 0 "
            "THEN replay_facts ELSE NULL END AS cand_facts "
            "FROM subagent_runs ORDER BY task_run_id ASC LIMIT %s"
        ),
        batch_sql_next=(
            "SELECT task_run_id, "
            "CASE WHEN replay_facts IS NOT NULL AND replay_facts ? 'images' "
            "AND jsonb_typeof(replay_facts -> 'images') = 'object' "
            "AND position('response-images/' in (replay_facts -> 'images')::text) > 0 "
            "THEN replay_facts ELSE NULL END AS cand_facts "
            "FROM subagent_runs WHERE task_run_id > %s ORDER BY task_run_id ASC LIMIT %s"
        ),
        where_summary=(
            "full-table batched scan: subagent_runs replay_facts->'images' structurally verified "
            "against site/main-bucket origin and response-images/ object path prefix"
        ),
        source_ref="migrations/versions/065_replay_facts.py:29-31; src/server/services/history/replay/facts.py:64-65; src/ptc_agent/agent/middleware/image_capture.py:88-91",
    ),
    _build_batched_bool_spec(
        name="chat_attachment_history",
        table="public.conversation_queries",
        table_ident="conversation_queries",
        pk_columns=("conversation_query_id",),
        hit_expr=(
            "metadata IS NOT NULL AND metadata ? 'attachments' "
            "AND jsonb_typeof(metadata -> 'attachments') = 'array' "
            "AND EXISTS (SELECT 1 FROM jsonb_array_elements(metadata -> 'attachments') AS att "
            "WHERE COALESCE(att ->> 'url', '') LIKE '%%/attachments/%%' "
            "OR COALESCE(att ->> 'object_key', '') LIKE '%%attachments/%%')"
        ),
        where_summary="full-table batched scan: metadata->'attachments' url containing '/attachments/'",
        source_ref="migrations/versions/001_initial_schema.py:309-329; src/server/utils/multimodal_context.py:171-196; src/server/handlers/chat/ptc_run.py:641-658",
    ),
    # --- Structurally verified widget consumers (14-16) ---
    StrictConsumerSpec(
        name="widgets_turn_slices",
        table="public.turn_slices",
        mode="turn_slices",
        pk_columns=("conversation_response_id",),
        where_summary=(
            "full-table batched scan: candidate pre-filter position('\"data_ref\"' in lines)>0, "
            "structurally verified as artifact/html_widget without inlined data and non-empty data_ref.key"
        ),
        source_ref="migrations/versions/064_turn_slices.py:27-47; src/server/services/history/replay/lines.py:38-42; src/server/services/history/replay/widgets.py:15-39",
    ),
    StrictConsumerSpec(
        name="widgets_checkpoint_blobs",
        table="public.checkpoint_blobs",
        mode="checkpoint_blobs",
        pk_columns=("thread_id", "checkpoint_ns", "channel", "version"),
        where_summary=(
            "full-table batched scan: candidate pre-filter channel='messages' (b'data_ref' or b'captured_images') "
            "and channel='ui' (b'image_capture'), deserialized via JsonPlusSerializer and structurally verified "
            "on ShowWidget ToolMessage.artifact.data_ref.key, AIMessage.response_metadata.captured_images, "
            "and legacy image_capture UI records"
        ),
        source_ref=(
            "src/ptc_agent/agent/tools/show_widget.py:272-305; src/server/services/history/projector.py:495-516; "
            "src/server/services/history/replay/widgets.py:19-39; src/server/services/history/replay/turn.py:31,148-155,232-240; "
            "src/server/services/history/replay/run_lane.py:142-144; src/ptc_agent/agent/middleware/image_capture.py:52-62"
        ),
    ),
    StrictConsumerSpec(
        name="widgets_checkpoint_writes",
        table="public.checkpoint_writes",
        mode="checkpoint_writes",
        pk_columns=("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
        where_summary=(
            "full-table batched scan: candidate pre-filter channel='messages' (b'data_ref' or b'captured_images') "
            "and channel='ui' (b'image_capture'), deserialized via JsonPlusSerializer and structurally verified "
            "on ShowWidget ToolMessage.artifact.data_ref.key, AIMessage.response_metadata.captured_images, "
            "and legacy image_capture UI records"
        ),
        source_ref=(
            "src/server/database/thread_fork.py:14-16; src/ptc_agent/agent/tools/show_widget.py:272-305; "
            "src/server/services/history/projector.py:495-516; src/server/services/history/replay/turn.py:31,148-155,232-240; "
            "src/server/services/history/replay/run_lane.py:142-144; src/ptc_agent/agent/middleware/image_capture.py:52-62"
        ),
    ),
    # --- Avatars: recorded object key = hit; URL-shape-only = suspected warning (17) ---
    StrictConsumerSpec(
        name="users_avatars",
        table="public.users",
        mode="users_avatars",
        pk_columns=("user_id",),
        where_summary=(
            "full-table batched scan: confirmed hit ONLY on upload-recorded object key column; "
            "site-shaped avatar_url without recorded object key emits suspected WARNING only"
        ),
        source_ref="migrations/versions/001_initial_schema.py:77-94; src/server/app/users.py:407,1172-1182",
    ),
)


def _emit_progress(
    stream: TextIO | None,
    consumer: StrictConsumerSpec,
    scanned: int,
    started: float,
    *,
    status: str = "scanning",
) -> None:
    if stream is None:
        return
    elapsed = time.monotonic() - started
    stream.write(
        f"[storage_refs] {status}: {consumer.name} ({consumer.table}) "
        f"scanned={scanned} elapsed={elapsed:.2f}s\n"
    )
    stream.flush()


async def _detect_avatar_object_key_column(cur: Any) -> str | None:
    """Return the recorded avatar object-key column on ``public.users``, if any."""
    await cur.execute(
        "SELECT attname FROM pg_attribute "
        "WHERE attrelid = to_regclass('public.users') "
        "AND attnum > 0 AND NOT attisdropped "
        "AND attname = ANY(%s) ORDER BY attnum ASC LIMIT 1",
        (list(_AVATAR_OBJECT_KEY_COLUMNS),),
    )
    row = await cur.fetchone()
    val = _extract_first_value(row)
    return str(val) if isinstance(val, str) and val in _AVATAR_OBJECT_KEY_COLUMNS else None


async def _run_strict_single_consumer(
    conn: Any,
    consumer: StrictConsumerSpec,
    *,
    batch_size: int,
    batch_timeout: str,
    progress_stream: TextIO | None,
    target_logger: logging.Logger,
    env: Mapping[str, str] | None,
    serde: Any | None,
    stop_on_hit: bool,
) -> ConsumerCheckOutcome:
    started = time.monotonic()
    scanned = 0
    hits = 0
    suspected = 0

    try:
        # Check required tables exist first
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (batch_timeout,),
                )
                missing = await _check_tables_exist(cur, consumer.tables_to_check())
                if missing is not None:
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                    target_logger.info(
                        "Strict main-storage check skipped %s: table %s does not exist",
                        consumer.name,
                        missing,
                    )
                    return ConsumerCheckOutcome(
                        name=consumer.name,
                        table=consumer.table,
                        has_references=False,
                        skipped_missing_table=True,
                        elapsed_ms=elapsed_ms,
                    )

        if consumer.mode == "indexed_exists":
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (batch_timeout,),
                    )
                    await cur.execute(consumer.exists_sql)
                    row = await cur.fetchone()
                    found = bool(_extract_first_value(row))
            scanned = 1 if found else 0
            hits = 1 if found else 0
            _emit_progress(
                progress_stream,
                consumer,
                scanned,
                started,
                status="hit" if found else "clean",
            )
            return ConsumerCheckOutcome(
                name=consumer.name,
                table=consumer.table,
                has_references=found,
                hit_count=hits,
                scanned_rows=scanned,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        if consumer.mode == "batched_bool":
            pk_len = len(consumer.pk_columns)
            cursor_pk: tuple[Any, ...] | None = None
            while True:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (batch_timeout,),
                        )
                        if cursor_pk is None:
                            await cur.execute(consumer.batch_sql_first, (batch_size,))
                        else:
                            await cur.execute(consumer.batch_sql_next, (*cursor_pk, batch_size))
                        rows = await cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    vals = _extract_tuple(row, pk_len + 1)
                    scanned += 1
                    if bool(vals[pk_len]):
                        hits += 1
                        if stop_on_hit:
                            _emit_progress(progress_stream, consumer, scanned, started, status="hit")
                            return ConsumerCheckOutcome(
                                name=consumer.name,
                                table=consumer.table,
                                has_references=True,
                                hit_count=hits,
                                scanned_rows=scanned,
                                elapsed_ms=(time.monotonic() - started) * 1000.0,
                            )
                cursor_pk = _extract_tuple(rows[-1], pk_len)
                _emit_progress(progress_stream, consumer, scanned, started)
                if len(rows) < batch_size:
                    break
            _emit_progress(
                progress_stream,
                consumer,
                scanned,
                started,
                status="hit" if hits else "clean",
            )
            return ConsumerCheckOutcome(
                name=consumer.name,
                table=consumer.table,
                has_references=hits > 0,
                hit_count=hits,
                scanned_rows=scanned,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        if consumer.mode in ("response_images_turns", "response_images_subagents"):
            include_legacy = consumer.mode == "response_images_turns"
            cursor_id: Any = None
            while True:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (batch_timeout,),
                        )
                        if cursor_id is None:
                            await cur.execute(consumer.batch_sql_first, (batch_size,))
                        else:
                            await cur.execute(consumer.batch_sql_next, (cursor_id, batch_size))
                        rows = await cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    _row_id, cand_facts = _extract_tuple(row, 2)
                    scanned += 1
                    if cand_facts is not None and replay_facts_has_response_image_ref(
                        cand_facts, include_legacy=include_legacy, env=env
                    ):
                        hits += 1
                        if stop_on_hit:
                            _emit_progress(progress_stream, consumer, scanned, started, status="hit")
                            return ConsumerCheckOutcome(
                                name=consumer.name,
                                table=consumer.table,
                                has_references=True,
                                hit_count=hits,
                                scanned_rows=scanned,
                                elapsed_ms=(time.monotonic() - started) * 1000.0,
                            )
                cursor_id = _extract_tuple(rows[-1], 1)[0]
                _emit_progress(progress_stream, consumer, scanned, started)
                if len(rows) < batch_size:
                    break
            _emit_progress(
                progress_stream,
                consumer,
                scanned,
                started,
                status="hit" if hits else "clean",
            )
            return ConsumerCheckOutcome(
                name=consumer.name,
                table=consumer.table,
                has_references=hits > 0,
                hit_count=hits,
                scanned_rows=scanned,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        if consumer.mode == "turn_slices":
            cursor_id: Any = None
            sql_first = (
                "SELECT conversation_response_id, "
                "CASE WHEN lines IS NOT NULL AND position('\"data_ref\"' in lines) > 0 "
                "THEN lines ELSE NULL END AS candidate_lines "
                "FROM turn_slices ORDER BY conversation_response_id ASC LIMIT %s"
            )
            sql_next = (
                "SELECT conversation_response_id, "
                "CASE WHEN lines IS NOT NULL AND position('\"data_ref\"' in lines) > 0 "
                "THEN lines ELSE NULL END AS candidate_lines "
                "FROM turn_slices WHERE conversation_response_id > %s "
                "ORDER BY conversation_response_id ASC LIMIT %s"
            )
            while True:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (batch_timeout,),
                        )
                        if cursor_id is None:
                            await cur.execute(sql_first, (batch_size,))
                        else:
                            await cur.execute(sql_next, (cursor_id, batch_size))
                        rows = await cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    row_id, candidate_lines = _extract_tuple(row, 2)
                    scanned += 1
                    if candidate_lines is not None and turn_slice_has_widget_data_ref(candidate_lines):
                        hits += 1
                        if stop_on_hit:
                            _emit_progress(progress_stream, consumer, scanned, started, status="hit")
                            return ConsumerCheckOutcome(
                                name=consumer.name,
                                table=consumer.table,
                                has_references=True,
                                hit_count=hits,
                                scanned_rows=scanned,
                                elapsed_ms=(time.monotonic() - started) * 1000.0,
                            )
                cursor_id = _extract_tuple(rows[-1], 1)[0]
                _emit_progress(progress_stream, consumer, scanned, started)
                if len(rows) < batch_size:
                    break
            _emit_progress(
                progress_stream,
                consumer,
                scanned,
                started,
                status="hit" if hits else "clean",
            )
            return ConsumerCheckOutcome(
                name=consumer.name,
                table=consumer.table,
                has_references=hits > 0,
                hit_count=hits,
                scanned_rows=scanned,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        if consumer.mode in ("checkpoint_blobs", "checkpoint_writes"):
            table_ident = "checkpoint_blobs" if consumer.mode == "checkpoint_blobs" else "checkpoint_writes"
            pk_cols = consumer.pk_columns
            pk_len = len(pk_cols)
            pk_select = ", ".join(pk_cols)
            order_by = ", ".join(f"{c} ASC" for c in pk_cols)
            placeholders = ", ".join(["%s"] * pk_len)
            cand_cond = (
                "blob IS NOT NULL AND ("
                "(channel = 'messages' AND ("
                "position(convert_to('data_ref', 'UTF8') in blob) > 0 "
                "OR position(convert_to('captured_images', 'UTF8') in blob) > 0"
                ")) OR ("
                "channel = 'ui' AND position(convert_to('image_capture', 'UTF8') in blob) > 0"
                "))"
            )
            sql_first = (
                f"SELECT {pk_select}, "
                f"CASE WHEN {cand_cond} THEN type ELSE NULL END AS cand_type, "
                f"CASE WHEN {cand_cond} THEN blob ELSE NULL END AS cand_blob, "
                f"CASE WHEN {cand_cond} THEN channel ELSE NULL END AS cand_channel "
                f"FROM {table_ident} ORDER BY {order_by} LIMIT %s"
            )
            sql_next = (
                f"SELECT {pk_select}, "
                f"CASE WHEN {cand_cond} THEN type ELSE NULL END AS cand_type, "
                f"CASE WHEN {cand_cond} THEN blob ELSE NULL END AS cand_blob, "
                f"CASE WHEN {cand_cond} THEN channel ELSE NULL END AS cand_channel "
                f"FROM {table_ident} WHERE ({pk_select}) > ({placeholders}) "
                f"ORDER BY {order_by} LIMIT %s"
            )
            cursor_pk = None
            while True:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (batch_timeout,),
                        )
                        if cursor_pk is None:
                            await cur.execute(sql_first, (batch_size,))
                        else:
                            await cur.execute(sql_next, (*cursor_pk, batch_size))
                        rows = await cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    row_vals = tuple(row.values()) if isinstance(row, dict) else tuple(row)
                    cand_type = row_vals[pk_len]
                    cand_blob = row_vals[pk_len + 1]
                    if len(row_vals) > pk_len + 2:
                        cand_channel = row_vals[pk_len + 2]
                    elif consumer.mode == "checkpoint_blobs":
                        cand_channel = row_vals[2]
                    else:
                        cand_channel = None
                    scanned += 1
                    if cand_blob is not None and decode_and_check_checkpoint_ref(
                        cand_type, cand_blob, channel=cand_channel, serde=serde, env=env
                    ):
                        hits += 1
                        if stop_on_hit:
                            _emit_progress(progress_stream, consumer, scanned, started, status="hit")
                            return ConsumerCheckOutcome(
                                name=consumer.name,
                                table=consumer.table,
                                has_references=True,
                                hit_count=hits,
                                scanned_rows=scanned,
                                elapsed_ms=(time.monotonic() - started) * 1000.0,
                            )
                cursor_pk = _extract_tuple(rows[-1], pk_len)
                _emit_progress(progress_stream, consumer, scanned, started)
                if len(rows) < batch_size:
                    break
            _emit_progress(
                progress_stream,
                consumer,
                scanned,
                started,
                status="hit" if hits else "clean",
            )
            return ConsumerCheckOutcome(
                name=consumer.name,
                table=consumer.table,
                has_references=hits > 0,
                hit_count=hits,
                scanned_rows=scanned,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        if consumer.mode == "users_avatars":
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (batch_timeout,),
                    )
                    key_col = await _detect_avatar_object_key_column(cur)

            if key_col is not None:
                sql_first = (
                    f"SELECT user_id, {key_col}, avatar_url "
                    "FROM users ORDER BY user_id ASC LIMIT %s"
                )
                sql_next = (
                    f"SELECT user_id, {key_col}, avatar_url "
                    "FROM users WHERE user_id > %s ORDER BY user_id ASC LIMIT %s"
                )
            else:
                sql_first = (
                    "SELECT user_id, NULL::text, avatar_url "
                    "FROM users ORDER BY user_id ASC LIMIT %s"
                )
                sql_next = (
                    "SELECT user_id, NULL::text, avatar_url "
                    "FROM users WHERE user_id > %s ORDER BY user_id ASC LIMIT %s"
                )

            cursor_uid: str | None = None
            while True:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT set_config('statement_timeout', %s, true)",
                            (batch_timeout,),
                        )
                        if cursor_uid is None:
                            await cur.execute(sql_first, (batch_size,))
                        else:
                            await cur.execute(sql_next, (cursor_uid, batch_size))
                        rows = await cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    uid, obj_key, avatar_url = _extract_tuple(row, 3)
                    scanned += 1
                    if isinstance(obj_key, str) and obj_key.strip():
                        hits += 1
                        if stop_on_hit:
                            _emit_progress(progress_stream, consumer, scanned, started, status="hit")
                            return ConsumerCheckOutcome(
                                name=consumer.name,
                                table=consumer.table,
                                has_references=True,
                                hit_count=hits,
                                suspected_count=suspected,
                                scanned_rows=scanned,
                                elapsed_ms=(time.monotonic() - started) * 1000.0,
                            )
                    elif is_site_avatar_url(avatar_url, env=env):
                        suspected += 1
                cursor_uid = str(_extract_tuple(rows[-1], 1)[0])
                _emit_progress(progress_stream, consumer, scanned, started)
                if len(rows) < batch_size:
                    break

            if suspected > 0:
                target_logger.warning(
                    "Suspected main-bucket avatar reference(s) in %s: %d row(s) have "
                    "site-shaped avatar_url without a recorded upload object key "
                    "(warning only; not counted as confirmed reference)",
                    consumer.table,
                    suspected,
                )
                if progress_stream is not None:
                    progress_stream.write(
                        f"[storage_refs] WARNING: {consumer.name} ({consumer.table}) "
                        f"has {suspected} suspected site-shaped avatar_url row(s) "
                        "(no recorded object key; not counted as confirmed hit)\n"
                    )
                    progress_stream.flush()

            _emit_progress(
                progress_stream,
                consumer,
                scanned,
                started,
                status="hit" if hits else ("suspected" if suspected else "clean"),
            )
            return ConsumerCheckOutcome(
                name=consumer.name,
                table=consumer.table,
                has_references=hits > 0,
                hit_count=hits,
                suspected_count=suspected,
                scanned_rows=scanned,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )

        raise ValueError(f"Unknown strict consumer mode: {consumer.mode!r}")
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        err_text = f"{type(exc).__name__}: {exc}"
        target_logger.error(
            "Strict main-storage check failed for %s (%s): %s",
            consumer.name,
            consumer.table,
            err_text,
        )
        return ConsumerCheckOutcome(
            name=consumer.name,
            table=consumer.table,
            has_references=False,
            hit_count=hits,
            suspected_count=suspected,
            scanned_rows=scanned,
            error=err_text,
            elapsed_ms=elapsed_ms,
        )


@dataclass(frozen=True)
class StrictCheckReport:
    """Aggregate report returned by :func:`run_strict_main_bucket_check`."""

    exit_code: int
    outcomes: list[ConsumerCheckOutcome]
    total_elapsed_ms: float
    stopped_early: bool = False
    interrupted: bool = False

    @property
    def confirmed_hits(self) -> list[str]:
        return [o.name for o in self.outcomes if o.has_references]

    @property
    def suspected_warnings(self) -> dict[str, int]:
        return {o.name: o.suspected_count for o in self.outcomes if o.suspected_count > 0}

    @property
    def errors(self) -> list[str]:
        return [o.name for o in self.outcomes if o.error]

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stopped_early": self.stopped_early,
            "interrupted": self.interrupted,
            "total_elapsed_ms": round(self.total_elapsed_ms, 2),
            "hits": self.confirmed_hits,
            "suspected": self.suspected_warnings,
            "errors": self.errors,
            "consumers": [o.to_dict() for o in self.outcomes],
        }


async def run_strict_main_bucket_check(
    *,
    conn: Any,
    consumers: tuple[StrictConsumerSpec, ...] = STRICT_MAIN_BUCKET_CONSUMERS,
    batch_size: int = STRICT_BATCH_SIZE,
    batch_timeout: str = STRICT_BATCH_TIMEOUT,
    stop_on_hit: bool = True,
    progress_stream: TextIO | None = None,
    target_logger: logging.Logger | None = None,
    env: Mapping[str, str] | None = None,
    serde: Any | None = None,
) -> StrictCheckReport:
    """Execute full batched verification across all main-bucket consumers.

    Exit codes:
    - ``0`` (:data:`EXIT_CLEAN`): no confirmed main-bucket references found.
    - ``1`` (:data:`EXIT_REFERENCES_FOUND`): at least one confirmed reference found.
    - ``2`` (:data:`EXIT_CHECK_FAILED`): any check error or interruption.
    """
    log = target_logger or logger
    total_start = time.monotonic()
    outcomes: list[ConsumerCheckOutcome] = []
    stopped_early = False
    effective_batch = max(1, int(batch_size))

    for consumer in consumers:
        outcome = await _run_strict_single_consumer(
            conn,
            consumer,
            batch_size=effective_batch,
            batch_timeout=batch_timeout,
            progress_stream=progress_stream,
            target_logger=log,
            env=env,
            serde=serde,
            stop_on_hit=stop_on_hit,
        )
        outcomes.append(outcome)
        if outcome.error:
            total_ms = (time.monotonic() - total_start) * 1000.0
            return StrictCheckReport(
                exit_code=EXIT_CHECK_FAILED,
                outcomes=outcomes,
                total_elapsed_ms=total_ms,
                stopped_early=len(outcomes) < len(consumers),
            )
        if outcome.has_references and stop_on_hit:
            stopped_early = len(outcomes) < len(consumers)
            break

    total_ms = (time.monotonic() - total_start) * 1000.0
    if any(o.error for o in outcomes):
        code = EXIT_CHECK_FAILED
    elif any(o.has_references for o in outcomes):
        code = EXIT_REFERENCES_FOUND
    else:
        code = EXIT_CLEAN
    return StrictCheckReport(
        exit_code=code,
        outcomes=outcomes,
        total_elapsed_ms=total_ms,
        stopped_early=stopped_early,
    )


# ---------------------------------------------------------------------------
# Full-consumer snapshot enumeration & backup reconciliation (§3.1, §2.1 step 4)
# ---------------------------------------------------------------------------

_SKILL_CONTENT_HASH_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

TERMINAL_ASR_ARCHIVE_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "cancelled"}
)
# Terminal ASR statuses that have produced a result and whose source audio expires
# on the secondary bucket after ``TRANSCRIPTION_AUDIO_EXPIRE_DAYS`` (7 days).
TERMINAL_ASR_RESULT_STATUSES: frozenset[str] = frozenset({"succeeded"})
# Non-terminal ASR statuses (in-flight + indeterminate) whose source audio MUST
# still exist in the ASR bucket regardless of age.
INFLIGHT_ASR_STATUSES: frozenset[str] = frozenset(
    {
        "queued",
        "downloading",
        "probing",
        "splitting",
        "submitting",
        "transcribing",
        "assembling",
        "indeterminate",
    }
)
ACTIVE_OR_SUBMITTED_ASR_STATUSES: frozenset[str] = (
    INFLIGHT_ASR_STATUSES | TERMINAL_ASR_RESULT_STATUSES
)


def _coerce_utc_datetime(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return (
            val.replace(tzinfo=timezone.utc)
            if val.tzinfo is None
            else val.astimezone(timezone.utc)
        )
    if isinstance(val, str):
        raw = val.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (
            parsed.replace(tzinfo=timezone.utc)
            if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc)
        )
    return None


def _normalize_endpoint_identity(url: str | None) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        has_scheme = "://" in raw
        parts = urlsplit(raw if has_scheme else f"//{raw}")
        if parts.hostname is not None:
            host = parts.hostname.lower()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            port = parts.port
            netloc = f"{host}:{port}" if port is not None else host
            path = parts.path.rstrip("/") if not (parts.query or parts.fragment) else parts.path
            if has_scheme:
                return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, parts.fragment))
            return urlunsplit(("", netloc, path, parts.query, parts.fragment)).lstrip("/")
    except Exception:
        pass
    return raw.lower()


def _resolve_asr_expire_bucket_context(
    env: Mapping[str, str] | None = None,
    *,
    asr_bucket_target: tuple[str, str] | None = None,
    asr_bucket_role: str | None = None,
) -> tuple[str, str, str | None]:
    """Resolve ``(asr_bucket, asr_key_prefix, managed_bucket_role)`` for transcription audio expiration.

    Returns ``managed_bucket_role=ROLE_SECONDARY`` only when the ASR bucket is known
    and distinct from the physical primary/main bucket ``(endpoint, bucket)``.
    Returns ``ROLE_MAIN`` when ASR shares the physical primary bucket, or ``None``
    when the target bucket cannot be determined (conservative: never exempt).
    """
    if asr_bucket_target is not None or asr_bucket_role is not None:
        b_name = str(asr_bucket_target[0]).strip() if asr_bucket_target else ""
        k_prefix = str(asr_bucket_target[1]).strip().strip("/") if asr_bucket_target else ""
        role = str(asr_bucket_role).strip().lower() if asr_bucket_role else None
        if not b_name or role not in (ROLE_MAIN, ROLE_SECONDARY):
            return b_name, k_prefix, None
        return b_name, k_prefix, role

    from src.dalton.storage_config import resolve as resolve_storage_config

    em = os.environ if env is None else env
    asr_bucket = (em.get("DALTON_ASR_STORAGE_BUCKET_NAME") or "").strip()
    asr_prefix = (em.get("DALTON_ASR_STORAGE_KEY_PREFIX") or "").strip().strip("/")
    if not asr_bucket:
        return "", asr_prefix, None

    if asr_bucket == STRICT_MAIN_BUCKET_NAME:
        return asr_bucket, asr_prefix, ROLE_MAIN

    asr_ep = _normalize_endpoint_identity(
        em.get("DALTON_ASR_STORAGE_ENDPOINT_URL") or "http://minio:9000"
    )
    cfg = resolve_storage_config(env=em)
    if cfg.is_storage_enabled and cfg.bucket_name:
        main_bucket = cfg.bucket_name.strip()
        main_ep = _normalize_endpoint_identity(cfg.endpoint_url or "")
        if asr_bucket == main_bucket and (not main_ep or not asr_ep or asr_ep == main_ep):
            return asr_bucket, asr_prefix, ROLE_MAIN

    return asr_bucket, asr_prefix, ROLE_SECONDARY


def is_terminal_asr_source_audio_expired(
    *,
    status: str,
    source_object_key: str = "",
    completed_at: Any = None,
    upload_completed_at: Any = None,
    upload_completed: bool = False,
    bucket_role: str | None = None,
    key_prefix: str = "",
    now: datetime | None = None,
    expire_days: int = TRANSCRIPTION_AUDIO_EXPIRE_DAYS,
) -> bool:
    """Return True iff a terminal result-bearing ASR job's source audio is covered by
    the managed secondary-bucket 7-day rule and has exceeded ``expire_days`` relative
    to the backup snapshot time ``now``.

    - Never uses ``created_at`` or ``updated_at``: requires a verifiable upload
      completion timestamp (``upload_completed_at`` when ``upload_completed=True``)
      or job completion timestamp (``completed_at``).
    - Only exempts keys confirmed to sit under a secondary bucket covered by the
      managed 7-day rule AND the ``<key_prefix>transcriptions/`` prefix (shared with
      ``src.dalton.storage_bucket_rules``).
    - Whenever bucket coverage, prefix coverage, or trustworthy timestamp cannot be
      determined, returns ``False`` so the reference is checked as usual.
    """
    st_s = str(status or "").strip().lower()
    if st_s not in TERMINAL_ASR_RESULT_STATUSES:
        return False
    if not is_key_covered_by_transcription_audio_expire_rule(
        source_object_key,
        bucket_role=bucket_role,
        key_prefix=key_prefix,
    ):
        return False
    upload_ts = _coerce_utc_datetime(upload_completed_at) if upload_completed else None
    job_done_ts = _coerce_utc_datetime(completed_at)
    ref_ts = upload_ts if upload_ts is not None else job_done_ts
    if ref_ts is None:
        return False
    effective_now = _coerce_utc_datetime(now) or datetime.now(timezone.utc)
    return (effective_now - ref_ts) > timedelta(days=expire_days)


def parse_skill_content_hash(content_hash: str | None) -> str:
    """Validate a skill archive ``content_hash`` (``sha256:<64 hex>``) and return the hex digest.

    Raises ``ValueError`` when the ``sha256:`` prefix is missing or the digest is malformed.
    """
    raw = str(content_hash or "").strip()
    match = _SKILL_CONTENT_HASH_RE.fullmatch(raw)
    if not match:
        raise ValueError(
            f"Invalid skill archive content_hash (expected 'sha256:<64 lowercase hex>'): {content_hash!r}"
        )
    return match.group(1)


def _apply_bucket_prefix(object_key: str, key_prefix: str) -> str:
    cleaned_key = str(object_key or "").strip().lstrip("/")
    cleaned_prefix = str(key_prefix or "").strip().strip("/")
    if not cleaned_prefix:
        return cleaned_key
    prefix_slash = f"{cleaned_prefix}/"
    if cleaned_key.startswith(prefix_slash):
        return cleaned_key
    return f"{prefix_slash}{cleaned_key}"


def _extract_widget_data_refs_from_turn_slice(lines_text: str | None) -> list[dict[str, Any]]:
    if not lines_text or '"data_ref"' not in lines_text:
        return []
    refs: list[dict[str, Any]] = []
    for raw in lines_text.split("\n"):
        if "\t" not in raw:
            continue
        parts = raw.split("\t", 2)
        if len(parts) != 3:
            continue
        event, _flags, data_json = parts
        if event != "artifact":
            continue
        try:
            data = json.loads(data_json)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("artifact_type") != "html_widget":
            continue
        payload = data.get("payload")
        if _widget_payload_has_data_ref(payload):
            refs.append(dict(payload["data_ref"]))
    return refs


def _extract_widget_data_refs_from_checkpoint(value: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if value is None:
        return refs
    if type(value).__name__ == "_DeltaSnapshot" and hasattr(value, "value"):
        return _extract_widget_data_refs_from_checkpoint(value.value)
    if isinstance(value, (list, tuple)):
        for item in value:
            refs.extend(_extract_widget_data_refs_from_checkpoint(item))
        return refs
    msg_type = getattr(value, "type", None)
    if msg_type == "tool" and hasattr(value, "artifact"):
        art = getattr(value, "artifact", None)
        if _is_show_widget_tool_artifact(getattr(value, "name", None), art):
            refs.append(dict(art["data_ref"]))
        return refs
    if isinstance(value, dict):
        if value.get("type") == "tool" and "artifact" in value:
            art = value.get("artifact")
            if _is_show_widget_tool_artifact(value.get("name"), art):
                refs.append(dict(art["data_ref"]))
            return refs
        if (
            value.get("lc") == 1
            and value.get("type") == "constructor"
            and isinstance(value.get("id"), list)
            and value["id"]
            and value["id"][-1] == "ToolMessage"
            and isinstance(value.get("kwargs"), dict)
        ):
            kw = value["kwargs"]
            art = kw.get("artifact")
            if _is_show_widget_tool_artifact(kw.get("name"), art):
                refs.append(dict(art["data_ref"]))
    return refs


def extract_avatar_object_key(
    avatar_url: str | None, *, env: Mapping[str, str] | None = None
) -> str | None:
    """Return logical ``avatars/<name>.<ext>`` key if ``avatar_url`` matches this site."""
    raw = (avatar_url or "").strip()
    if not raw or not is_site_avatar_url(raw, env=env):
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    path = parsed.path or ""
    for _rule_origin, full_prefix, _path_ere in site_avatar_url_rules(env=env):
        if path.startswith(full_prefix):
            tail = path[len(full_prefix) :]
            if _AVATAR_KEY_TAIL_RE.match(tail):
                return f"avatars/{tail}"
    return None


def extract_response_image_object_key(
    url: str | None, *, env: Mapping[str, str] | None = None
) -> str | None:
    """Return logical ``private-media/response-images/...`` or ``response-images/...`` key for this site's main bucket."""
    raw = (url or "").strip()
    if not raw or not is_main_bucket_response_image_url(raw, env=env):
        return None
    for logical_prefix in _RESPONSE_IMAGE_LOGICAL_PREFIXES:
        if raw.startswith(logical_prefix):
            tail = unquote(raw[len(logical_prefix) :])
            if _is_valid_response_image_tail(tail, require_hash=True):
                return f"{logical_prefix}{tail}"
            return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    path = parsed.path or ""
    rules = _site_prefix_rules(_RESPONSE_IMAGE_LOGICAL_PREFIXES, env=env)
    if rules:
        for rule_origin, full_prefix, logical_prefix in rules:
            if parsed.scheme:
                origin = _origin_from_split(parsed)
                if rule_origin != origin:
                    continue
            if path.startswith(full_prefix):
                tail = path[len(full_prefix) :]
                if _is_valid_response_image_tail(tail, require_hash=False):
                    return f"{logical_prefix}{tail}"
        return None
    key_prefix = _normalize_storage_key_prefix(env)
    for logical_prefix in _RESPONSE_IMAGE_LOGICAL_PREFIXES:
        for prefix in (f"/{key_prefix}{logical_prefix}", f"/storage/{key_prefix}{logical_prefix}"):
            if path.startswith(prefix):
                tail = path[len(prefix) :]
                if _is_valid_response_image_tail(tail, require_hash=True):
                    return f"{logical_prefix}{tail}"
    return None


def _extract_response_image_keys_from_map(
    captured: Any, *, env: Mapping[str, str] | None = None
) -> list[str]:
    if not isinstance(captured, dict):
        return []
    keys: list[str] = []
    for k, v in captured.items():
        if k and isinstance(v, str):
            obj_key = extract_response_image_object_key(v, env=env)
            if obj_key:
                keys.append(obj_key)
    return keys


def _extract_response_image_keys_from_text(
    text: str, *, env: Mapping[str, str] | None = None
) -> list[str]:
    if not text or "response-images/" not in text:
        return []
    keys: list[str] = []
    for match in _MARKDOWN_IMAGE_OR_LINK_RE.finditer(text):
        target = _extract_markdown_target_url(match.group(3))
        obj_key = extract_response_image_object_key(target, env=env)
        if obj_key:
            keys.append(obj_key)
    for match in _HTML_IMG_SRC_RE.finditer(text):
        src = match.group(1) or match.group(2) or match.group(3) or ""
        obj_key = extract_response_image_object_key(src, env=env)
        if obj_key:
            keys.append(obj_key)
    return keys


def _extract_response_image_keys_from_replay_facts(
    replay_facts: Any,
    *,
    include_legacy: bool = True,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    facts = replay_facts
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except (ValueError, TypeError):
            return []
    if not isinstance(facts, dict):
        return []
    keys: list[str] = []
    keys.extend(_extract_response_image_keys_from_map(facts.get("images"), env=env))
    if include_legacy:
        legacy_obj = facts.get("legacy")
        if isinstance(legacy_obj, dict):
            keys.extend(_extract_response_image_keys_from_map(legacy_obj.get("images"), env=env))
            verbatim = legacy_obj.get("verbatim")
            if isinstance(verbatim, list):
                grouped_texts: dict[tuple[Any, Any], list[str]] = {}
                for stored in verbatim:
                    if not isinstance(stored, dict):
                        continue
                    event = stored.get("event")
                    data = stored.get("data")
                    if not isinstance(data, dict):
                        continue
                    if event == "message_chunk":
                        content_type = data.get("content_type")
                        content = data.get("content")
                        if content_type in ("text", None) and isinstance(content, str) and content:
                            grouped_texts.setdefault(
                                (data.get("agent"), data.get("id")), []
                            ).append(content)
                    elif event == "image_capture" or data.get("name") == "image_capture":
                        props = data.get("props")
                        if isinstance(props, dict):
                            keys.extend(
                                _extract_response_image_keys_from_map(
                                    props.get("path_to_url"), env=env
                                )
                            )
                        keys.extend(
                            _extract_response_image_keys_from_map(
                                data.get("path_to_url"), env=env
                            )
                        )
                for pieces in grouped_texts.values():
                    keys.extend(
                        _extract_response_image_keys_from_text("".join(pieces), env=env)
                    )
    return keys


def _extract_response_image_keys_from_checkpoint(
    value: Any, *, channel: str | None = None, env: Mapping[str, str] | None = None
) -> list[str]:
    keys: list[str] = []
    if value is None:
        return keys
    if type(value).__name__ == "_DeltaSnapshot" and hasattr(value, "value"):
        return _extract_response_image_keys_from_checkpoint(
            value.value, channel=channel, env=env
        )
    if isinstance(value, (list, tuple)):
        for item in value:
            keys.extend(
                _extract_response_image_keys_from_checkpoint(item, channel=channel, env=env)
            )
        return keys
    if channel != "ui":
        msg_type = getattr(value, "type", None)
        if msg_type in ("ai", "AIMessageChunk") and hasattr(value, "response_metadata"):
            meta = getattr(value, "response_metadata", None)
            if isinstance(meta, dict):
                keys.extend(
                    _extract_response_image_keys_from_map(
                        meta.get("captured_images"), env=env
                    )
                )
        elif isinstance(value, dict):
            if value.get("type") in ("ai", "AIMessageChunk") and isinstance(
                value.get("response_metadata"), dict
            ):
                keys.extend(
                    _extract_response_image_keys_from_map(
                        value["response_metadata"].get("captured_images"), env=env
                    )
                )
            elif (
                value.get("lc") == 1
                and value.get("type") == "constructor"
                and isinstance(value.get("id"), list)
                and value["id"]
                and value["id"][-1] in ("AIMessage", "AIMessageChunk")
                and isinstance(value.get("kwargs"), dict)
            ):
                meta = value["kwargs"].get("response_metadata")
                if isinstance(meta, dict):
                    keys.extend(
                        _extract_response_image_keys_from_map(
                            meta.get("captured_images"), env=env
                        )
                    )
    if channel != "messages":
        if isinstance(value, dict) and value.get("name") == "image_capture":
            props = value.get("props")
            if isinstance(props, dict):
                keys.extend(
                    _extract_response_image_keys_from_map(props.get("path_to_url"), env=env)
                )
    return keys


def _clean_decoded_attachment_tail(raw_tail: str, *, decode_once: bool) -> str | None:
    tail = unquote(raw_tail) if decode_once else raw_tail
    tail = tail.strip("/")
    if not tail or "\x00" in tail:
        return None
    segments = tail.split("/")
    if any(not seg or seg in (".", "..") for seg in segments):
        return None
    return tail


def extract_attachment_object_key(
    url_or_key: str | None, *, env: Mapping[str, str] | None = None
) -> str | None:
    """Extract logical ``attachments/...`` or ``private-media/attachments/...`` key from a stored attachment URL or key.

    Validates URL origin against this deployment's configured site/main-bucket URL bases
    (when configured) and percent-decodes URL paths exactly once to match ``_object_url(quote(key, safe='/'))``.
    """
    raw = (url_or_key or "").strip()
    if not raw:
        return None
    for logical_prefix in _ATTACHMENT_LOGICAL_PREFIXES:
        if raw.startswith(logical_prefix):
            cleaned = _clean_decoded_attachment_tail(
                raw[len(logical_prefix) :], decode_once=False
            )
            return f"{logical_prefix}{cleaned}" if cleaned else None

    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    path = parsed.path or ""
    if "attachments/" not in path:
        return None

    rules = _site_prefix_rules(_ATTACHMENT_LOGICAL_PREFIXES, env=env)
    if rules:
        if parsed.scheme:
            origin = _origin_from_split(parsed)
            if origin is None:
                return None
            for rule_origin, full_prefix, logical_prefix in rules:
                if rule_origin == origin and path.startswith(full_prefix):
                    tail = _clean_decoded_attachment_tail(
                        path[len(full_prefix) :], decode_once=True
                    )
                    if tail:
                        return f"{logical_prefix}{tail}"
            return None
        if parsed.netloc or not path.startswith("/"):
            return None
        for _rule_origin, full_prefix, logical_prefix in rules:
            if path.startswith(full_prefix):
                tail = _clean_decoded_attachment_tail(
                    path[len(full_prefix) :], decode_once=True
                )
                if tail:
                    return f"{logical_prefix}{tail}"
        key_prefix = _normalize_storage_key_prefix(env)
        for logical_prefix in _ATTACHMENT_LOGICAL_PREFIXES:
            for prefix in (
                f"/{key_prefix}{logical_prefix}",
                f"/storage/{key_prefix}{logical_prefix}",
            ):
                if path.startswith(prefix):
                    tail = _clean_decoded_attachment_tail(
                        path[len(prefix) :], decode_once=True
                    )
                    if tail:
                        return f"{logical_prefix}{tail}"
        return None

    # Fallback when no site/main-bucket URL base is configured in env:
    # still enforce valid http(s)/relative URL and exact root or /storage/ object prefix.
    if parsed.scheme:
        if _origin_from_split(parsed) is None:
            return None
    elif parsed.netloc or not path.startswith("/"):
        return None

    key_prefix = _normalize_storage_key_prefix(env)
    for logical_prefix in _ATTACHMENT_LOGICAL_PREFIXES:
        for prefix in (
            f"/{key_prefix}{logical_prefix}",
            f"/storage/{key_prefix}{logical_prefix}",
        ):
            if path.startswith(prefix):
                tail = _clean_decoded_attachment_tail(
                    path[len(prefix) :], decode_once=True
                )
                if tail:
                    return f"{logical_prefix}{tail}"
    return None


@dataclass(frozen=True)
class ExpiredByRuleObject:
    """One referenced object exempted because it exceeded a managed bucket lifecycle rule."""

    bucket: str
    key: str
    consumer: str
    rule_id: str = RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE
    row_id: str = ""

    def to_dict(self) -> dict[str, str]:
        out = {
            "bucket": self.bucket,
            "key": self.key,
            "consumer": self.consumer,
            "rule_id": self.rule_id,
        }
        if self.row_id:
            out["row_id"] = self.row_id
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExpiredByRuleObject:
        return cls(
            bucket=str(data.get("bucket") or "").strip(),
            key=str(data.get("key") or "").strip().lstrip("/"),
            consumer=str(data.get("consumer") or "").strip(),
            rule_id=str(data.get("rule_id") or RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE).strip(),
            row_id=str(data.get("row_id") or "").strip(),
        )


@dataclass(frozen=True)
class ObjectReference:
    """One live object-storage reference enumerated from a PostgreSQL snapshot."""

    consumer: str
    bucket_role: str  # "main" | "chat_upload" | "asr"
    object_key: str
    row_id: str
    expected_size: int | None = None
    expected_sha256: str | None = None
    check_mode: str = "strict"  # "strict" | "exists_only" | "exists_warn_size" | "invalid"
    invalid_reason: str | None = None
    require_present: bool = False  # True = never tolerated under secondary missing-ref threshold

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "consumer": self.consumer,
            "bucket_role": self.bucket_role,
            "object_key": self.object_key,
            "row_id": self.row_id,
            "expected_size": self.expected_size,
            "expected_sha256": self.expected_sha256,
            "check_mode": self.check_mode,
            "invalid_reason": self.invalid_reason,
        }
        if self.require_present:
            out["require_present"] = True
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ObjectReference:
        return cls(
            consumer=str(data["consumer"]),
            bucket_role=str(data["bucket_role"]),
            object_key=str(data.get("object_key") or ""),
            row_id=str(data.get("row_id") or ""),
            expected_size=(
                int(data["expected_size"]) if data.get("expected_size") is not None else None
            ),
            expected_sha256=(
                str(data["expected_sha256"]) if data.get("expected_sha256") is not None else None
            ),
            check_mode=str(data.get("check_mode") or "strict"),
            invalid_reason=(
                str(data["invalid_reason"]) if data.get("invalid_reason") is not None else None
            ),
            require_present=bool(data.get("require_present", False)),
        )


@dataclass
class SnapshotRefInventory:
    """All live object references and terminal ASR job IDs captured under one DB snapshot."""

    references: list[ObjectReference] = field(default_factory=list)
    terminal_asr_job_ids: list[str] = field(default_factory=list)
    succeeded_asr_job_ids: list[str] = field(default_factory=list)
    skipped_pending: dict[str, int] = field(default_factory=dict)
    skipped_expired: dict[str, int] = field(default_factory=dict)
    expired_by_rule_objects: list[ExpiredByRuleObject] = field(default_factory=list)
    skipped_missing_tables: list[str] = field(default_factory=list)

    def keys_by_bucket_role(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {"main": set(), "chat_upload": set(), "asr": set()}
        for ref in self.references:
            if ref.object_key:
                out.setdefault(ref.bucket_role, set()).add(ref.object_key)
        return out

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "references": [r.to_dict() for r in self.references],
            "terminal_asr_job_ids": list(self.terminal_asr_job_ids),
            "succeeded_asr_job_ids": list(self.succeeded_asr_job_ids),
            "skipped_pending": dict(self.skipped_pending),
            "skipped_missing_tables": list(self.skipped_missing_tables),
        }
        if self.skipped_expired:
            out["skipped_expired"] = dict(self.skipped_expired)
        if self.expired_by_rule_objects:
            out["expired_by_rule_objects"] = [x.to_dict() for x in self.expired_by_rule_objects]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SnapshotRefInventory:
        return cls(
            references=[
                ObjectReference.from_dict(item)
                for item in (data.get("references") or [])
                if isinstance(item, Mapping)
            ],
            terminal_asr_job_ids=[str(x) for x in (data.get("terminal_asr_job_ids") or [])],
            succeeded_asr_job_ids=[str(x) for x in (data.get("succeeded_asr_job_ids") or [])],
            skipped_pending={
                str(k): int(v) for k, v in (data.get("skipped_pending") or {}).items()
            },
            skipped_expired={
                str(k): int(v) for k, v in (data.get("skipped_expired") or {}).items()
            },
            expired_by_rule_objects=[
                ExpiredByRuleObject.from_dict(item)
                for item in (data.get("expired_by_rule_objects") or [])
                if isinstance(item, Mapping)
            ],
            skipped_missing_tables=[str(x) for x in (data.get("skipped_missing_tables") or [])],
        )


@dataclass(frozen=True)
class ReconcileIssue:
    """One error or warning emitted by backup-set reconciliation."""

    consumer: str
    code: str
    row_id: str
    object_key: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "consumer": self.consumer,
            "code": self.code,
            "row_id": self.row_id,
            "object_key": self.object_key,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ReconcileReport:
    """Outcome of reconciling a snapshot reference inventory against archived objects & ASR jobs."""

    ok: bool
    checked_refs: int
    checked_asr_jobs: int
    errors: tuple[ReconcileIssue, ...] = ()
    warnings: tuple[ReconcileIssue, ...] = ()
    skipped_pending: dict[str, int] = field(default_factory=dict)
    known_missing: tuple[ReconcileIssue, ...] = ()
    expired_by_rule: tuple[ReconcileIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_refs": self.checked_refs,
            "checked_asr_jobs": self.checked_asr_jobs,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "known_missing_count": len(self.known_missing),
            "expired_by_rule_count": len(self.expired_by_rule),
            "skipped_pending": dict(self.skipped_pending),
            "errors": [e.to_dict() for e in self.errors],
            "warnings": [w.to_dict() for w in self.warnings],
            "known_missing": [k.to_dict() for k in self.known_missing],
            "expired_by_rule": [e.to_dict() for e in self.expired_by_rule],
        }


def _blob_storage_key_safe(user_id: str, sha256: str) -> tuple[str | None, str | None]:
    try:
        mapped = storage_user_id(str(user_id))
    except Exception as exc:
        return None, f"invalid storage_user_id for {user_id!r}: {exc}"
    clean_sha = str(sha256 or "").strip().lower()
    if not _SHA256_HEX_RE.fullmatch(clean_sha):
        return None, f"invalid sha256 digest: {sha256!r}"
    return f"blobs/{mapped}/{clean_sha}", None


async def _table_exists(cur: Any, table: str) -> bool:
    await cur.execute("SELECT to_regclass(%s)", (table,))
    row = await cur.fetchone()
    return _extract_first_value(row) is not None


async def enumerate_snapshot_refs(
    conn: Any,
    *,
    env: Mapping[str, str] | None = None,
    serde: Any | None = None,
    include_main: bool = True,
    include_aux: bool = True,
    now: datetime | None = None,
    asr_bucket_target: tuple[str, str] | None = None,
    asr_bucket_role: str | None = None,
    skip_expired_asr: bool = True,
) -> SnapshotRefInventory:
    """Enumerate all live object references and terminal ASR job IDs inside the caller's DB snapshot.

    Runs directly on ``conn`` without committing or rolling back the caller's
    ``REPEATABLE READ READ ONLY`` transaction.
    """
    inv = SnapshotRefInventory()
    blob_registry: dict[tuple[str, str], int] = {}
    effective_now = _coerce_utc_datetime(now) or datetime.now(timezone.utc)
    asr_bucket, asr_prefix, effective_asr_bucket_role = _resolve_asr_expire_bucket_context(
        env=env,
        asr_bucket_target=asr_bucket_target,
        asr_bucket_role=asr_bucket_role,
    )

    async with conn.cursor() as cur:
        # 1. workspace_file_blobs (live rows: condemned_at IS NULL)
        has_blobs = await _table_exists(cur, "public.workspace_file_blobs")
        if include_main:
            if has_blobs:
                await cur.execute(
                    "SELECT user_id, sha256, byte_len FROM workspace_file_blobs "
                    "WHERE condemned_at IS NULL"
                )
                for row in await cur.fetchall():
                    uid, sha, byte_len = _extract_tuple(row, 3)
                    uid_s = str(uid)
                    sha_s = str(sha).strip().lower()
                    blen = int(byte_len)
                    blob_registry[(uid_s, sha_s)] = blen
                    key, err = _blob_storage_key_safe(uid_s, sha_s)
                    if err or not key:
                        inv.references.append(
                            ObjectReference(
                                consumer="workspace_file_blobs",
                                bucket_role="main",
                                object_key="",
                                row_id=f"{uid_s}:{sha_s}",
                                check_mode="invalid",
                                invalid_reason=err,
                            )
                        )
                    else:
                        inv.references.append(
                            ObjectReference(
                                consumer="workspace_file_blobs",
                                bucket_role="main",
                                object_key=key,
                                row_id=f"{uid_s}:{sha_s}",
                                expected_size=blen,
                                expected_sha256=sha_s,
                                check_mode="strict",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.workspace_file_blobs")

            # 2. workspace_files (blob_sha256 and pack_sha256)
            has_ws_files = await _table_exists(cur, "public.workspace_files")
            has_workspaces = await _table_exists(cur, "public.workspaces")
            if has_ws_files and has_workspaces:
                await cur.execute(
                    "SELECT f.workspace_file_id, w.user_id, f.file_size, "
                    "f.blob_sha256, f.pack_sha256, f.pack_offset "
                    "FROM workspace_files f "
                    "JOIN workspaces w ON w.workspace_id = f.workspace_id "
                    "WHERE f.blob_sha256 IS NOT NULL OR f.pack_sha256 IS NOT NULL"
                )
                pack_rows_needed: dict[tuple[str, str], list[tuple[str, int, int | None]]] = {}
                for row in await cur.fetchall():
                    wf_id, uid, file_size, blob_sha, pack_sha, pack_offset = _extract_tuple(row, 6)
                    uid_s = str(uid)
                    fsize = int(file_size or 0)
                    if blob_sha is not None:
                        bsha = str(blob_sha).strip().lower()
                        key, err = _blob_storage_key_safe(uid_s, bsha)
                        if err or not key:
                            inv.references.append(
                                ObjectReference(
                                    consumer="workspace_files",
                                    bucket_role="main",
                                    object_key="",
                                    row_id=str(wf_id),
                                    check_mode="invalid",
                                    invalid_reason=err,
                                )
                            )
                        else:
                            inv.references.append(
                                ObjectReference(
                                    consumer="workspace_files",
                                    bucket_role="main",
                                    object_key=key,
                                    row_id=str(wf_id),
                                    expected_size=fsize,
                                    expected_sha256=bsha,
                                    check_mode="strict",
                                )
                            )
                    if pack_sha is not None:
                        psha = str(pack_sha).strip().lower()
                        poff = int(pack_offset) if pack_offset is not None else None
                        pack_rows_needed.setdefault((uid_s, psha), []).append(
                            (str(wf_id), fsize, poff)
                        )

                # Resolve pack byte_len from live blob_registry (or condemned row if present)
                for (uid_s, psha), members in pack_rows_needed.items():
                    pack_byte_len = blob_registry.get((uid_s, psha))
                    if pack_byte_len is None and has_blobs:
                        await cur.execute(
                            "SELECT byte_len FROM workspace_file_blobs "
                            "WHERE user_id = %s AND sha256 = %s",
                            (uid_s, psha),
                        )
                        brow = await cur.fetchone()
                        if brow is not None:
                            pack_byte_len = int(_extract_first_value(brow))
                    key, err = _blob_storage_key_safe(uid_s, psha)
                    if err or not key:
                        inv.references.append(
                            ObjectReference(
                                consumer="workspace_files_pack",
                                bucket_role="main",
                                object_key="",
                                row_id=f"{uid_s}:{psha}",
                                check_mode="invalid",
                                invalid_reason=err,
                            )
                        )
                        continue
                    if pack_byte_len is None:
                        inv.references.append(
                            ObjectReference(
                                consumer="workspace_files_pack",
                                bucket_role="main",
                                object_key=key,
                                row_id=f"{uid_s}:{psha}",
                                check_mode="invalid",
                                invalid_reason=(
                                    f"pack_sha256 {psha} for user {uid_s} has no "
                                    "workspace_file_blobs registry row"
                                ),
                            )
                        )
                        continue
                    inv.references.append(
                        ObjectReference(
                            consumer="workspace_files_pack",
                            bucket_role="main",
                            object_key=key,
                            row_id=f"{uid_s}:{psha}",
                            expected_size=pack_byte_len,
                            expected_sha256=psha,
                            check_mode="strict",
                        )
                    )
                    for wf_id, fsize, poff in members:
                        if poff is None or poff < 0 or fsize < 0 or (poff + fsize) > pack_byte_len:
                            inv.references.append(
                                ObjectReference(
                                    consumer="workspace_files_pack_member",
                                    bucket_role="main",
                                    object_key=key,
                                    row_id=wf_id,
                                    check_mode="invalid",
                                    invalid_reason=(
                                        f"pack member slice out of bounds: "
                                        f"pack_offset={poff}, file_size={fsize}, "
                                        f"pack_byte_len={pack_byte_len}"
                                    ),
                                )
                            )
            else:
                if not has_ws_files:
                    inv.skipped_missing_tables.append("public.workspace_files")
                if not has_workspaces:
                    inv.skipped_missing_tables.append("public.workspaces")

            # 3. thread_transcript_files (content IS NULL)
            if await _table_exists(cur, "public.thread_transcript_files"):
                await cur.execute(
                    "SELECT conversation_thread_id, path, user_id, sha256, byte_len "
                    "FROM thread_transcript_files WHERE content IS NULL"
                )
                for row in await cur.fetchall():
                    tid, path, uid, sha, byte_len = _extract_tuple(row, 5)
                    uid_s = str(uid)
                    sha_s = str(sha).strip().lower()
                    key, err = _blob_storage_key_safe(uid_s, sha_s)
                    if err or not key:
                        inv.references.append(
                            ObjectReference(
                                consumer="thread_transcript_files",
                                bucket_role="main",
                                object_key="",
                                row_id=f"{tid}:{path}",
                                check_mode="invalid",
                                invalid_reason=err,
                            )
                        )
                    else:
                        inv.references.append(
                            ObjectReference(
                                consumer="thread_transcript_files",
                                bucket_role="main",
                                object_key=key,
                                row_id=f"{tid}:{path}",
                                expected_size=int(byte_len),
                                expected_sha256=sha_s,
                                check_mode="strict",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.thread_transcript_files")

            # 4. artifact_version_files (content IS NULL)
            if await _table_exists(cur, "public.artifact_version_files"):
                await cur.execute(
                    "SELECT artifact_id, seq, path, user_id, sha256, byte_len "
                    "FROM artifact_version_files WHERE content IS NULL"
                )
                for row in await cur.fetchall():
                    aid, seq, path, uid, sha, byte_len = _extract_tuple(row, 6)
                    uid_s = str(uid)
                    sha_s = str(sha).strip().lower()
                    key, err = _blob_storage_key_safe(uid_s, sha_s)
                    if err or not key:
                        inv.references.append(
                            ObjectReference(
                                consumer="artifact_version_files",
                                bucket_role="main",
                                object_key="",
                                row_id=f"{aid}:{seq}:{path}",
                                check_mode="invalid",
                                invalid_reason=err,
                            )
                        )
                    else:
                        inv.references.append(
                            ObjectReference(
                                consumer="artifact_version_files",
                                bucket_role="main",
                                object_key=key,
                                row_id=f"{aid}:{seq}:{path}",
                                expected_size=int(byte_len),
                                expected_sha256=sha_s,
                                check_mode="strict",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.artifact_version_files")

            # 5. drive_contents (blob_user_id IS NOT NULL) & drive_uploads pending count
            if await _table_exists(cur, "public.drive_contents"):
                await cur.execute(
                    "SELECT space_id, sha256, byte_len, blob_user_id "
                    "FROM drive_contents WHERE blob_user_id IS NOT NULL"
                )
                for row in await cur.fetchall():
                    sid, sha, byte_len, buid = _extract_tuple(row, 4)
                    uid_s = str(buid)
                    sha_s = str(sha).strip().lower()
                    key, err = _blob_storage_key_safe(uid_s, sha_s)
                    if err or not key:
                        inv.references.append(
                            ObjectReference(
                                consumer="drive_contents",
                                bucket_role="main",
                                object_key="",
                                row_id=f"{sid}:{sha_s}",
                                check_mode="invalid",
                                invalid_reason=err,
                            )
                        )
                    else:
                        inv.references.append(
                            ObjectReference(
                                consumer="drive_contents",
                                bucket_role="main",
                                object_key=key,
                                row_id=f"{sid}:{sha_s}",
                                expected_size=int(byte_len),
                                expected_sha256=sha_s,
                                check_mode="strict",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.drive_contents")

            if await _table_exists(cur, "public.drive_uploads"):
                await cur.execute(
                    "SELECT COUNT(*) FROM drive_uploads WHERE state IN ('uploading', 'verifying')"
                )
                cnt = int(_extract_first_value(await cur.fetchone()) or 0)
                if cnt:
                    inv.skipped_pending["drive_uploads"] = cnt

            # 5b. workspace_file_uploads: `pulling` rows depend on an assembled object in main storage;
            #     `uploading`/`completing` rows are pending multipart uploads skipped during backup
            #     and converged by post-restore reconciliation.
            if await _table_exists(cur, "public.workspace_file_uploads"):
                await cur.execute(
                    "SELECT upload_id, object_key, size, sha256, state "
                    "FROM workspace_file_uploads "
                    "WHERE cleaned_at IS NULL AND state IN ('uploading', 'completing', 'pulling')"
                )
                ws_upload_pending = 0
                for row in await cur.fetchall():
                    uid, okey, sz, sha, st = _extract_tuple(row, 5)
                    st_clean = str(st or "").strip().lower()
                    if st_clean in ("uploading", "completing"):
                        ws_upload_pending += 1
                        continue
                    if st_clean != "pulling":
                        continue
                    clean_key = str(okey or "").strip().lstrip("/")
                    if not clean_key:
                        inv.references.append(
                            ObjectReference(
                                consumer="workspace_file_uploads",
                                bucket_role="main",
                                object_key="",
                                row_id=str(uid),
                                check_mode="invalid",
                                invalid_reason="pulling workspace_file_uploads row has empty object_key",
                            )
                        )
                        continue
                    inv.references.append(
                        ObjectReference(
                            consumer="workspace_file_uploads",
                            bucket_role="main",
                            object_key=clean_key,
                            row_id=str(uid),
                            expected_size=int(sz) if sz is not None else None,
                            expected_sha256=(
                                str(sha).strip().lower() if sha and str(sha).strip() else None
                            ),
                            check_mode="strict",
                        )
                    )
                if ws_upload_pending:
                    inv.skipped_pending["workspace_file_uploads"] = ws_upload_pending
            else:
                inv.skipped_missing_tables.append("public.workspace_file_uploads")

            # 6. provenance_result_bodies (mutable key: check exists, warn on size mismatch)
            if await _table_exists(cur, "public.provenance_result_bodies"):
                await cur.execute(
                    "SELECT result_sha256, object_key, byte_len "
                    "FROM provenance_result_bodies "
                    "WHERE object_key IS NOT NULL AND object_key <> ''"
                )
                for row in await cur.fetchall():
                    rsha, okey, byte_len = _extract_tuple(row, 3)
                    inv.references.append(
                        ObjectReference(
                            consumer="provenance_result_bodies",
                            bucket_role="main",
                            object_key=str(okey).strip().lstrip("/"),
                            row_id=str(rsha),
                            expected_size=int(byte_len) if byte_len is not None else None,
                            check_mode="exists_warn_size",
                        )
                    )
            else:
                inv.skipped_missing_tables.append("public.provenance_result_bodies")

            # 7. Memos in store (binary_ref.key -> exists_only)
            if await _table_exists(cur, "public.store"):
                await cur.execute(
                    "SELECT prefix, key, value -> 'binary_ref' ->> 'key' "
                    "FROM store "
                    "WHERE prefix LIKE '%%.memos' AND value ? 'binary_ref' "
                    "AND jsonb_typeof(value -> 'binary_ref') = 'object' "
                    "AND COALESCE(value -> 'binary_ref' ->> 'key', '') <> ''"
                )
                for row in await cur.fetchall():
                    pref, skey, bkey = _extract_tuple(row, 3)
                    inv.references.append(
                        ObjectReference(
                            consumer="memo_binaries",
                            bucket_role="main",
                            object_key=str(bkey).strip().lstrip("/"),
                            row_id=f"{pref}:{skey}",
                            check_mode="exists_only",
                        )
                    )
            else:
                inv.skipped_missing_tables.append("public.store")

            # 8. user_skills (archive_key + content_hash with mandatory 'sha256:' prefix)
            if await _table_exists(cur, "public.user_skills"):
                await cur.execute(
                    "SELECT user_skill_id, archive_key, content_hash "
                    "FROM user_skills "
                    "WHERE archive_key IS NOT NULL AND archive_key <> ''"
                )
                for row in await cur.fetchall():
                    skill_id, akey, chash = _extract_tuple(row, 3)
                    clean_key = str(akey).strip().lstrip("/")
                    try:
                        expected_hex = parse_skill_content_hash(chash)
                    except ValueError as exc:
                        inv.references.append(
                            ObjectReference(
                                consumer="user_skills",
                                bucket_role="main",
                                object_key=clean_key,
                                row_id=str(skill_id),
                                check_mode="invalid",
                                invalid_reason=str(exc),
                            )
                        )
                    else:
                        inv.references.append(
                            ObjectReference(
                                consumer="user_skills",
                                bucket_role="main",
                                object_key=clean_key,
                                row_id=str(skill_id),
                                expected_sha256=expected_hex,
                                check_mode="strict",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.user_skills")

            # 9. org_library_items (kind = 'skill', archive_key + content_hash 'sha256:<hex>')
            if await _table_exists(cur, "public.org_library_items"):
                await cur.execute(
                    "SELECT item_id, archive_key, content_hash "
                    "FROM org_library_items "
                    "WHERE kind = 'skill' AND archive_key IS NOT NULL AND archive_key <> ''"
                )
                for row in await cur.fetchall():
                    item_id, akey, chash = _extract_tuple(row, 3)
                    clean_key = str(akey).strip().lstrip("/")
                    try:
                        expected_hex = parse_skill_content_hash(chash)
                    except ValueError as exc:
                        inv.references.append(
                            ObjectReference(
                                consumer="org_library_items",
                                bucket_role="main",
                                object_key=clean_key,
                                row_id=str(item_id),
                                check_mode="invalid",
                                invalid_reason=str(exc),
                            )
                        )
                    else:
                        inv.references.append(
                            ObjectReference(
                                consumer="org_library_items",
                                bucket_role="main",
                                object_key=clean_key,
                                row_id=str(item_id),
                                expected_sha256=expected_hex,
                                check_mode="strict",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.org_library_items")

            # 10. Widgets (turn_slices, checkpoint_blobs, checkpoint_writes)
            if await _table_exists(cur, "public.turn_slices"):
                await cur.execute(
                    "SELECT conversation_response_id, lines FROM turn_slices "
                    "WHERE lines IS NOT NULL AND position('\"data_ref\"' in lines) > 0"
                )
                for row in await cur.fetchall():
                    rid, lines_txt = _extract_tuple(row, 2)
                    for ref in _extract_widget_data_refs_from_turn_slice(lines_txt):
                        inv.references.append(
                            ObjectReference(
                                consumer="widgets_turn_slices",
                                bucket_role="main",
                                object_key=str(ref["key"]).strip().lstrip("/"),
                                row_id=str(rid),
                                expected_size=(
                                    int(ref["size"]) if ref.get("size") is not None else None
                                ),
                                expected_sha256=(
                                    str(ref["sha256"]).strip().lower()
                                    if ref.get("sha256")
                                    else None
                                ),
                                check_mode="strict",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.turn_slices")

            for cp_table, pk_expr in (
                ("checkpoint_blobs", "thread_id || ':' || channel || ':' || version"),
                (
                    "checkpoint_writes",
                    "thread_id || ':' || checkpoint_id || ':' || task_id || ':' || idx",
                ),
            ):
                if await _table_exists(cur, f"public.{cp_table}"):
                    await cur.execute(
                        f"SELECT {pk_expr} AS row_id, channel, type, blob FROM {cp_table} "
                        "WHERE blob IS NOT NULL AND ("
                        "(channel = 'messages' AND ("
                        "position(convert_to('data_ref', 'UTF8') in blob) > 0 "
                        "OR position(convert_to('captured_images', 'UTF8') in blob) > 0"
                        ")) OR ("
                        "channel = 'ui' AND position(convert_to('image_capture', 'UTF8') in blob) > 0"
                        "))"
                    )
                    ser = serde if serde is not None else _get_checkpoint_serde()
                    for row in await cur.fetchall():
                        row_id, ch, btype, blob = _extract_tuple(row, 4)
                        if not btype or btype == "empty" or blob is None:
                            continue
                        raw_bytes = bytes(blob)
                        try:
                            value = ser.loads_typed((btype, raw_bytes))
                        except Exception:
                            continue
                        if ch == "messages" and b"data_ref" in raw_bytes:
                            for ref in _extract_widget_data_refs_from_checkpoint(value):
                                inv.references.append(
                                    ObjectReference(
                                        consumer=f"widgets_{cp_table}",
                                        bucket_role="main",
                                        object_key=str(ref["key"]).strip().lstrip("/"),
                                        row_id=str(row_id),
                                        expected_size=(
                                            int(ref["size"])
                                            if ref.get("size") is not None
                                            else None
                                        ),
                                        expected_sha256=(
                                            str(ref["sha256"]).strip().lower()
                                            if ref.get("sha256")
                                            else None
                                        ),
                                        check_mode="strict",
                                    )
                                )
                        if (ch == "messages" and b"captured_images" in raw_bytes) or (
                            ch == "ui" and b"image_capture" in raw_bytes
                        ):
                            for img_key in _extract_response_image_keys_from_checkpoint(
                                value, channel=ch, env=env
                            ):
                                inv.references.append(
                                    ObjectReference(
                                        consumer=f"response_images_{cp_table}",
                                        bucket_role="main",
                                        object_key=img_key,
                                        row_id=str(row_id),
                                        check_mode="exists_only",
                                    )
                                )
                else:
                    inv.skipped_missing_tables.append(f"public.{cp_table}")

            # 11. Users avatars (recorded object key or site-shaped avatar_url -> exists_only)
            if await _table_exists(cur, "public.users"):
                key_col = await _detect_avatar_object_key_column(cur)
                if key_col is not None:
                    await cur.execute(
                        f"SELECT user_id, {key_col}, avatar_url FROM users "
                        f"WHERE ({key_col} IS NOT NULL AND {key_col} <> '') "
                        "OR (avatar_url IS NOT NULL AND avatar_url <> '')"
                    )
                else:
                    await cur.execute(
                        "SELECT user_id, NULL::text, avatar_url FROM users "
                        "WHERE avatar_url IS NOT NULL AND avatar_url <> ''"
                    )
                for row in await cur.fetchall():
                    uid, recorded_key, avatar_url = _extract_tuple(row, 3)
                    obj_key = (
                        str(recorded_key).strip().lstrip("/")
                        if isinstance(recorded_key, str) and recorded_key.strip()
                        else extract_avatar_object_key(avatar_url, env=env)
                    )
                    if obj_key:
                        inv.references.append(
                            ObjectReference(
                                consumer="users_avatars",
                                bucket_role="main",
                                object_key=obj_key,
                                row_id=str(uid),
                                check_mode="exists_only",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.users")

            # 12. Response images in conversation_responses & subagent_runs
            if await _table_exists(cur, "public.conversation_responses"):
                await cur.execute(
                    "SELECT conversation_response_id, replay_facts FROM conversation_responses "
                    "WHERE replay_facts IS NOT NULL AND ("
                    "(replay_facts ? 'images' AND position('response-images/' in (replay_facts -> 'images')::text) > 0) "
                    "OR (replay_facts ? 'legacy' AND position('response-images/' in (replay_facts -> 'legacy')::text) > 0)"
                    ")"
                )
                for row in await cur.fetchall():
                    rid, rfacts = _extract_tuple(row, 2)
                    for img_key in _extract_response_image_keys_from_replay_facts(
                        rfacts, include_legacy=True, env=env
                    ):
                        inv.references.append(
                            ObjectReference(
                                consumer="response_images_turns",
                                bucket_role="main",
                                object_key=img_key,
                                row_id=str(rid),
                                check_mode="exists_only",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.conversation_responses")

            if await _table_exists(cur, "public.subagent_runs"):
                await cur.execute(
                    "SELECT task_run_id, replay_facts FROM subagent_runs "
                    "WHERE replay_facts IS NOT NULL AND replay_facts ? 'images' "
                    "AND position('response-images/' in (replay_facts -> 'images')::text) > 0"
                )
                for row in await cur.fetchall():
                    rid, rfacts = _extract_tuple(row, 2)
                    for img_key in _extract_response_image_keys_from_replay_facts(
                        rfacts, include_legacy=False, env=env
                    ):
                        inv.references.append(
                            ObjectReference(
                                consumer="response_images_subagents",
                                bucket_role="main",
                                object_key=img_key,
                                row_id=str(rid),
                                check_mode="exists_only",
                            )
                        )
            else:
                inv.skipped_missing_tables.append("public.subagent_runs")

            # 13. chat_attachments (only cleaned_at IS NULL AND state = 'ready' AND object_key IS NOT NULL)
            if await _table_exists(cur, "public.chat_attachments"):
                await cur.execute(
                    "SELECT attachment_id, state, object_key, size "
                    "FROM chat_attachments WHERE cleaned_at IS NULL"
                )
                pending_att = 0
                for row in await cur.fetchall():
                    aid, st, okey, sz = _extract_tuple(row, 4)
                    st_s = str(st or "")
                    if st_s == "uploading":
                        pending_att += 1
                        continue
                    if st_s != "ready":
                        continue
                    if not isinstance(okey, str) or not okey.strip():
                        # Pre-upgrade inline attachment (object_key IS NULL): exempt!
                        continue
                    inv.references.append(
                        ObjectReference(
                            consumer="chat_attachments",
                            bucket_role="main",
                            object_key=okey.strip().lstrip("/"),
                            row_id=str(aid),
                            expected_size=int(sz) if sz is not None else None,
                            check_mode="strict",
                        )
                    )
                if pending_att:
                    inv.skipped_pending["chat_attachments"] = pending_att
            else:
                inv.skipped_missing_tables.append("public.chat_attachments")

            # 14. conversation_queries attachment history (attachments/* -> exists_only)
            if await _table_exists(cur, "public.conversation_queries"):
                await cur.execute(
                    "SELECT conversation_query_id, metadata -> 'attachments' "
                    "FROM conversation_queries "
                    "WHERE metadata IS NOT NULL AND metadata ? 'attachments' "
                    "AND jsonb_typeof(metadata -> 'attachments') = 'array'"
                )
                for row in await cur.fetchall():
                    qid, atts = _extract_tuple(row, 2)
                    if isinstance(atts, str):
                        try:
                            atts = json.loads(atts)
                        except (ValueError, TypeError):
                            atts = []
                    if not isinstance(atts, list):
                        continue
                    for item in atts:
                        if not isinstance(item, dict):
                            continue
                        obj_key = extract_attachment_object_key(
                            item.get("object_key") or item.get("url"), env=env
                        )
                        if obj_key:
                            inv.references.append(
                                ObjectReference(
                                    consumer="chat_attachment_history",
                                    bucket_role="main",
                                    object_key=obj_key,
                                    row_id=str(qid),
                                    check_mode="exists_only",
                                )
                            )
            else:
                inv.skipped_missing_tables.append("public.conversation_queries")

        if include_aux:
            # 15. dalton_chat_file_uploads (aux / chat_upload bucket)
            if await _table_exists(cur, "public.dalton_chat_file_uploads"):
                await cur.execute(
                    "SELECT upload_id, state, object_key, size_bytes, actual_sha256 "
                    "FROM dalton_chat_file_uploads WHERE cleaned_at IS NULL"
                )
                pending_chat = 0
                for row in await cur.fetchall():
                    uid, st, okey, sz, sha = _extract_tuple(row, 5)
                    st_s = str(st or "")
                    if st_s in ("uploading", "completing"):
                        pending_chat += 1
                        continue
                    if st_s != "ready":
                        continue
                    inv.references.append(
                        ObjectReference(
                            consumer="dalton_chat_file_uploads",
                            bucket_role="chat_upload",
                            object_key=str(okey or "").strip().lstrip("/"),
                            row_id=str(uid),
                            expected_size=int(sz) if sz is not None else None,
                            expected_sha256=(
                                str(sha).strip().lower() if isinstance(sha, str) and sha.strip() else None
                            ),
                            check_mode="strict",
                        )
                    )
                if pending_chat:
                    inv.skipped_pending["dalton_chat_file_uploads"] = pending_chat
            else:
                inv.skipped_missing_tables.append("public.dalton_chat_file_uploads")

            # 16. dalton_transcription_jobs (aux / asr bucket + terminal job IDs)
            if await _table_exists(cur, "public.dalton_transcription_jobs"):
                await cur.execute(
                    "SELECT job_id, status, source_object_key, source_size_bytes, "
                    "source_sha256, usage, created_at, completed_at, updated_at "
                    "FROM dalton_transcription_jobs "
                    "ORDER BY created_at ASC, job_id ASC"
                )
                pending_asr = 0
                expired_asr = 0
                for row in await cur.fetchall():
                    if isinstance(row, Mapping):
                        jid = row.get("job_id")
                        st = row.get("status")
                        okey = row.get("source_object_key")
                        sz = row.get("source_size_bytes")
                        sha = row.get("source_sha256")
                        usage = row.get("usage")
                        completed_at = row.get("completed_at")
                    else:
                        raw_vals = tuple(row)
                        padded = raw_vals + (None,) * max(0, 9 - len(raw_vals))
                        (
                            jid,
                            st,
                            okey,
                            sz,
                            sha,
                            usage,
                            _created_at,
                            completed_at,
                            _updated_at,
                        ) = padded[:9]
                    jid_s = str(jid)
                    st_s = str(st or "").strip().lower()
                    if isinstance(usage, str):
                        try:
                            usage = json.loads(usage)
                        except (ValueError, TypeError):
                            usage = {}
                    upload_meta = (
                        usage.get("upload")
                        if isinstance(usage, dict) and isinstance(usage.get("upload"), dict)
                        else {}
                    )
                    upload_completed = upload_meta.get("completed") is True
                    upload_completed_at = (
                        upload_meta.get("completed_at")
                        or upload_meta.get("uploaded_at")
                    )
                    submit_attempted = (
                        isinstance(usage, dict) and usage.get("submit_attempted") is True
                    )
                    clean_okey = str(okey or "").strip().lstrip("/")

                    if st_s in TERMINAL_ASR_ARCHIVE_STATUSES:
                        inv.terminal_asr_job_ids.append(jid_s)
                    if st_s == "succeeded":
                        inv.succeeded_asr_job_ids.append(jid_s)

                    if st_s == "awaiting_upload" and not (upload_completed or submit_attempted):
                        pending_asr += 1
                        continue
                    if skip_expired_asr and is_terminal_asr_source_audio_expired(
                        status=st_s,
                        source_object_key=clean_okey,
                        completed_at=completed_at,
                        upload_completed_at=upload_completed_at,
                        upload_completed=upload_completed,
                        bucket_role=effective_asr_bucket_role,
                        key_prefix=asr_prefix,
                        now=effective_now,
                        expire_days=TRANSCRIPTION_AUDIO_EXPIRE_DAYS,
                    ):
                        expired_asr += 1
                        inv.expired_by_rule_objects.append(
                            ExpiredByRuleObject(
                                bucket=asr_bucket,
                                key=_apply_bucket_prefix(clean_okey, asr_prefix),
                                consumer="dalton_transcription_jobs",
                                rule_id=RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE,
                                row_id=jid_s,
                            )
                        )
                        continue
                    must_exist_strict = (
                        st_s in INFLIGHT_ASR_STATUSES
                        or (
                            st_s == "awaiting_upload"
                            and (upload_completed or submit_attempted)
                        )
                    )
                    if must_exist_strict or st_s in TERMINAL_ASR_RESULT_STATUSES:
                        inv.references.append(
                            ObjectReference(
                                consumer="dalton_transcription_jobs",
                                bucket_role="asr",
                                object_key=clean_okey,
                                row_id=jid_s,
                                expected_size=int(sz) if sz is not None else None,
                                expected_sha256=(
                                    str(sha).strip().lower()
                                    if isinstance(sha, str) and sha.strip()
                                    else None
                                ),
                                check_mode="strict",
                                require_present=must_exist_strict,
                            )
                        )
                if pending_asr:
                    inv.skipped_pending["dalton_transcription_jobs"] = pending_asr
                if expired_asr:
                    inv.skipped_expired["dalton_transcription_jobs"] = expired_asr
            else:
                inv.skipped_missing_tables.append("public.dalton_transcription_jobs")

            # 17. Channel chart snapshots (conversation_responses.metadata->'chart_snapshots')
            if await _table_exists(cur, "public.conversation_responses"):
                await cur.execute(
                    "SELECT conversation_response_id, metadata -> 'chart_snapshots' "
                    "FROM conversation_responses "
                    "WHERE metadata IS NOT NULL AND metadata ? 'chart_snapshots' "
                    "AND jsonb_typeof(metadata -> 'chart_snapshots') = 'array'"
                )
                pending_snaps = 0
                for row in await cur.fetchall():
                    rid, snaps = _extract_tuple(row, 2)
                    if isinstance(snaps, str):
                        try:
                            snaps = json.loads(snaps)
                        except (ValueError, TypeError):
                            snaps = []
                    if not isinstance(snaps, list):
                        continue
                    for item in snaps:
                        if not isinstance(item, dict):
                            continue
                        st_s = str(item.get("status") or "").strip().lower()
                        if st_s == "pending":
                            pending_snaps += 1
                            continue
                        if st_s != "ready":
                            continue
                        skey = str(item.get("snapshot_key") or "").strip().lstrip("/")
                        if skey:
                            inv.references.append(
                                ObjectReference(
                                    consumer="channel_snapshots",
                                    bucket_role="chat_upload",
                                    object_key=skey,
                                    row_id=f"{rid}:{item.get('layer_id', '')}",
                                    check_mode="exists_only",
                                )
                            )
                if pending_snaps:
                    inv.skipped_pending["channel_snapshots"] = pending_snaps

    return inv


def _index_manifest_objects_with_duplicates(
    manifest_objects: Mapping[tuple[str, str], Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], list[tuple[str, str]]]:
    if isinstance(manifest_objects, Mapping):
        return (
            {
                (str(k[0]), str(k[1]).lstrip("/")): v
                for k, v in manifest_objects.items()
            },
            [],
        )
    indexed: dict[tuple[str, str], Mapping[str, Any]] = {}
    duplicates: list[tuple[str, str]] = []
    for entry in manifest_objects:
        if not isinstance(entry, Mapping):
            continue
        bucket = str(entry.get("bucket") or "")
        key = str(entry.get("key") or "").lstrip("/")
        if bucket and key:
            pair = (bucket, key)
            if pair in indexed:
                duplicates.append(pair)
                continue
            indexed[pair] = entry
    return indexed, duplicates


def _index_manifest_objects(
    manifest_objects: Mapping[tuple[str, str], Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    indexed, _dups = _index_manifest_objects_with_duplicates(manifest_objects)
    return indexed


def _read_asr_archive_json(
    archive_members: Mapping[str, bytes] | None,
    asr_dir: Path | None,
    rel_path: str,
) -> tuple[bool, Any | None, str | None]:
    """Return ``(exists, parsed_json_or_none, error_detail)`` for a path inside ASR jobs archive or dir."""
    norm = rel_path.lstrip("./")
    raw: bytes | None = None
    if archive_members is not None:
        if norm not in archive_members:
            return False, None, f"missing {norm} in asr-jobs archive"
        raw = archive_members[norm]
    elif asr_dir is not None:
        fpath = asr_dir / norm
        if not fpath.is_file():
            return False, None, f"missing {norm} in {asr_dir}"
        try:
            raw = fpath.read_bytes()
        except OSError as exc:
            return False, None, f"unreadable {norm}: {exc}"
    else:
        return False, None, "neither asr_jobs_archive nor asr_jobs_dir was provided"

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return True, None, f"invalid JSON in {norm}: {exc}"
    return True, parsed, None


def _load_tar_members_bytes(tar_path: Path) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            norm = member.name.lstrip("./")
            fh = tf.extractfile(member)
            if fh is not None:
                members[norm] = fh.read()
    return members


def _index_known_missing_objects(
    known_missing_objects: Sequence[Any] | None,
) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    if not known_missing_objects:
        return out
    for item in known_missing_objects:
        bucket = ""
        key = ""
        if isinstance(item, Mapping):
            bucket = str(item.get("bucket") or "").strip()
            key = str(item.get("key") or "").strip().lstrip("/")
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            bucket = str(item[0] or "").strip()
            key = str(item[1] or "").strip().lstrip("/")
        elif hasattr(item, "bucket") and hasattr(item, "key"):
            bucket = str(getattr(item, "bucket") or "").strip()
            key = str(getattr(item, "key") or "").strip().lstrip("/")
        if bucket and key:
            out.add((bucket, key))
    return out


def _query_target_bucket_objects(
    s3_client: Any,
    bucket: str,
    prefixes: Sequence[str] = ("",),
) -> dict[str, int | None]:
    """List current objects in ``bucket`` via ``list_objects_v2`` and return ``key -> size``."""
    norm_prefixes = [str(p or "").strip().strip("/") for p in prefixes]
    query_prefixes = [""] if (not norm_prefixes or "" in norm_prefixes) else sorted(set(norm_prefixes))
    present: dict[str, int | None] = {}
    for pref in query_prefixes:
        kwargs: dict[str, Any] = {"Bucket": bucket}
        if pref:
            kwargs["Prefix"] = f"{pref}/"
        while True:
            resp = s3_client.list_objects_v2(**kwargs) or {}
            for item in resp.get("Contents") or []:
                if not isinstance(item, Mapping):
                    continue
                k = str(item.get("Key") or "").strip().lstrip("/")
                if not k:
                    continue
                sz = item.get("Size")
                present[k] = int(sz) if sz is not None else None
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
            if not token:
                break
            kwargs["ContinuationToken"] = token
    return present


def _build_target_s3_clients_for_reconcile(
    *,
    targets: Mapping[str, tuple[str, str]],
    manifest_objects: Sequence[Mapping[str, Any]],
    env: Mapping[str, str] | None = None,
    rewrite_container_minio: bool = False,
) -> dict[str, Any]:
    """Resolve S3 clients by bucket name for querying restore target buckets."""
    from src.dalton.consistent_backup import resolve_runtime_bucket_specs

    specs = resolve_runtime_bucket_specs(
        env=env,
        apply_core_defaults=False,
        rewrite_container_minio=rewrite_container_minio,
    )
    clients_by_bucket: dict[str, Any] = {}
    clients_by_role: dict[str, Any] = {}
    for spec in specs:
        if spec.bucket and spec.s3_client is not None:
            clients_by_bucket.setdefault(spec.bucket, spec.s3_client)
        for role in spec.roles:
            if spec.s3_client is not None:
                clients_by_role.setdefault(role, spec.s3_client)

    for role, (bucket_name, _pref) in targets.items():
        clean_b = str(bucket_name or "").strip()
        if not clean_b or clean_b in clients_by_bucket:
            continue
        if role in clients_by_role:
            clients_by_bucket[clean_b] = clients_by_role[role]
        elif role == "chat_upload" and "asr" in clients_by_role:
            clients_by_bucket[clean_b] = clients_by_role["asr"]
        elif role == "asr" and "chat_upload" in clients_by_role:
            clients_by_bucket[clean_b] = clients_by_role["chat_upload"]
        elif len(clients_by_bucket) == 1:
            clients_by_bucket[clean_b] = next(iter(clients_by_bucket.values()))

    for entry in manifest_objects:
        if not isinstance(entry, Mapping):
            continue
        mb = str(entry.get("bucket") or "").strip()
        if mb and mb not in clients_by_bucket and len(clients_by_bucket) == 1:
            clients_by_bucket[mb] = next(iter(clients_by_bucket.values()))

    return clients_by_bucket


def _validate_and_extract_known_missing(
    *,
    manifest_path: Path,
    manifest_data: Mapping[str, Any],
    backup_set_path: Path | None = None,
) -> list[dict[str, str]]:
    """Return actual known-missing objects strictly from this backup set's manifest.

    Validates that ``backup_set_path`` (when provided) matches ``manifest_path``
    (same ``backup_set_id``, ``snapshot_id``, ``manifest_sha256``, and no extra
    foreign ``missing_referenced_objects``) and excludes any object actually
    present in ``manifest_data['objects']``.
    """
    raw_manifest_objects = manifest_data.get("objects")
    manifest_present_pairs: set[tuple[str, str]] = set()
    if isinstance(raw_manifest_objects, list):
        for entry in raw_manifest_objects:
            if not isinstance(entry, Mapping):
                continue
            b = str(entry.get("bucket") or "").strip()
            k = str(entry.get("key") or "").strip().lstrip("/")
            if b and k:
                manifest_present_pairs.add((b, k))

    raw_manifest_km = manifest_data.get("missing_referenced_objects")
    manifest_km_list: list[dict[str, str]] = []
    manifest_km_pairs: set[tuple[str, str]] = set()
    if isinstance(raw_manifest_km, list):
        for item in raw_manifest_km:
            if not isinstance(item, Mapping):
                continue
            b = str(item.get("bucket") or "").strip()
            k = str(item.get("key") or "").strip().lstrip("/")
            c = str(item.get("consumer") or "").strip()
            if not b or not k:
                continue
            if (b, k) in manifest_present_pairs:
                # Present in manifest -> NOT an actual missing object from this backup set
                continue
            if (b, k) not in manifest_km_pairs:
                manifest_km_pairs.add((b, k))
                manifest_km_list.append({"bucket": b, "key": k, "consumer": c})

    if backup_set_path is not None:
        set_data = json.loads(backup_set_path.read_text(encoding="utf-8"))
        if not isinstance(set_data, dict):
            raise ValueError("backup-set JSON must be an object")

        expected_sha = str(set_data.get("manifest_sha256") or "").strip().lower()
        if expected_sha:
            actual_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest().lower()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"backup-set manifest_sha256 mismatch: expected {expected_sha}, got {actual_sha}"
                )

        set_id = str(set_data.get("backup_set_id") or "").strip()
        mf_id = str(manifest_data.get("backup_set_id") or "").strip()
        if set_id and mf_id and set_id != mf_id:
            raise ValueError(
                f"backup-set backup_set_id={set_id!r} does not match manifest backup_set_id={mf_id!r}"
            )

        set_snap = str(set_data.get("snapshot_id") or "").strip()
        mf_snap = str(manifest_data.get("snapshot_id") or "").strip()
        if set_snap and mf_snap and set_snap != mf_snap:
            raise ValueError(
                f"backup-set snapshot_id={set_snap!r} does not match manifest snapshot_id={mf_snap!r}"
            )

        raw_set_km = set_data.get("missing_referenced_objects")
        if isinstance(raw_set_km, list):
            set_km_pairs = _index_known_missing_objects(raw_set_km)
            extra_in_set = sorted(set_km_pairs - manifest_km_pairs)
            if extra_in_set:
                sample = ", ".join(f"{b}/{k}" for b, k in extra_in_set[:5])
                raise ValueError(
                    "backup-set missing_referenced_objects contains entries not registered as "
                    f"actual missing in manifest: {sample}"
                )

    return manifest_km_list


def _format_s3_exception_summary(exc: BaseException, *, operation: str = "") -> str:
    """Format an S3/Botocore exception using safe metadata only (never raw SDK text or body)."""
    parts: list[str] = []
    if operation:
        parts.append(f"op={operation}")
    parts.append(f"type={type(exc).__name__}")
    resp = getattr(exc, "response", None)
    if isinstance(resp, Mapping):
        err = resp.get("Error")
        if isinstance(err, Mapping):
            code = str(err.get("Code") or "").strip()
            if code:
                parts.append(f"code={code}")
        meta = resp.get("ResponseMetadata")
        if isinstance(meta, Mapping):
            http_status = meta.get("HTTPStatusCode")
            if http_status is not None and str(http_status).strip():
                parts.append(f"http_status={http_status}")
            req_id = str(meta.get("RequestId") or "").strip()
            if req_id:
                parts.append(f"request_id={req_id}")
    return ", ".join(parts)


def _is_sdk_exception(exc: BaseException) -> bool:
    mod = type(exc).__module__ or ""
    if mod.startswith(("botocore", "boto3")):
        return True
    return isinstance(getattr(exc, "response", None), Mapping)


def _index_expired_by_rule_objects(
    expired_by_rule_objects: Sequence[Any] | None,
) -> dict[tuple[str, str], list[tuple[str, str, str]]]:
    """Index expired-by-rule entries by ``(bucket, key) -> [(consumer, row_id, rule_id), ...]``."""
    out: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    if not expired_by_rule_objects:
        return out
    for item in expired_by_rule_objects:
        bucket = ""
        key = ""
        consumer = ""
        row_id = ""
        rule_id = RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE
        if isinstance(item, Mapping):
            bucket = str(item.get("bucket") or "").strip()
            key = str(item.get("key") or "").strip().lstrip("/")
            consumer = str(item.get("consumer") or "").strip()
            row_id = str(item.get("row_id") or "").strip()
            rule_id = str(item.get("rule_id") or RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE).strip()
        elif hasattr(item, "bucket") and hasattr(item, "key"):
            bucket = str(getattr(item, "bucket") or "").strip()
            key = str(getattr(item, "key") or "").strip().lstrip("/")
            consumer = str(getattr(item, "consumer", "") or "").strip()
            row_id = str(getattr(item, "row_id", "") or "").strip()
            rule_id = str(
                getattr(item, "rule_id", RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE)
                or RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE
            ).strip()
        if bucket and key:
            out.setdefault((bucket, key), []).append((consumer, row_id, rule_id))
    return out


def _validate_and_extract_expired_by_rule(
    *,
    manifest_path: Path,
    manifest_data: Mapping[str, Any],
    backup_set_path: Path | None = None,
) -> list[dict[str, str]]:
    """Return expired-by-rule objects strictly from this backup set's manifest.

    Excludes any object actually present in ``manifest_data['objects']`` and verifies
    that ``backup_set_path`` (when provided) does not inject extra foreign entries.
    """
    raw_manifest_objects = manifest_data.get("objects")
    manifest_present_pairs: set[tuple[str, str]] = set()
    if isinstance(raw_manifest_objects, list):
        for entry in raw_manifest_objects:
            if not isinstance(entry, Mapping):
                continue
            b = str(entry.get("bucket") or "").strip()
            k = str(entry.get("key") or "").strip().lstrip("/")
            if b and k:
                manifest_present_pairs.add((b, k))

    raw_manifest_exp = manifest_data.get("expired_by_rule_objects")
    manifest_exp_list: list[dict[str, str]] = []
    manifest_exp_seen: set[tuple[str, str, str, str]] = set()
    manifest_exp_pairs: set[tuple[str, str]] = set()
    if isinstance(raw_manifest_exp, list):
        for item in raw_manifest_exp:
            if not isinstance(item, Mapping):
                continue
            b = str(item.get("bucket") or "").strip()
            k = str(item.get("key") or "").strip().lstrip("/")
            c = str(item.get("consumer") or "").strip()
            rid = str(item.get("row_id") or "").strip()
            rule_id = str(item.get("rule_id") or RULE_ID_TRANSCRIPTION_AUDIO_EXPIRE).strip()
            if not b or not k:
                continue
            if (b, k) in manifest_present_pairs:
                continue
            dedup = (b, k, c, rid)
            if dedup not in manifest_exp_seen:
                manifest_exp_seen.add(dedup)
                manifest_exp_pairs.add((b, k))
                rec = {"bucket": b, "key": k, "consumer": c, "rule_id": rule_id}
                if rid:
                    rec["row_id"] = rid
                manifest_exp_list.append(rec)

    if backup_set_path is not None:
        set_data = json.loads(backup_set_path.read_text(encoding="utf-8"))
        if not isinstance(set_data, dict):
            raise ValueError("backup-set JSON must be an object")
        raw_set_exp = set_data.get("expired_by_rule_objects")
        if isinstance(raw_set_exp, list):
            set_exp_pairs = _index_known_missing_objects(raw_set_exp)
            extra_in_set = sorted(set_exp_pairs - manifest_exp_pairs)
            if extra_in_set:
                sample = ", ".join(f"{b}/{k}" for b, k in extra_in_set[:5])
                raise ValueError(
                    "backup-set expired_by_rule_objects contains entries not registered in "
                    f"manifest: {sample}"
                )

    return manifest_exp_list


def reconcile_inventory_against_manifest(
    inventory: SnapshotRefInventory,
    manifest_objects: Mapping[tuple[str, str], Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    *,
    bucket_targets: Mapping[str, tuple[str, str]],
    asr_jobs_archive: Path | None = None,
    asr_jobs_dir: Path | None = None,
    source_asr_jobs_dir: Path | None = None,
    require_raw_response_job_ids: set[str] | frozenset[str] | None = None,
    known_missing_objects: Sequence[Any] | None = None,
    expired_by_rule_objects: Sequence[Any] | None = None,
    target_s3_clients: Mapping[str, Any] | None = None,
    target_bucket_objects: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    verify_target_buckets: bool = False,
    target_logger: logging.Logger | None = None,
) -> ReconcileReport:
    """Reconcile all snapshot consumer references against the archived object manifest & ASR jobs archive.

    Follows the §3.1 reconciliation matrix:
    - Strict consumers verify existence, size (when specified), and ``archived_sha256``.
    - Skill archives (``user_skills`` / ``org_library_items``) validate ``sha256:<hex>`` prefix.
    - ``provenance_result_bodies`` checks existence only; size mismatch logs WARNING and passes.
    - ``succeeded`` ASR jobs check ``<job_id>/state.json`` (status='succeeded'), ``result.json``,
      and ``raw_response.json`` whenever the job writes it.
    - Objects registered in ``known_missing_objects`` (already absent at backup snapshot time
      and NOT present in ``manifest_objects``) are recorded as ``known_missing_object``
      warnings rather than reconciliation errors.
    - When ``verify_target_buckets=True`` (or ``target_s3_clients`` / ``target_bucket_objects``
      is supplied), also queries the restore target bucket(s) and fails if ANY object present
      in ``manifest_objects`` is missing or size-mismatched in the target bucket.
    """
    log = target_logger or logger
    index, duplicate_pairs = _index_manifest_objects_with_duplicates(manifest_objects)
    # Only actual missing objects (not present in manifest_objects) can ever count as known missing
    known_missing_set = _index_known_missing_objects(known_missing_objects) - set(index.keys())
    expired_by_rule_index = {
        pair: entries
        for pair, entries in _index_expired_by_rule_objects(expired_by_rule_objects).items()
        if pair not in index
    }
    errors: list[ReconcileIssue] = []
    warnings: list[ReconcileIssue] = []
    known_missing_issues: list[ReconcileIssue] = []
    expired_by_rule_issues: list[ReconcileIssue] = []
    checked_refs = 0

    for dup_bucket, dup_key in duplicate_pairs:
        errors.append(
            ReconcileIssue(
                consumer="backup_manifest",
                code="duplicate_manifest_object",
                row_id=f"{dup_bucket}/{dup_key}",
                object_key=f"{dup_bucket}/{dup_key}",
                detail=f"Duplicate manifest entry for object {dup_bucket}/{dup_key}",
            )
        )

    check_target_buckets = bool(
        verify_target_buckets
        or target_s3_clients is not None
        or target_bucket_objects is not None
    )
    target_present: dict[tuple[str, str], int | None] = {}
    errored_target_buckets: set[str] = set()
    if check_target_buckets:
        if target_bucket_objects is not None:
            for (tb, tk), meta in target_bucket_objects.items():
                sz = meta.get("size") if isinstance(meta, Mapping) else None
                target_present[(str(tb).strip(), str(tk).strip().lstrip("/"))] = (
                    int(sz) if sz is not None else None
                )
        else:
            prefixes_by_bucket: dict[str, set[str]] = {}
            for mb, _mk in index:
                prefixes_by_bucket.setdefault(mb, set())
            for ref in inventory.references:
                t = bucket_targets.get(ref.bucket_role)
                if t and t[0]:
                    prefixes_by_bucket.setdefault(t[0], set()).add(t[1].strip().strip("/"))
            # Ensure prefixes cover every manifest key for each bucket; otherwise list full bucket.
            for mb, mk in index:
                prefs = prefixes_by_bucket.setdefault(mb, set())
                if not prefs or "" in prefs:
                    prefs.add("")
                elif not any(mk.startswith(f"{p}/") for p in prefs if p):
                    prefs.add("")

            clients_map = target_s3_clients or {}
            for tb in sorted(prefixes_by_bucket):
                client = clients_map.get(tb)
                if client is None:
                    errors.append(
                        ReconcileIssue(
                            consumer="restore_target_bucket",
                            code="missing_target_bucket_client",
                            row_id=tb,
                            object_key=tb,
                            detail=f"No S3 client configured to verify restore target bucket {tb!r}",
                        )
                    )
                    errored_target_buckets.add(tb)
                    continue
                try:
                    listed = _query_target_bucket_objects(
                        client,
                        tb,
                        prefixes=sorted(prefixes_by_bucket[tb]),
                    )
                except Exception as exc:
                    errors.append(
                        ReconcileIssue(
                            consumer="restore_target_bucket",
                            code="target_bucket_query_error",
                            row_id=tb,
                            object_key=tb,
                            detail=(
                                f"Failed querying restore target bucket {tb!r}: "
                                f"{_format_s3_exception_summary(exc, operation='ListObjectsV2')}"
                            ),
                        )
                    )
                    errored_target_buckets.add(tb)
                    continue
                for lk, lsz in listed.items():
                    target_present[(tb, lk)] = lsz

    checked_target_pairs: set[tuple[str, str]] = set()
    for ref in inventory.references:
        checked_refs += 1
        if ref.check_mode == "invalid":
            errors.append(
                ReconcileIssue(
                    consumer=ref.consumer,
                    code="invalid_reference",
                    row_id=ref.row_id,
                    object_key=ref.object_key,
                    detail=ref.invalid_reason or "invalid reference metadata",
                )
            )
            continue

        target = bucket_targets.get(ref.bucket_role)
        if target is None or not target[0]:
            errors.append(
                ReconcileIssue(
                    consumer=ref.consumer,
                    code="unconfigured_bucket_role",
                    row_id=ref.row_id,
                    object_key=ref.object_key,
                    detail=f"No bucket configured for role {ref.bucket_role!r}",
                )
            )
            continue

        bucket_name, key_prefix = target
        full_key = _apply_bucket_prefix(ref.object_key, key_prefix)
        entry = index.get((bucket_name, full_key))
        if entry is None:
            if (bucket_name, full_key) in known_missing_set:
                km_issue = ReconcileIssue(
                    consumer=ref.consumer,
                    code="known_missing_object",
                    row_id=ref.row_id,
                    object_key=f"{bucket_name}/{full_key}",
                    detail=(
                        "Referenced object was missing at backup snapshot time "
                        "(registered in manifest missing_referenced_objects)"
                    ),
                )
                warnings.append(km_issue)
                known_missing_issues.append(km_issue)
                continue
            if not ref.require_present and (bucket_name, full_key) in expired_by_rule_index:
                matched_rule_id = next(
                    (
                        exp_rule_id
                        for exp_consumer, exp_row_id, exp_rule_id in expired_by_rule_index[
                            (bucket_name, full_key)
                        ]
                        if (not exp_consumer or exp_consumer == ref.consumer)
                        and (not exp_row_id or exp_row_id == ref.row_id)
                    ),
                    None,
                )
                if matched_rule_id is not None:
                    expired_by_rule_issues.append(
                        ReconcileIssue(
                            consumer=ref.consumer,
                            code="expired_by_rule_object",
                            row_id=ref.row_id,
                            object_key=f"{bucket_name}/{full_key}",
                            detail=(
                                f"Referenced object was expired by managed rule {matched_rule_id!r} "
                                "at backup snapshot time (registered in manifest expired_by_rule_objects)"
                            ),
                        )
                    )
                    continue
            errors.append(
                ReconcileIssue(
                    consumer=ref.consumer,
                    code="missing_object",
                    row_id=ref.row_id,
                    object_key=f"{bucket_name}/{full_key}",
                    detail="Referenced object is missing from backup manifest",
                )
            )
            continue

        if check_target_buckets and bucket_name not in errored_target_buckets:
            checked_target_pairs.add((bucket_name, full_key))
            if (bucket_name, full_key) not in target_present:
                errors.append(
                    ReconcileIssue(
                        consumer=ref.consumer,
                        code="missing_in_target_bucket",
                        row_id=ref.row_id,
                        object_key=f"{bucket_name}/{full_key}",
                        detail=(
                            "Referenced object is present in backup manifest but missing "
                            "from restore target bucket"
                        ),
                    )
                )
            elif (
                ref.check_mode == "strict"
                and entry.get("size") is not None
                and target_present[(bucket_name, full_key)] is not None
                and int(target_present[(bucket_name, full_key)]) != int(entry["size"])
            ):
                errors.append(
                    ReconcileIssue(
                        consumer=ref.consumer,
                        code="target_size_mismatch",
                        row_id=ref.row_id,
                        object_key=f"{bucket_name}/{full_key}",
                        detail=(
                            "Restore target bucket object size mismatch "
                            f"(target={target_present[(bucket_name, full_key)]}, manifest={entry['size']})"
                        ),
                    )
                )

        if ref.check_mode == "exists_only":
            continue

        actual_size = entry.get("size")
        actual_sha = str(entry.get("archived_sha256") or entry.get("sha256") or "").strip().lower()

        if ref.check_mode == "exists_warn_size":
            if (
                ref.expected_size is not None
                and actual_size is not None
                and int(actual_size) != int(ref.expected_size)
            ):
                issue = ReconcileIssue(
                    consumer=ref.consumer,
                    code="mutable_size_drift",
                    row_id=ref.row_id,
                    object_key=f"{bucket_name}/{full_key}",
                    detail=(
                        f"Mutable object size differs from DB byte_len "
                        f"(manifest={actual_size}, db={ref.expected_size}); continuing"
                    ),
                )
                warnings.append(issue)
                log.warning(
                    "[storage_refs] WARNING %s row=%s key=%s: %s",
                    issue.consumer,
                    issue.row_id,
                    issue.object_key,
                    issue.detail,
                )
            continue

        # Strict mode
        if ref.expected_size is not None:
            if actual_size is None or int(actual_size) != int(ref.expected_size):
                errors.append(
                    ReconcileIssue(
                        consumer=ref.consumer,
                        code="size_mismatch",
                        row_id=ref.row_id,
                        object_key=f"{bucket_name}/{full_key}",
                        detail=f"Object size mismatch (manifest={actual_size}, expected={ref.expected_size})",
                    )
                )
        if ref.expected_sha256 is not None:
            if actual_sha != ref.expected_sha256.lower():
                errors.append(
                    ReconcileIssue(
                        consumer=ref.consumer,
                        code="sha256_mismatch",
                        row_id=ref.row_id,
                        object_key=f"{bucket_name}/{full_key}",
                        detail=(
                            f"Object SHA-256 mismatch "
                            f"(archived_sha256={actual_sha!r}, expected={ref.expected_sha256.lower()!r})"
                        ),
                    )
                )

    # When verifying restore target buckets, ALSO check any manifest objects that were not
    # directly covered by inventory.references (every object in the manifest MUST be restored).
    if check_target_buckets:
        for (mb, mk), entry in sorted(index.items()):
            if mb in errored_target_buckets or (mb, mk) in checked_target_pairs:
                continue
            if (mb, mk) not in target_present:
                errors.append(
                    ReconcileIssue(
                        consumer="restore_target_bucket",
                        code="missing_in_target_bucket",
                        row_id=f"{mb}/{mk}",
                        object_key=f"{mb}/{mk}",
                        detail="Object present in backup manifest is missing from restore target bucket",
                    )
                )
            elif (
                entry.get("size") is not None
                and target_present[(mb, mk)] is not None
                and int(target_present[(mb, mk)]) != int(entry["size"])
            ):
                errors.append(
                    ReconcileIssue(
                        consumer="restore_target_bucket",
                        code="target_size_mismatch",
                        row_id=f"{mb}/{mk}",
                        object_key=f"{mb}/{mk}",
                        detail=(
                            "Restore target bucket object size mismatch "
                            f"(target={target_present[(mb, mk)]}, manifest={entry['size']})"
                        ),
                    )
                )

    # Reconcile ASR jobs archive:
    # 1. Verify NO archived job directory contains a non-terminal status (e.g. indeterminate or active)
    # 2. Verify all succeeded ASR jobs have valid state.json, result.json, and raw_response.json
    checked_asr_jobs = 0
    archive_members: dict[str, bytes] | None = None
    if asr_jobs_archive is not None:
        if asr_jobs_archive.is_file():
            try:
                archive_members = _load_tar_members_bytes(asr_jobs_archive)
            except Exception as exc:
                errors.append(
                    ReconcileIssue(
                        consumer="dalton_transcription_jobs_archive",
                        code="corrupt_asr_archive",
                        row_id="*",
                        object_key=str(asr_jobs_archive),
                        detail=f"Failed reading asr-jobs archive: {exc}",
                    )
                )
        elif inventory.succeeded_asr_job_ids:
            errors.append(
                ReconcileIssue(
                    consumer="dalton_transcription_jobs_archive",
                    code="missing_asr_archive",
                    row_id="*",
                    object_key=str(asr_jobs_archive),
                    detail="Succeeded ASR jobs exist but asr-jobs archive is missing",
                )
            )

    if archive_members is not None:
        for member_name, raw_bytes in sorted(archive_members.items()):
            parts = member_name.split("/")
            if len(parts) == 2 and parts[1] == "state.json":
                arch_job_id = parts[0]
                try:
                    st_obj = json.loads(raw_bytes.decode("utf-8"))
                except Exception as exc:
                    errors.append(
                        ReconcileIssue(
                            consumer="dalton_transcription_jobs_archive",
                            code="invalid_archived_state_json",
                            row_id=arch_job_id,
                            object_key=member_name,
                            detail=f"Invalid JSON in archived state.json: {exc}",
                        )
                    )
                    continue
                arch_status = (
                    str(st_obj.get("status") or "").strip().lower()
                    if isinstance(st_obj, dict)
                    else ""
                )
                if arch_status not in TERMINAL_ASR_ARCHIVE_STATUSES:
                    errors.append(
                        ReconcileIssue(
                            consumer="dalton_transcription_jobs_archive",
                            code="non_terminal_archived_asr_job",
                            row_id=arch_job_id,
                            object_key=member_name,
                            detail=(
                                f"ASR archive must contain only terminal jobs "
                                f"{sorted(TERMINAL_ASR_ARCHIVE_STATUSES)}, got status={arch_status!r}"
                            ),
                        )
                    )

    if inventory.succeeded_asr_job_ids:
        if archive_members is not None or asr_jobs_dir is not None:
            req_raw_ids = set(require_raw_response_job_ids or ())
            for job_id in inventory.succeeded_asr_job_ids:
                checked_asr_jobs += 1
                ok_state, state_obj, state_err = _read_asr_archive_json(
                    archive_members, asr_jobs_dir, f"{job_id}/state.json"
                )
                if not ok_state or not isinstance(state_obj, dict):
                    errors.append(
                        ReconcileIssue(
                            consumer="dalton_transcription_jobs_archive",
                            code="missing_or_invalid_state_json",
                            row_id=job_id,
                            object_key=f"{job_id}/state.json",
                            detail=state_err or "state.json is not a JSON object",
                        )
                    )
                elif str(state_obj.get("status") or "").strip().lower() != "succeeded":
                    errors.append(
                        ReconcileIssue(
                            consumer="dalton_transcription_jobs_archive",
                            code="state_status_mismatch",
                            row_id=job_id,
                            object_key=f"{job_id}/state.json",
                            detail=(
                                f"Expected state.json status='succeeded', "
                                f"got {state_obj.get('status')!r}"
                            ),
                        )
                    )

                ok_res, res_obj, res_err = _read_asr_archive_json(
                    archive_members, asr_jobs_dir, f"{job_id}/result.json"
                )
                if not ok_res or not isinstance(res_obj, dict):
                    errors.append(
                        ReconcileIssue(
                            consumer="dalton_transcription_jobs_archive",
                            code="missing_or_invalid_result_json",
                            row_id=job_id,
                            object_key=f"{job_id}/result.json",
                            detail=res_err or "result.json is not a JSON object",
                        )
                    )
                    continue

                raw_filename = str(res_obj.get("raw_response_file") or "").strip()
                expect_raw = bool(
                    raw_filename
                    or job_id in req_raw_ids
                    or (
                        source_asr_jobs_dir is not None
                        and (source_asr_jobs_dir / job_id / "raw_response.json").is_file()
                    )
                )
                if expect_raw:
                    raw_rel = f"{job_id}/{raw_filename or 'raw_response.json'}"
                    ok_raw, raw_obj, raw_err = _read_asr_archive_json(
                        archive_members, asr_jobs_dir, raw_rel
                    )
                    if not ok_raw or not isinstance(raw_obj, dict):
                        errors.append(
                            ReconcileIssue(
                                consumer="dalton_transcription_jobs_archive",
                                code="missing_or_invalid_raw_response_json",
                                row_id=job_id,
                                object_key=raw_rel,
                                detail=raw_err or "raw_response.json is not a JSON object",
                            )
                        )
        elif asr_jobs_archive is None and asr_jobs_dir is None:
            errors.append(
                ReconcileIssue(
                    consumer="dalton_transcription_jobs_archive",
                    code="missing_asr_archive",
                    row_id="*",
                    object_key="asr-jobs",
                    detail="Succeeded ASR jobs exist but no asr-jobs archive or directory was provided",
                )
            )

    return ReconcileReport(
        ok=len(errors) == 0,
        checked_refs=checked_refs,
        checked_asr_jobs=checked_asr_jobs,
        errors=tuple(errors),
        warnings=tuple(warnings),
        skipped_pending=dict(inventory.skipped_pending),
        known_missing=tuple(known_missing_issues),
        expired_by_rule=tuple(expired_by_rule_issues),
    )


def resolve_bucket_targets_from_env(
    env: Mapping[str, str] | None = None,
) -> dict[str, tuple[str, str]]:
    """Resolve ``bucket_role -> (bucket_name, key_prefix)`` from environment variables."""
    from src.dalton.storage_config import resolve as resolve_storage_config

    em = os.environ if env is None else env
    cfg = resolve_storage_config(env=em)
    targets: dict[str, tuple[str, str]] = {}
    if cfg.is_storage_enabled and cfg.bucket_name:
        main_prefix = _normalize_storage_key_prefix(em).rstrip("/")
        targets["main"] = (cfg.bucket_name, main_prefix)

    asr_bucket = (em.get("DALTON_ASR_STORAGE_BUCKET_NAME") or "").strip()
    asr_prefix = (em.get("DALTON_ASR_STORAGE_KEY_PREFIX") or "").strip().strip("/")
    if asr_bucket:
        targets["asr"] = (asr_bucket, asr_prefix)

    chat_bucket = (em.get("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME") or "").strip()
    chat_prefix = (em.get("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX") or "").strip().strip("/")
    if chat_bucket:
        targets["chat_upload"] = (chat_bucket, chat_prefix)
    elif asr_bucket:
        targets["chat_upload"] = (asr_bucket, asr_prefix)
    return targets


# ---------------------------------------------------------------------------
# Operator CLI (`python -m src.dalton.storage_refs check --strict | reconcile`)
# ---------------------------------------------------------------------------


async def _open_cli_readonly_connection() -> Any:
    """Open a standalone read-only PostgreSQL session connection for CLI commands."""
    conn = await open_session_connection()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
    except Exception:
        await conn.close()
        raise
    return conn


async def _run_cli_check(
    *,
    strict: bool,
    batch_size: int,
    batch_timeout: str,
    stop_on_hit: bool,
    progress_stream: TextIO | None,
) -> int:
    conn: Any | None = None
    try:
        conn = await _open_cli_readonly_connection()
        if strict:
            report = await run_strict_main_bucket_check(
                conn=conn,
                batch_size=batch_size,
                batch_timeout=batch_timeout,
                stop_on_hit=stop_on_hit,
                progress_stream=progress_stream,
            )
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            return report.exit_code
        outcomes, total_ms = await probe_main_bucket_references(conn=conn)
        hits = [o.name for o in outcomes if o.has_references]
        errors = [o.name for o in outcomes if o.error]
        exit_code = (
            EXIT_CHECK_FAILED
            if errors
            else (EXIT_REFERENCES_FOUND if hits else EXIT_CLEAN)
        )
        payload = {
            "exit_code": exit_code,
            "total_elapsed_ms": round(total_ms, 2),
            "hits": hits,
            "errors": errors,
            "consumers": [o.to_dict() for o in outcomes],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return exit_code
    except KeyboardInterrupt:
        if progress_stream is not None:
            progress_stream.write("[storage_refs] interrupted; aborting check\n")
            progress_stream.flush()
        return EXIT_CHECK_FAILED
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        logger.error("storage_refs CLI failed: %s", err_msg)
        if progress_stream is not None:
            progress_stream.write(f"[storage_refs] ERROR: {err_msg}\n")
            progress_stream.flush()
        return EXIT_CHECK_FAILED
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.dalton.storage_refs",
        description=(
            "Read-only main-bucket consumer reference audit (does not print object keys). "
            "Run `check --strict` and verify exit code 0 before disabling the main bucket."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser(
        "check",
        help=(
            "Audit main-bucket consumer tables. Pass `--strict` for batched full-table "
            "structural verification (exit 0=clean, 1=references found, 2=check failed)."
        ),
    )
    check_p.add_argument(
        "--strict",
        action="store_true",
        help="Run batched full-table structural verification across all 17 consumers.",
    )
    check_p.add_argument(
        "--batch-size",
        type=int,
        default=STRICT_BATCH_SIZE,
        help=f"Rows per keyset batch during --strict scans (default: {STRICT_BATCH_SIZE}).",
    )
    check_p.add_argument(
        "--batch-timeout",
        default=STRICT_BATCH_TIMEOUT,
        help=f"PostgreSQL statement_timeout per batch (default: {STRICT_BATCH_TIMEOUT}).",
    )
    check_p.add_argument(
        "--no-fail-fast",
        action="store_true",
        help="Continue scanning remaining consumers after a confirmed hit.",
    )
    check_p.add_argument(
        "--quiet-progress",
        action="store_true",
        help="Suppress per-batch progress output on stderr.",
    )

    sub.add_parser(
        "probe",
        help="Run the fast indexed startup advisory probes (exit 0=clean, 1=hits, 2=error).",
    )

    reconcile_p = sub.add_parser(
        "reconcile",
        help=(
            "Reconcile snapshot consumer references (from --refs-json or live DB) against "
            "a backup object manifest JSON and optional asr-jobs archive/dir."
        ),
    )
    reconcile_p.add_argument(
        "--manifest",
        required=True,
        help="Path to object manifest JSON (containing 'objects' list with bucket, key, size, archived_sha256).",
    )
    reconcile_p.add_argument(
        "--refs-json",
        default="",
        help="Optional pre-exported SnapshotRefInventory JSON; if omitted, enumerates from DB.",
    )
    reconcile_p.add_argument(
        "--asr-archive",
        default="",
        help="Optional path to asr-jobs tar.gz archive for verifying succeeded ASR jobs.",
    )
    reconcile_p.add_argument(
        "--asr-dir",
        default="",
        help="Optional path to extracted asr_jobs directory for verifying succeeded ASR jobs.",
    )
    reconcile_p.add_argument(
        "--source-asr-dir",
        default="",
        help="Optional source asr_jobs directory to detect which jobs produced raw_response.json.",
    )
    reconcile_p.add_argument(
        "--main-bucket",
        default="",
        help="Override main bucket name (default: resolved from env).",
    )
    reconcile_p.add_argument(
        "--main-prefix",
        default="",
        help="Override main bucket key prefix.",
    )
    reconcile_p.add_argument(
        "--aux-bucket",
        default="",
        help="Override aux (asr/chat_upload) bucket name (default: resolved from env).",
    )
    reconcile_p.add_argument(
        "--aux-prefix",
        default="",
        help="Override aux bucket key prefix.",
    )
    reconcile_p.add_argument(
        "--backup-set",
        default="",
        help="Optional backup-set JSON file (validated against --manifest for the same backup set).",
    )
    reconcile_p.add_argument(
        "--verify-target-buckets",
        action="store_true",
        help="Query restore target S3/MinIO bucket(s) and fail on any manifest object missing there.",
    )
    reconcile_p.add_argument(
        "--rewrite-container-minio",
        action="store_true",
        help="Rewrite container-internal MinIO hostnames (minio/minio-s3) to host loopback when querying target buckets.",
    )

    args = parser.parse_args(argv)
    if args.command == "check":
        progress = None if args.quiet_progress else sys.stderr
        try:
            return asyncio.run(
                _run_cli_check(
                    strict=bool(args.strict),
                    batch_size=args.batch_size,
                    batch_timeout=args.batch_timeout,
                    stop_on_hit=not args.no_fail_fast,
                    progress_stream=progress,
                )
            )
        except KeyboardInterrupt:
            return EXIT_CHECK_FAILED
    if args.command == "probe":
        try:
            return asyncio.run(
                _run_cli_check(
                    strict=False,
                    batch_size=STRICT_BATCH_SIZE,
                    batch_timeout=STARTUP_STATEMENT_TIMEOUT,
                    stop_on_hit=False,
                    progress_stream=sys.stderr,
                )
            )
        except KeyboardInterrupt:
            return EXIT_CHECK_FAILED
    if args.command == "reconcile":
        try:
            from src.dalton.consistent_backup import (
                UNSUPPORTED_STORAGE_TOPOLOGY_MESSAGE,
                UnsupportedStorageTopologyError,
                validate_env_storage_topology,
            )

            validate_env_storage_topology(
                os.environ,
                rewrite_container_minio=bool(args.rewrite_container_minio),
            )

            manifest_path = Path(args.manifest)
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            objects = (
                manifest_data.get("objects")
                if isinstance(manifest_data, dict)
                else manifest_data
            )
            if not isinstance(objects, list):
                raise ValueError("manifest JSON must contain an 'objects' list")

            targets = resolve_bucket_targets_from_env()
            if args.main_bucket:
                targets["main"] = (args.main_bucket, args.main_prefix.strip("/"))
            if args.aux_bucket:
                targets["asr"] = (args.aux_bucket, args.aux_prefix.strip("/"))
                targets["chat_upload"] = (args.aux_bucket, args.aux_prefix.strip("/"))

            main_target_bucket = targets.get("main", ("", ""))[0].strip()
            if main_target_bucket:
                shared_aux = [
                    role
                    for role in ("asr", "chat_upload")
                    if targets.get(role, ("", ""))[0].strip() == main_target_bucket
                ]
                if shared_aux:
                    raise UnsupportedStorageTopologyError(
                        f"{UNSUPPORTED_STORAGE_TOPOLOGY_MESSAGE}：主桶与辅存储 ({', '.join(shared_aux)}) "
                        f"位于同一桶名 ({main_target_bucket})；仅支持单一 MinIO 端点且主桶与辅桶名称不同的布局"
                    )

            if args.refs_json:
                refs_data = json.loads(Path(args.refs_json).read_text(encoding="utf-8"))
                inventory = SnapshotRefInventory.from_dict(refs_data)
            else:
                async def _enum() -> SnapshotRefInventory:
                    conn = await _open_cli_readonly_connection()
                    try:
                        # Do NOT recompute 7-day transcription expiration against restore day;
                        # reconcile strictly against this backup set's recorded expired_by_rule_objects.
                        return await enumerate_snapshot_refs(conn, skip_expired_asr=False)
                    finally:
                        await conn.close()

                inventory = asyncio.run(_enum())

            known_missing_entries: list[dict[str, str]] = []
            expired_by_rule_entries: list[dict[str, str]] = []
            if isinstance(manifest_data, dict):
                known_missing_entries = _validate_and_extract_known_missing(
                    manifest_path=manifest_path,
                    manifest_data=manifest_data,
                    backup_set_path=Path(args.backup_set) if args.backup_set else None,
                )
                expired_by_rule_entries = _validate_and_extract_expired_by_rule(
                    manifest_path=manifest_path,
                    manifest_data=manifest_data,
                    backup_set_path=Path(args.backup_set) if args.backup_set else None,
                )
            elif args.backup_set:
                raise ValueError("--backup-set requires a dict manifest JSON from the same backup set")

            target_clients: dict[str, Any] | None = None
            if args.verify_target_buckets:
                target_clients = _build_target_s3_clients_for_reconcile(
                    targets=targets,
                    manifest_objects=objects,
                    env=os.environ,
                    rewrite_container_minio=bool(args.rewrite_container_minio),
                )

            report = reconcile_inventory_against_manifest(
                inventory,
                objects,
                bucket_targets=targets,
                asr_jobs_archive=Path(args.asr_archive) if args.asr_archive else None,
                asr_jobs_dir=Path(args.asr_dir) if args.asr_dir else None,
                source_asr_jobs_dir=Path(args.source_asr_dir) if args.source_asr_dir else None,
                known_missing_objects=known_missing_entries,
                expired_by_rule_objects=expired_by_rule_entries,
                target_s3_clients=target_clients,
                verify_target_buckets=bool(args.verify_target_buckets),
            )
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            return EXIT_CLEAN if report.ok else EXIT_REFERENCES_FOUND
        except Exception as exc:
            if _is_sdk_exception(exc):
                print(
                    f"[storage_refs] ERROR: {_format_s3_exception_summary(exc)}",
                    file=sys.stderr,
                )
            else:
                print(f"[storage_refs] ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
            return EXIT_CHECK_FAILED
    return EXIT_CHECK_FAILED


if __name__ == "__main__":
    sys.exit(main())
