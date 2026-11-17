#!/usr/bin/env python3
"""
flomo_import.py — Parse Flomo HTML export and import memos into company-wiki.

Parses the exported HTML, maps hierarchical tags (#China/电商/PDD) to company/sector,
merges same-date memos for the same target, deduplicates against existing wiki docs,
and imports with auto-tagging + embedding.

Usage:
  python3 flomo_import.py --html PATH [--images-dir PATH] [--dry-run] [--verbose]
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

# ---------------------------------------------------------------------------
# Setup — import shared functions from company_wiki_cli
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from company_wiki_cli import (
    get_genai_client, embed_texts, resolve_embedding_model, chunk_text,
    init_db, store_document, update_index, load_config, log as wiki_log,
    WORKSPACE, WIKI_DIR, COMPANIES_DIR, SECTORS_DIR, DB_PATH,
    resolve_category_name, slugify, VERBOSE as WIKI_VERBOSE, tagger_context
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_HTML = WORKSPACE / "temp" / "flomo" / "Lumos的笔记.html"
DEFAULT_IMAGES_DIR = WORKSPACE / "temp" / "flomo" / "file"

# Tag to sector mapping (2-level tags without company)
TAG_SECTOR_MAP = {
    "China/电商": "china-internet",
    "China/跨境": "china-internet",
    "China/本地生活": "china-internet",
    "China/广告": "china-internet",
    "China/招聘": "china-internet",
    "China/SaaS": "china-internet",
    "China": "china-internet",
    "China/物流": "china-logistics",
    "China/工业": "china-industrials",
    "China/旅游": "china-consumer",
    "China/消费": "china-consumer",
    "China/Fintech": "china-internet",
    "US/AI": "us-ai-semiconductor",
    "US/Ads": "us-internet",
    "US/Retailer": "us-internet",
    "US/LocalService": "us-internet",
    "US/Travel": "us-internet",
    "US/Fintech": "us-financials",
    "US/Rates": "macro",
    "US": "us-internet",
    "Market": "macro",
    "Market/Weekly": "macro",
    "Market/Earnings": "macro",
    "Market/13F": "macro",
    "Crypto": "crypto",
    "Crypto/Bitcoin": "crypto",
    "ASEAN": "em-internet",
    "ASEAN/电商": "em-internet",
    "ASEAN/本地生活": "em-internet",
    "ASEAN/物流": "em-internet",
    "LatAm": "em-internet",
    "LatAm/电商": "em-internet",
    "Accounting": "others",
    "欢迎": None,  # Skip welcome memos
    "欢迎/指南": None,
}

# Company name overrides for tags that don't match config aliases directly
TAG_COMPANY_OVERRIDES = {
    "Temu": "PDD",
    "PopMart": "POPMART",
    "ADY": "ADYEN",
    "J&T": "JT",
    "菜鸟": "BABA",
    "ATAT": "ATAT",
    "TCEL": "TCEL",
    "BZUN": "BZUN",
    "GCT": "GCT",
    "Yeahka": "YEAHKA",
    "猎聘": "LIEPIN",
    "BZ": "BZ",
    "TT": "字节跳动",
    "东方雨虹": "东方雨虹",
    "Bitcoin": None,  # Not a company — fall through to sector
}


def build_known_tags():
    """Build the set of known tags from the sector map and company overrides."""
    tags = set(TAG_SECTOR_MAP.keys())
    company_tags = [
        "ASEAN/本地生活/GRAB", "ASEAN/电商/SE", "ASEAN/电商/TT",
        "China/本地生活/美团", "China/本地生活/字节", "China/本地生活/BEKE",
        "China/本地生活/DIDI", "China/电商/快手", "China/电商/字节",
        "China/电商/BABA", "China/电商/BZUN", "China/电商/JD",
        "China/电商/PDD", "China/电商/VIPS",
        "China/工业/东方雨虹", "China/广告/字节",
        "China/招聘/BZ", "China/招聘/猎聘",
        "China/旅游/ATAT", "China/旅游/HTHT", "China/旅游/TCEL", "China/旅游/TCOM",
        "China/物流/J&T", "China/物流/YTO", "China/物流/Yunda",
        "China/物流/ZTO", "China/物流/菜鸟",
        "China/消费/LKNCY",
        "China/跨境/GCT", "China/跨境/MNSO", "China/跨境/PopMart",
        "China/跨境/SHEIN", "China/跨境/Temu",
        "China/Fintech/FUTU", "China/Fintech/TIGR", "China/Fintech/Yeahka",
        "Crypto/Bitcoin",
        "LatAm/电商/MELI",
        "Market/13F", "Market/Earnings", "Market/Weekly",
        "US/Ads/GOOGL", "US/Ads/META", "US/Ads/TT",
        "US/Fintech/ADY", "US/Fintech/AFRM", "US/Fintech/HOOD",
        "US/Fintech/MA", "US/Fintech/PYPL", "US/Fintech/SCHW",
        "US/Fintech/SQ", "US/Fintech/V",
        "US/LocalService/DASH", "US/LocalService/UBER",
        "US/Retailer/AMZN", "US/Retailer/SHOP",
    ]
    tags.update(company_tags)
    return tags


KNOWN_TAGS = build_known_tags()


# ---------------------------------------------------------------------------
# HTML Parser
# ---------------------------------------------------------------------------
class FlomoHTMLParser(HTMLParser):
    """Parse Flomo exported HTML to extract memo blocks."""

    def __init__(self):
        super().__init__()
        self.memos = []
        self._current_memo = None
        self._in_memo = False
        self._in_time = False
        self._in_content = False
        self._in_files = False
        self._content_depth = 0
        self._content_html_parts = []
        self._memo_div_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        if tag == "div" and cls == "memo":
            self._in_memo = True
            self._memo_div_depth = 1
            self._current_memo = {"time": "", "content_html": "", "images": []}
            return

        if not self._in_memo:
            return

        if tag == "div":
            self._memo_div_depth += 1

        if tag == "div" and cls == "time":
            self._in_time = True
        elif tag == "div" and cls == "content":
            self._in_content = True
            self._content_depth = 1
            self._content_html_parts = []
        elif tag == "div" and cls == "files":
            self._in_files = True
        elif tag == "img" and self._in_files:
            src = attrs_dict.get("src", "")
            if src:
                self._current_memo["images"].append(src)

        if self._in_content:
            if not (tag == "div" and cls == "content"):
                if tag == "div":
                    self._content_depth += 1
                attr_str = " ".join(f'{k}="{v}"' for k, v in attrs)
                self._content_html_parts.append(
                    f"<{tag} {attr_str}>" if attr_str else f"<{tag}>"
                )

    def handle_endtag(self, tag):
        if not self._in_memo:
            return

        if self._in_content:
            if tag == "div":
                self._content_depth -= 1
                if self._content_depth <= 0:
                    self._in_content = False
                    self._current_memo["content_html"] = "".join(self._content_html_parts)
                    # Fall through to decrement _memo_div_depth below
                else:
                    self._content_html_parts.append(f"</{tag}>")
                    return
            else:
                self._content_html_parts.append(f"</{tag}>")
                return

        if tag == "div":
            if self._in_time:
                self._in_time = False
            elif self._in_files:
                self._in_files = False

            self._memo_div_depth -= 1
            if self._memo_div_depth <= 0:
                self._in_memo = False
                if self._current_memo and self._current_memo.get("time"):
                    self.memos.append(self._current_memo)
                self._current_memo = None

    def handle_data(self, data):
        if not self._in_memo:
            return
        if self._in_time:
            self._current_memo["time"] = data.strip()
        elif self._in_content:
            self._content_html_parts.append(data)

    def handle_entityref(self, name):
        if self._in_content:
            self._content_html_parts.append(f"&{name};")

    def handle_charref(self, name):
        if self._in_content:
            self._content_html_parts.append(f"&#{name};")


# ---------------------------------------------------------------------------
# Text processing
# ---------------------------------------------------------------------------
def strip_html(html_text):
    """Strip HTML tags and return plain text."""
    text = html_text
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'</p>', '\n', text)
    text = re.sub(r'</li>', '\n', text)
    text = re.sub(r'</h[1-6]>', '\n', text)
    text = re.sub(r'</?(?:ul|ol)>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_tags_from_text(text):
    """Extract Flomo hierarchical tags from text.

    Handles messy formats like '#China/电商/PDDPDD拼多多...' where the tag
    and content run together without whitespace.

    Returns:
        (list of tag strings without #, cleaned content string)
    """
    tag_pattern = re.compile(r'#((?:[\w\u4e00-\u9fff&]+)(?:/(?:[\w\u4e00-\u9fff&]+))*)')
    tags = []
    matches = list(tag_pattern.finditer(text))
    content = text

    for match in reversed(matches):
        full_tag = match.group(1)
        resolved_tag = _resolve_tag(full_tag)
        if resolved_tag:
            tags.insert(0, resolved_tag)

        start = match.start()
        end = match.end()
        if resolved_tag and len(resolved_tag) < len(full_tag):
            extra = full_tag[len(resolved_tag):]
            content = content[:start] + extra + content[end:]
        else:
            content = content[:start] + content[end:]

    lines = [line.strip() for line in content.split('\n')]
    content = '\n'.join(lines).strip()
    content = re.sub(r'\n{3,}', '\n\n', content)

    return tags, content


def _resolve_tag(raw_tag):
    """Resolve a potentially messy tag to a known tag."""
    if raw_tag in KNOWN_TAGS:
        return raw_tag

    parts = raw_tag.split('/')
    for length in range(len(parts), 0, -1):
        candidate = '/'.join(parts[:length])
        if candidate in KNOWN_TAGS:
            return candidate

        if length >= 2:
            last_part = parts[length - 1]
            parent = '/'.join(parts[:length - 1])
            for known in KNOWN_TAGS:
                known_parts = known.split('/')
                if len(known_parts) == length:
                    known_parent = '/'.join(known_parts[:length - 1])
                    if known_parent == parent:
                        known_last = known_parts[-1]
                        if last_part.startswith(known_last) and len(last_part) > len(known_last):
                            return known

    if len(raw_tag) < 50:
        return raw_tag
    return None


# ---------------------------------------------------------------------------
# Tag → Target mapping
# ---------------------------------------------------------------------------
def map_tag_to_target(tag, config):
    """Map a Flomo tag to a wiki target.

    Returns:
        tuple (target_type, target_name) or None to skip.
    """
    parts = tag.split('/')

    if len(parts) >= 3:
        company_id = parts[-1]

        if company_id in TAG_COMPANY_OVERRIDES:
            resolved = TAG_COMPANY_OVERRIDES[company_id]
            if resolved is None:
                pass  # fall through to sector mapping
            else:
                return ("company", resolved)
        else:
            aliases = config.get("aliases", {})
            alias_map = {k.lower(): v for k, v in aliases.items()}
            if company_id.lower() in alias_map:
                return ("company", alias_map[company_id.lower()])

            if company_id.isupper() and 1 <= len(company_id) <= 5:
                return ("company", company_id)

            if company_id in aliases:
                return ("company", aliases[company_id])

            return ("company", company_id)

    for i in range(len(parts), 0, -1):
        candidate = '/'.join(parts[:i])
        if candidate in TAG_SECTOR_MAP:
            sector = TAG_SECTOR_MAP[candidate]
            if sector is None:
                return None
            return ("sector", sector)

    return ("sector", "others")


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def content_hash(text):
    """Normalized SHA256 hash for exact dedup."""
    normalized = re.sub(r'\s+', ' ', text.lower().strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def trigram_set(text):
    """Character trigrams for fuzzy matching."""
    normalized = re.sub(r'\s+', ' ', text.lower().strip())
    if len(normalized) < 3:
        return {normalized}
    return {normalized[i:i + 3] for i in range(len(normalized) - 2)}


def trigram_similarity(text1, text2):
    """Jaccard similarity of trigram sets."""
    s1 = trigram_set(text1)
    s2 = trigram_set(text2)
    if not s1 or not s2:
        return 0.0
    intersection = len(s1 & s2)
    union = len(s1 | s2)
    return intersection / union if union > 0 else 0.0


def check_duplicate(content_text, target_dir, threshold=0.90):
    """Check if similar content already exists in target directory."""
    new_hash = content_hash(content_text)
    if not target_dir.exists():
        return False

    for existing_file in target_dir.iterdir():
        if not existing_file.is_file() or existing_file.name == "index.md" or existing_file.suffix != ".md":
            continue
        try:
            existing_text = existing_file.read_text(encoding="utf-8")
            if existing_text.startswith("---"):
                end_match = re.search(r'\n---\s*\n', existing_text[3:])
                if end_match:
                    existing_text = existing_text[3 + end_match.end():]

            if content_hash(existing_text) == new_hash:
                return True

            len_ratio = min(len(content_text), len(existing_text)) / max(len(content_text), len(existing_text), 1)
            if len_ratio > 0.5:
                sim = trigram_similarity(content_text, existing_text)
                if sim > threshold:
                    return True
        except Exception:
            continue

    return False


# ---------------------------------------------------------------------------
# Image OCR
# ---------------------------------------------------------------------------
def ocr_image(image_path, config):
    """Extract text from an image using Gemini Vision."""
    if not image_path.exists():
        return ""
    try:
        client = get_genai_client()
        vision_model = config.get("vision", {}).get("model", "gemini-2.5-flash")

        suffix = image_path.suffix.lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"
        }
        mime_type = mime_map.get(suffix, "image/jpeg")

        with open(image_path, "rb") as f:
            image_data = f.read()

        from google.genai import types
        response = client.models.generate_content(
            model=vision_model,
            contents=[
                types.Content(parts=[
                    types.Part(inline_data=types.Blob(mime_type=mime_type, data=image_data)),
                    types.Part(text=(
                        "请完整提取这张图片中的所有文字和信息。"
                        "包括所有数据、表格、图表中的数字、标题、注释等。"
                        "如果有表格，请用markdown表格格式输出。"
                        "如果有图表，请描述图表内容和关键数据点。"
                        "请用原文语言输出。只输出提取的内容，不要加额外说明。"
                    ))
                ])
            ]
        )
        return response.text.strip() if response.text else ""
    except Exception as e:
        print(f"  WARNING: Vision OCR failed for {image_path.name}: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# Ghost record cleanup
# ---------------------------------------------------------------------------
def cleanup_ghosts(dry_run=False):
    """Remove DB records where the local file no longer exists."""
    conn = init_db()
    rows = conn.execute("SELECT id, filepath FROM documents").fetchall()
    ghost_ids = []

    for doc_id, filepath in rows:
        full_path = WORKSPACE / filepath
        if not full_path.exists():
            ghost_ids.append(doc_id)

    if not ghost_ids:
        print("No ghost records found.", file=sys.stderr)
        conn.close()
        return 0

    if dry_run:
        print(f"DRY RUN: Would remove {len(ghost_ids)} ghost records:", file=sys.stderr)
        for doc_id in ghost_ids[:10]:
            fp = conn.execute("SELECT filepath FROM documents WHERE id=?", (doc_id,)).fetchone()[0]
            print(f"  id={doc_id} path={fp}", file=sys.stderr)
        if len(ghost_ids) > 10:
            print(f"  ... and {len(ghost_ids) - 10} more", file=sys.stderr)
    else:
        for doc_id in ghost_ids:
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
        conn.commit()
        print(f"Cleaned {len(ghost_ids)} ghost records from DB.", file=sys.stderr)

    conn.close()
    return len(ghost_ids)


# ---------------------------------------------------------------------------
# Import plan builder
# ---------------------------------------------------------------------------
def parse_flomo_html(html_path):
    """Parse Flomo HTML and return list of memo dicts."""
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    parser = FlomoHTMLParser()
    parser.feed(html_content)

    memos = []
    for raw in parser.memos:
        plain_text = strip_html(raw["content_html"])
        tags, clean_content = extract_tags_from_text(plain_text)

        ts = raw["time"]
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            date_str = dt.strftime("%Y-%m-%d")
        except ValueError:
            date_str = ts[:10] if len(ts) >= 10 else "unknown"
            dt = None

        memos.append({
            "timestamp": ts,
            "datetime": dt,
            "date": date_str,
            "tags": tags,
            "content": clean_content,
            "content_html": raw["content_html"],
            "images": raw["images"],
        })

    return memos


def build_import_plan(memos, config, images_dir):
    """Map memos to targets and merge by (date, target).

    Returns list of import entry dicts.
    """
    memo_targets = []

    for memo in memos:
        if not memo["tags"]:
            memo_targets.append((memo, "sector", "others"))
            continue

        targets_seen = set()
        any_valid = False
        for tag in memo["tags"]:
            result = map_tag_to_target(tag, config)
            if result is None:
                continue
            any_valid = True
            target_type, target_name = result
            key = (target_type, target_name)
            if key not in targets_seen:
                targets_seen.add(key)
                memo_targets.append((memo, target_type, target_name))

        if not targets_seen:
            if not any_valid:
                # All tags mapped to None (e.g., 欢迎) — skip entirely
                continue
            memo_targets.append((memo, "sector", "others"))

    # Group by (date, target_type, target_name)
    groups = defaultdict(list)
    for memo, ttype, tname in memo_targets:
        key = (memo["date"], ttype, tname)
        groups[key].append(memo)

    plan = []
    for (date, ttype, tname), group_memos in sorted(groups.items()):
        group_memos.sort(key=lambda m: m["timestamp"])

        parts = []
        all_images = []
        for m in group_memos:
            if m["content"].strip():
                parts.append(m["content"])
            all_images.extend(m["images"])

        merged_content = "\n\n---\n\n".join(parts) if len(parts) > 1 else (parts[0] if parts else "")

        plan.append({
            "target_type": ttype,
            "target_name": tname,
            "date": date,
            "memos": group_memos,
            "content": merged_content,
            "images": all_images,
            "memo_count": len(group_memos),
        })

    return plan


# ---------------------------------------------------------------------------
# Main import
# ---------------------------------------------------------------------------
def run_import(html_path, images_dir, dry_run=False, verbose=False):
    """Main import workflow."""
    config = load_config()

    # Step 1: Clean up ghosts
    print("=" * 60, file=sys.stderr)
    print("Step 1: Cleaning ghost records...", file=sys.stderr)
    ghosts_cleaned = cleanup_ghosts(dry_run=dry_run)
    print(f"  Ghost records cleaned: {ghosts_cleaned}", file=sys.stderr)
    print("", file=sys.stderr)

    # Step 2: Parse HTML
    print("Step 2: Parsing Flomo HTML...", file=sys.stderr)
    memos = parse_flomo_html(html_path)
    print(f"  Parsed {len(memos)} memos", file=sys.stderr)

    tag_counts = defaultdict(int)
    for m in memos:
        for t in m["tags"]:
            tag_counts[t] += 1
    no_tag_count = sum(1 for m in memos if not m["tags"])
    print(f"  Memos without tags: {no_tag_count}", file=sys.stderr)
    print(f"  Unique tags: {len(tag_counts)}", file=sys.stderr)

    # Step 3: Build import plan
    print("\nStep 3: Building import plan...", file=sys.stderr)
    plan = build_import_plan(memos, config, images_dir)
    print(f"  Merged into {len(plan)} documents", file=sys.stderr)

    company_docs = sum(1 for p in plan if p["target_type"] == "company")
    sector_docs = sum(1 for p in plan if p["target_type"] == "sector")
    print(f"  Company documents: {company_docs}", file=sys.stderr)
    print(f"  Sector documents: {sector_docs}", file=sys.stderr)

    if dry_run:
        print("\n" + "=" * 60, file=sys.stderr)
        print("DRY RUN — showing import plan:", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        for i, entry in enumerate(plan, 1):
            ttype = entry["target_type"]
            tname = entry["target_name"]
            date = entry["date"]
            n_memos = entry["memo_count"]
            n_images = len(entry["images"])
            content_len = len(entry["content"])
            path_prefix = "company" if ttype == "company" else "sector"
            print(f"  {i:3d}. {path_prefix}/{tname}/{date}-Flomo笔记.md "
                  f"({n_memos} memo(s), {content_len} chars"
                  f"{f', {n_images} images' if n_images else ''})",
                  file=sys.stderr)
        print(f"\nTotal: {len(plan)} documents would be created", file=sys.stderr)
        return {
            "parsed": len(memos),
            "merged": len(plan),
            "imported": 0,
            "skipped": 0,
            "errors": 0,
            "ghosts_cleaned": ghosts_cleaned,
            "dry_run": True,
        }

    # Step 4: Import
    print("\nStep 4: Importing documents...", file=sys.stderr)
    model_name, dims = resolve_embedding_model(config)

    # Try to import auto-tagger
    tagger_available = False
    if config.get("tagger", {}).get("enabled", False):
        try:
            from auto_tagger import auto_tag, build_frontmatter, tags_to_json, related_to_json
            tagger_available = True
        except ImportError:
            print("  WARNING: auto_tagger not available, skipping auto-tagging", file=sys.stderr)

    conn = init_db()

    imported = 0
    skipped = 0
    errors = 0
    total = len(plan)

    # Track which indices need updating
    updated_dirs = set()

    for i, entry in enumerate(plan, 1):
        ttype = entry["target_type"]
        tname = entry["target_name"]
        date = entry["date"]
        content = entry["content"]
        images = entry["images"]
        n_memos = entry["memo_count"]

        base_dir = COMPANIES_DIR if ttype == "company" else SECTORS_DIR
        target_dir = base_dir / tname
        path_prefix = "companies" if ttype == "company" else "sectors"

        print(f"  [{i}/{total}] {path_prefix}/{tname}/{date}-Flomo笔记.md "
              f"({n_memos} memo(s))", file=sys.stderr, end="")

        try:
            # OCR images if any
            if images:
                ocr_parts = []
                for img_src in images:
                    img_path = images_dir / img_src.replace("file/", "", 1) if img_src.startswith("file/") else images_dir / img_src
                    if not img_path.exists():
                        # Try with the full relative path from the HTML
                        img_path = images_dir.parent / img_src
                    if img_path.exists():
                        ocr_text = ocr_image(img_path, config)
                        if ocr_text:
                            ocr_parts.append(f"\n\n[图片内容]\n{ocr_text}")
                        time.sleep(2)  # Rate limit vision calls
                if ocr_parts:
                    content = content + "\n".join(ocr_parts)

            # Skip empty content
            if not content.strip():
                print(" SKIP (empty)", file=sys.stderr)
                skipped += 1
                continue

            # Dedup check
            if check_duplicate(content, target_dir):
                print(" SKIP (duplicate)", file=sys.stderr)
                skipped += 1
                continue

            # Create target directory
            target_dir.mkdir(parents=True, exist_ok=True)

            # Auto-tag
            tag_result = None
            tags_json = None
            related_json = None
            if tagger_available:
                try:
                    tag_result = auto_tag(content, config, get_client_fn=get_genai_client, verbose=verbose,
                                          context=tagger_context(ttype, tname, "Flomo笔记", conn=conn))
                    tags_json = tags_to_json(tag_result)
                    related_json = related_to_json(tag_result)
                except Exception as e:
                    if verbose:
                        print(f" (tagger error: {e})", file=sys.stderr, end="")

            # Build file content
            content_type = "Flomo笔记"
            file_parts = []
            if tag_result and any(tag_result.get(k) for k in ("tags", "related_companies", "related_sectors")):
                file_parts.append(build_frontmatter(tag_result))

            header = f"# {content_type}\n\n"
            header += f"**日期**: {date}\n"
            header += f"**分类**: {tname} ({'公司' if ttype == 'company' else '行业'})\n"
            header += f"**来源**: Flomo ({n_memos} memo(s))\n\n"
            header += "---\n\n"
            file_parts.append(header + content)

            full_content = "".join(file_parts)

            # Generate filename
            filename = f"{date}-{content_type}.md"
            filepath = target_dir / filename

            # Handle duplicate filenames
            if filepath.exists():
                counter = 2
                while filepath.exists():
                    filename = f"{date}-{content_type}-{counter}.md"
                    filepath = target_dir / filename
                    counter += 1

            # Write file
            filepath.write_text(full_content, encoding="utf-8")

            # Chunk and embed
            chunk_size = config["embedding"].get("chunk_size", 1500)
            chunk_overlap = config["embedding"].get("chunk_overlap", 200)
            chunks = chunk_text(content, chunk_size, chunk_overlap)
            embeddings = embed_texts(chunks, model_name)

            # Store in DB
            rel_path = str(filepath.relative_to(WORKSPACE))
            store_document(conn, ttype, tname, content_type, date,
                           filename, rel_path, chunks, embeddings,
                           tags_json=tags_json, related_json=related_json)

            updated_dirs.add((target_dir, ttype, tname))
            imported += 1
            print(" OK", file=sys.stderr)

        except Exception as e:
            errors += 1
            print(f" ERROR: {e}", file=sys.stderr)
            if verbose:
                import traceback
                traceback.print_exc()

    conn.close()

    # Step 5: Update indices
    print(f"\nStep 5: Updating {len(updated_dirs)} index files...", file=sys.stderr)
    for target_dir, ttype, tname in updated_dirs:
        update_index(target_dir, ttype, tname)

    # Summary
    print("\n" + "=" * 60, file=sys.stderr)
    print("Import Summary:", file=sys.stderr)
    print(f"  Memos parsed:      {len(memos)}", file=sys.stderr)
    print(f"  Documents merged:  {total}", file=sys.stderr)
    print(f"  Imported:          {imported}", file=sys.stderr)
    print(f"  Skipped (dedup):   {skipped}", file=sys.stderr)
    print(f"  Errors:            {errors}", file=sys.stderr)
    print(f"  Ghosts cleaned:    {ghosts_cleaned}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    return {
        "parsed": len(memos),
        "merged": total,
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "ghosts_cleaned": ghosts_cleaned,
        "dry_run": False,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Import Flomo HTML export into company-wiki"
    )
    parser.add_argument("--html", type=str, default=str(DEFAULT_HTML),
                        help=f"Path to Flomo HTML export (default: {DEFAULT_HTML})")
    parser.add_argument("--images-dir", type=str, default=str(DEFAULT_IMAGES_DIR),
                        help=f"Path to Flomo images directory (default: {DEFAULT_IMAGES_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and show plan without importing")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose output")
    parser.add_argument("--json", action="store_true",
                        help="JSON output for summary")

    args = parser.parse_args()

    html_path = Path(args.html).expanduser()
    images_dir = Path(args.images_dir).expanduser()

    if not html_path.exists():
        print(f"ERROR: HTML file not found: {html_path}", file=sys.stderr)
        sys.exit(1)

    # Set verbose mode in wiki module too
    import company_wiki_cli
    company_wiki_cli.VERBOSE = args.verbose

    result = run_import(html_path, images_dir, dry_run=args.dry_run, verbose=args.verbose)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()