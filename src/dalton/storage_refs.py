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
import json
import logging
import os
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TextIO
from urllib.parse import urlsplit

from src.server.database.pool import get_db_connection, open_session_connection
from src.utils.storage import is_storage_enabled

logger = logging.getLogger(__name__)

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


def site_response_image_url_rules(
    *, env: Mapping[str, str] | None = None
) -> list[tuple[str | None, str]]:
    """Return ``(origin_or_none, full_response_images_path_prefix)`` rules for this site/bucket.

    Covers every URL base that this deployment's main bucket or site proxy can emit for
    ``get_public_url("response-images/...")``:
    - ``STORAGE_PUBLIC_URL_BASE`` / ``S3_PUBLIC_URL_BASE`` (absolute or site-relative ``/...``)
    - ``PUBLIC_APP_URL`` + ``/storage``
    - Configured S3 bucket (AWS default virtual host + ``STORAGE_ENDPOINT_URL`` /
      ``S3_ENDPOINT_URL`` / ``STORAGE_BROWSER_ENDPOINT_URL`` in path and virtual-hosted styles)
    - Configured OSS bucket (``OSS_BUCKET_NAME`` + ``OSS_ENDPOINT``)
    """
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

    key_prefix = _normalize_storage_key_prefix(em)
    rules: list[tuple[str | None, str]] = []
    seen: set[tuple[str | None, str]] = set()
    for raw_base in candidates:
        if raw_base.startswith("/") and not raw_base.startswith("//"):
            base_path = raw_base.rstrip("/")
            full_prefix = f"{base_path}/{key_prefix}response-images/"
            pair: tuple[str | None, str] = (None, full_prefix)
            if pair not in seen:
                seen.add(pair)
                rules.append(pair)
            continue
        try:
            parsed = urlsplit(raw_base.rstrip("/"))
        except ValueError:
            continue
        origin = _origin_from_split(parsed)
        if origin is None:
            continue
        base_path = parsed.path.rstrip("/")
        full_prefix = f"{base_path}/{key_prefix}response-images/"
        pair = (origin, full_prefix)
        if pair not in seen:
            seen.add(pair)
            rules.append(pair)
    return rules


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
    for prefix in (f"/{key_prefix}response-images/", f"/storage/{key_prefix}response-images/"):
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
            "WHERE COALESCE(att ->> 'url', '') LIKE '%%/attachments/%%')"
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
# Operator CLI (`python -m src.dalton.storage_refs check --strict`)
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
    return EXIT_CHECK_FAILED


if __name__ == "__main__":
    sys.exit(main())
