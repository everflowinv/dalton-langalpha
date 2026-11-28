#!/usr/bin/env python3
"""Read-only views over existing wiki and market-digest stores. No ingestion."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_CODE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_research_roots(default_code_root: Path = _DEFAULT_CODE_ROOT) -> tuple[Path, Path]:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code_root.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return code_root, home_root


CODE_ROOT, DATA_ROOT = _resolve_research_roots()
ROOT = DATA_ROOT
SOURCES = ('company-wiki', 'sales-notes', 'tmtb', 'funda-ai')
KEY_SQL = "COALESCE(NULLIF(message_id,''),NULLIF(email_id,''),'row:' || id)"


_DOTENV_DISABLED_VALUES = frozenset({"1", "true", "t", "yes", "y"})


def _parse_dotenv_stdlib(path: Path, base_env: dict[str, str]) -> dict[str, str]:
    """Pure-stdlib .env reader used only when python-dotenv is absent; never mutates os.environ."""
    resolved: dict[str, str] = {}
    var_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, raw_val = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = raw_val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            quote = val[0]
            val = val[1:-1]
            if quote == "'":
                resolved[key] = val
                continue
        else:
            val = re.sub(r"\s+#.*$", "", val).strip()
        lookup = {**resolved, **base_env}
        resolved[key] = var_re.sub(lambda m: lookup.get(m.group(1), ""), val)
    return resolved


_YAML_SIMPLE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _strip_yaml_inline_comment(value: str) -> str:
    """Strip an unquoted YAML `# ...` comment (at index 0 or following whitespace)."""
    in_single = False
    in_double = False
    for idx, ch in enumerate(value):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single and (idx == 0 or value[idx - 1] != "\\"):
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or value[idx - 1] in (" ", "\t"):
                return value[:idx].strip()
    return value.strip()


def _unquote_yaml_simple_key(raw_key: str) -> str:
    key = raw_key.strip()
    if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
        key = key[1:-1].strip()
    if not _YAML_SIMPLE_KEY_RE.fullmatch(key):
        raise ValueError(f"Unparseable YAML key without PyYAML: {raw_key!r}")
    return key.lower()


def _parse_yaml_strict_bool(raw_val: str) -> bool:
    norm = _strip_yaml_inline_comment(raw_val).lower()
    if norm == "true":
        return True
    if norm == "false":
        return False
    raise ValueError(f"Unparseable YAML boolean without PyYAML: {raw_val!r}")


def _parse_yaml_flow_capabilities(flow_text: str) -> dict[str, bool]:
    """Strictly parse a single-line YAML flow mapping like `{sales_notes: false}`; raise on anything ambiguous."""
    stripped = flow_text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        raise ValueError(f"Unsupported capabilities flow syntax without PyYAML: {flow_text!r}")
    inner = stripped[1:-1].strip()
    if not inner:
        return {}
    if any(ch in inner for ch in ("{", "}", "[", "]", "&", "*", "!", "\n")):
        raise ValueError(f"Complex capabilities flow syntax requires PyYAML: {flow_text!r}")
    out: dict[str, bool] = {}
    for raw_entry in inner.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if entry.count(":") != 1:
            raise ValueError(f"Unparseable capabilities flow entry without PyYAML: {entry!r}")
        raw_k, raw_v = entry.split(":", 1)
        out[_unquote_yaml_simple_key(raw_k)] = _parse_yaml_strict_bool(raw_v)
    return out


def _read_yaml_top_and_capabilities_fallback(text: str) -> tuple[bool | None, dict[str, bool]]:
    """Fail-closed stdlib YAML subset parser used only when PyYAML is absent.

    Handles top-level `enabled:` and `capabilities:` (block mapping with optional trailing
    comments or single-line flow mapping `{k: bool}`). Any syntax that cannot be reliably
    parsed raises `ValueError` so host-direct callers fail closed (`[]`) rather than
    falling back to Everflow defaults.
    """
    enabled_val: bool | None = None
    caps_out: dict[str, bool] = {}
    in_caps = False
    seen_caps = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        leading_ws = raw_line[: len(raw_line) - len(raw_line.lstrip(" \t"))]
        if "\t" in leading_ws:
            raise ValueError("Tab indentation in YAML requires PyYAML")
        indent = len(leading_ws)
        if indent == 0:
            in_caps = False
            if stripped in ("---", "..."):
                continue
            if stripped.startswith(("{", "[", "?", "<<:", "&", "*", "!")):
                raise ValueError(f"Unsupported top-level YAML construct without PyYAML: {stripped!r}")
            if ":" not in stripped:
                continue
            raw_k, raw_rest = stripped.split(":", 1)
            key_clean = raw_k.strip()
            if len(key_clean) >= 2 and key_clean[0] == key_clean[-1] and key_clean[0] in ("'", '"'):
                key_clean = key_clean[1:-1].strip()
            key_lower = key_clean.lower()
            if key_lower == "enabled":
                enabled_val = _parse_yaml_strict_bool(raw_rest)
            elif key_lower == "capabilities":
                if seen_caps:
                    raise ValueError("Duplicate capabilities key in YAML requires PyYAML")
                seen_caps = True
                rest_clean = _strip_yaml_inline_comment(raw_rest)
                if not rest_clean:
                    in_caps = True
                elif rest_clean.lower() in ("null", "~"):
                    in_caps = False
                elif rest_clean.startswith("{"):
                    caps_out.update(_parse_yaml_flow_capabilities(rest_clean))
                    in_caps = False
                else:
                    raise ValueError(
                        f"Unsupported capabilities value without PyYAML: {raw_rest!r}"
                    )
        elif in_caps:
            line_clean = _strip_yaml_inline_comment(stripped)
            if not line_clean:
                continue
            if ":" not in line_clean or line_clean.startswith(("-", "<<:", "&", "*", "!", "{", "[")):
                raise ValueError(
                    f"Unsupported capabilities block entry without PyYAML: {stripped!r}"
                )
            raw_k, raw_v = line_clean.split(":", 1)
            caps_out[_unquote_yaml_simple_key(raw_k)] = _parse_yaml_strict_bool(raw_v)
    return enabled_val, caps_out


def _read_yaml_top_and_capabilities(path: Path) -> tuple[bool | None, dict[str, bool]]:
    """Read optional top-level `enabled:` and `capabilities:` booleans without importing src.config."""
    if not path.is_file():
        return None, {}
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return _read_yaml_top_and_capabilities_fallback(text)

    data = yaml.safe_load(text)
    if data is None:
        return None, {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    raw_enabled = data.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ValueError(f"Invalid non-boolean 'enabled' in {path}")
    enabled = raw_enabled if isinstance(raw_enabled, bool) else None
    caps_raw = data.get("capabilities")
    if caps_raw is not None and not isinstance(caps_raw, dict):
        raise ValueError(f"Invalid non-mapping 'capabilities' in {path}")
    caps: dict[str, bool] = {}
    if isinstance(caps_raw, dict):
        for k, v in caps_raw.items():
            if not isinstance(k, str) or not isinstance(v, bool):
                raise ValueError(f"Invalid capability entry {k!r}={v!r} in {path}")
            caps[k.strip().lower()] = v
    return enabled, caps


def _resolve_host_direct_library_sources() -> list[str]:
    """Resolve deployment-enabled local library source names for host-direct calls.

    Reads only profile & capability control keys from process env, deployment `.env`,
    and `dalton.yaml` / `dalton.core.yaml` using `src.dalton.profile` and
    `src.dalton.capabilities` (never importing `src.config` / `src.dalton.settings`
    and never mutating `os.environ`). Fails closed (`[]`) if deployment config
    cannot be resolved.
    """
    try:
        repo_root = Path(__file__).resolve().parents[4]
        if not (
            (repo_root / "src" / "dalton" / "profile.py").is_file()
            and (repo_root / "src" / "dalton" / "capabilities.py").is_file()
        ):
            return []
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))

        from src.dalton.capabilities import (
            CAPABILITY_SPECS,
            CapabilityId,
            _parse_explicit_cap_env,
            is_capability_control_env_key,
        )
        from src.dalton.profile import DeploymentProfile, resolve_profile

        proc_env = dict(os.environ)
        cap_env: dict[str, str] = {
            k: v for k, v in proc_env.items() if is_capability_control_env_key(k)
        }

        dotenv_disabled_raw = proc_env.get("PYTHON_DOTENV_DISABLED")
        dotenv_disabled = (
            dotenv_disabled_raw is not None
            and str(dotenv_disabled_raw).strip().casefold() in _DOTENV_DISABLED_VALUES
        )
        if not dotenv_disabled:
            explicit_dotenv = (
                str(proc_env.get("DALTON_DOTENV_PATH") or "").strip()
                or str(proc_env.get("DALTON_REPO_ENV_FILE") or "").strip()
            )
            env_path: Path | None = None
            if explicit_dotenv:
                cand = Path(explicit_dotenv).expanduser()
                if not cand.is_file():
                    return []
                env_path = cand.resolve()
            else:
                repo_dotenv = repo_root / ".env"
                if repo_dotenv.is_file():
                    env_path = repo_dotenv.resolve()
            if env_path is not None:
                try:
                    from src.dalton.config_env import parse_and_resolve_dotenv

                    parsed_dotenv = parse_and_resolve_dotenv(
                        env_path, base_env=proc_env, override=False
                    )
                except ImportError:
                    parsed_dotenv = _parse_dotenv_stdlib(env_path, proc_env)
                for k, v in parsed_dotenv.items():
                    if is_capability_control_env_key(k) and k not in cap_env:
                        cap_env[k] = v

        active_profile = resolve_profile(
            app_env=cap_env.get("APP_ENV", ""),
            dalton_profile=cap_env.get("DALTON_PROFILE", ""),
        )

        yaml_enabled, yaml_caps = _read_yaml_top_and_capabilities(
            repo_root / "src" / "dalton" / "dalton.yaml"
        )
        if active_profile is DeploymentProfile.CORE:
            core_enabled, core_caps = _read_yaml_top_and_capabilities(
                repo_root / "src" / "dalton" / "dalton.core.yaml"
            )
            if core_enabled is not None:
                yaml_enabled = core_enabled
            yaml_caps.update(core_caps)

        if "DALTON_ENABLED" in cap_env:
            dalton_on = cap_env["DALTON_ENABLED"].strip().lower() in ("1", "true", "yes")
        else:
            dalton_on = True if yaml_enabled is None else bool(yaml_enabled)
        if not dalton_on:
            return []

        source_caps = (
            ("company-wiki", CapabilityId.COMPANY_WIKI),
            ("sales-notes", CapabilityId.SALES_NOTES),
            ("tmtb", CapabilityId.TMTB),
            ("funda-ai", CapabilityId.FUNDA_AI),
        )
        enabled: list[str] = []
        for source_name, cap_id in source_caps:
            spec = CAPABILITY_SPECS[cap_id]
            explicit = _parse_explicit_cap_env(spec.env_var, cap_env.get(spec.env_var))
            if explicit is not None:
                is_on = explicit
            elif cap_id.value in yaml_caps:
                is_on = bool(yaml_caps[cap_id.value])
            else:
                is_on = (
                    spec.core_default
                    if active_profile is DeploymentProfile.CORE
                    else spec.everflow_default
                )
            if is_on:
                enabled.append(source_name)
        return enabled
    except Exception:
        return []


def _enabled_library_sources(allowed_sources=None):
    """Resolve enabled local library sources from explicit allowlist, bridge env, or deployment config.

    When called through the bridge (`DALTON_ENABLED_LIBRARY_SOURCES` or `--allowed-sources`),
    uses only the supplied source names and never reads deployment files or imports `src.*`.
    When called host-directly without an allowlist, resolves source names once from deployment
    profile/capabilities without loading `.env` into `os.environ`, failing closed (`[]`) if
    deployment config cannot be resolved.
    """
    if allowed_sources is not None:
        if isinstance(allowed_sources, str):
            allowed = {part.strip() for part in allowed_sources.split(",") if part.strip()}
        else:
            allowed = {str(s).strip() for s in allowed_sources if str(s).strip()}
        return [s for s in SOURCES if s in allowed]
    raw_env = os.environ.get("DALTON_ENABLED_LIBRARY_SOURCES")
    if raw_env is not None:
        allowed = {part.strip() for part in raw_env.split(",") if part.strip()}
        return [s for s in SOURCES if s in allowed]
    return _resolve_host_direct_library_sources()


def validate_request(route, operation, query=None, document=None, params=None, allowed_sources=None):
    p = dict(params or {})
    allowed = {'company', 'sector', 'tag', 'sender', 'layer', 'sources',
               'search_mode', 'no_decay', 'offset', 'max_chars', 'media', 'indexed_since'}
    if set(p) - allowed:
        raise ValueError('未知内部资料参数: ' + ','.join(sorted(set(p)-allowed)))
    for k in ('company', 'sector', 'tag', 'sender'):
        if k in p and (not isinstance(p[k], str) or not p[k].strip()):
            raise ValueError(k + ' 须为非空字符串')
    if p.get('company') and p.get('sector'):
        raise ValueError('company 与 sector 只能选一个')
    for k, lo, hi in [('offset', 0, 100000000), ('max_chars', 1, 30000)]:
        if k in p and (type(p[k]) is not int or not lo <= p[k] <= hi):
            raise ValueError(k + ' 超出范围')
    if 'indexed_since' in p and not (isinstance(p['indexed_since'], str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}(T[0-9:.+-]+Z?)?', p['indexed_since'])):
        raise ValueError('indexed_since 须为 YYYY-MM-DD 或 UTC ISO 时间')
    if 'no_decay' in p and type(p['no_decay']) is not bool:
        raise ValueError('no_decay 须为布尔值')
    if p.get('search_mode', 'hybrid') not in ('hybrid', 'keyword'):
        raise ValueError('search_mode 须为 hybrid 或 keyword')
    if p.get('media','all') not in ('all','text','images'):
        raise ValueError('media 须为 all、text 或 images')
    if p.get('layer', 'raw') not in ('raw', 'digest', 'all'):
        raise ValueError('layer 须为 raw、digest 或 all')
    if 'sender' in p and p.get('layer','raw') != 'raw':
        raise ValueError('sender 筛选仅用于原文层')
    enabled_sources = _enabled_library_sources(allowed_sources)
    if route == 'all':
        if 'sources' in p:
            sources = p['sources']
            if not isinstance(sources, list) or not sources or any(s not in SOURCES for s in sources):
                raise ValueError('sources 仅支持 company-wiki、sales-notes、tmtb、funda-ai')
            disabled = [s for s in sources if s not in enabled_sources]
            if disabled:
                raise ValueError('capability_disabled: 已禁用来源 ' + ', '.join(disabled))
        else:
            sources = list(enabled_sources)
            if not sources:
                raise ValueError('capability_disabled: 所有本地资料来源均已禁用')
    else:
        if route not in SOURCES:
            raise ValueError('sources 仅支持 company-wiki、sales-notes、tmtb、funda-ai')
        if route not in enabled_sources:
            raise ValueError(f"capability_disabled: 来源 '{route}' 已禁用")
        sources = [route]
    if 'sources' in p and route != 'all':
        raise ValueError('sources 仅用于 all')
    if operation not in ('search', 'read', 'list', 'topic'):
        raise ValueError('只支持 search/read/list/topic')
    if operation == 'topic' and route != 'company-wiki':
        raise ValueError('topic 仅用于 company-wiki')
    if operation == 'search' and not (query and query.strip()):
        raise ValueError('search 需要 query')
    if operation == 'topic' and not (p.get('tag') or query):
        raise ValueError('topic 需要 tag 或 query')
    if operation == 'read':
        if not document:
            raise ValueError('read 需要搜索返回的 document_id')
        if set(p)-{'sources', 'offset', 'max_chars'} or query:
            raise ValueError('read 只接受 document、offset、max_chars 和 all 的 sources')
    elif document or any(k in p for k in ('offset','max_chars')):
        raise ValueError('document/offset/max_chars 仅用于 read')
    if route == 'company-wiki' and any(k in p for k in ('sender','layer')):
        raise ValueError('wiki 不支持 sender/layer')
    if route in ('sales-notes','tmtb','funda-ai') and any(k in p for k in ('company','sector','tag','no_decay','indexed_since')):
        raise ValueError('邮件/TMTB 不支持 wiki 专用筛选；公司名放 query')
    if route in ('tmtb','funda-ai') and p.get('layer','raw') != 'raw':
        raise ValueError('TMTB/FUNDA AI 只读原文缓存')
    # Never silently apply a source-specific filter to only part of a federation.
    if route == 'all' and any(k in p for k in ('company','sector','tag','no_decay','indexed_since')) and sources != ['company-wiki']:
        raise ValueError('wiki 专用筛选需要 sources=[company-wiki]；跨库公司名用 query')
    if route == 'all' and 'sender' in p and 'company-wiki' in sources:
        raise ValueError('sender 不能用于含 wiki 的联搜')
    if route == 'all' and p.get('layer') == 'digest' and sources != ['sales-notes']:
        raise ValueError('只查摘要须用 sales-notes 或 sources=[sales-notes]；联搜附带摘要用 layer=all')
    if route == 'all' and p.get('layer') == 'all' and 'sales-notes' not in sources:
        raise ValueError('layer=all 需包含 sales-notes；其他来源只有原文层')
    return p, list(dict.fromkeys(sources))


@contextlib.contextmanager
def readonly(path):
    # mode=ro avoids init_db migrations, new databases, indexing or writes.
    conn = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=10)
    conn.execute('PRAGMA query_only=ON')
    try:
        yield conn
    finally:
        conn.close()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def page(text, p):
    start, size = p.get('offset',0), p.get('max_chars',10000)
    if start > len(text):
        raise ValueError('offset 超过已存正文长度')
    end = min(len(text), start+size)
    return {'content':text[start:end], 'offset':start, 'next_offset':end if end<len(text) else None,
            'total_chars':len(text), 'truncated':end<len(text),
            'content_sha256':hashlib.sha256(text.encode()).hexdigest()}


def words(query):
    return list(dict.fromkeys(re.findall(r'[\w.-]+', query.casefold())))[:30]


def keyword_score(text, terms):
    low=text.casefold()
    return sum(bool(re.search(r'(?<!\w)'+re.escape(t)+r'(?!\w)', low)) if t.isascii()
               else t in low for t in terms)


def source_status(results, warnings):
    return 'partial' if warnings else ('ok' if results else 'empty')


class Library:
    def __init__(self, root=None, *, code_root=None, data_root=None):
        if root is not None:
            resolved_data = Path(data_root) if data_root is not None else Path(root)
            resolved_code = Path(code_root) if code_root is not None else Path(root)
        else:
            resolved_data = Path(data_root) if data_root is not None else DATA_ROOT
            resolved_code = Path(code_root) if code_root is not None else CODE_ROOT
        self.root=resolved_data
        self.data_root=resolved_data
        self.code_root=resolved_code
        scripts_dir = str(self.code_root / 'scripts')
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        self.wiki_db=self.data_root/'wiki/vectors.db'
        self.mail_db=self.data_root/'skills/market-digest/vectors.db'
        self._modules={}
        self._embeddings={}
        from research_images import ImageStore
        self._images=ImageStore(self.data_root, code_root=self.code_root)

    def module(self, source):
        name='company-wiki' if source=='company-wiki' else 'market-digest'
        if name not in self._modules:
            filename='company_wiki_cli.py' if name=='company-wiki' else 'market_digest_cli.py'
            self._modules[name]=load_module('research_'+name.replace('-','_'),self.code_root/'skills'/name/'scripts'/filename)
        return self._modules[name]

    def embed(self, source, query):
        name='company-wiki' if source=='company-wiki' else 'market-digest'
        key=(source if source=='funda-ai' else name,query)
        if key not in self._embeddings:
            mod=self.module(source)
            if name=='market-digest':
                cfg_candidates=(
                    self.data_root/'config/config.json',
                    self.data_root/'skills/market-digest/config.json',
                    self.code_root/'skills/market-digest/config.json',
                )
                cfg_file=next((c for c in cfg_candidates if c.is_file()),cfg_candidates[0])
                config=json.loads(cfg_file.read_text())
            else:
                config=json.loads((self.code_root/'skills'/name/'config.json').read_text())
            if source=='funda-ai':
                from funda_ai import API_MODEL,DIMS
                config['embedding']={'model':API_MODEL,'dimensions':DIMS}
            # Suppress provider fail() output; never let auth diagnostics enter JSON.
            with contextlib.redirect_stdout(io.StringIO()):
                if source=='company-wiki':
                    model,_=mod.resolve_embedding_model(config)
                    self._embeddings[key]=mod.embed_query(query,model)
                else:
                    self._embeddings[key]=mod.embed_query(query,config)
        return self._embeddings[key]

    def wiki_file(self, row):
        path=(self.root/row['filepath']).resolve()
        wiki=(self.root/'wiki').resolve()
        if not path.is_relative_to(wiki) or path.suffix != '.md':
            raise ValueError('wiki 文件超出允许范围')
        return path

    def wiki_meta(self, row):
        path=self.wiki_file(row)
        # Exact file provenance stays with the source; no model-based extraction.
        extras={}
        with path.open() as f:
            head=f.read(8192)
        if head.startswith('---\n'):
            for line in head.split('---',2)[1].splitlines():
                m=re.match(r'^(source|source_url):\s*(.*?)\s*$',line)
                if m:extras[m[1]]=m[2].strip('"\'')
        did='wiki:'+str(row['id'])
        return {'document_id':did,'canonical_id':extras.get('source_url') or did,
                'source':'company-wiki','original_source':extras.get('source') or 'company-wiki',
                'original_document_id':str(row['id']),'title':row['filename'],
                'date':row['date'],'date_basis':'wiki 文档日期，非严格历史可见时间',
                'indexed_at':row['created_at'],'url':extras.get('source_url'),
                'location':row['filepath'],'company':row['category_name'] if row['category_type']=='company' else None,
                'sector':row['category_name'] if row['category_type']=='sector' else None,
                'tags':json.loads(row['tags'] or '[]'),'evidence_role':'source_document',
                'original_completeness':'as_stored','private':True}

    def wiki(self, op, query, document, start, end, limit, p):
        mod=self.module('company-wiki')
        config=json.loads((self.code_root/'skills/company-wiki/config.json').read_text())
        ct='company' if p.get('company') else ('sector' if p.get('sector') else None)
        cn=mod.resolve_category_name(p[ct],ct,config) if ct else None
        tag=p.get('tag') or (query if op=='topic' else None)
        filters=mod.build_doc_filters(ct,cn,start,end,tag)
        # date 是文档日期，补录的旧纪要会落在窗口外；增量追踪按入库时间（created_at，UTC ISO）筛
        if p.get('indexed_since'):filters[0].append('d.created_at>=?');filters[1].append(str(p['indexed_since']))
        warnings=[]
        with readonly(self.wiki_db) as conn:
            if op=='read':
                m=re.fullmatch(r'wiki:(\d+)',document)
                if not m:raise ValueError('wiki 需要 wiki:<id>')
                conn.row_factory=sqlite3.Row
                row=conn.execute('SELECT * FROM documents WHERE id=?',(int(m[1]),)).fetchone()
                if row is None:raise ValueError('文档不存在')
                result=self.wiki_meta(row)
                result.update(page(self.wiki_file(row).read_text(),p))
                return {'ok':True,'status':'ok','document':result,'warnings':[]}
            hits=[]
            if op=='search':
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='chunks_fts'").fetchone():
                    raise ValueError('wiki 关键词索引缺失；请用原维护流程修复，本入口不重建索引')
                vec=None
                if p.get('search_mode')!='keyword':
                    try:vec=self.embed('company-wiki',query)
                    except (Exception,SystemExit):warnings.append('语义检索不可用，已降为关键词检索')
                if vec is not None:
                    try:
                        hits=mod.hybrid_search(conn,query,vec,config,top_k=limit,filters=filters,no_decay=p.get('no_decay',False))
                    except (ValueError,TypeError):
                        warnings.append('向量维度或数据异常，已降为关键词检索');vec=None
                if vec is None:
                    rows=mod.keyword_search(conn,query,filters,limit=max(50,limit*5))
                    seen=set()
                    for r in rows:
                        if r[1] not in seen:
                            hits.append({'filepath':r[8],'content_preview':r[2][:800],'score':1/(61+len(hits)),'matched':['keyword']});seen.add(r[1])
                        if len(hits)>=limit:break
            else:
                where=' AND '.join(filters[0]) or '1=1'
                rows=conn.execute('SELECT d.filepath FROM documents d WHERE '+where+' ORDER BY d.date DESC,d.id DESC LIMIT ?',filters[1]+[limit+1]).fetchall()
                hits=[{'filepath':r[0]} for r in rows[:limit]]
                if len(rows)>limit:warnings.append('列表有更多文档，请缩小筛选或提高 limit')
            conn.row_factory=sqlite3.Row
            results=[]
            for hit in hits:
                row=conn.execute('SELECT * FROM documents WHERE filepath=?',(hit['filepath'],)).fetchone()
                try:
                    item=self.wiki_meta(row)
                    item.update(snippet=hit.get('content_preview',''),score=hit.get('score'),matched=hit.get('matched',[]))
                    results.append(item)
                except (OSError,ValueError):warnings.append('索引命中文档缺失或路径无效：'+str(row['id']))
            count,latest=conn.execute('SELECT COUNT(*),MAX(date) FROM documents').fetchone()
        return {'ok':True,'status':source_status(results,warnings),'results':results,'warnings':warnings,
                'coverage':{'scope':'local_index','documents':count,'latest_document_date':latest}}

    def manual_notes(self):
        # 手动入库/wiki 迁入的卖方文档：整篇正文和元数据在 state/sales-notes（见 sales_notes_store）
        if not hasattr(self,'_manual_notes'):
            import sales_notes_store
            self._manual_notes=sales_notes_store.load_manifest(self.root)
        return self._manual_notes

    def mail_meta(self, row):
        tmtb=row['doc_key'].startswith('alphaengine:')
        docid=('tmtb:'+row['doc_key'].split(':',1)[1]) if tmtb else ('mail:'+str(row['id']))
        meta={'document_id':docid,'canonical_id':row['doc_key'] if tmtb else 'mail-message:'+row['doc_key'],
                'source':'tmtb' if tmtb else 'sales-notes','original_source':row['sender'],
                'original_document_id':row['doc_key'],'title':row['subject'],'date':row['date'],
                'date_basis':'原同步脚本日期；TMTB 旧转换含固定时差假设' if tmtb else '邮件库日期，保留原导入口径',
                'indexed_at':row['created_at'],'url':None,'location':'market-digest/email_chunks',
                'email_id':row['email_id'],'evidence_role':'source_document',
                'original_completeness':'unknown_cached_chunks','private':True}
        if row['doc_key'].startswith('manual:'):
            entry=self.manual_notes().get(row['doc_key'],{})
            migrated=entry.get('origin')=='wiki-migrated'
            meta.update(canonical_id='sales-note:'+row['doc_key'],ingest_channel=entry.get('origin','manual'),
                        date_basis='沿用 wiki 文档日期' if migrated else '收到日期；报告/会议日期见 doc_date',
                        doc_date=entry.get('doc_date'),kind=entry.get('kind'),tickers=entry.get('tickers') or [],
                        url=entry.get('source_url'),location=entry.get('path') or meta['location'],
                        original_file=entry.get('original'),migrated_from=entry.get('migrated_from'),
                        original_completeness='stored_file' if entry.get('path') else 'unknown_cached_chunks')
        return meta

    def mail_read(self, conn, source, document, p):
        conn.row_factory=sqlite3.Row
        if re.fullmatch(r'digest:\d+',document):
            if source!='sales-notes':raise ValueError('摘要仅属于 sales-notes')
            row=conn.execute('SELECT * FROM chunks WHERE id=?',(int(document.split(':')[1]),)).fetchone()
            if row is None:raise ValueError('摘要不存在')
            result=self.digest_meta(row);result.update(page(row['content'],p))
            return {'ok':True,'status':'ok','document':result,'warnings':[]}
        if re.fullmatch(r'tmtb:[A-Za-z0-9][A-Za-z0-9_-]{0,159}',document):
            if source!='tmtb':raise ValueError('文档属于 tmtb，请使用对应来源')
            key='alphaengine:'+document.split(':')[1]
        elif re.fullmatch(r'mail:\d+',document):
            if source!='sales-notes':raise ValueError('文档属于 sales-notes')
            row=conn.execute('SELECT '+KEY_SQL+' AS doc_key FROM email_chunks WHERE id=?',(int(document.split(':')[1]),)).fetchone()
            if row is None or row['doc_key'].startswith('alphaengine:'):raise ValueError('邮件不存在或来源不符')
            key=row['doc_key']
        else:raise ValueError('需要搜索返回的 mail:/tmtb:/digest: 文档 ID')
        # 按 KEY_SQL 表达式比较走不了索引，1.5GB 的库每次读都要全表扫（实测 20–50 秒）；拆成可走索引的等价条件
        sel='SELECT *, '+KEY_SQL+' AS doc_key FROM email_chunks WHERE '
        rows=conn.execute(sel+'message_id=? UNION ALL '+sel+"email_id=? AND COALESCE(message_id,'')='' "
                          +("UNION ALL "+sel+"id=? AND COALESCE(message_id,'')='' AND COALESCE(email_id,'')='' " if key.startswith('row:') else '')
                          +'ORDER BY chunk_index,id',(key,key)+((int(key[4:]),) if key.startswith('row:') else ())).fetchall()
        if not rows:raise ValueError('文档不存在')
        result=self.mail_meta(rows[0]); parts=[]; seen=set(); warnings=[]; indexes=[]
        if source=='sales-notes':result['document_id']='mail:'+str(min(r['id'] for r in rows))
        if key.startswith('manual:'):
            import sales_notes_store
            stored=sales_notes_store.note_file(self.root,self.manual_notes().get(key))
            if stored is not None:
                # 分块带重叠和重复抬头，整篇正文以落盘文件为准
                result.update(page(stored.read_text(encoding='utf-8'),p));result['stored_chunks']=len(rows)
                result['read_note']='手动入库的卖方文档，返回 state/sales-notes 保存的整篇正文。'
                return {'ok':True,'status':'ok','document':result,'warnings':[]}
            warnings.append('手动入库文档的正文文件缺失，改为拼接已存分块')
        for r in rows:
            identity=(r['chunk_index'],r['content'])
            if identity in seen:continue
            seen.add(identity);indexes.append(r['chunk_index'])
            # Stored repeated From/Subject headers are retained, not guessed away.
            parts.append(r['content'])
        unique=sorted(set(i for i in indexes if i is not None))
        if not unique or unique!=list(range(max(unique)+1)) or len(unique)!=len(indexes):
            warnings.append('缓存分块存在缺号或重复版本，不能声明正文完整')
        result.update(page('\n\n'.join(parts),p));result['stored_chunks']=len(parts)
        result['read_note']='按原分块顺序读取全部已存内容；保留重复上下文头。历史导入可能截断，非未截断原件保证。'
        return {'ok':True,'status':'partial' if warnings else 'ok','document':result,'warnings':warnings}

    @staticmethod
    def digest_meta(row):
        did='digest:'+str(row['id'])
        return {'document_id':did,'canonical_id':did,'source':'sales-notes','layer':'digest',
                'original_source':'market-digest 摘要','original_document_id':str(row['id']),
                'title':row['section_label'] or row['section_id'],'date':row['date'],
                'date_basis':'摘要归档日期','indexed_at':row['created_at'],'url':None,
                'location':'market-digest/chunks','evidence_role':'derived_summary',
                'derived_from':json.loads(row['source_emails'] or '[]'),
                'independent_evidence':False,'original_completeness':'stored_summary','private':True}

    def mail(self, source, op, query, document, start, end, limit, p):
        warnings=[]; results=[]
        with readonly(self.mail_db) as conn:
            if op=='read':return self.mail_read(conn,source,document,p)
            terms=words(query or '')
            vec=None
            np=None
            if op=='search' and p.get('search_mode')!='keyword':
                try:
                    import numpy as np
                    vec=np.asarray(self.embed(source,query),dtype=np.float32)
                    norm=np.linalg.norm(vec)
                    if not norm or not np.isfinite(norm):raise ValueError('invalid query vector')
                    vec=vec/norm
                except (Exception,SystemExit):
                    vec=None;warnings.append('语义检索不可用，已降为关键词检索')
            layers=['raw'] if source=='tmtb' else (['raw','digest'] if p.get('layer')=='all' else [p.get('layer','raw')])
            coverage={}; mismatched=0; unindexed=0
            for layer in layers:
                raw=layer=='raw'; table='email_chunks' if raw else 'chunks'
                clauses=[];params=[]
                if raw:clauses.append(KEY_SQL+(' LIKE ' if source=='tmtb' else ' NOT LIKE ')+"'alphaengine:%'")
                if start:clauses.append('date>=?');params.append(start)
                if end:clauses.append('date<=?');params.append(end)
                if p.get('sender'):
                    if not raw:raise ValueError('sender 筛选仅用于原文层')
                    clauses.append('instr(lower(COALESCE(sender,\'\')),lower(?))>0');params.append(p['sender'])
                where=' AND '.join(clauses) or '1=1'
                count,documents,latest=conn.execute('SELECT COUNT(*),COUNT(DISTINCT '+(KEY_SQL if raw else 'id')+'),MAX(date) FROM '+table+' WHERE '+where,params).fetchone()
                coverage[layer]={'chunks':count,'documents':documents,'latest_document_date':latest}
                conn.row_factory=sqlite3.Row
                extra=(', '+KEY_SQL+' AS doc_key') if raw else ''
                # Keyword/list operations need no vector blobs (the live store is >1 GB).
                columns='*' if vec is not None else (
                    'id,date,email_id,message_id,sender,subject,chunk_index,content,created_at,NULL AS embedding' if raw else
                    'id,date,period,section_id,section_label,content,source_emails,created_at,NULL AS embedding')
                rows=conn.execute('SELECT '+columns+extra+' FROM '+table+' WHERE '+where+' ORDER BY date DESC,id',params)
                best={}; anchors={}
                for row in rows:
                    key=row['doc_key'] if raw else 'digest:'+str(row['id'])
                    if raw:anchors[key]=min(anchors.get(key,row['id']),row['id'])
                    kw=keyword_score(row['content'],terms) if op=='search' else 0
                    similarity=None
                    if vec is not None and row['embedding']:
                        stored=np.frombuffer(row['embedding'],dtype=np.float32)
                        if stored.shape!=vec.shape:mismatched+=1
                        else:
                            norm=np.linalg.norm(stored)
                            if norm and np.isfinite(norm):similarity=float(np.dot(vec,stored)/norm)
                            else:unindexed+=1
                    elif vec is not None:unindexed+=1
                    if op=='search' and similarity is None and not kw:continue
                    rank=(similarity if similarity is not None else -1.0,kw) if vec is not None else (kw,)
                    if op!='search':rank=(row['date'],)
                    if key not in best or rank>best[key]['rank']:
                        item=self.mail_meta(row) if raw else self.digest_meta(row)
                        item.update(layer=layer,snippet=row['content'][:800],score=similarity,keyword_hits=kw)
                        best[key]={'rank':rank,'item':item}
                # Document-level ranks; never let a long note occupy all result slots.
                for entry in sorted(best.values(),key=lambda x:x['rank'],reverse=True)[:limit]:
                    item=entry['item']
                    if raw and source=='sales-notes':item['document_id']='mail:'+str(anchors[item['original_document_id']])
                    results.append(item)
                conn.row_factory=None
            if mismatched:warnings.append(f'{mismatched} 个分块向量维度不符，语义覆盖不完整')
            if unindexed:warnings.append(f'{unindexed} 个分块缺少有效向量，仅作关键词匹配')
        if len(layers)>1:
            # Scores from the two layers use the same embedding model, unlike wiki.
            results.sort(key=(lambda x:(x.get('date') or '')) if op!='search' else
                         (lambda x:(x.get('score') if x.get('score') is not None else -1,x.get('keyword_hits',0))),reverse=True)
        return {'ok':True,'status':source_status(results,warnings),'results':results[:limit],
                'warnings':warnings,'coverage':{'scope':'local_index_in_requested_window','layers':coverage}}

    def run(self, route, op, query=None, document=None, start=None, end=None, limit=3, params=None, allowed_sources=None):
        p,sources=validate_request(route,op,query,document,params,allowed_sources=allowed_sources)
        if op=='read' and (start or end):raise ValueError('read 不接受日期筛选')
        if op=='read' and document.startswith('image:'):
            if p.get('offset') or p.get('max_chars'):raise ValueError('图片读取不使用正文分页参数')
            result=self._images.read(document,sources)
            return {**result,'retrieved_at':datetime.now(timezone.utc).isoformat(),'local_only':True}
        if op=='read' and route=='all':
            prefix=(document or '').split(':',1)[0]
            wanted={'wiki':'company-wiki','mail':'sales-notes','digest':'sales-notes','tmtb':'tmtb','funda':'funda-ai'}.get(prefix)
            if wanted not in sources:raise ValueError('文档不属于选定 sources')
            sources=[wanted]
        batches={}
        for source in sources:
            # Federation layer=all adds sales digests only; other libraries
            # remain raw and never inherit a sales-only layer filter.
            source_params=dict(p)
            if source!='sales-notes':source_params.pop('layer',None)
            try:
                if source=='funda-ai' and p.get('media')!='images':
                    from funda_ai import Store
                    batches[source]=Store(self.root).query(op,query,document,start,end,limit,source_params,lambda q:self.embed(source,q))
                else:
                    batches[source]={'ok':True,'status':'empty','results':[]} if p.get('media')=='images' else (self.wiki(op,query,document,start,end,limit,source_params) if source=='company-wiki'
                        else self.mail(source,op,query,document,start,end,limit,source_params))
            except (Exception,SystemExit) as exc:
                batches[source]={'ok':False,'status':'error','results':[],
                    'error':str(exc) if isinstance(exc,ValueError) else type(exc).__name__,
                    'hint':'核对来源数据库、索引与既有 embedding 配置；本入口不自动创建库、同步或重建。'}
            if op=='read' and batches[source].get('document',{}).get('evidence_role')=='source_document':
                doc=batches[source]['document']
                key=doc['document_id'] if source=='company-wiki' else doc['original_document_id']
                try:doc['images']=self._images.related(source,key)
                except Exception:doc['images']={'status':'unavailable','hint':'原文可读；图片索引暂时不可用'}
            if op!='read' and p.get('media','all')!='text':
                try:
                    ip=dict(p)
                    if source=='company-wiki':
                        config=json.loads((self.code_root/'skills/company-wiki/config.json').read_text())
                        for key in ('company','sector'):
                            if ip.get(key):ip[key]=self.module(source).resolve_category_name(ip[key],key,config)
                        if op=='topic':ip['tag']=ip.get('tag') or query
                    visual=self._images.search(source,query,start,end,limit,ip,op)
                except Exception as exc:
                    visual={'ok':False,'status':'error','results':[],'warnings':['图片索引读取失败：'+type(exc).__name__]}
                text_batch=batches[source]
                if p.get('media')=='images':batches[source]=visual
                else:
                    # Rank fusion only; never mix embedding model spaces or cosine scales.
                    merged=[]
                    for arm,batch in [('text',text_batch),('image',visual)]:
                        for rank,item in enumerate(batch.get('results',[]),1):
                            merged.append({**item,'media_rrf_score':1/(60+rank),'retrieval_arm':arm})
                    hits=sorted(merged,key=lambda x:x['media_rrf_score'],reverse=True)[:limit]
                    ok=text_batch.get('ok') or (visual.get('ok') and bool(visual.get('results')))
                    partial=any(not b.get('ok') or b.get('status')=='partial' for b in (text_batch,visual))
                    batches[source]={**text_batch,'ok':bool(ok),'status':('partial' if partial else ('ok' if hits else 'empty')) if ok else 'error',
                        'results':hits,'image_status':{k:v for k,v in visual.items() if k!='results'}}
        if route!='all' or op=='read':
            result=batches[sources[0]]
        else:
            fused={}
            for source,batch in batches.items():
                for rank,item in enumerate(batch.get('results',[]),1):
                    key=item['canonical_id']
                    entry=fused.setdefault(key,{**item,'rrf_score':0.0,'matched_sources':[],'alternate_document_ids':[]})
                    entry['rrf_score']+=1/(60+rank)
                    entry['matched_sources'].append(source)
                    if item['document_id']!=entry['document_id']:entry['alternate_document_ids'].append(item['document_id'])
            hits=sorted(fused.values(),key=lambda x:x['rrf_score'],reverse=True)[:limit]
            success=any(b['ok'] for b in batches.values())
            partial=any(not b['ok'] or b['status']=='partial' for b in batches.values())
            result={'ok':success,'status':('partial' if partial else ('ok' if hits else 'empty')) if success else 'error',
                    'results':hits,'ranking':'RRF of per-source document ranks; raw scores are not mixed'}
        result={**result,'source_status':{s:{k:v for k,v in b.items() if k not in ('results','document')} for s,b in batches.items()},
                'retrieved_at':datetime.now(timezone.utc).isoformat(),'local_only':True}
        if 'results' in result:result['result_count']=len(result['results'])
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('route',choices=(*SOURCES,'all'))
    parser.add_argument('--operation',choices=('search','read','list','topic'),default='search')
    for k in ('query','document','start','end'):parser.add_argument('--'+k)
    parser.add_argument('--limit',type=int,default=3)
    parser.add_argument('--params-json',default='{}')
    parser.add_argument('--allowed-sources',default=None)
    args=parser.parse_args()
    try:
        if not 1<=args.limit<=100:raise ValueError('limit 须在 1–100')
        for value in (args.start,args.end):
            if value and datetime.strptime(value,'%Y-%m-%d').strftime('%Y-%m-%d')!=value:raise ValueError('日期格式错误')
        if args.start and args.end and args.start>args.end:raise ValueError('起止日期倒置')
        params=json.loads(args.params_json)
        if not isinstance(params,dict):raise ValueError('params 须为对象')
        result=Library().run(args.route,args.operation,args.query,args.document,args.start,args.end,args.limit,params,allowed_sources=args.allowed_sources)
    except (ValueError,OSError) as exc:
        result={'ok':False,'status':'error','error':str(exc),'hint':'核对 catalog 和内部资料参数；未执行写入。'}
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['ok'] else 1


if __name__=='__main__':sys.exit(main())
