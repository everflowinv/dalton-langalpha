"""Byte-level streaming secret redactor for large workspace file downloads.

Operates directly on raw ``bytes`` chunks without decoding the full file into
memory, matching both UTF-8 and Latin-1 encodings of literal secrets plus the
unbounded sandbox and Claude setup token patterns from
:mod:`src.server.utils.secret_redactor`.
"""

from __future__ import annotations

import heapq
import re
from collections.abc import AsyncIterator, Iterator, Mapping

from src.server.utils.secret_redactor import MIN_REDACT_SECRET_LEN, get_redactor

# Cap on the digit run in an unclosed ``sk-ant-oat\d+`` prefix held across
# chunks. Longer digit runs are treated as non-token plaintext so memory stays
# strictly bounded on adversarial input.
MAX_TOKEN_PREFIX_DIGITS = 64 * 1024

# Internal processing window for ``feed()`` so even an 8 MiB chunk packed with
# dense disjoint matches never materialises millions of spans at once.
_FEED_WINDOW_BYTES = 256 * 1024

_SANDBOX_TOKEN_PATTERN = rb"gxs[ar]_[A-Za-z0-9_.\-]+"
_SETUP_TOKEN_PATTERN = rb"sk-ant-oat\d+-[A-Za-z0-9_-]+"

_SANDBOX_TOKEN_RE = re.compile(_SANDBOX_TOKEN_PATTERN)
_SETUP_TOKEN_RE = re.compile(_SETUP_TOKEN_PATTERN)

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


def _compile_period_extensions(pat: bytes) -> tuple[re.Pattern[bytes], ...]:
    """Compile ``(?:ext)+`` matchers for primitive self-overlap periods of ``pat``.

    A positive period ``d < len(pat)`` satisfies ``pat[d:] == pat[:-d]``; when
    an occurrence ends at ``cur_e``, a strictly overlapping next occurrence
    starts at ``cur_e - len(pat) + d`` iff ``buf`` matches ``pat[len(pat) - d:]``
    at ``cur_e``. Dropping any period whose extension starts with a shorter
    period's extension preserves exact coverage while letting ``(?:ext)+``
    consume periodic runs in C with O(1) memory.
    """
    pat_len = len(pat)
    exts: list[bytes] = []
    matchers: list[re.Pattern[bytes]] = []
    for d in range(1, pat_len):
        if pat[d:] == pat[:-d]:
            ext = pat[pat_len - d :]
            if any(ext.startswith(prev) for prev in exts):
                continue
            exts.append(ext)
            matchers.append(re.compile(rb"(?:" + re.escape(ext) + rb")+"))
    return tuple(matchers)


def _iter_literal_spans(
    buf: bytes, pat: bytes, ext_matchers: tuple[re.Pattern[bytes], ...]
) -> Iterator[tuple[int, int]]:
    """Yield maximal strictly-overlapping merged ``(start, end)`` runs of ``pat``."""
    pat_len = len(pat)
    idx = buf.find(pat)
    if not ext_matchers:
        while idx != -1:
            end = idx + pat_len
            yield idx, end
            idx = buf.find(pat, end)
        return

    while idx != -1:
        cur_s = idx
        cur_e = idx + pat_len
        while True:
            for matcher in ext_matchers:
                m = matcher.match(buf, cur_e)
                if m is not None:
                    cur_e = m.end()
                    break
            else:
                break
        yield cur_s, cur_e
        idx = buf.find(pat, cur_e)


def _prefer_end_token(a: str | None, b: str | None) -> str | None:
    """Choose the swallowing continuation class when token spans reach buffer end.

    ``sandbox`` continuation bytes (``[A-Za-z0-9_.-]``) are a strict superset of
    ``setup`` continuation bytes (``[A-Za-z0-9_-]``), so prefer ``"sandbox"`` if
    both reach the chunk end.
    """
    if a == "sandbox" or b == "sandbox":
        return "sandbox"
    return a or b


class StreamingSecretRedactor:
    """Incremental byte-stream secret redactor.

    Matches:
    1. Global MCP secrets from ``get_redactor()._secrets`` and owner
       ``vault_secrets`` (character length ``>= MIN_REDACT_SECRET_LEN``) in
       UTF-8 and, where encodable, Latin-1 byte encodings, longest-first.
    2. Unbounded sandbox tokens (``gxs[ar]_[A-Za-z0-9_.-]+``) and Claude setup
       tokens (``sk-ant-oat\\d+-[A-Za-z0-9_-]+``), entering an O(L)-memory
       swallow state when a token runs past the end of a chunk while retaining
       the last ``max_lit_len - 1`` swallowed bytes so a literal bridging past
       the end of the token is never leaked.

    All literal occurrences and token occurrences are collected independently
    and overlapping spans are merged into a single redacted union before
    emitting, so neither staggered overlapping secrets nor literal/token
    overlaps can leak a prefix or suffix fragment. Emitted ``[REDACTED:...]``
    markers are written directly to output and are never re-scanned.
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
        self._lit_patterns: list[
            tuple[bytes, bytes, tuple[re.Pattern[bytes], ...]]
        ] = [
            (pat, pattern_to_repl[pat], _compile_period_extensions(pat))
            for pat in sorted_patterns
        ]
        self._max_lit_len = len(sorted_patterns[0]) if sorted_patterns else 0
        self._lit_keep = max(self._max_lit_len - 1, 0)

        self._tail: bytes = b""
        # Number of leading bytes in ``self._tail`` already covered by an
        # emitted ``[REDACTED:...]`` marker; retained only to detect overlapping
        # matches bridging out of an already-emitted span (including swallowed
        # unbounded tokens) and never re-emitted as plaintext.
        self._covered_prefix: int = 0
        # None, "sandbox", or "setup" while discarding continuation bytes of an
        # unbounded token that reached the end of the previous chunk.
        self._swallowing: str | None = None

    def feed(self, chunk: bytes) -> bytes:
        """Process the next input chunk and return safe redacted bytes."""
        if not chunk:
            return b""
        if len(chunk) <= _FEED_WINDOW_BYTES:
            return self._feed_one(chunk)
        parts: list[bytes] = []
        for off in range(0, len(chunk), _FEED_WINDOW_BYTES):
            piece = self._feed_one(chunk[off : off + _FEED_WINDOW_BYTES])
            if piece:
                parts.append(piece)
        if not parts:
            return b""
        return parts[0] if len(parts) == 1 else b"".join(parts)

    def _feed_one(self, chunk: bytes) -> bytes:
        if self._swallowing is not None:
            cont_re = (
                _SANDBOX_CONT_RE if self._swallowing == "sandbox" else _SETUP_CONT_RE
            )
            m = cont_re.match(chunk)
            if m is not None:
                swallowed = chunk[: m.end()]
                if self._lit_keep > 0:
                    self._tail = (self._tail + swallowed)[-self._lit_keep :]
                    self._covered_prefix = len(self._tail)
                else:
                    self._tail = b""
                    self._covered_prefix = 0
                if m.end() == len(chunk):
                    return b""
                chunk = chunk[m.end() :]
            self._swallowing = None

        buf = self._tail + chunk if self._tail else chunk
        covered = self._covered_prefix
        self._tail = b""
        self._covered_prefix = 0
        return self._process(buf, covered=covered, final=False)

    def flush(self) -> bytes:
        """Flush buffered tail bytes at end-of-stream and reset state."""
        if self._swallowing is not None:
            self._swallowing = None
            self._tail = b""
            self._covered_prefix = 0
            return b""
        if not self._tail:
            self._covered_prefix = 0
            return b""
        buf = self._tail
        covered = self._covered_prefix
        self._tail = b""
        self._covered_prefix = 0
        return self._process(buf, covered=covered, final=True)

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

    def _collect_merged_spans(
        self, buf: bytes, *, covered: int
    ) -> list[tuple[int, int, bytes, str | None]]:
        """Find all literal & token matches and incrementally merge overlapping spans.

        Each literal pattern yields already self-merged disjoint runs, and a
        bounded k-way min-heap merges across patterns and token streams on the
        fly so self-overlapping matches never allocate per-occurrence records.

        Returns sorted disjoint tuples ``(start, end, replacement, end_token)``
        where ``end_token`` is ``"sandbox"`` / ``"setup"`` if a token match in
        the merged group reaches ``len(buf)``, else ``None``.
        """
        n = len(buf)
        # Priority order (strictly unique per source so heap ties never compare iterators):
        #   (-1, 0) already-covered prefix (emits b"");
        #   (0, pat_idx) literals longest-first (global before vault on tie);
        #   (1, 0) sandbox token; (2, 0) setup token.
        SpanItem = tuple[int, int, tuple[int, int], bytes, str | None]

        def _lit_source(
            pat_idx: int,
            pat: bytes,
            repl: bytes,
            ext_matchers: tuple[re.Pattern[bytes], ...],
        ) -> Iterator[SpanItem]:
            pri = (0, pat_idx)
            for s, e in _iter_literal_spans(buf, pat, ext_matchers):
                yield s, e, pri, repl, None

        def _sandbox_source() -> Iterator[SpanItem]:
            for m in _SANDBOX_TOKEN_RE.finditer(buf):
                end = m.end()
                yield (
                    m.start(),
                    end,
                    (1, 0),
                    _SANDBOX_REPLACEMENT,
                    "sandbox" if end == n else None,
                )

        def _setup_source() -> Iterator[SpanItem]:
            for m in _SETUP_TOKEN_RE.finditer(buf):
                end = m.end()
                yield (
                    m.start(),
                    end,
                    (2, 0),
                    _SETUP_REPLACEMENT,
                    "setup" if end == n else None,
                )

        heap: list[
            tuple[
                int,
                int,
                tuple[int, int],
                bytes,
                str | None,
                Iterator[SpanItem] | None,
            ]
        ] = []

        if covered > 0:
            heapq.heappush(heap, (0, -covered, (-1, 0), b"", None, None))

        for pat_idx, (pat, repl, ext_matchers) in enumerate(self._lit_patterns):
            it = _lit_source(pat_idx, pat, repl, ext_matchers)
            first = next(it, None)
            if first is not None:
                s, e, pri, repl_b, tok = first
                heapq.heappush(heap, (s, -e, pri, repl_b, tok, it))

        for tok_it in (_sandbox_source(), _setup_source()):
            first = next(tok_it, None)
            if first is not None:
                s, e, pri, repl_b, tok = first
                heapq.heappush(heap, (s, -e, pri, repl_b, tok, tok_it))

        if not heap:
            return []

        merged: list[tuple[int, int, bytes, str | None]] = []
        s, neg_e, cur_pri, cur_repl, cur_end_tok, it = heapq.heappop(heap)
        cur_s, cur_e = s, -neg_e
        if it is not None:
            nxt = next(it, None)
            if nxt is not None:
                ns, ne, npri, nrepl, ntok = nxt
                heapq.heappush(heap, (ns, -ne, npri, nrepl, ntok, it))

        while heap:
            s, neg_e, pri, repl, end_tok, it = heapq.heappop(heap)
            e = -neg_e
            if it is not None:
                nxt = next(it, None)
                if nxt is not None:
                    ns, ne, npri, nrepl, ntok = nxt
                    heapq.heappush(heap, (ns, -ne, npri, nrepl, ntok, it))

            if s < cur_e:
                if e > cur_e:
                    cur_e = e
                if pri < cur_pri:
                    cur_pri = pri
                    cur_repl = repl
                cur_end_tok = _prefer_end_token(cur_end_tok, end_tok)
            else:
                merged.append((cur_s, cur_e, cur_repl, cur_end_tok))
                cur_s, cur_e, cur_pri, cur_repl, cur_end_tok = (
                    s,
                    e,
                    pri,
                    repl,
                    end_tok,
                )

        merged.append((cur_s, cur_e, cur_repl, cur_end_tok))
        return merged

    def _process(self, buf: bytes, *, covered: int, final: bool) -> bytes:
        n = len(buf)
        merged = self._collect_merged_spans(buf, covered=covered)
        out: list[bytes] = []
        pos = 0

        if final:
            for ms, me, repl, _ in merged:
                if ms > pos:
                    out.append(buf[pos:ms])
                if repl:
                    out.append(repl)
                pos = me
            if pos < n:
                out.append(buf[pos:])
            return b"".join(out)

        tok_keep = _unclosed_token_prefix_len(buf, 0)
        keep = max(self._lit_keep, tok_keep)
        hold_start = max(0, n - keep)

        for ms, me, repl, end_token in merged:
            # An unbounded token reaching the end of the chunk can keep growing
            # in subsequent chunks regardless of hold_start (e.g. when lit_keep == 0).
            if me == n and end_token is not None:
                if (n - ms) > self._lit_keep:
                    if ms > pos:
                        out.append(buf[pos:ms])
                    if repl:
                        out.append(repl)
                    if self._lit_keep > 0:
                        self._tail = buf[max(ms, n - self._lit_keep) : n]
                        self._covered_prefix = len(self._tail)
                    else:
                        self._tail = b""
                        self._covered_prefix = 0
                    self._swallowing = end_token
                    return b"".join(out)
                # Short token prefix within lit_keep: hold from min(ms, hold_start)
                # so a literal starting before the token and completing in the next
                # chunk is never prematurely emitted as plaintext.
                emit_limit = min(ms, hold_start)
                if emit_limit > pos:
                    out.append(buf[pos:emit_limit])
                self._tail = buf[emit_limit:]
                self._covered_prefix = (
                    covered - emit_limit if pos == 0 and emit_limit < covered else 0
                )
                return b"".join(out)

            if me <= hold_start:
                # Completely before hold-back boundary: no future incomplete
                # literal or unclosed token prefix can overlap [ms, me).
                if ms > pos:
                    out.append(buf[pos:ms])
                if repl:
                    out.append(repl)
                pos = me
                continue

            if ms < hold_start:
                # Interval straddles hold_start.
                if repl == b"" or (me - ms) > self._lit_keep:
                    # Already-covered prefix or long merged span: emit now and
                    # keep buf[hold_start:] with covered_prefix = me - hold_start
                    # so tail memory stays O(lit_keep + tok_keep).
                    if ms > pos:
                        out.append(buf[pos:ms])
                    if repl:
                        out.append(repl)
                    self._tail = buf[hold_start:]
                    self._covered_prefix = me - hold_start
                    return b"".join(out)

                # Short span straddling hold_start: retain from ms so an
                # overlapping longer literal in the next chunk merges with the
                # exact same marker as whole-buffer redaction.
                if ms > pos:
                    out.append(buf[pos:ms])
                self._tail = buf[ms:]
                self._covered_prefix = covered if ms == 0 else 0
                return b"".join(out)

            # ms >= hold_start: emit safe plaintext before hold_start and hold the rest.
            if hold_start > pos:
                out.append(buf[pos:hold_start])
            self._tail = buf[hold_start:]
            self._covered_prefix = (
                covered - hold_start if pos == 0 and hold_start < covered else 0
            )
            return b"".join(out)

        if hold_start > pos:
            out.append(buf[pos:hold_start])
        self._tail = buf[hold_start:]
        self._covered_prefix = 0
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
