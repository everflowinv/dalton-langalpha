"""Byte-level streaming secret redactor for large workspace file downloads.

Operates directly on raw ``bytes`` chunks without decoding the full file into
memory, matching both UTF-8 and Latin-1 encodings of literal secrets plus the
unbounded sandbox and Claude setup token patterns from
:mod:`src.server.utils.secret_redactor`.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping

from src.server.utils.secret_redactor import MIN_REDACT_SECRET_LEN, get_redactor

# Cap on the digit run in an unclosed ``sk-ant-oat\d+`` prefix held across
# chunks. Longer digit runs are treated as non-token plaintext so memory stays
# strictly bounded on adversarial input.
MAX_TOKEN_PREFIX_DIGITS = 64 * 1024

_SANDBOX_TOKEN_PATTERN = rb"gxs[ar]_[A-Za-z0-9_.\-]+"
_SETUP_TOKEN_PATTERN = rb"sk-ant-oat\d+-[A-Za-z0-9_-]+"

_SANDBOX_CONT_RE = re.compile(rb"^[A-Za-z0-9_.\-]+")
_SETUP_CONT_RE = re.compile(rb"^[A-Za-z0-9_-]+")

_SANDBOX_REPLACEMENT = b"[REDACTED:SANDBOX_TOKEN]"
_SETUP_REPLACEMENT = b"[REDACTED:CLAUDE_SETUP_TOKEN]"

_SETUP_FIXED_PREFIX = b"sk-ant-oat"
_SANDBOX_PREFIXES_BY_LEN: dict[int, frozenset[bytes]] = {
    1: frozenset({b"g"}),
    2: frozenset({b"gx"}),
    3: frozenset({b"gxs"}),
    4: frozenset({b"gxsa", b"gxsr"}),
    5: frozenset({b"gxsa_", b"gxsr_"}),
}


def _read_global_secrets() -> list[tuple[str, str]]:
    """Read and validate ``get_redactor()._secrets`` (fail-closed on drift)."""
    redactor = get_redactor()
    if not hasattr(redactor, "_secrets"):
        raise RuntimeError(
            "SecretRedactor no longer exposes '_secrets'; refusing unredacted stream"
        )
    secrets = redactor._secrets
    if not isinstance(secrets, list):
        raise TypeError(
            f"SecretRedactor._secrets must be list[tuple[str, str]], got {type(secrets).__name__}"
        )
    for item in secrets:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise TypeError(
                "SecretRedactor._secrets entries must be (str, str) tuples"
            )
    return secrets


def _unclosed_token_prefix_len(buf: bytes, start: int) -> int:
    """Length of the longest unclosed sandbox/setup token prefix at the end of ``buf[start:]``."""
    end = len(buf)
    avail = end - start
    if avail <= 0:
        return 0

    best = 0

    # Fixed prefixes of ``gxsa_`` / ``gxsr_`` (1..5 bytes).
    max_sb = min(5, avail)
    for k in range(max_sb, 0, -1):
        if buf[end - k : end] in _SANDBOX_PREFIXES_BY_LEN[k]:
            best = k
            break

    # Fixed prefixes of ``sk-ant-oat`` (1..10 bytes).
    max_setup = min(len(_SETUP_FIXED_PREFIX), avail)
    for k in range(max_setup, best, -1):
        if buf[end - k : end] == _SETUP_FIXED_PREFIX[:k]:
            best = k
            break

    # Dynamic prefix ``sk-ant-oat\d+-?`` with digits capped at MAX_TOKEN_PREFIX_DIGITS.
    has_dash = buf[end - 1] == ord("-")
    digit_end = end - 1 if has_dash else end
    i = digit_end - 1
    digits = 0
    while i >= start and 48 <= buf[i] <= 57:
        digits += 1
        if digits > MAX_TOKEN_PREFIX_DIGITS:
            break
        i -= 1

    if 1 <= digits <= MAX_TOKEN_PREFIX_DIGITS:
        prefix_start = digit_end - digits - len(_SETUP_FIXED_PREFIX)
        if (
            prefix_start >= start
            and buf[prefix_start : prefix_start + len(_SETUP_FIXED_PREFIX)]
            == _SETUP_FIXED_PREFIX
        ):
            cand = end - prefix_start
            if cand > best:
                best = cand

    return best


class StreamingSecretRedactor:
    """Incremental byte-stream secret redactor.

    Matches:
    1. Global MCP secrets from ``get_redactor()._secrets`` and owner
       ``vault_secrets`` (character length ``>= MIN_REDACT_SECRET_LEN``) in
       UTF-8 and, where encodable, Latin-1 byte encodings, longest-first.
    2. Unbounded sandbox tokens (``gxs[ar]_[A-Za-z0-9_.-]+``) and Claude setup
       tokens (``sk-ant-oat\\d+-[A-Za-z0-9_-]+``), entering an O(1)-memory
       swallow state when a token runs past the end of a chunk.

    Emitted ``[REDACTED:...]`` markers are written directly to output and are
    never re-scanned.
    """

    def __init__(self, vault_secrets: Mapping[str, str] | None = None) -> None:
        global_secrets = _read_global_secrets()

        pattern_to_repl: dict[bytes, bytes] = {}

        def _add_secret(name: str, value: str) -> None:
            if not isinstance(value, str) or len(value) < MIN_REDACT_SECRET_LEN:
                return
            repl = b"[REDACTED:" + name.encode("utf-8") + b"]"
            utf8_pat = value.encode("utf-8")
            pattern_to_repl.setdefault(utf8_pat, repl)
            try:
                latin1_pat = value.encode("latin-1")
            except UnicodeEncodeError:
                return
            pattern_to_repl.setdefault(latin1_pat, repl)

        # Global MCP secrets first, then vault secrets, matching SecretRedactor.redact().
        for name, value in sorted(
            global_secrets, key=lambda kv: len(kv[1]), reverse=True
        ):
            _add_secret(name, value)

        if vault_secrets:
            for name, value in sorted(
                vault_secrets.items(),
                key=lambda kv: len(kv[1]) if isinstance(kv[1], str) else 0,
                reverse=True,
            ):
                _add_secret(name, value)

        sorted_patterns = sorted(pattern_to_repl.keys(), key=len, reverse=True)
        self._pattern_to_repl = pattern_to_repl
        self._max_lit_len = len(sorted_patterns[0]) if sorted_patterns else 0
        self._lit_keep = max(self._max_lit_len - 1, 0)

        parts: list[bytes] = []
        if sorted_patterns:
            lit_alt = b"|".join(re.escape(p) for p in sorted_patterns)
            parts.append(rb"(?P<lit>" + lit_alt + rb")")
        parts.append(rb"(?P<sandbox>" + _SANDBOX_TOKEN_PATTERN + rb")")
        parts.append(rb"(?P<setup>" + _SETUP_TOKEN_PATTERN + rb")")
        self._regex = re.compile(b"|".join(parts))

        self._tail: bytes = b""
        # None, "sandbox", or "setup" while discarding continuation bytes of an
        # unbounded token that reached the end of the previous chunk.
        self._swallowing: str | None = None

    def feed(self, chunk: bytes) -> bytes:
        """Process the next input chunk and return safe redacted bytes."""
        if not chunk:
            return b""

        if self._swallowing is not None:
            cont_re = (
                _SANDBOX_CONT_RE if self._swallowing == "sandbox" else _SETUP_CONT_RE
            )
            m = cont_re.match(chunk)
            if m is not None:
                if m.end() == len(chunk):
                    return b""
                chunk = chunk[m.end() :]
            self._swallowing = None

        buf = self._tail + chunk if self._tail else chunk
        self._tail = b""
        return self._process(buf, final=False)

    def flush(self) -> bytes:
        """Flush buffered tail bytes at end-of-stream and reset state."""
        if self._swallowing is not None:
            self._swallowing = None
            self._tail = b""
            return b""
        if not self._tail:
            return b""
        buf = self._tail
        self._tail = b""
        return self._process(buf, final=True)

    async def redact_stream(
        self, chunks: AsyncIterator[bytes]
    ) -> AsyncIterator[bytes]:
        """Yield redacted byte chunks from an async byte iterator."""
        async for chunk in chunks:
            out = self.feed(chunk)
            if out:
                yield out
        tail = self.flush()
        if tail:
            yield tail

    def _process(self, buf: bytes, *, final: bool) -> bytes:
        n = len(buf)
        safe_limit = n if final else n - self._lit_keep
        out: list[bytes] = []
        pos = 0

        while pos < n:
            m = self._regex.search(buf, pos)
            if m is None or (not final and m.start() >= safe_limit):
                break

            if m.start() > pos:
                out.append(buf[pos : m.start()])

            group = m.lastgroup
            if group == "lit":
                out.append(self._pattern_to_repl[m.group(0)])
                pos = m.end()
            elif group == "sandbox":
                out.append(_SANDBOX_REPLACEMENT)
                pos = m.end()
                if not final and pos == n:
                    self._swallowing = "sandbox"
                    self._tail = b""
                    return b"".join(out)
            else:  # "setup"
                out.append(_SETUP_REPLACEMENT)
                pos = m.end()
                if not final and pos == n:
                    self._swallowing = "setup"
                    self._tail = b""
                    return b"".join(out)

        if final:
            if pos < n:
                out.append(buf[pos:])
            return b"".join(out)

        tok_keep = _unclosed_token_prefix_len(buf, pos)
        keep = max(self._lit_keep, tok_keep)
        emit_end = max(pos, n - keep)
        if emit_end > pos:
            out.append(buf[pos:emit_end])
        self._tail = buf[emit_end:]
        return b"".join(out)


async def redact_stream(
    chunks: AsyncIterator[bytes],
    *,
    vault_secrets: Mapping[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """Convenience wrapper applying :class:`StreamingSecretRedactor` to ``chunks``."""
    redactor = StreamingSecretRedactor(vault_secrets=vault_secrets)
    async for out in redactor.redact_stream(chunks):
        yield out
