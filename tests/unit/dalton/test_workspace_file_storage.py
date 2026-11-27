"""Unit tests for workspace-file-storage Batch 1 step 1.

Covers:
1. StreamingSecretRedactor (8 MiB boundary UTF-8 non-ASCII, Latin-1, overlapping
   secrets, 16 KiB unbounded tokens, unclosed prefixes + 64 KiB digit cap,
   200+ randomized chunk equivalence vs redact_bytes, _secrets shape lock).
2. Stopped-workspace backup_large_download (8 KiB split UTF-8 char sniff,
   binary passthrough with Content-Length, read-only colleague 403, first-chunk
   503, 16-slot budget 503 Retry-After + release, boundary pass-through).
3. Public share shared_large_download (always redacted including binary,
   Cache-Control: no-store, first-chunk exception -> 404).
4. SQL 1-based inline slicing (offset + 1) and resolve_stored_byte_range dispatch.
5. system:dalton-company storage_user_id mapping across all 4 key builders,
   round-trip fetch/GC consistency, and reserved sys_ namespace rejection.
6. Hardened backfill (--stats-only with storage off, skipped PUT still verified,
   same-length corrupt object re-upload repair & failure isolation, skipped_lossy,
   CAS skipped_raced, company owner mapping, and --reverse >100 MiB content_binary).
"""

from __future__ import annotations

import hashlib
import importlib.util
import random
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from fastapi import HTTPException

from ptc_agent.core.sandbox.runtime import STREAM_CHUNK_BYTES
from src.dalton import company_identity
from src.dalton import storage_keys
from src.dalton import streaming_redactor as sr_mod
from src.dalton import workspace_file_stream as wfs_mod
from src.dalton import wsfiles_backfill as bf_mod
from src.dalton.streaming_redactor import (
    MAX_TOKEN_PREFIX_DIGITS,
    StreamingSecretRedactor,
    redact_stream,
)
from src.dalton.workspace_file_stream import (
    StoredStreamSizeMismatchError,
    StoredStreamVersionChangedError,
    backup_large_download,
    get_inline_file_byte_range,
    resolve_stored_byte_range,
    shared_large_download,
    stream_stored_file,
)
from src.server.app.workspace_files import _shared as ws_shared
from src.server.app.workspace_files.backup_reads import download_from_backup
from src.server.database import workspace_file_blobs as blobs_mod
from src.server.database.blob_keys import (
    INLINE_MAX_BYTES,
    RELAY_MAX_BYTES,
    BlobError,
    blob_key,
    upload_key,
)
from src.server.services import memo_binary_storage, skill_archive_storage
from src.server.services.persistence import resolve as resolve_mod
from src.server.services.persistence.resolve import (
    SERVE_MAX_BYTES,
    FileBytesUnavailable,
)
from src.server.services.workspace_file_containment import UTF8_SNIFF_BYTES
from src.server.utils.secret_redactor import SecretRedactor
from src.utils import storage as storage_facade


def _make_upstream_redactor(secrets: list[tuple[str, str]]) -> SecretRedactor:
    """Create a real SecretRedactor instance with controlled global _secrets."""
    inst = SecretRedactor.__new__(SecretRedactor)
    inst._secrets = sorted(secrets, key=lambda kv: len(kv[1]), reverse=True)
    return inst


async def _collect_stream(body_iter) -> bytes:
    parts: list[bytes] = []
    async for chunk in body_iter:
        parts.append(chunk)
    return b"".join(parts)


async def _aiter_chunks(chunks: list[bytes]):
    for ch in chunks:
        yield ch


def _load_backfill_script():
    name = "_backfill_script_dalton_unit"
    path = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "ops"
        / "backfill_workspace_file_blobs.py"
    )
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 1. StreamingSecretRedactor
# ---------------------------------------------------------------------------


def test_secret_redactor_secrets_attribute_shape_lock():
    """Lock upstream SecretRedactor._secrets existence and list[tuple[str, str]] shape."""
    with patch("src.config.tool_settings._get_agent_config_dict", return_value={}):
        redactor = SecretRedactor()
    assert hasattr(redactor, "_secrets")
    assert isinstance(redactor._secrets, list)
    for item in redactor._secrets:
        assert isinstance(item, tuple) and len(item) == 2
        assert isinstance(item[0], str) and isinstance(item[1], str)


def test_streaming_redactor_fails_closed_on_missing_or_malformed_secrets():
    """Never silently degrade to unredacted output if upstream renames _secrets."""
    broken_missing = SimpleNamespace()
    with patch.object(sr_mod, "get_redactor", return_value=broken_missing):
        with pytest.raises(RuntimeError, match="_secrets"):
            StreamingSecretRedactor()

    broken_shape = SimpleNamespace(_secrets={"API_KEY": "secret123456"})
    with patch.object(sr_mod, "get_redactor", return_value=broken_shape):
        with pytest.raises(TypeError, match="list"):
            StreamingSecretRedactor()

    broken_entry = SimpleNamespace(_secrets=[("API_KEY", 12345678)])
    with patch.object(sr_mod, "get_redactor", return_value=broken_entry):
        with pytest.raises(TypeError, match="tuples"):
            StreamingSecretRedactor()


@pytest.mark.asyncio
async def test_streaming_redactor_utf8_non_ascii_secret_across_8mib_boundary():
    """Non-ASCII UTF-8 secret straddling an 8 MiB chunk boundary matches redact_bytes."""
    secret_val = "密钥_café_秘密987654"
    secret_bytes = secret_val.encode("utf-8")
    redactor = _make_upstream_redactor([("GLOBAL_CN", secret_val)])

    eight_mib = 8 * 1024 * 1024
    # Place the secret so half of it is before 8 MiB and half after.
    prefix_len = eight_mib - (len(secret_bytes) // 2)
    payload = b"A" * prefix_len + secret_bytes + b"B" * 4096

    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        stream_r = StreamingSecretRedactor(vault_secrets=None)
        chunks = [payload[:eight_mib], payload[eight_mib:]]
        out_feed = stream_r.feed(chunks[0]) + stream_r.feed(chunks[1]) + stream_r.flush()
        out_async = await _collect_stream(
            redact_stream(_aiter_chunks(chunks), vault_secrets=None)
        )

    expected = redactor.redact_bytes(payload)
    assert out_feed == expected
    assert out_async == expected
    assert secret_bytes not in out_feed
    assert b"[REDACTED:GLOBAL_CN]" in out_feed


def test_streaming_redactor_latin1_file_non_ascii_secret():
    """Latin-1 file (invalid UTF-8) with non-ASCII secret matches redact_bytes."""
    secret_val = "café_sëcret_42"
    redactor = _make_upstream_redactor([])
    vault = {"LATIN_KEY": secret_val}

    # Include invalid UTF-8 bytes (\xff\xfe) so redact_bytes takes its latin-1 fallback.
    payload = b"\xff\xfeheader:" + secret_val.encode("latin-1") + b":\xe9tail"

    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        sr = StreamingSecretRedactor(vault_secrets=vault)
        # Split right inside the latin-1 secret
        split = 12
        out = sr.feed(payload[:split]) + sr.feed(payload[split:]) + sr.flush()

    expected = redactor.redact_bytes(payload, vault_secrets=vault)
    assert out == expected
    assert secret_val.encode("latin-1") not in out
    assert b"[REDACTED:LATIN_KEY]" in out


def test_streaming_redactor_overlapping_secrets_leave_zero_residue():
    """Overlapping secrets leave zero bytes of any secret across chunk boundaries."""
    redactor = _make_upstream_redactor([("S_SHORT", "ALPHA12345")])
    vault = {
        "S_LONG": "ALPHA12345BETA",
        "S_OVERLAP": "12345BETA67890",
    }
    payload = b"pre ALPHA12345BETA67890 post ALPHA12345 end"

    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        sr = StreamingSecretRedactor(vault_secrets=vault)
        pieces = [payload[i : i + 5] for i in range(0, len(payload), 5)]
        out = b"".join(sr.feed(p) for p in pieces) + sr.flush()

    for val in ("ALPHA12345", "ALPHA12345BETA", "12345BETA67890"):
        assert val.encode("utf-8") not in out
    assert b"[REDACTED:S_LONG]" in out
    assert b"[REDACTED:S_SHORT]" in out


@pytest.mark.asyncio
async def test_streaming_redactor_unbounded_16kib_tokens_and_prefix_boundaries():
    """16 KiB sandbox & setup tokens split across chunks and prefixes at chunk tail."""
    redactor = _make_upstream_redactor([("VAULT_X", "mysecret_9999")])
    sandbox_tok = b"gxsa_" + (b"A1._-" * 3300)  # > 16 KiB
    setup_tok = b"sk-ant-oat01-" + (b"Z9_-" * 4200)  # > 16 KiB
    assert len(sandbox_tok) > 16 * 1024
    assert len(setup_tok) > 16 * 1024

    # Construct explicit splits right at unclosed prefixes (`gxsa_`, `sk-ant-oat12`, `sk-ant-oat12-`).
    chunks = [
        b"start ",
        b"gxsa_",
        sandbox_tok[5:4096],
        sandbox_tok[4096:],
        b" mid sk-ant-oat12",
        b"-",
        setup_tok[len(b"sk-ant-oat01-") : 4096],
        setup_tok[4096:],
        b" tail gxs",
        b"r_short_tok! end",
    ]

    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        out = await _collect_stream(redact_stream(_aiter_chunks(chunks)))

    assert out == (
        b"start [REDACTED:SANDBOX_TOKEN] mid [REDACTED:CLAUDE_SETUP_TOKEN] "
        b"tail [REDACTED:SANDBOX_TOKEN]! end"
    )
    assert b"gxsa_" not in out
    assert b"gxsr_" not in out
    assert b"sk-ant-oat" not in out


def test_streaming_redactor_unclosed_setup_prefix_digit_cap_64kib():
    """Digit run > 64 KiB after ``sk-ant-oat`` is flushed as non-token to bound memory."""
    redactor = _make_upstream_redactor([])
    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        sr = StreamingSecretRedactor()
        # Feed prefix + 64 KiB + 10 digits with no closing dash yet.
        huge_digits = b"sk-ant-oat" + (b"7" * (MAX_TOKEN_PREFIX_DIGITS + 10))
        out1 = sr.feed(huge_digits)
        # Must NOT hold the entire >64 KiB prefix in tail memory.
        assert len(sr._tail) < 64
        out2 = sr.feed(b"!done") + sr.flush()
    assert out1 + out2 == huge_digits + b"!done"


def test_streaming_redactor_randomized_chunk_equivalence_200_trials():
    """200+ fixed-seed random chunkings match redact_bytes byte-for-byte (UTF-8 & Latin-1)."""
    rng = random.Random(20261126)
    redactor = _make_upstream_redactor(
        [
            ("MCP_ASCII", "mcp_secret_token_987654"),
            ("MCP_UTF8", "全局密钥_é_8888"),
        ]
    )
    vault = {
        "VAULT_ONE": "vault_secret_alpha_112233",
        "VAULT_LATIN": "clé_secrète_445566",
        "TOO_SHORT": "1234567",  # < MIN_REDACT_SECRET_LEN, must not redact
    }

    utf8_payload = (
        "head 1234567 mcp_secret_token_987654 middle "
        "全局密钥_é_8888 gxsa_tok_123.45-6 "
        "vault_secret_alpha_112233 sk-ant-oat99-abc_DEF-01 "
        "clé_secrète_445566 end"
    ).encode("utf-8")
    expected_utf8 = redactor.redact_bytes(utf8_payload, vault_secrets=vault)

    latin1_payload = (
        b"\xff\xfe binary-ish "
        + "clé_secrète_445566".encode("latin-1")
        + b" \xe9 gxsr_abc-1.2 "
        + b"mcp_secret_token_987654 "
        + b"sk-ant-oat01-xyz_99 \x80end"
    )
    expected_latin1 = redactor.redact_bytes(latin1_payload, vault_secrets=vault)

    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        for trial in range(220):
            data = utf8_payload if trial % 2 == 0 else latin1_payload
            expected = expected_utf8 if trial % 2 == 0 else expected_latin1
            sr = StreamingSecretRedactor(vault_secrets=vault)
            pos = 0
            out_parts: list[bytes] = []
            while pos < len(data):
                step = rng.randint(1, 29)
                out_parts.append(sr.feed(data[pos : pos + step]))
                pos += step
            out_parts.append(sr.flush())
            assert b"".join(out_parts) == expected, f"mismatch on trial {trial}"


def _run_streaming_chunks(
    data: bytes,
    split_sizes: list[int],
    *,
    vault_secrets: dict[str, str] | None = None,
) -> bytes:
    sr = StreamingSecretRedactor(vault_secrets=vault_secrets)
    parts: list[bytes] = []
    pos = 0
    for step in split_sizes:
        if pos >= len(data):
            break
        parts.append(sr.feed(data[pos : pos + step]))
        pos += step
    if pos < len(data):
        parts.append(sr.feed(data[pos:]))
    parts.append(sr.flush())
    return b"".join(parts)


def test_streaming_redactor_staggered_overlapping_secrets_whole_split_and_latin1():
    """Staggered overlapping secrets leave zero secret fragments across whole, arbitrary splits & Latin-1."""
    s_short = "ABCDEFGH"
    s_long = "Hpassword-987654"
    s_short_l1 = "ABCDÉFGH"
    s_long_l1 = "Hpasswörd-987654"
    non_overlap = "plain_secret_8888"

    redactor = _make_upstream_redactor([("SHORT", s_short), ("SHORT_L1", s_short_l1)])
    vault = {
        "LONG": s_long,
        "LONG_L1": s_long_l1,
        "PLAIN": non_overlap,
    }

    utf8_payload = (
        f"head {s_short}{s_long[1:]} mid {non_overlap} tail"
    ).encode("utf-8")
    latin1_payload = (
        b"\xff\xfe"
        + s_short_l1.encode("latin-1")
        + s_long_l1[1:].encode("latin-1")
        + b" "
        + non_overlap.encode("latin-1")
        + b"\x80end"
    )

    rng = random.Random(20261127)
    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        for payload, forbidden in (
            (
                utf8_payload,
                (
                    b"ABCDEFGH",
                    b"ABCDEFG",
                    b"Hpassword-987654",
                    b"password-987654",
                    non_overlap.encode("utf-8"),
                ),
            ),
            (
                latin1_payload,
                (
                    s_short_l1.encode("latin-1"),
                    s_short_l1[:-1].encode("latin-1"),
                    s_long_l1.encode("latin-1"),
                    s_long_l1[1:].encode("latin-1"),
                    non_overlap.encode("latin-1"),
                ),
            ),
        ):
            whole_out = _run_streaming_chunks(payload, [len(payload)], vault_secrets=vault)
            for frag in forbidden:
                assert frag not in whole_out
            assert b"[REDACTED:PLAIN]" in whole_out
            # Non-overlapping suffix/prefix matches redact_bytes.
            expected_plain = redactor.redact_bytes(
                b" mid " + non_overlap.encode("utf-8") + b" tail", vault_secrets=vault
            )
            if payload is utf8_payload:
                assert whole_out.endswith(expected_plain)

            # Every single split point and 1-byte chunks produce identical output.
            for cut in range(1, len(payload)):
                out_cut = _run_streaming_chunks(
                    payload, [cut, len(payload) - cut], vault_secrets=vault
                )
                assert out_cut == whole_out, f"mismatch at cut={cut}"
            assert (
                _run_streaming_chunks(payload, [1] * len(payload), vault_secrets=vault)
                == whole_out
            )
            for _ in range(50):
                steps = [rng.randint(1, 7) for _ in range(len(payload))]
                assert (
                    _run_streaming_chunks(payload, steps, vault_secrets=vault)
                    == whole_out
                )


def test_streaming_redactor_token_before_literal_and_swallow_bridge():
    """Token starting earlier than literal (gxsa_prod_key+SUPER) never leaks literal suffix."""
    secret_ascii = "prod_key+SUPER"
    secret_latin1 = "pröd_key+SÜPER"
    redactor = _make_upstream_redactor([])
    vault = {"PROD": secret_ascii, "PROD_L1": secret_latin1}

    # Short overlap: token prefix fits inside lit_keep.
    payload_short = b"pre gxsa_prod_key+SUPER post"
    # Long overlap: token runs > lit_keep before literal bridges out of swallow state!
    payload_long_swallow = b"pre gxsa_" + (b"Z" * 128) + b"prod_key+SUPER post"
    # Latin-1 input where non-ASCII byte terminates sandbox token right inside literal.
    payload_latin1 = (
        b"\xff\xfe gxsa_abc" + secret_latin1.encode("latin-1") + b" \x80tail"
    )

    rng = random.Random(20261128)
    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        for payload, forbidden in (
            (
                payload_short,
                (b"gxsa_", b"prod_key", b"+SUPER", b"SUPER"),
            ),
            (
                payload_long_swallow,
                (b"gxsa_", b"ZZZZ", b"prod_key", b"+SUPER", b"SUPER"),
            ),
            (
                payload_latin1,
                (
                    b"gxsa_",
                    secret_latin1.encode("latin-1"),
                    b"+S\xdcPER",
                    b"S\xdcPER",
                ),
            ),
        ):
            whole_out = _run_streaming_chunks(payload, [len(payload)], vault_secrets=vault)
            for frag in forbidden:
                assert frag not in whole_out
            assert whole_out.startswith(payload[:3])
            assert whole_out.endswith(payload[-4:])

            # Every 2-way split + 1-byte split + random splits leave zero fragments.
            for cut in range(1, len(payload)):
                out_cut = _run_streaming_chunks(
                    payload, [cut, len(payload) - cut], vault_secrets=vault
                )
                for frag in forbidden:
                    assert frag not in out_cut, f"leaked {frag!r} at cut={cut}"
                if payload is payload_short:
                    assert out_cut == whole_out

            out_1b = _run_streaming_chunks(
                payload, [1] * len(payload), vault_secrets=vault
            )
            for frag in forbidden:
                assert frag not in out_1b

            for _ in range(50):
                steps = [rng.randint(1, 9) for _ in range(len(payload))]
                out_rand = _run_streaming_chunks(payload, steps, vault_secrets=vault)
                for frag in forbidden:
                    assert frag not in out_rand


def test_streaming_redactor_literal_overlapping_token_leaves_zero_token_tail():
    """Literal grabbing token prefix (or inside token) merges spans so zero token tail leaks."""
    lit_grab_sb = "mysecret_gxsa"
    lit_grab_setup = "lead_sk-ant-oat01"
    lit_inside = "prod_key"
    lit_l1 = "café_gxsa"

    redactor = _make_upstream_redactor(
        [
            ("GRAB_SB", lit_grab_sb),
            ("GRAB_SETUP", lit_grab_setup),
            ("INSIDE", lit_inside),
            ("GRAB_L1", lit_l1),
        ]
    )

    utf8_payload = (
        b"A "
        + b"mysecret_gxsa_tok_987654321 "
        + b"lead_sk-ant-oat01-xyz_999888 "
        + b"gxsa_prod_key_tail_777666 "
        + b"Z"
    )
    latin1_payload = (
        b"\xff\xfe " + lit_l1.encode("latin-1") + b"_secret_tail_555 \x80Z"
    )

    rng = random.Random(20261129)
    with patch.object(sr_mod, "get_redactor", return_value=redactor):
        for payload, forbidden in (
            (
                utf8_payload,
                (
                    b"mysecret",
                    b"gxsa_",
                    b"tok_987654321",
                    b"lead_sk-ant-oat01",
                    b"xyz_999888",
                    b"prod_key",
                    b"tail_777666",
                ),
            ),
            (
                latin1_payload,
                (lit_l1.encode("latin-1"), b"gxsa_", b"secret_tail_555"),
            ),
        ):
            whole_out = _run_streaming_chunks(payload, [len(payload)])
            for frag in forbidden:
                assert frag not in whole_out

            assert _run_streaming_chunks(payload, [1] * len(payload)) == whole_out
            for _ in range(60):
                steps = [rng.randint(1, 11) for _ in range(len(payload))]
                out_rand = _run_streaming_chunks(payload, steps)
                assert out_rand == whole_out
                for frag in forbidden:
                    assert frag not in out_rand


# ---------------------------------------------------------------------------
# 2. SQL inline slicing & resolve_stored_byte_range
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_inline_file_byte_range_passes_offset_plus_one():
    """Verify PostgreSQL 1-based offset+1 is passed to SQL execute."""
    recorded: list[tuple[str, tuple]] = []

    class _FakeCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, sql, params):
            recorded.append((" ".join(sql.split()), params))

        async def fetchone(self):
            return {"chunk": b"hello"}

    class _FakeConn:
        def cursor(self, row_factory=None):
            return _FakeCursor()

    out_bin = await get_inline_file_byte_range(
        "ws-1", "a.bin", 0, 5, inline_kind="binary", conn=_FakeConn()
    )
    out_txt = await get_inline_file_byte_range(
        "ws-1", "a.txt", 100, 16, inline_kind="text", conn=_FakeConn()
    )
    out_auto = await get_inline_file_byte_range(
        "ws-1", "a.dat", 42, 8, conn=_FakeConn()
    )

    assert out_bin == b"hello"
    assert out_txt == b"hello"
    assert out_auto == b"hello"

    sql_bin, params_bin = recorded[0]
    assert "substring(content_binary FROM %s FOR %s)" in sql_bin
    assert params_bin == (1, 5, "ws-1", "a.bin")

    sql_txt, params_txt = recorded[1]
    assert "substring(convert_to(content_text, 'UTF8') FROM %s FOR %s)" in sql_txt
    assert params_txt == (101, 16, "ws-1", "a.txt")

    sql_auto, params_auto = recorded[2]
    assert "convert_to(content_text, 'UTF8')" in sql_auto
    assert params_auto == (43, 8, 43, 8, "ws-1", "a.dat")


@pytest.mark.asyncio
async def test_resolve_stored_byte_range_dispatches_and_reassembles():
    """Inline rows go to get_inline_file_byte_range; pointer rows to resolve_file_byte_range."""
    inline_data = "你好世界-inline-payload-0123456789".encode("utf-8")
    blob_data = b"BLOB-BYTES-" * 20

    inline_stub = create_autospec(get_inline_file_byte_range, spec_set=True)

    async def _fake_inline(
        ws_id,
        path,
        offset,
        length,
        *,
        inline_kind=None,
        expected_file_id=wfs_mod._UNSET,
        expected_updated_at=wfs_mod._UNSET,
        expected_content_hash=wfs_mod._UNSET,
        conn=None,
    ):
        return inline_data[offset : offset + length]

    inline_stub.side_effect = _fake_inline

    pointer_stub = create_autospec(resolve_mod.resolve_file_byte_range, spec_set=True)

    async def _fake_pointer(file_record, offset, length, *, user_id):
        # Must never contain stray inline keys.
        assert "content_binary" not in file_record
        assert "content_text" not in file_record
        return blob_data[offset : offset + length], len(blob_data)

    pointer_stub.side_effect = _fake_pointer

    inline_meta = {
        "file_path": "notes.txt",
        "file_size": len(inline_data),
        "inline_kind": "text",
        "blob_sha256": None,
        "pack_sha256": None,
    }
    pointer_meta = {
        "file_path": "big.bin",
        "file_size": len(blob_data),
        "inline_kind": None,
        "blob_sha256": hashlib.sha256(blob_data).hexdigest(),
        "pack_sha256": None,
        "content_binary": b"should-be-stripped",
    }

    with (
        patch.object(wfs_mod, "get_inline_file_byte_range", inline_stub),
        patch.object(wfs_mod, "resolve_file_byte_range", pointer_stub),
    ):
        re_inline = b""
        for off in range(0, len(inline_data) + 10, 7):
            re_inline += await resolve_stored_byte_range(
                "ws-1", inline_meta, off, 7, user_id="u1"
            )
        assert re_inline == inline_data

        re_blob = b""
        for off in range(0, len(blob_data) + 10, 19):
            re_blob += await resolve_stored_byte_range(
                "ws-1", pointer_meta, off, 19, user_id="u1"
            )
        assert re_blob == blob_data


# ---------------------------------------------------------------------------
# 3. Stopped-workspace streaming download (backup_large_download)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backup_large_download_unknown_mime_split_utf8_at_8kib_redacts_later_secret():
    """Unknown MIME with multi-byte UTF-8 char split at 8 KiB still sniffs as text and redacts."""
    fake_size = SERVE_MAX_BYTES + 16_384  # > 100 MiB, <= 256 MiB
    secret_val = "VAULT_SECRET_XYZ_987654"
    # Build 8192-byte head ending on the first 2 bytes of a 3-byte UTF-8 char '中' (\xe4\xb8\xad).
    utf8_char = "中".encode("utf-8")
    assert len(utf8_char) == 3
    head_8k = b"a" * (UTF8_SNIFF_BYTES - 2) + utf8_char[:2]
    assert len(head_8k) == UTF8_SNIFF_BYTES
    second_chunk = utf8_char[2:] + b" secret=" + secret_val.encode("utf-8") + b"!"
    # Pad remainder logically in the stub so total equals fake_size without allocating 100 MiB.
    tail_pad_len = fake_size - len(head_8k) - len(second_chunk)

    meta = {
        "file_path": "exports/data.unknownext",
        "file_name": "data.unknownext",
        "file_size": fake_size,
        "content_hash": "a" * 64,
        "blob_sha256": "a" * 64,
        "pack_sha256": None,
        "pack_offset": None,
        "mime_type": "application/octet-stream",
        "is_binary": False,
        "inline_kind": None,
    }

    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True)
    meta_stub.return_value = meta

    range_stub = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)

    async def _fake_range(ws_id, file_meta, offset, length, *, user_id, conn=None):
        if offset == 0:
            return head_8k[:length]
        if offset == UTF8_SNIFF_BYTES:
            # Return second_chunk + small filler up to remaining size in chunks
            return second_chunk
        rem = fake_size - offset
        if rem <= 0:
            return b""
        # Yield in compact chunks up to length to keep test fast while summing to fake_size.
        take = min(length, rem)
        return b"x" * take

    range_stub.side_effect = _fake_range

    vault_stub = create_autospec(wfs_mod.get_vault_secrets_for_redaction, spec_set=True)
    vault_stub.return_value = {"MY_API_KEY": secret_val}
    redactor = _make_upstream_redactor([])

    with (
        patch.object(wfs_mod, "get_large_file_meta", meta_stub),
        patch.object(wfs_mod, "resolve_stored_byte_range", range_stub),
        patch.object(wfs_mod, "get_vault_secrets_for_redaction", vault_stub),
        patch.object(sr_mod, "get_redactor", return_value=redactor),
        # Use a large fake stride? Actually 100 MiB in 8 MiB chunks is only 13 chunks (~50ms)!
    ):
        resp = await backup_large_download(
            "ws-1",
            {"user_id": "owner-1"},
            "exports/data.unknownext",
            "attachment",
        )
        assert resp is not None
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "private, no-cache"
        assert "content-length" not in resp.headers
        assert "etag" not in resp.headers
        assert "attachment" in resp.headers["content-disposition"]

        body_head_and_secret = bytearray()
        total_out = 0
        async for chunk in resp.body_iterator:
            total_out += len(chunk)
            if len(body_head_and_secret) < UTF8_SNIFF_BYTES + 256:
                body_head_and_secret.extend(chunk)

    sample = bytes(body_head_and_secret)
    assert secret_val.encode("utf-8") not in sample
    assert b"[REDACTED:MY_API_KEY]" in sample
    # Split UTF-8 character across 8 KiB boundary survived intact!
    assert utf8_char in sample[UTF8_SNIFF_BYTES - 4 : UTF8_SNIFF_BYTES + 4]
    expected_delta = len(b"[REDACTED:MY_API_KEY]") - len(secret_val.encode("utf-8"))
    assert total_out == fake_size + expected_delta
    assert tail_pad_len > 0


@pytest.mark.asyncio
async def test_backup_large_download_binary_passthrough_has_content_length():
    """Binary file (>100 MiB) passes through unredacted with exact Content-Length."""
    fake_size = SERVE_MAX_BYTES + 1024
    bin_head = b"\x89PNG\r\n\x1a\n\xff\xfe\x00\x01" + b"\xff" * (UTF8_SNIFF_BYTES - 12)
    meta = {
        "file_path": "archive.bin",
        "file_name": "archive.bin",
        "file_size": fake_size,
        "content_hash": "b" * 64,
        "blob_sha256": "b" * 64,
        "pack_sha256": None,
        "pack_offset": None,
        "mime_type": "application/octet-stream",
        "is_binary": True,
        "inline_kind": None,
    }

    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True)
    meta_stub.return_value = meta
    range_stub = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)

    async def _fake_range(ws_id, file_meta, offset, length, *, user_id, conn=None):
        if offset == 0:
            return bin_head[:length]
        rem = fake_size - offset
        return b"\xab" * min(length, rem) if rem > 0 else b""

    range_stub.side_effect = _fake_range
    vault_stub = create_autospec(wfs_mod.get_vault_secrets_for_redaction, spec_set=True)

    with (
        patch.object(wfs_mod, "get_large_file_meta", meta_stub),
        patch.object(wfs_mod, "resolve_stored_byte_range", range_stub),
        patch.object(wfs_mod, "get_vault_secrets_for_redaction", vault_stub),
    ):
        resp = await backup_large_download(
            "ws-1", {"user_id": "owner-1"}, "archive.bin", "inline"
        )
        assert resp is not None
        assert resp.headers["content-length"] == str(fake_size)
        assert resp.headers["cache-control"] == "private, no-cache"
        assert "etag" not in resp.headers
        vault_stub.assert_not_called()

        seen = 0
        async for chunk in resp.body_iterator:
            seen += len(chunk)
        assert seen == fake_size


@pytest.mark.asyncio
async def test_backup_large_download_readonly_colleague_binary_403():
    """Read-only colleague gets 403 for binary files (both metadata and sniff)."""
    fake_size = SERVE_MAX_BYTES + 1024
    # Case A: is_binary=True
    meta_flag = {
        "file_path": "report.pdf",
        "file_name": "report.pdf",
        "file_size": fake_size,
        "blob_sha256": "c" * 64,
        "pack_sha256": None,
        "mime_type": "application/pdf",
        "is_binary": True,
        "inline_kind": None,
    }
    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True)
    meta_stub.return_value = meta_flag

    with patch.object(wfs_mod, "get_large_file_meta", meta_stub):
        with pytest.raises(HTTPException) as exc:
            await backup_large_download(
                "ws-1",
                {"user_id": "owner-1"},
                "report.pdf",
                "attachment",
                is_non_owner_reader=True,
            )
    assert exc.value.status_code == 403
    assert exc.value.detail == "Binary file download is not available in read-only mode"

    # Case B: is_binary=False & unknown extension, but non-UTF-8 head fails sniff
    meta_sniff = {
        "file_path": "mystery.dat",
        "file_name": "mystery.dat",
        "file_size": fake_size,
        "blob_sha256": "d" * 64,
        "pack_sha256": None,
        "mime_type": "application/octet-stream",
        "is_binary": False,
        "inline_kind": None,
    }
    meta_stub.return_value = meta_sniff
    range_stub = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)
    range_stub.return_value = b"\xff\xfe\x80\x81" * 100

    with (
        patch.object(wfs_mod, "get_large_file_meta", meta_stub),
        patch.object(wfs_mod, "resolve_stored_byte_range", range_stub),
    ):
        with pytest.raises(HTTPException) as exc2:
            await backup_large_download(
                "ws-1",
                {"user_id": "owner-1"},
                "mystery.dat",
                "attachment",
                is_non_owner_reader=True,
            )
    assert exc2.value.status_code == 403
    assert exc2.value.detail == "Binary file download is not available in read-only mode"


@pytest.mark.asyncio
async def test_backup_large_download_first_chunk_unavailable_503():
    """First-chunk FileBytesUnavailable returns 503 before headers."""
    meta = {
        "file_path": "big.log",
        "file_name": "big.log",
        "file_size": SERVE_MAX_BYTES + 2048,
        "blob_sha256": "e" * 64,
        "pack_sha256": None,
        "mime_type": "text/plain",
        "is_binary": False,
        "inline_kind": None,
    }
    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True)
    meta_stub.return_value = meta
    range_stub = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)
    range_stub.side_effect = FileBytesUnavailable("MinIO timeout")

    with (
        patch.object(wfs_mod, "get_large_file_meta", meta_stub),
        patch.object(wfs_mod, "resolve_stored_byte_range", range_stub),
    ):
        with pytest.raises(HTTPException) as exc:
            await backup_large_download(
                "ws-1", {"user_id": "owner-1"}, "big.log", "attachment"
            )
    assert exc.value.status_code == 503
    assert exc.value.detail == "File content temporarily unavailable"


@pytest.mark.asyncio
async def test_backup_large_download_slot_exhaustion_503_and_release():
    """When all 16 stream slots are full, answer 503 Retry-After; release slot on stream end."""
    fake_size = SERVE_MAX_BYTES + 128
    meta = {
        "file_path": "big.bin",
        "file_name": "big.bin",
        "file_size": fake_size,
        "blob_sha256": "f" * 64,
        "pack_sha256": None,
        "mime_type": "application/octet-stream",
        "is_binary": True,
        "inline_kind": None,
    }
    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True)
    meta_stub.return_value = meta
    range_stub = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)

    async def _fake_range(ws_id, file_meta, offset, length, *, user_id, conn=None):
        if offset == 0:
            return b"\xff\xfe" * (UTF8_SNIFF_BYTES // 2)
        rem = fake_size - offset
        return b"\x00" * min(length, rem) if rem > 0 else b""

    range_stub.side_effect = _fake_range

    budget = ws_shared.streamed_download_budget()
    holds = []
    # Occupy 15 of 16 slots externally, leaving exactly 1 slot free.
    for _ in range(15):
        cm = budget.hold(STREAM_CHUNK_BYTES)
        await cm.__aenter__()
        holds.append(cm)

    try:
        with (
            patch.object(wfs_mod, "get_large_file_meta", meta_stub),
            patch.object(wfs_mod, "resolve_stored_byte_range", range_stub),
            patch.object(wfs_mod, "_STREAM_SLOT_WAIT_S", 0.05),
        ):
            # Take the 16th slot with one streaming download.
            resp1 = await backup_large_download(
                "ws-1", {"user_id": "owner-1"}, "big.bin", "attachment"
            )
            assert resp1 is not None

            # 17th concurrent download must fail with 503 Retry-After.
            with pytest.raises(HTTPException) as exc:
                await backup_large_download(
                    "ws-1", {"user_id": "owner-1"}, "big.bin", "attachment"
                )
            assert exc.value.status_code == 503
            assert exc.value.headers and "Retry-After" in exc.value.headers

            # Finish streaming resp1 -> its slot must be released.
            async for _ in resp1.body_iterator:
                pass

            # Now another download can acquire the released slot.
            resp2 = await backup_large_download(
                "ws-1", {"user_id": "owner-1"}, "big.bin", "attachment"
            )
            assert resp2 is not None
            async for _ in resp2.body_iterator:
                pass
    finally:
        for cm in holds:
            await cm.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_backup_large_download_returns_none_outside_window_and_on_range():
    """<= 100 MiB, > 256 MiB, and Range requests return None to hand back to upstream."""
    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True)

    # Range request -> None immediately (without even querying metadata).
    with patch.object(wfs_mod, "get_large_file_meta", meta_stub):
        assert (
            await backup_large_download(
                "ws-1",
                {"user_id": "u1"},
                "a.bin",
                "inline",
                byte_range=(0, 1023),
            )
            is None
        )
    meta_stub.assert_not_called()

    # Exact 100 MiB boundary -> None
    meta_stub.return_value = {"file_size": SERVE_MAX_BYTES, "file_path": "a.bin"}
    with patch.object(wfs_mod, "get_large_file_meta", meta_stub):
        assert (
            await backup_large_download("ws-1", {"user_id": "u1"}, "a.bin", "inline")
            is None
        )

    # Over 256 MiB -> None
    meta_stub.return_value = {"file_size": RELAY_MAX_BYTES + 1, "file_path": "a.bin"}
    with patch.object(wfs_mod, "get_large_file_meta", meta_stub):
        assert (
            await backup_large_download("ws-1", {"user_id": "u1"}, "a.bin", "inline")
            is None
        )


@pytest.mark.asyncio
async def test_download_from_backup_hook_delegates_large_file():
    """Upstream backup_reads.download_from_backup invokes backup_large_download hook."""
    sentinel = SimpleNamespace(status_code=200, marker="streamed")
    hook_stub = create_autospec(backup_large_download, spec_set=True)
    hook_stub.return_value = sentinel
    fake_request = SimpleNamespace(headers={})

    with patch.object(wfs_mod, "backup_large_download", hook_stub):
        result = await download_from_backup(
            "ws-1",
            {"user_id": "u1"},
            "reports/big.csv",
            "/home/workspace",
            (),
            None,
            fake_request,
            "attachment",
        )
    assert result is sentinel
    hook_stub.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_stored_file_aborts_on_size_mismatch():
    """Mid-stream truncation logs ERROR and raises StoredStreamSizeMismatchError."""
    meta = {"file_path": "trunc.bin", "file_size": 1000, "blob_sha256": "a" * 64}
    range_stub = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)
    range_stub.side_effect = [b"x" * 400, b""]

    with patch.object(wfs_mod, "resolve_stored_byte_range", range_stub):
        with pytest.raises(StoredStreamSizeMismatchError):
            await _collect_stream(
                stream_stored_file("ws-1", meta, user_id="u1")
            )


@pytest.mark.asyncio
async def test_inline_stream_version_pin_aborts_on_midstream_overwrite():
    """Overwriting an inline file mid-stream raises StoredStreamVersionChangedError (and 503/404 pre-header)."""
    from datetime import datetime, timezone

    fid = str(uuid.uuid4())
    ts_v1 = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    v1_body = b"plain-text-line-" * 2048  # 32 KiB
    v1_hash = hashlib.sha256(v1_body).hexdigest()

    # Simulated DB row that gets overwritten with same-length binary bytes after slice 0!
    state = {
        "workspace_file_id": fid,
        "updated_at": ts_v1,
        "content_hash": v1_hash,
        "content_text": v1_body.decode("utf-8"),
        "content_binary": None,
    }
    recorded_sqls: list[tuple[str, tuple]] = []

    class _VersionedConn:
        def cursor(self, row_factory=None):
            class _Cur:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

                async def execute(self, sql, params):
                    recorded_sqls.append((" ".join(sql.split()), params))
                    self._last = params

                async def fetchone(self):
                    pg_from, length, ws_id, fpath, exp_fid, exp_ts, exp_hash = self._last
                    if (
                        state["workspace_file_id"] != exp_fid
                        or state["updated_at"] != exp_ts
                        or state["content_hash"] != exp_hash
                        or state["content_text"] is None
                    ):
                        return None
                    raw = state["content_text"].encode("utf-8")
                    start = pg_from - 1
                    return {"chunk": raw[start : start + length]}

            return _Cur()

    meta = {
        "workspace_file_id": fid,
        "file_path": "report.txt",
        "file_name": "report.txt",
        "file_size": len(v1_body),
        "content_hash": v1_hash,
        "updated_at": ts_v1,
        "blob_sha256": None,
        "pack_sha256": None,
        "pack_offset": None,
        "mime_type": "text/plain",
        "is_binary": False,
        "inline_kind": "text",
    }

    conn = _VersionedConn()
    first = await resolve_stored_byte_range(
        "ws-1", meta, 0, UTF8_SNIFF_BYTES, user_id="u1", conn=conn
    )
    assert first == v1_body[:UTF8_SNIFF_BYTES]
    assert "workspace_file_id = %s" in recorded_sqls[0][0]
    assert "updated_at IS NOT DISTINCT FROM %s" in recorded_sqls[0][0]
    assert "content_hash IS NOT DISTINCT FROM %s" in recorded_sqls[0][0]
    assert "content_text IS NOT NULL" in recorded_sqls[0][0]

    # Overwrite same path with same-length binary bytes mid-download!
    v2_bin = b"\xff\xfe\x00\x01" * (len(v1_body) // 4)
    state["content_text"] = None
    state["content_binary"] = v2_bin
    state["content_hash"] = hashlib.sha256(v2_bin).hexdigest()
    state["updated_at"] = datetime(2026, 11, 26, 12, 0, 5, tzinfo=timezone.utc)

    # Next slice via stream_stored_file must abort and never yield v2_bin bytes!
    emitted: list[bytes] = []
    with pytest.raises(StoredStreamVersionChangedError):
        async for ch in stream_stored_file(
            "ws-1", meta, user_id="u1", first_chunk=first, conn=conn
        ):
            emitted.append(ch)
    assert b"".join(emitted) == first
    assert b"\xff\xfe" not in b"".join(emitted)

    # Pre-header version change maps to 503 in backup_large_download and 404 in shared_large_download.
    large_meta = {**meta, "file_size": SERVE_MAX_BYTES + 4096}
    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True, return_value=large_meta)
    range_stub = create_autospec(
        wfs_mod.resolve_stored_byte_range,
        spec_set=True,
        side_effect=StoredStreamVersionChangedError("overwritten before sniff"),
    )
    with (
        patch.object(wfs_mod, "get_large_file_meta", meta_stub),
        patch.object(wfs_mod, "resolve_stored_byte_range", range_stub),
    ):
        with pytest.raises(HTTPException) as exc_backup:
            await backup_large_download(
                "ws-1", {"user_id": "owner-1"}, "report.txt", "attachment"
            )
        assert exc_backup.value.status_code == 503

        with pytest.raises(HTTPException) as exc_share:
            await shared_large_download(
                "ws-1", "owner-1", "report.txt", not_found_detail="File not found"
            )
        assert exc_share.value.status_code == 404


# ---------------------------------------------------------------------------
# 4. Public share streaming download (shared_large_download)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_large_download_always_redacts_binary_and_no_store():
    """Public share streams binary files through StreamingSecretRedactor with no-store."""
    fake_size = SERVE_MAX_BYTES + 2048
    secret_val = "SHARED_SECRET_999888"
    bin_head = b"\x89PNG\r\n\x1a\n\xff\xfe" + secret_val.encode("utf-8")
    bin_head = bin_head + b"\x00" * (UTF8_SNIFF_BYTES - len(bin_head))

    meta = {
        "file_path": "deck.pdf",
        "file_name": "deck.pdf",
        "file_size": fake_size,
        "blob_sha256": "1" * 64,
        "pack_sha256": None,
        "mime_type": "application/pdf",
        "is_binary": True,
        "inline_kind": None,
    }
    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True)
    meta_stub.return_value = meta
    range_stub = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)

    async def _fake_range(ws_id, file_meta, offset, length, *, user_id, conn=None):
        if offset == 0:
            return bin_head[:length]
        rem = fake_size - offset
        return b"\x00" * min(length, rem) if rem > 0 else b""

    range_stub.side_effect = _fake_range
    vault_stub = create_autospec(wfs_mod.get_vault_secrets_for_redaction, spec_set=True)
    vault_stub.return_value = {"DOC_SECRET": secret_val}
    redactor = _make_upstream_redactor([])

    with (
        patch.object(wfs_mod, "get_large_file_meta", meta_stub),
        patch.object(wfs_mod, "resolve_stored_byte_range", range_stub),
        patch.object(wfs_mod, "get_vault_secrets_for_redaction", vault_stub),
        patch.object(sr_mod, "get_redactor", return_value=redactor),
    ):
        resp = await shared_large_download(
            "ws-1", "owner-1", "deck.pdf", not_found_detail="Shared resource not found"
        )
        assert resp is not None
        assert resp.headers["cache-control"] == "no-store"
        assert "content-length" not in resp.headers
        assert "etag" not in resp.headers

        first_bytes = bytearray()
        async for chunk in resp.body_iterator:
            if len(first_bytes) < UTF8_SNIFF_BYTES:
                first_bytes.extend(chunk)

    assert secret_val.encode("utf-8") not in first_bytes
    assert b"[REDACTED:DOC_SECRET]" in first_bytes


@pytest.mark.asyncio
async def test_shared_large_download_first_chunk_error_collapses_to_404():
    """Any storage failure on first chunk folds into 404 with caller's detail."""
    meta = {
        "file_path": "deck.pdf",
        "file_name": "deck.pdf",
        "file_size": SERVE_MAX_BYTES + 4096,
        "blob_sha256": "2" * 64,
        "pack_sha256": None,
        "mime_type": "application/pdf",
        "is_binary": True,
        "inline_kind": None,
    }
    meta_stub = create_autospec(wfs_mod.get_large_file_meta, spec_set=True)
    meta_stub.return_value = meta
    range_stub = create_autospec(wfs_mod.resolve_stored_byte_range, spec_set=True)
    range_stub.side_effect = FileBytesUnavailable("S3 down")

    with (
        patch.object(wfs_mod, "get_large_file_meta", meta_stub),
        patch.object(wfs_mod, "resolve_stored_byte_range", range_stub),
    ):
        with pytest.raises(HTTPException) as exc:
            await shared_large_download(
                "ws-1",
                "owner-1",
                "deck.pdf",
                not_found_detail="Shared resource not found",
            )
    assert exc.value.status_code == 404
    assert exc.value.detail == "Shared resource not found"


# ---------------------------------------------------------------------------
# 5. system:dalton-company storage key mapping
# ---------------------------------------------------------------------------


def test_storage_user_id_mapping_all_four_builders_and_reserved_rejection():
    """COMPANY_SYSTEM_USER_ID maps to sys_dalton-company across all 4 builders; sys_* raises."""
    assert storage_keys.COMPANY_SYSTEM_USER_ID == company_identity.COMPANY_SYSTEM_USER_ID

    company_uid = company_identity.COMPANY_SYSTEM_USER_ID
    digest = "a" * 64
    up_id = str(uuid.uuid4())
    skill_hash = f"sha256:{digest}"

    assert blob_key(company_uid, digest) == f"blobs/sys_dalton-company/{digest}"
    assert upload_key(company_uid, up_id) == f"uploads/sys_dalton-company/{up_id}"
    memo_k = memo_binary_storage._build_storage_key(company_uid, "application/pdf")
    assert memo_k.startswith("memo/sys_dalton-company/") and memo_k.endswith(".pdf")
    assert (
        skill_archive_storage.build_storage_key(company_uid, skill_hash)
        == f"user-skills/sys_dalton-company/{digest}.zip"
    )

    # Regular UUID and shared-{uuid} are unchanged.
    normal_uid = str(uuid.uuid4())
    shared_uid = f"shared-{uuid.uuid4()}"
    assert storage_keys.storage_user_id(normal_uid) == normal_uid
    assert storage_keys.storage_user_id(shared_uid) == shared_uid
    assert blob_key(normal_uid, digest) == f"blobs/{normal_uid}/{digest}"
    assert blob_key(shared_uid, digest) == f"blobs/{shared_uid}/{digest}"

    # Reserved sys_* namespace raises BlobError on every builder.
    for bad in ("sys_x", "sys_dalton-company"):
        with pytest.raises(BlobError):
            storage_keys.storage_user_id(bad)
        with pytest.raises(BlobError):
            blob_key(bad, digest)
        with pytest.raises(BlobError):
            upload_key(bad, up_id)
        with pytest.raises(BlobError):
            memo_binary_storage._build_storage_key(bad, "application/pdf")
        with pytest.raises(BlobError):
            skill_archive_storage.build_storage_key(bad, skill_hash)


def test_storage_keys_module_stays_import_clean():
    """storage_keys.py is imported by blob_keys.py so it must stay free of app config imports."""
    import ast

    tree = ast.parse(Path(storage_keys.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imported <= {"__future__", "src.server.database.blob_keys"}


@pytest.mark.asyncio
async def test_company_system_user_roundtrip_fetch_and_gc_delete_same_key():
    """fetch_blob, fetch_blob_range, and GC _reap_one all hit blobs/sys_dalton-company/<sha>."""
    company_uid = company_identity.COMPANY_SYSTEM_USER_ID
    payload = b"company-automation-file-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    expected_key = f"blobs/sys_dalton-company/{digest}"

    get_stub = create_autospec(storage_facade.get_bytes, spec_set=True)
    get_stub.return_value = payload
    range_stub = create_autospec(storage_facade.get_bytes_range, spec_set=True)
    range_stub.return_value = payload[0:7]
    del_stub = create_autospec(storage_facade.delete_object, spec_set=True)
    del_stub.return_value = True

    class _ReapConn:
        def __init__(self) -> None:
            self._fetchone_answers = iter([(1,), None])

        @asynccontextmanager
        async def transaction(self):
            yield

        @asynccontextmanager
        async def cursor(self):
            conn = self

            class _Cur:
                async def execute(self, sql, params=None):
                    pass

                async def fetchone(self):
                    return next(conn._fetchone_answers)

            yield _Cur()

    with (
        patch.object(blobs_mod, "_storage_get_bytes", get_stub),
        patch.object(blobs_mod, "_storage_get_bytes_range", range_stub),
        patch.object(blobs_mod, "_storage_delete_object", del_stub),
    ):
        assert await blobs_mod.fetch_blob(company_uid, digest) == payload
        assert await blobs_mod.fetch_blob_range(company_uid, digest, 0, 7) == payload[:7]
        assert await blobs_mod._reap_one(_ReapConn(), company_uid, digest, 24) is True

    get_stub.assert_called_once_with(expected_key)
    range_stub.assert_called_once_with(expected_key, 0, 7)
    del_stub.assert_called_once_with(expected_key)


# ---------------------------------------------------------------------------
# 6. Hardened backfill & --reverse > 100 MiB
# ---------------------------------------------------------------------------


class _FakeBackfillConn:
    """In-memory async connection double for wsfiles_backfill unit tests."""

    def __init__(self, rows: dict[str, dict], *, cas_rowcount: int = 1) -> None:
        self.rows = rows
        self.cas_rowcount = cas_rowcount
        self.claim_calls: list[tuple] = []
        self.register_calls: list[tuple] = []
        self.repoint_calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @asynccontextmanager
    async def transaction(self):
        yield

    @asynccontextmanager
    async def cursor(self, row_factory=None):
        conn = self

        class _Cur:
            rowcount = 0

            async def execute(self, sql, params=None):
                norm = " ".join(sql.split())
                if "UPDATE workspace_file_blobs" in norm:
                    conn.claim_calls.append(params)
                    self.rowcount = 1
                elif "INSERT INTO workspace_file_blobs" in norm:
                    conn.register_calls.append(params)
                    self.rowcount = 1
                elif "UPDATE workspace_files" in norm:
                    conn.repoint_calls.append(params)
                    self.rowcount = conn.cas_rowcount

        yield _Cur()


def _make_inline_row(
    *,
    user_id: str | None = None,
    content_text: str | None = "hello world",
    content_binary: bytes | None = None,
    content_hash: str | None = None,
    file_path: str = "a.txt",
) -> dict:
    fid = str(uuid.uuid4())
    uid = user_id or str(uuid.uuid4())
    raw = content_binary if content_binary is not None else (content_text or "").encode("utf-8")
    digest = content_hash if content_hash is not None else hashlib.sha256(raw).hexdigest()
    return {
        "workspace_file_id": fid,
        "workspace_id": str(uuid.uuid4()),
        "user_id": uid,
        "file_path": file_path,
        "content_hash": digest,
        "content_text": content_text,
        "content_binary": content_binary,
        "blob_sha256": None,
        "pack_sha256": None,
        "pack_offset": None,
        "file_size": len(raw),
        "is_binary": content_binary is not None,
    }


@pytest.mark.asyncio
async def test_backfill_stats_only_runs_when_storage_disabled_and_flags_bad_owner():
    """--stats-only skips is_storage_enabled(), never touches storage, detects collisions/lossy."""
    script = _load_backfill_script()
    good_row = _make_inline_row(user_id=company_identity.COMPANY_SYSTEM_USER_ID, content_text="ok")
    lossy_row = _make_inline_row(
        content_text="stripped", content_hash="0" * 64, file_path="lossy.txt"
    )
    rows = {good_row["workspace_file_id"]: good_row, lossy_row["workspace_file_id"]: lossy_row}
    conn = _FakeBackfillConn(rows)

    pages = [list(rows.keys()), []]

    async def _next_ids(c, after, limit, ws_id):
        return pages.pop(0)

    async def _load_row(c, fid):
        return rows[fid]

    next_stub = create_autospec(bf_mod._next_inline_ids, spec_set=True, side_effect=_next_ids)
    load_stub = create_autospec(bf_mod._load_row, spec_set=True, side_effect=_load_row)
    is_enabled_stub = create_autospec(script.is_storage_enabled, spec_set=True, return_value=False)
    connect_stub = create_autospec(
        script.psycopg.AsyncConnection.connect, spec_set=True, return_value=conn
    )

    with (
        patch.object(bf_mod, "_next_inline_ids", next_stub),
        patch.object(bf_mod, "_load_row", load_stub),
        patch.object(script, "is_storage_enabled", is_enabled_stub),
        patch.object(script.psycopg.AsyncConnection, "connect", connect_stub),
    ):
        rc = await script._run(
            apply=False,
            reverse=False,
            batch=100,
            workspace_id=None,
            include_legacy_owners=True,
            stats_only=True,
        )
    assert rc == 0
    is_enabled_stub.assert_not_called()

    # Now add a reserved sys_* owner -> must fail with non-zero exit code.
    bad_row = _make_inline_row(user_id="sys_reserved_bad", content_text="boom")
    bad_rows = {bad_row["workspace_file_id"]: bad_row}
    bad_pages = [list(bad_rows.keys()), []]
    next_stub2 = create_autospec(
        bf_mod._next_inline_ids, spec_set=True, side_effect=lambda *a, **k: bad_pages.pop(0)
    )
    load_stub2 = create_autospec(
        bf_mod._load_row, spec_set=True, side_effect=lambda c, fid: bad_rows[fid]
    )
    with (
        patch.object(bf_mod, "_next_inline_ids", next_stub2),
        patch.object(bf_mod, "_load_row", load_stub2),
    ):
        rc_bad = await bf_mod.run_stats_only(_FakeBackfillConn(bad_rows))
    assert rc_bad == 1


@pytest.mark.asyncio
async def test_backfill_skip_put_still_verifies_sha256_and_repairs_corrupt_object():
    """Skip PUT on matching size still runs sha256_object; corrupt object re-uploads & heals."""
    r_ok = _make_inline_row(content_text="already-stored-good", file_path="good.txt")
    r_corrupt = _make_inline_row(content_text="same-len-bad-bytes!", file_path="bad.txt")
    rows = {r_ok["workspace_file_id"]: r_ok, r_corrupt["workspace_file_id"]: r_corrupt}
    conn = _FakeBackfillConn(rows)

    k_ok = blob_key(r_ok["user_id"], r_ok["content_hash"])
    k_bad = blob_key(r_corrupt["user_id"], r_corrupt["content_hash"])

    store: dict[str, bytes] = {
        k_ok: b"already-stored-good",
        k_bad: b"X" * len(b"same-len-bad-bytes!"),  # same length, wrong digest!
    }

    size_stub = create_autospec(
        storage_facade.object_size,
        spec_set=True,
        side_effect=lambda k: len(store[k]) if k in store else None,
    )
    put_stub = create_autospec(storage_facade.upload_bytes, spec_set=True)

    def _fake_put(key, data, content_type=None, max_size=None):
        store[key] = data
        return True

    put_stub.side_effect = _fake_put
    sha_stub = create_autospec(
        storage_facade.sha256_object,
        spec_set=True,
        side_effect=lambda k, **kw: hashlib.sha256(store[k]).hexdigest() if k in store else None,
    )

    pages = [list(rows.keys()), []]
    next_stub = create_autospec(
        bf_mod._next_inline_ids, spec_set=True, side_effect=lambda *a, **k: pages.pop(0)
    )
    load_stub = create_autospec(
        bf_mod._load_row, spec_set=True, side_effect=lambda c, fid: rows[fid]
    )

    with (
        patch.object(bf_mod, "_next_inline_ids", next_stub),
        patch.object(bf_mod, "_load_row", load_stub),
        patch.object(storage_facade, "object_size", size_stub),
        patch.object(storage_facade, "upload_bytes", put_stub),
        patch.object(storage_facade, "sha256_object", sha_stub),
    ):
        rc = await bf_mod.run_forward(conn, apply=True)

    assert rc == 0
    # k_ok skipped PUT, k_bad skipped initial PUT then re-uploaded once after digest mismatch!
    assert put_stub.call_count == 1
    assert put_stub.call_args.args[0] == k_bad
    # sha256_object called once for k_ok (even though PUT was skipped!) and twice for k_bad.
    called_sha_keys = [c.args[0] for c in sha_stub.call_args_list]
    assert called_sha_keys.count(k_ok) == 1
    assert called_sha_keys.count(k_bad) == 2
    assert store[k_bad] == b"same-len-bad-bytes!"
    assert len(conn.repoint_calls) == 2


@pytest.mark.asyncio
async def test_backfill_persistent_corrupt_fails_and_keeps_inline():
    """If digest stays wrong after re-upload, failed+=1, exit code 1, row stays inline."""
    row = _make_inline_row(content_text="always-corrupt-store")
    rows = {row["workspace_file_id"]: row}
    conn = _FakeBackfillConn(rows)

    size_stub = create_autospec(storage_facade.object_size, spec_set=True, return_value=None)
    put_stub = create_autospec(storage_facade.upload_bytes, spec_set=True, return_value=True)
    sha_stub = create_autospec(
        storage_facade.sha256_object, spec_set=True, return_value="f" * 64
    )

    pages = [list(rows.keys()), []]
    next_stub = create_autospec(
        bf_mod._next_inline_ids, spec_set=True, side_effect=lambda *a, **k: pages.pop(0)
    )
    load_stub = create_autospec(
        bf_mod._load_row, spec_set=True, side_effect=lambda c, fid: rows[fid]
    )

    with (
        patch.object(bf_mod, "_next_inline_ids", next_stub),
        patch.object(bf_mod, "_load_row", load_stub),
        patch.object(storage_facade, "object_size", size_stub),
        patch.object(storage_facade, "upload_bytes", put_stub),
        patch.object(storage_facade, "sha256_object", sha_stub),
    ):
        rc = await bf_mod.run_forward(conn, apply=True)

    assert rc == 1
    assert conn.repoint_calls == []
    assert put_stub.call_count == 2  # initial PUT + 1 retry
    assert sha_stub.call_count == 2


@pytest.mark.asyncio
async def test_backfill_lossy_raced_and_company_owner_mapping():
    """Verify skipped_lossy, CAS skipped_raced, and system:dalton-company mapped key + raw DB user_id."""
    company_uid = company_identity.COMPANY_SYSTEM_USER_ID
    r_company = _make_inline_row(
        user_id=company_uid, content_text="company-report", file_path="co.md"
    )
    r_lossy = _make_inline_row(
        content_text="nul-stripped", content_hash="1" * 64, file_path="lossy.txt"
    )
    rows = {
        r_company["workspace_file_id"]: r_company,
        r_lossy["workspace_file_id"]: r_lossy,
    }
    # cas_rowcount=0 simulates concurrent rewrite -> skipped_raced
    conn = _FakeBackfillConn(rows, cas_rowcount=0)
    store: dict[str, bytes] = {}

    size_stub = create_autospec(storage_facade.object_size, spec_set=True, return_value=None)
    put_stub = create_autospec(
        storage_facade.upload_bytes,
        spec_set=True,
        side_effect=lambda k, d, ct=None, max_size=None: (store.__setitem__(k, d) or True),
    )
    sha_stub = create_autospec(
        storage_facade.sha256_object,
        spec_set=True,
        side_effect=lambda k, **kw: hashlib.sha256(store[k]).hexdigest(),
    )

    pages = [list(rows.keys()), []]
    next_stub = create_autospec(
        bf_mod._next_inline_ids, spec_set=True, side_effect=lambda *a, **k: pages.pop(0)
    )
    load_stub = create_autospec(
        bf_mod._load_row, spec_set=True, side_effect=lambda c, fid: rows[fid]
    )

    with (
        patch.object(bf_mod, "_next_inline_ids", next_stub),
        patch.object(bf_mod, "_load_row", load_stub),
        patch.object(storage_facade, "object_size", size_stub),
        patch.object(storage_facade, "upload_bytes", put_stub),
        patch.object(storage_facade, "sha256_object", sha_stub),
        patch.object(bf_mod.logger, "info") as log_info,
    ):
        rc = await bf_mod.run_forward(conn, apply=True, include_legacy_owners=True)

    assert rc == 0
    expected_key = f"blobs/sys_dalton-company/{r_company['content_hash']}"
    assert expected_key in store
    # DB registry & claim MUST retain raw 'system:dalton-company', not mapped id.
    assert conn.claim_calls == [(company_uid, [r_company["content_hash"]])]
    assert conn.register_calls == [
        (company_uid, r_company["content_hash"], len(b"company-report"))
    ]
    # Summary log reports skipped_lossy=1 and skipped_raced=1, moved=0.
    summary_args = log_info.call_args.args
    rendered = summary_args[0] % summary_args[1:]
    assert "moved=0" in rendered
    assert "skipped_raced=1" in rendered
    assert "skipped_lossy=1" in rendered


def test_reverse_choose_inline_target_keeps_large_text_in_content_binary_with_original_flag():
    """--reverse for (100 MiB, 256 MiB] writes content_binary and preserves is_binary=False."""
    script = _load_backfill_script()
    large_text = b"a" * (INLINE_MAX_BYTES + 16)
    text_col, bin_col, final_is_bin, relabelled = bf_mod.choose_reverse_inline_target(
        large_text, False, script._inline_columns
    )
    assert text_col is None
    assert bin_col is large_text
    assert final_is_bin is False
    assert relabelled is False

    # Small clean UTF-8 text still restores to content_text as before.
    small_text = b"hello"
    t2, b2, f2, r2 = bf_mod.choose_reverse_inline_target(
        small_text, False, script._inline_columns
    )
    assert t2 == "hello" and b2 is None and f2 is False and r2 is False


@pytest.mark.asyncio
async def test_backfill_second_batch_put_invalidates_verified_digest_and_rejects_bad_bytes():
    """After batch 1 PUT->verify->CAS, a batch 2 PUT writing same-length bad bytes must NOT CAS-clear."""
    uid = str(uuid.uuid4())
    payload = "shared-content-across-two-batches"
    r1 = _make_inline_row(user_id=uid, content_text=payload, file_path="batch1.txt")
    r2 = _make_inline_row(user_id=uid, content_text=payload, file_path="batch2.txt")
    assert r1["content_hash"] == r2["content_hash"]

    rows = {r1["workspace_file_id"]: r1, r2["workspace_file_id"]: r2}
    conn = _FakeBackfillConn(rows)
    store: dict[str, bytes] = {}
    put_calls = 0

    # Batch 1 page returns r1; between batch 1 and batch 2 the object vanishes
    # so batch 2 performs a fresh PUT that writes same-length corrupt bytes.
    pages = [[r1["workspace_file_id"]], [r2["workspace_file_id"]], []]

    async def _next_ids(c, after, limit, ws_id):
        if len(pages) == 2:
            # Right before batch 2 starts, remove the object so batch 2 must PUT again.
            store.clear()
        return pages.pop(0)

    def _fake_put(key, data, content_type=None, max_size=None):
        nonlocal put_calls
        put_calls += 1
        if put_calls == 1:
            store[key] = data  # Batch 1 writes good bytes
        else:
            # Batch 2 (and its Step-3 retry) silently writes same-length corrupt bytes
            store[key] = b"X" * len(data)
        return True

    size_stub = create_autospec(
        storage_facade.object_size,
        spec_set=True,
        side_effect=lambda k: len(store[k]) if k in store else None,
    )
    put_stub = create_autospec(
        storage_facade.upload_bytes, spec_set=True, side_effect=_fake_put
    )
    sha_stub = create_autospec(
        storage_facade.sha256_object,
        spec_set=True,
        side_effect=lambda k, **kw: (
            hashlib.sha256(store[k]).hexdigest() if k in store else None
        ),
    )
    next_stub = create_autospec(
        bf_mod._next_inline_ids, spec_set=True, side_effect=_next_ids
    )
    load_stub = create_autospec(
        bf_mod._load_row, spec_set=True, side_effect=lambda c, fid: rows[fid]
    )

    with (
        patch.object(bf_mod, "_next_inline_ids", next_stub),
        patch.object(bf_mod, "_load_row", load_stub),
        patch.object(storage_facade, "object_size", size_stub),
        patch.object(storage_facade, "upload_bytes", put_stub),
        patch.object(storage_facade, "sha256_object", sha_stub),
    ):
        rc = await bf_mod.run_forward(conn, apply=True, batch=1)

    assert rc == 1
    # Only row 1 was CAS-cleared; row 2 stayed inline and counted as failed.
    assert len(conn.repoint_calls) == 1
    assert conn.repoint_calls[0][2] == r1["workspace_file_id"]
    # Batch 1: 1 PUT + 1 verify. Batch 2: 1 PUT + 1 verify (mismatch) + 1 retry PUT + 1 retry verify.
    assert put_stub.call_count == 3
    assert sha_stub.call_count == 3


def test_dalton_integration_conftest_disables_dotenv_before_alembic(tmp_path):
    """The Dalton integration session fixture disables dotenv.load_dotenv for both Alembic chains."""
    import dotenv
    import dotenv.main

    from tests.integration.dalton.conftest import disable_dotenv_loading

    env_file = tmp_path / ".env"
    env_file.write_text("DALTON_DOTENV_LEAK_TEST_KEY=should_not_load\n", encoding="utf-8")

    gen = disable_dotenv_loading.__wrapped__()
    stub = next(gen)
    try:
        from dotenv import load_dotenv as imported_load_dotenv

        assert imported_load_dotenv is stub
        assert dotenv.load_dotenv is stub
        assert dotenv.main.load_dotenv is stub
        assert imported_load_dotenv(env_file) is False
        assert "DALTON_DOTENV_LEAK_TEST_KEY" not in __import__("os").environ
    finally:
        with pytest.raises(StopIteration):
            next(gen)
