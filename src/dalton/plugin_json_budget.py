"""Bounded JSON parsing and serialization for untrusted plugin archives.

Enforces four budgets across the canonical (``plugin.json`` / ``mcp.json``),
vendor (``.<vendor>-plugin/plugin.json`` / ``.mcp.json`` / named MCP files),
and inline (``plugin.json`` ``mcpServers``) paths before and after ``json.loads``:
1. Raw input byte ceiling (``MAX_JSON_DOCUMENT_BYTES`` = 1 MiB) before decoding,
   and raw key byte ceiling (``MAX_JSON_KEY_BYTES`` = 4 KiB) before key decoding.
2. String- and escape-aware bracket nesting scan (``MAX_JSON_DEPTH`` = 64)
   before calling ``json.loads`` (Python 3.14 uses a heap stack in ``json.loads``
   and no longer raises ``RecursionError`` on 100k-deep arrays).
3. Post-parse iterative AST node cap (``MAX_JSON_NODES`` = 100,000).
4. Pre-format ``indent=2`` output byte budget check and post-format verification
   (``MAX_JSON_OUTPUT_BYTES`` = 1 MiB) so dialect adaptation preserves upstream
   ``content_hash`` while refusing documents whose formatted output exceeds 1 MiB.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

# 1 MiB raw byte ceiling: matches mcp.py's MAX_DOCUMENT_BYTES (1 MiB) and sits
# >16x above a normal manifest or MCP config (<64 KiB) while rejecting oversized
# archive members (up to 10 MiB) before UTF-8 decode or json.loads.
MAX_JSON_DOCUMENT_BYTES = 1024 * 1024

# 4 KiB raw object-key ceiling: legitimate manifest/MCP object keys are short
# identifiers (<256 bytes); capping raw key bytes before decoding escaped keys
# prevents a ~1 MiB key from entering json.loads inside the inline splitter.
MAX_JSON_KEY_BYTES = 4 * 1024

# 64 container nesting levels: legitimate plugin manifests, MCP configs, and
# marketplace catalogs nest 3-8 levels deep (<16 with extension schemas).
MAX_JSON_DEPTH = 64

# 100,000 decoded JSON nodes: bounds object-graph memory for shallow-but-dense
# documents (e.g. [0,0,...] of 500k elements inside 1 MiB).
MAX_JSON_NODES = 100_000

# 1 MiB serialized output budget for indent=2 formatting.
MAX_JSON_OUTPUT_BYTES = 1024 * 1024

LimitKind = Literal["max_bytes", "max_depth", "max_nodes", "max_output_bytes"]

# Matches a complete JSON string literal (consuming any escaped byte `\.` inside
# the string possessively), an unclosed `"`, or a structural bracket outside strings.
_DEPTH_SCAN_RE = re.compile(rb'"(?:\\.|[^"\\])*+"|"|[\[\]{}]', re.DOTALL)

# Same string-aware tokenizer extended with `:` and `,` for locating top-level
# inline `"mcpServers"` spans in raw manifest bytes without calling json.loads.
_STRUCT_SCAN_RE = re.compile(rb'"(?:\\.|[^"\\])*+"|"|[\[\]{}:,]', re.DOTALL)

_INLINE_PATH_REF_MAX_BYTES = 4096


class JsonBudgetError(ValueError):
    """Raised when a JSON document or component exceeds a configured budget."""

    def __init__(
        self,
        *,
        target: str,
        limit_kind: LimitKind,
        limit: int,
        actual: int | None = None,
        message: str | None = None,
    ) -> None:
        self.target = target
        self.limit_kind: LimitKind = limit_kind
        self.limit = limit
        self.actual = actual
        if message is None:
            message = _format_budget_message(
                target=target,
                limit_kind=limit_kind,
                limit=limit,
                actual=actual,
            )
        super().__init__(message)


class BudgetErrorSentinel(bytes):
    """Out-of-band sentinel object carrying a ``JsonBudgetError`` across dialect
    adaptation without encoding error payloads into file bytes.

    Archive extraction (``zipfile``/``tarfile``) only ever produces plain
    ``bytes`` instances (``type(raw) is bytes``), so no uploaded file content
    can ever satisfy ``isinstance(raw, BudgetErrorSentinel)``.
    """

    error: JsonBudgetError

    def __new__(cls, error: JsonBudgetError) -> BudgetErrorSentinel:
        obj = super().__new__(cls, b"")
        obj.error = error
        return obj


def budget_error_sentinel(err: JsonBudgetError) -> BudgetErrorSentinel:
    """Wrap ``err`` in an out-of-band ``BudgetErrorSentinel`` instance."""
    return BudgetErrorSentinel(err)


def unwrap_budget_error(raw: Any) -> JsonBudgetError | None:
    """Return the ``JsonBudgetError`` carried by a ``BudgetErrorSentinel``, or ``None``."""
    if isinstance(raw, BudgetErrorSentinel):
        return raw.error
    return None


def _format_budget_message(
    *,
    target: str,
    limit_kind: LimitKind,
    limit: int,
    actual: int | None,
) -> str:
    if limit_kind == "max_bytes":
        detail = f"got {actual} bytes" if actual is not None else f"limit {limit}"
        return (
            f"{target} exceeds maximum JSON size of {limit} bytes "
            f"(max_bytes={limit}, {detail})"
        )
    if limit_kind == "max_depth":
        detail = f"reached {actual}" if actual is not None else f"limit {limit}"
        return (
            f"{target} exceeds maximum JSON nesting depth of {limit} "
            f"(max_depth={limit}, {detail})"
        )
    if limit_kind == "max_nodes":
        detail = f"reached {actual}" if actual is not None else f"limit {limit}"
        return (
            f"{target} exceeds maximum JSON node count of {limit} "
            f"(max_nodes={limit}, {detail})"
        )
    detail = f"got {actual} bytes" if actual is not None else f"limit {limit}"
    return (
        f"{target} exceeds maximum JSON output size of {limit} bytes "
        f"(max_output_bytes={limit}, {detail})"
    )


def check_json_byte_budget(
    raw: bytes,
    *,
    target: str = "<json>",
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
) -> None:
    """Reject raw bytes exceeding ``max_bytes`` before decoding or parsing."""
    if len(raw) > max_bytes:
        raise JsonBudgetError(
            target=target,
            limit_kind="max_bytes",
            limit=max_bytes,
            actual=len(raw),
        )


def scan_json_depth(
    raw: bytes,
    *,
    target: str = "<json>",
    max_depth: int = MAX_JSON_DEPTH,
) -> int:
    """Scan bracket nesting depth before ``json.loads``, ignoring brackets inside
    JSON strings and respecting backslash escapes (``\\\"``, ``\\\\``)."""
    depth = 0
    peak = 0
    for match in _DEPTH_SCAN_RE.finditer(raw):
        tok = match.group(0)
        first = tok[0]
        if first == 0x22:  # ord('"')
            if len(tok) == 1:
                # Unclosed string literal through EOF; remaining bytes are inside it.
                break
            continue
        if first in (0x5B, 0x7B):  # '[' or '{'
            depth += 1
            if depth > max_depth:
                raise JsonBudgetError(
                    target=target,
                    limit_kind="max_depth",
                    limit=max_depth,
                    actual=depth,
                )
            if depth > peak:
                peak = depth
        elif depth > 0:  # ']' or '}'
            depth -= 1
    return peak


def _indent2_container_overhead(count: int, depth: int) -> int:
    """Exact structural + whitespace bytes contributed by a container of
    ``count`` items at 1-based nesting ``depth`` when formatted with ``indent=2``."""
    if count == 0:
        return 2
    return (count + 1) * (2 * depth + 2) - 2


def check_json_nodes(
    value: Any,
    *,
    target: str = "<json>",
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
    max_output_bytes: int | None = None,
    check_surrogates: bool = True,
) -> int:
    """Iteratively verify container depth, total AST nodes, UTF-8 encodability,
    and optional ``indent=2`` serialized byte budget without recursion."""
    if max_nodes < 1:
        raise JsonBudgetError(
            target=target,
            limit_kind="max_nodes",
            limit=max_nodes,
            actual=1,
        )
    if not isinstance(value, (list, dict)):
        if isinstance(value, str) and (
            check_surrogates or max_output_bytes is not None
        ):
            encoded = value.encode(
                "utf-8", "strict" if check_surrogates else "surrogatepass"
            )
            if max_output_bytes is not None and len(encoded) + 2 > max_output_bytes:
                raise JsonBudgetError(
                    target=target,
                    limit_kind="max_output_bytes",
                    limit=max_output_bytes,
                    actual=len(encoded) + 2,
                )
        elif max_output_bytes is not None:
            scalar_len = _scalar_serialized_len(value)
            if scalar_len > max_output_bytes:
                raise JsonBudgetError(
                    target=target,
                    limit_kind="max_output_bytes",
                    limit=max_output_bytes,
                    actual=scalar_len,
                )
        return 1

    if max_depth < 1:
        raise JsonBudgetError(
            target=target,
            limit_kind="max_depth",
            limit=max_depth,
            actual=1,
        )

    err_mode = "strict" if check_surrogates else "surrogatepass"
    nodes = 1
    estimated_bytes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]

    while stack:
        curr, depth = stack.pop()
        if isinstance(curr, list):
            count = len(curr)
            nodes += count
            if nodes > max_nodes:
                raise JsonBudgetError(
                    target=target,
                    limit_kind="max_nodes",
                    limit=max_nodes,
                    actual=nodes,
                )
            if max_output_bytes is not None:
                estimated_bytes += _indent2_container_overhead(count, depth)
                if estimated_bytes > max_output_bytes:
                    raise JsonBudgetError(
                        target=target,
                        limit_kind="max_output_bytes",
                        limit=max_output_bytes,
                        actual=estimated_bytes,
                    )
            for item in curr:
                if isinstance(item, (list, dict)):
                    next_depth = depth + 1
                    if next_depth > max_depth:
                        raise JsonBudgetError(
                            target=target,
                            limit_kind="max_depth",
                            limit=max_depth,
                            actual=next_depth,
                        )
                    stack.append((item, next_depth))
                elif isinstance(item, str):
                    if check_surrogates or max_output_bytes is not None:
                        b = item.encode("utf-8", err_mode)
                        if max_output_bytes is not None:
                            estimated_bytes += len(b) + 2
                            if estimated_bytes > max_output_bytes:
                                raise JsonBudgetError(
                                    target=target,
                                    limit_kind="max_output_bytes",
                                    limit=max_output_bytes,
                                    actual=estimated_bytes,
                                )
                elif max_output_bytes is not None:
                    estimated_bytes += _scalar_serialized_len(item)
                    if estimated_bytes > max_output_bytes:
                        raise JsonBudgetError(
                            target=target,
                            limit_kind="max_output_bytes",
                            limit=max_output_bytes,
                            actual=estimated_bytes,
                        )
        elif isinstance(curr, dict):
            count = len(curr)
            nodes += count
            if nodes > max_nodes:
                raise JsonBudgetError(
                    target=target,
                    limit_kind="max_nodes",
                    limit=max_nodes,
                    actual=nodes,
                )
            if max_output_bytes is not None:
                estimated_bytes += _indent2_container_overhead(count, depth)
                if estimated_bytes > max_output_bytes:
                    raise JsonBudgetError(
                        target=target,
                        limit_kind="max_output_bytes",
                        limit=max_output_bytes,
                        actual=estimated_bytes,
                    )
            for k, v in curr.items():
                if isinstance(k, str) and (
                    check_surrogates or max_output_bytes is not None
                ):
                    kb = k.encode("utf-8", err_mode)
                    if max_output_bytes is not None:
                        estimated_bytes += len(kb) + 4  # "key": 
                        if estimated_bytes > max_output_bytes:
                            raise JsonBudgetError(
                                target=target,
                                limit_kind="max_output_bytes",
                                limit=max_output_bytes,
                                actual=estimated_bytes,
                            )
                if isinstance(v, (list, dict)):
                    next_depth = depth + 1
                    if next_depth > max_depth:
                        raise JsonBudgetError(
                            target=target,
                            limit_kind="max_depth",
                            limit=max_depth,
                            actual=next_depth,
                        )
                    stack.append((v, next_depth))
                elif isinstance(v, str):
                    if check_surrogates or max_output_bytes is not None:
                        vb = v.encode("utf-8", err_mode)
                        if max_output_bytes is not None:
                            estimated_bytes += len(vb) + 2
                            if estimated_bytes > max_output_bytes:
                                raise JsonBudgetError(
                                    target=target,
                                    limit_kind="max_output_bytes",
                                    limit=max_output_bytes,
                                    actual=estimated_bytes,
                                )
                elif max_output_bytes is not None:
                    estimated_bytes += _scalar_serialized_len(v)
                    if estimated_bytes > max_output_bytes:
                        raise JsonBudgetError(
                            target=target,
                            limit_kind="max_output_bytes",
                            limit=max_output_bytes,
                            actual=estimated_bytes,
                        )
    return nodes


def _scalar_serialized_len(value: Any) -> int:
    if value is None or value is True:
        return 4
    if value is False:
        return 5
    return len(str(value))


def loads_json_with_budget(
    raw: bytes,
    *,
    target: str = "<json>",
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
    check_surrogates: bool = True,
) -> Any:
    """Parse ``raw`` JSON bytes after enforcing byte and bracket-depth budgets,
    then enforce post-parse AST node count."""
    existing_err = unwrap_budget_error(raw)
    if existing_err is not None:
        raise existing_err
    check_json_byte_budget(raw, target=target, max_bytes=max_bytes)
    scan_json_depth(raw, target=target, max_depth=max_depth)
    doc = json.loads(raw.decode("utf-8"))
    check_json_nodes(
        doc,
        target=target,
        max_depth=max_depth,
        max_nodes=max_nodes,
        check_surrogates=check_surrogates,
    )
    return doc


def dumps_json_with_budget(
    value: Any,
    *,
    target: str = "<json>",
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
    max_output_bytes: int = MAX_JSON_OUTPUT_BYTES,
) -> bytes:
    """Verify node, depth, and ``indent=2`` output byte budgets before formatting,
    then serialize with ``indent=2`` (preserving upstream ``content_hash``) and
    verify the final UTF-8 byte length."""
    check_json_nodes(
        value,
        target=target,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_output_bytes=max_output_bytes,
        check_surrogates=False,
    )
    encoded = json.dumps(value, indent=2).encode("utf-8")
    if len(encoded) > max_output_bytes:
        raise JsonBudgetError(
            target=target,
            limit_kind="max_output_bytes",
            limit=max_output_bytes,
            actual=len(encoded),
        )
    return encoded


def sanitize_json_surrogates(value: Any) -> Any:
    """Iteratively replace lone-surrogate strings with ``None`` and drop
    unencodable dict keys."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return None
        return value
    if not isinstance(value, (list, dict)):
        return value

    stack: list[Any] = [value]
    while stack:
        curr = stack.pop()
        if isinstance(curr, list):
            for i, item in enumerate(curr):
                if isinstance(item, str):
                    try:
                        item.encode("utf-8")
                    except UnicodeEncodeError:
                        curr[i] = None
                elif isinstance(item, (list, dict)):
                    stack.append(item)
        elif isinstance(curr, dict):
            bad_keys: list[Any] = []
            for k, v in curr.items():
                if isinstance(k, str):
                    try:
                        k.encode("utf-8")
                    except UnicodeEncodeError:
                        bad_keys.append(k)
                        continue
                if isinstance(v, str):
                    try:
                        v.encode("utf-8")
                    except UnicodeEncodeError:
                        curr[k] = None
                elif isinstance(v, (list, dict)):
                    stack.append(v)
            for k in bad_keys:
                del curr[k]
    return value


def _is_mcp_servers_key(
    tok: bytes,
    *,
    target: str = "<json>",
    max_key_bytes: int = MAX_JSON_KEY_BYTES,
) -> bool:
    """Check whether a raw JSON string token spells ``"mcpServers"``, enforcing
    a raw byte limit on the key before decoding any escapes."""
    if len(tok) > max_key_bytes:
        raise JsonBudgetError(
            target=target,
            limit_kind="max_bytes",
            limit=max_key_bytes,
            actual=len(tok),
        )
    if tok == b'"mcpServers"':
        return True
    if b"\\" not in tok:
        return False
    try:
        return (
            loads_json_with_budget(
                tok,
                target=target,
                max_bytes=max_key_bytes,
                max_depth=1,
                max_nodes=1,
            )
            == "mcpServers"
        )
    except JsonBudgetError:
        raise
    except Exception:
        return False


def split_inline_mcp_servers(
    raw: bytes,
    *,
    target: str = "<json>",
    max_outer_depth: int = MAX_JSON_DEPTH,
    max_key_bytes: int = MAX_JSON_KEY_BYTES,
) -> tuple[bytes, bytes] | None:
    """Locate top-level ``mcpServers`` values in a raw manifest JSON object
    without calling ``json.loads`` on the document.

    Honors JSON duplicate-key semantics ("last key wins"): all top-level
    ``mcpServers`` spans are replaced with ``null`` in ``manifest_without_mcp``,
    and the **last** occurrence's raw value bytes are returned as the second
    tuple element (including ``b"null"`` when the last occurrence is ``null``,
    so earlier occurrences never resurrect).
    """
    if b"mcpServers" not in raw and b"\\u" not in raw:
        return None

    depth = 0
    state = "start"  # start | key | colon | val | after_val
    is_mcp_key = False
    skip_until = 0
    mcp_spans: list[tuple[int, int]] = []

    for match in _STRUCT_SCAN_RE.finditer(raw):
        if match.start() < skip_until:
            continue
        tok = match.group(0)
        first = tok[0]

        if first == 0x22 and len(tok) == 1:
            return None

        if state == "start":
            if tok != b"{":
                return None
            depth = 1
            state = "key"
            continue

        if depth == 1:
            if state == "key":
                if tok == b"}":
                    depth = 0
                    break
                if first == 0x22:
                    is_mcp_key = _is_mcp_servers_key(
                        tok, target=target, max_key_bytes=max_key_bytes
                    )
                    state = "colon"
                else:
                    return None
                continue

            if state == "colon":
                if tok != b":":
                    return None
                if is_mcp_key:
                    val_start = match.end()
                    n = len(raw)
                    while val_start < n and raw[val_start] in (
                        0x20,
                        0x09,
                        0x0A,
                        0x0D,
                    ):
                        val_start += 1
                    if val_start >= n:
                        return None
                    val_end = _find_inline_value_end(raw, val_start)
                    if val_end <= val_start:
                        return None
                    mcp_spans.append((val_start, val_end))
                    skip_until = val_end
                    state = "after_val"
                    continue
                state = "val"
                continue

            if state == "after_val":
                if tok == b",":
                    state = "key"
                elif tok == b"}":
                    depth = 0
                    break
                else:
                    return None
                continue

        # state == "val" at depth >= 1
        if first in (0x5B, 0x7B):  # '[' or '{'
            depth += 1
            if depth > max_outer_depth:
                return None
        elif first in (0x5D, 0x7D):  # ']' or '}'
            depth -= 1
            if depth == 0:
                break
            if depth == 1:
                state = "after_val"
        elif depth == 1 and tok == b",":
            state = "key"

    if not mcp_spans:
        return None

    last_start, last_end = mcp_spans[-1]
    last_val = raw[last_start:last_end].strip()
    if not last_val:
        return None

    # A single short string value (e.g. "./config/servers.json") is a file path
    # reference, not an inline MCP server map.
    if (
        len(mcp_spans) == 1
        and last_val[0] == 0x22
        and len(last_val) <= _INLINE_PATH_REF_MAX_BYTES
    ):
        return None

    parts: list[bytes] = []
    cursor = 0
    for s, e in mcp_spans:
        parts.append(raw[cursor:s])
        parts.append(b"null")
        cursor = e
    parts.append(raw[cursor:])
    manifest_without_mcp = b"".join(parts)
    return manifest_without_mcp, last_val


def _find_inline_value_end(raw: bytes, val_start: int) -> int:
    """Find the end byte offset of a top-level value starting at ``val_start``."""
    sub_depth = 0
    for sub_match in _STRUCT_SCAN_RE.finditer(raw, val_start):
        sub_tok = sub_match.group(0)
        first = sub_tok[0]
        if first == 0x22:
            if len(sub_tok) == 1:
                break
            if sub_depth == 0:
                return sub_match.end()
            continue
        if first in (0x5B, 0x7B):
            sub_depth += 1
        elif first in (0x5D, 0x7D):
            if sub_depth == 0:
                # Closing `}` of the outer manifest object itself.
                return sub_match.start()
            sub_depth -= 1
            if sub_depth == 0:
                return sub_match.end()
        elif sub_depth == 0 and sub_tok == b",":
            return sub_match.start()

    stripped = raw.rstrip()
    if stripped.endswith(b"}") and len(stripped) - 1 > val_start:
        return len(stripped) - 1
    return len(raw)


def parse_manifest_with_budget(
    manifest_raw: bytes,
    *,
    manifest_path: str,
    max_bytes: int = MAX_JSON_DOCUMENT_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
    max_nodes: int = MAX_JSON_NODES,
) -> tuple[dict[str, Any] | None, bytes | None, bytes | None]:
    """Parse ``manifest_raw`` under budget while isolating inline ``mcpServers``.

    Returns ``(manifest_dict, manifest_error_bytes, inline_mcp_error_bytes)``:
    - When the manifest itself violates a budget or is not a JSON object,
      ``manifest_dict`` is ``None`` and ``manifest_error_bytes`` carries a
      ``BudgetErrorSentinel`` (or raw bytes for syntax/schema handling).
    - When an inline ``mcpServers`` declaration violates a budget (or is
      invalid JSON) while the rest of the manifest is valid, ``manifest_dict``
      is returned without ``mcpServers`` and ``inline_mcp_error_bytes`` carries
      the MCP component error for ``validate_mcp_document``.
    """
    try:
        split = split_inline_mcp_servers(
            manifest_raw,
            target=manifest_path,
            max_outer_depth=max_depth,
        )
    except JsonBudgetError as err:
        budget_err = (
            JsonBudgetError(
                target=manifest_path,
                limit_kind="max_bytes",
                limit=max_bytes,
                actual=len(manifest_raw),
            )
            if len(manifest_raw) > max_bytes
            else err
        )
        return None, budget_error_sentinel(budget_err), None

    if split is not None:
        manifest_without_mcp_raw, inline_val_raw = split
        try:
            parsed_manifest = loads_json_with_budget(
                manifest_without_mcp_raw,
                target=manifest_path,
                max_bytes=max_bytes,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
        except JsonBudgetError as err:
            return None, budget_error_sentinel(err), None
        except (ValueError, RecursionError):
            return None, manifest_raw, None

        if not isinstance(parsed_manifest, dict):
            return (
                None,
                manifest_without_mcp_or_raw(manifest_without_mcp_raw, manifest_raw),
                None,
            )
        parsed_manifest.pop("mcpServers", None)

        # Last duplicate mcpServers was explicitly null -> no MCP servers.
        if inline_val_raw == b"null":
            return parsed_manifest, None, None

        # Last duplicate mcpServers was a short path string reference.
        if (
            inline_val_raw[0] == 0x22
            and len(inline_val_raw) <= _INLINE_PATH_REF_MAX_BYTES
        ):
            try:
                path_ref = loads_json_with_budget(
                    inline_val_raw,
                    target=manifest_path,
                    max_bytes=_INLINE_PATH_REF_MAX_BYTES,
                    max_depth=1,
                    max_nodes=1,
                )
            except (ValueError, RecursionError):
                return parsed_manifest, None, b'{"mcpServers":' + inline_val_raw + b"}"
            if isinstance(path_ref, str):
                parsed_manifest["mcpServers"] = path_ref
            return parsed_manifest, None, None

        inline_target = f"{manifest_path} (mcpServers)"
        inline_doc_raw = b'{"mcpServers":' + inline_val_raw + b"}"
        try:
            if len(inline_val_raw) > max_bytes:
                raise JsonBudgetError(
                    target=inline_target,
                    limit_kind="max_bytes",
                    limit=max_bytes,
                    actual=len(inline_val_raw),
                )
            parsed_inline_doc = loads_json_with_budget(
                inline_doc_raw,
                target=inline_target,
                max_bytes=max_bytes + 32,
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
        except JsonBudgetError as err:
            return parsed_manifest, None, budget_error_sentinel(err)
        except (ValueError, RecursionError):
            return parsed_manifest, None, inline_doc_raw

        declared = parsed_inline_doc.get("mcpServers")
        if isinstance(declared, dict):
            parsed_manifest["mcpServers"] = declared
            return parsed_manifest, None, None
        return parsed_manifest, None, inline_doc_raw

    try:
        parsed = loads_json_with_budget(
            manifest_raw,
            target=manifest_path,
            max_bytes=max_bytes,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    except JsonBudgetError as err:
        return None, budget_error_sentinel(err), None
    except (ValueError, RecursionError):
        return None, manifest_raw, None

    if not isinstance(parsed, dict):
        return None, manifest_raw, None
    return parsed, None, None


def manifest_without_mcp_or_raw(
    manifest_without_mcp_raw: bytes, manifest_raw: bytes
) -> bytes:
    return (
        manifest_without_mcp_raw
        if len(manifest_without_mcp_raw) <= MAX_JSON_DOCUMENT_BYTES
        else manifest_raw[:MAX_JSON_DOCUMENT_BYTES]
    )
