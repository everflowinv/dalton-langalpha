"""Thin re-export wrapper around ``scripts/dalton/web_search_broker_protocol.py``.

The standalone stdlib implementation lives in ``scripts/dalton/web_search_broker_protocol.py``
so the host-side launchd bridge can import it directly without triggering any
``src.*`` package ``__init__.py`` imports.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_standalone_protocol() -> ModuleType:
    module_name = "web_search_broker_protocol"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing

    script_path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "dalton"
        / "web_search_broker_protocol.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_proto = _load_standalone_protocol()

PROTOCOL_VERSION = _proto.PROTOCOL_VERSION
TOOL_NAME = _proto.TOOL_NAME
DEFAULT_PROFILE_ID = _proto.DEFAULT_PROFILE_ID
DEFAULT_CLIENT_ID = _proto.DEFAULT_CLIENT_ID
DEFAULT_TIMEOUT_MS = _proto.DEFAULT_TIMEOUT_MS
DEFAULT_MAX_TIMEOUT_MS = _proto.DEFAULT_MAX_TIMEOUT_MS
DEFAULT_QUEUE_TIMEOUT_SECONDS = _proto.DEFAULT_QUEUE_TIMEOUT_SECONDS
DEFAULT_QUEUE_TIMEOUT_S = _proto.DEFAULT_QUEUE_TIMEOUT_S
HTTP_TIMEOUT_MARGIN_SECONDS = _proto.HTTP_TIMEOUT_MARGIN_SECONDS
DEFAULT_HTTP_TIMEOUT_SECONDS = _proto.DEFAULT_HTTP_TIMEOUT_SECONDS
DEFAULT_MAX_RESPONSE_BYTES = _proto.DEFAULT_MAX_RESPONSE_BYTES
WebSearchBrokerError = _proto.WebSearchBrokerError
canonical_json = _proto.canonical_json
authentication_payload = _proto.authentication_payload
compute_mac = _proto.compute_mac
compute_http_timeout_seconds = _proto.compute_http_timeout_seconds
sign_request = _proto.sign_request
load_broker_key = _proto.load_broker_key
new_call_ref = _proto.new_call_ref
build_core_request = _proto.build_core_request
exchange_unix_socket = _proto.exchange_unix_socket
invoke_broker_over_socket = _proto.invoke_broker_over_socket

__all__ = list(_proto.__all__)
