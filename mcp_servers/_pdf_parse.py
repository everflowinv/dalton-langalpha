"""PDF → text extraction, shared by the in-process crawler's PDF extractor.

The parse functions are the subprocess entry points for
``src/tools/web/inhouse/extractors/pdf.py``: they run in a spawn child so a
pathological PDF can be terminated at its deadline instead of pinning a
thread's GIL for minutes. That is why this module follows the same rule as
``_extract.py`` — stdlib only at import time, ``pdfplumber``/``pypdf`` imported
inside the functions — so the child's boot stays cheap and this file can also
be imported inside a sandbox.

DALTON note: registered via ``scripts/dalton/dalton_paths.txt``.
"""

from __future__ import annotations

from io import BytesIO


def pdfplumber_extract(content: bytes) -> str:
    """Page-by-page text via pdfplumber (primary parser)."""
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(BytesIO(content)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            page.close()
            if text.strip():
                pages.append(f"--- Page {i} ---\n\n{text}")
    return "\n\n".join(pages)


def pypdf_extract(content: bytes) -> str:
    """Page-by-page text via pypdf (fallback parser)."""
    import pypdf

    reader = pypdf.PdfReader(BytesIO(content))
    pages: list[str] = []
    for i, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        if text.strip():
            pages.append(f"--- Page {i} ---\n\n{text}")
    return "\n\n".join(pages)
