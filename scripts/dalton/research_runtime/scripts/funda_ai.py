#!/usr/bin/env python3
"""FUNDA AI mail archive: isolated SQLite, lossless originals, resumable embeddings."""
import argparse
import base64
import contextlib
from datetime import datetime, timezone
from email import policy, utils
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit

_DEFAULT_CODE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_research_roots(default_code_root: Path = _DEFAULT_CODE_ROOT) -> tuple[Path, Path]:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    # Legacy compat: when new vars are unset and DALTON_OCW_ROOT is set, keep old single-root layout.
    if raw_ocw and not raw_code and not raw_home:
        ocw = Path(raw_ocw).expanduser().resolve()
        return ocw, ocw
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code_root.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return code_root, home_root


CODE_ROOT, DATA_ROOT = _resolve_research_roots()
ROOT = DATA_ROOT
SENDER = 'fundaai@substack.com'
MODEL = 'gemini-embedding-2-preview'  # index label; rows store it, keep as is
# Google retired the preview name on 2026-11-19; the GA model gives the same vectors (cosine 1.0).
API_MODEL = 'gemini-embedding-2'
DIMS = 3072


def sha(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def is_funda(mail):
    """Exact sender or explicit forwarded header, never a casual body mention."""
    headers = {h['name'].lower(): h['value'] for h in mail.get('_mime_payload', {}).get('payload', {}).get('headers', [])}
    if any(utils.parseaddr(v)[1].lower() == SENDER for v in
           [mail.get('from', ''), headers.get('x-original-from', ''), headers.get('reply-to', '')]):
        return True
    return bool(re.search(r'(?im)^(?:[> ]*)(?:From|发件人):[^\n]*\bfundaai@substack\.com\b', mail.get('body', '')[:8192]))


def iter_mbox(path):
    """Only RFC-style envelope lines split records; plain 'From ...' is body."""
    envelope = re.compile(rb'^From [^\s]+@[^\s]+ (?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[, ]')
    current = []
    with Path(path).open('rb') as file:
        for line in file:
            if envelope.match(line):
                if current:
                    yield b''.join(current)
                current = []
            else:
                current.append(line)
    if current:
        yield b''.join(current)


def mime_payload(msg):
    node = {'mimeType': msg.get_content_type(), 'filename': msg.get_filename() or '',
            'headers': [{'name': k, 'value': str(v)} for k, v in msg.items()], 'body': {}}
    if msg.is_multipart():
        node['parts'] = [mime_payload(p) for p in msg.iter_parts()]
    else:
        data = msg.get_payload(decode=True) or b''
        if msg.get_content_maintype() == 'text':
            try: data = data.decode(msg.get_content_charset() or 'utf-8', errors='replace').encode()
            except LookupError: data = data.decode('utf-8', errors='replace').encode()
        node['body']['data'] = base64.urlsafe_b64encode(data).decode()
    return node


def article_url(text):
    candidates = re.findall(r'https?://[^\s<>"\)]+', text)
    for url in candidates:
        u = urlsplit(url.replace('&amp;', '&'))
        if u.hostname == 'fundaai.substack.com' and u.path.startswith('/p/'):
            return 'https://fundaai.substack.com' + u.path.rstrip('/')
        if u.hostname == 'open.substack.com' and u.path.startswith('/pub/fundaai/p/'):
            return 'https://fundaai.substack.com' + u.path[len('/pub/fundaai'):].rstrip('/')
    return None


def mail_category(mail):
    headers = {h['name'].lower(): h['value'] for h in mail.get('_mime_payload', {}).get('payload', {}).get('headers', [])}
    try:
        category = json.loads(headers.get('x-mailgun-variables', '{}')).get('category')
    except (ValueError, TypeError): category = None
    if category:
        return category if category in ('post', 'chat-thread') else 'administrative'
    title = mail.get('subject', '').lower()
    if re.search(r'verification code|sign.?in|log.?in|payment receipt|subscription|welcome to|give a friend|send your gifts|you.re on the list', title):
        return 'administrative'
    return 'chat-thread' if 'new thread' in title else 'post'


def split_body(body, size=1800):
    # Non-overlapping character spans preserve every character, including long paragraphs.
    return [body[i:i+size] for i in range(0, len(body), size)]


class Store:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else DATA_ROOT
        self.home = self.root / 'state/funda-ai'
        self.db = self.home / 'index.sqlite'

    @contextlib.contextmanager
    def connect(self, write=False):
        if write:
            self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.home, 0o700)
            c = sqlite3.connect(self.db, timeout=30)
            c.execute('PRAGMA foreign_keys=ON')
            c.executescript('''
              CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY, title TEXT NOT NULL, sender TEXT NOT NULL,
                date TEXT NOT NULL, published_at TEXT NOT NULL, body TEXT NOT NULL,
                body_sha TEXT NOT NULL, url TEXT, original_path TEXT NOT NULL,
                created_at TEXT NOT NULL, indexed_at TEXT, embedding_model TEXT,
                dimensions INTEGER, status TEXT NOT NULL DEFAULT 'pending');
              CREATE TABLE IF NOT EXISTS aliases (key TEXT PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES documents(id));
              CREATE TABLE IF NOT EXISTS receipts (
                receipt_key TEXT PRIMARY KEY, doc_id INTEGER NOT NULL REFERENCES documents(id),
                original_path TEXT NOT NULL, received_at TEXT, imported_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS chunks (
                doc_id INTEGER NOT NULL REFERENCES documents(id), chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL, embedding BLOB NOT NULL, PRIMARY KEY(doc_id,chunk_index));
              CREATE INDEX IF NOT EXISTS idx_funda_date ON documents(date);
            ''')
            os.chmod(self.db, 0o600)
        else:
            c = sqlite3.connect(self.db.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
            c.execute('PRAGMA query_only=ON')
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def capture(self, mail, raw=None, received_at=None):
        if not is_funda(mail):
            raise ValueError('sender_not_funda_ai')
        category = mail_category(mail)
        if category == 'administrative':
            return {'ok': True, 'skipped': 'administrative_email', 'reused': False,
                    'document_id': None, 'original_path': None, 'image_job': None, 'status': 'excluded'}
        body = mail.get('body', '')
        if not body.strip():
            raise ValueError('empty_funda_body')
        stamp = utils.parsedate_to_datetime(mail['date'])
        if stamp.tzinfo is None:
            raise ValueError('email_date_missing_timezone')
        published = stamp.astimezone(timezone.utc).isoformat()
        payload = mail.get('_mime_payload', {})
        headers = {h['name'].lower(): h['value'] for h in payload.get('payload', {}).get('headers', [])}
        mid = headers.get('message-id', '').strip()
        body_hash = sha(body.encode())
        # The first publication link is the article itself, before recommendations/footer.
        from research_image_ingest import gmail_items
        html = ''
        def walk(node):
            nonlocal html
            if node.get('mimeType') == 'text/html' and node.get('body', {}).get('data'):
                html += base64.urlsafe_b64decode(node['body']['data']).decode(errors='replace')
            for child in node.get('parts', []): walk(child)
        walk(payload.get('payload', {}))
        url = (article_url(html) or article_url(body)) if category == 'post' else None
        aliases = ['body:' + body_hash]
        if mid: aliases.append('message:' + mid)
        if url: aliases.append('article:' + url)
        value = raw if raw is not None else json.dumps(payload or mail, ensure_ascii=False).encode()
        ext = '.eml' if raw is not None else '.json'
        file = self.home / 'originals' / (sha(value) + ext)
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not file.exists():
            with file.open('xb') as f:
                os.chmod(file, 0o600); f.write(value)
        receipt = ('gmail:' + mail['id']) if raw is None else ('eml:' + sha(raw))
        with self.connect(True) as c, c:
            matches = {r[0] for key in aliases for r in c.execute('SELECT doc_id FROM aliases WHERE key=?', (key,))}
            if len(matches) > 1:
                raise ValueError('conflicting_document_aliases')
            reused = bool(matches)
            if matches:
                did = matches.pop()
            else:
                cur = c.execute('INSERT INTO documents(title,sender,date,published_at,body,body_sha,url,original_path,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (mail['subject'], SENDER, published[:10], published, body, body_hash, url, str(file.relative_to(self.root)), now()))
                did = cur.lastrowid
            for key in aliases:
                c.execute('INSERT OR IGNORE INTO aliases VALUES(?,?)', (key, did))
            c.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)',
                      (receipt, did, str(file.relative_to(self.root)), received_at, now()))
            row = c.execute('SELECT * FROM documents WHERE id=?', (did,)).fetchone()
        image_job = None
        if gmail_items(payload):
            from research_images import enqueue
            image_job = enqueue('funda-ai', str(did), 'file' if raw is not None else 'gmail',
                {'path': str(file)} if raw is not None else payload,
                {'title': row['title'], 'date': row['date'], 'original_source': 'FUNDA AI',
                 'parent_document_id': 'funda:' + str(did), 'gmail_message_id': mail.get('id'),
                 'date_basis': '原邮件 Date（UTC）'}, root=self.root)
        return {'ok': True, 'document_id': 'funda:' + str(did), 'reused': reused,
                'image_job': image_job, 'status': row['status'], 'original_path': str(file.relative_to(self.root))}

    def index_pending(self, embed, max_docs=500, progress=None):
        import numpy as np
        import fcntl
        with self.connect(True): pass
        lock = (self.home / 'index.lock').open('w')
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close(); return {'ok': True, 'status': 'pending', 'reason': 'index_worker_running'}
        done = 0; chunks_done = 0
        try:
            with self.connect() as c:
                rows = c.execute("SELECT * FROM documents WHERE status='pending' ORDER BY id LIMIT ?", (max_docs,)).fetchall()
            for row in rows:
                chunks = split_body(row['body'])
                vectors = embed([f"FUNDA AI | {row['title']} | {row['date']}\n{x}" for x in chunks])
                if len(vectors) != len(chunks): raise ValueError('embedding_count_mismatch')
                arrays = [np.asarray(v, dtype=np.float32) for v in vectors]
                if any(v.shape != (DIMS,) or not np.isfinite(v).all() or not np.linalg.norm(v) for v in arrays):
                    raise ValueError('invalid_embedding_dimensions_or_values')
                # One atomic document: incomplete provider responses never mark a doc indexed.
                with self.connect(True) as c, c:
                    c.execute('DELETE FROM chunks WHERE doc_id=?', (row['id'],))
                    c.executemany('INSERT INTO chunks VALUES(?,?,?,?)',
                                  [(row['id'], i, txt, v.tobytes()) for i, (txt, v) in enumerate(zip(chunks, arrays))])
                    c.execute("UPDATE documents SET status='indexed',indexed_at=?,embedding_model=?,dimensions=? WHERE id=?",
                              (now(), MODEL, DIMS, row['id']))
                done += 1; chunks_done += len(chunks)
                if progress and (done % 10 == 0 or done == len(rows)):
                    progress({'indexed_this_run': done, 'selected': len(rows), 'chunks': chunks_done})
            return {'ok': True, 'indexed_this_run': done, 'chunks': chunks_done, **self.status()}
        finally: lock.close()

    def status(self):
        with self.connect() as c:
            docs, indexed, earliest, latest = c.execute("SELECT COUNT(*),SUM(status='indexed'),MIN(date),MAX(date) FROM documents WHERE status!='excluded'").fetchone()
            excluded = c.execute("SELECT COUNT(*) FROM documents WHERE status='excluded'").fetchone()[0]
            chunks = c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
            receipts = c.execute('SELECT COUNT(*) FROM receipts').fetchone()[0]
        return {'ok': True, 'documents': docs, 'indexed_documents': indexed or 0,
                'pending_documents': docs-(indexed or 0), 'excluded_administrative': excluded, 'chunks': chunks, 'receipts': receipts,
                'earliest_date': earliest, 'latest_date': latest, 'model': MODEL, 'dimensions': DIMS}

    @staticmethod
    def metadata(row):
        return {'document_id': 'funda:' + str(row['id']), 'canonical_id': row['url'] or 'funda:' + str(row['id']),
                'source': 'funda-ai', 'original_source': 'FUNDA AI', 'original_document_id': str(row['id']),
                'title': row['title'], 'date': row['date'], 'published_at': row['published_at'],
                'date_basis': '原邮件 Date（UTC）；转发邮件时间保留原件核对', 'indexed_at': row['indexed_at'],
                'url': row['url'], 'location': row['original_path'], 'evidence_role': 'source_document',
                'original_completeness': 'full_received_email; external_full_article_not_assumed', 'private': True}

    def query(self, operation, query=None, document=None, start=None, end=None, limit=3, params=None, embed=None):
        from research_library import page, words, keyword_score
        import numpy as np
        p = params or {}; warnings = []; hits = []
        with self.connect() as c:
            if operation == 'read':
                if not re.fullmatch(r'funda:\d+', document or ''): raise ValueError('需要 funda:<id>')
                row = c.execute('SELECT * FROM documents WHERE id=?', (int(document.split(':')[1]),)).fetchone()
                if row is None or row['status']=='excluded': raise ValueError('FUNDA AI 文档不存在或已排除非研究邮件')
                return {'ok': True, 'status': 'ok', 'document': {**self.metadata(row), **page(row['body'], p)}, 'warnings': []}
            clauses = ["status!='excluded'"]; values = []
            for bound, operator in ((start, '>='), (end, '<=')):
                if bound: clauses.append('date'+operator+'?'); values.append(bound)
            if p.get('sender'): clauses.append('instr(lower(sender),lower(?))>0'); values.append(p['sender'])
            rows = c.execute('SELECT * FROM documents WHERE '+(' AND '.join(clauses) or '1=1')+' ORDER BY date DESC,id DESC', values).fetchall()
            vec = None
            if operation == 'search' and rows and p.get('search_mode') != 'keyword':
                try:
                    vec = np.asarray(embed(query), dtype=np.float32)
                    if vec.shape != (DIMS,) or not np.isfinite(vec).all() or not np.linalg.norm(vec): raise ValueError('bad_query_vector')
                    vec /= np.linalg.norm(vec)
                except (Exception, SystemExit):
                    vec = None; warnings.append('FUNDA AI 语义检索不可用，已降为关键词检索')
            terms = words(query or '')
            for row in rows:
                kw = keyword_score(row['title']+'\n'+row['body'], terms); score = None; snippet = row['body'][:800]
                if vec is not None:
                    if row['embedding_model'] == MODEL and row['dimensions'] == DIMS:
                        for ch in c.execute('SELECT content,embedding FROM chunks WHERE doc_id=? ORDER BY chunk_index', (row['id'],)):
                            v = np.frombuffer(ch['embedding'], dtype=np.float32)
                            if v.shape != vec.shape or not np.isfinite(v).all() or not np.linalg.norm(v): continue
                            sim = float(np.dot(v, vec)/np.linalg.norm(v))
                            if score is None or sim > score: score, snippet = sim, ch['content'][:800]
                    else: warnings.append('FUNDA AI 有正文尚未完成 embedding，关键词仍可检索')
                if operation == 'search' and score is None and not kw: continue
                hits.append({**self.metadata(row), 'snippet': snippet, 'score': score, 'keyword_hits': kw})
        hits.sort(key=(lambda x: x['date']) if operation == 'list' else
                  (lambda x: (x['score'] if x['score'] is not None else -1, x['keyword_hits'])), reverse=True)
        warnings = list(dict.fromkeys(warnings))
        return {'ok': True, 'status': 'partial' if warnings else ('ok' if hits else 'empty'), 'results': hits[:limit],
                'warnings': warnings, 'coverage': {'scope': 'local_received_mail', **self.status()}}


def load_mail_module():
    import importlib.util
    path = CODE_ROOT / 'skills/market-digest/scripts/market_digest_cli.py'
    spec = importlib.util.spec_from_file_location('funda_market_digest', path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--json', action='store_true')
    sub = p.add_subparsers(dest='command', required=True)
    b = sub.add_parser('import-mbox'); b.add_argument('--file', required=True); b.add_argument('--dry-run', action='store_true')
    b = sub.add_parser('index'); b.add_argument('--max-docs', type=int, default=500)
    sub.add_parser('status'); sub.add_parser('self-test')
    args = p.parse_args(); store = Store()
    try:
        if args.command == 'import-mbox':
            md = load_mail_module(); total = 0; accepted = 0; duplicates = 0; dates = []; chars = 0; categories = {}
            for raw in iter_mbox(args.file):
                total += 1
                msg = BytesParser(policy=policy.default).parsebytes(raw)
                payload = {'payload': mime_payload(msg)}
                mail = md.parse_email(payload); mail['_mime_payload'] = payload
                if not is_funda(mail): raise ValueError('unexpected_sender_in_mbox')
                category=mail_category(mail);categories[category]=categories.get(category,0)+1
                if category=='administrative':continue
                if not mail['body'].strip(): raise ValueError('empty_mbox_body')
                accepted += 1; chars += len(mail['body']); dates.append(utils.parsedate_to_datetime(mail['date']).isoformat())
                if not args.dry_run: duplicates += store.capture(mail, raw=raw)['reused']
            result = {'ok': True, 'parsed_messages': total, 'accepted': accepted, 'duplicates': duplicates,
                      'body_chars': chars, 'categories': categories, 'first_date': min(dates) if dates else None,
                      'last_date': max(dates) if dates else None, 'dry_run': args.dry_run}
        elif args.command == 'index':
            if not 1 <= args.max_docs <= 10000: raise ValueError('max_docs_out_of_range')
            md = load_mail_module(); config = {'embedding': {'model': API_MODEL, 'dimensions': DIMS}}
            from research_images import atomic_json
            result = store.index_pending(lambda texts: md.embed_texts(texts, config), args.max_docs,
                lambda v: atomic_json(store.home/'index-progress.json', {**v, 'at': now()}))
        else:
            result = store.status()
            if args.command == 'self-test':
                with store.connect() as c:
                    result['integrity'] = c.execute('PRAGMA integrity_check').fetchone()[0]
                    result['missing_originals'] = sum(not (store.root / r[0]).is_file() for r in c.execute('SELECT original_path FROM receipts'))
                    result['chunk_body_mismatches'] = sum(''.join(x[0] for x in c.execute('SELECT content FROM chunks WHERE doc_id=? ORDER BY chunk_index',(r['id'],))) != r['body']
                        for r in c.execute("SELECT id,body FROM documents WHERE status='indexed'").fetchall())
                result['ok'] = result['integrity']=='ok' and not result['missing_originals'] and not result['chunk_body_mismatches']
                result['scope'] = 'local_archive_and_index; Gmail delivery not probed'
    except (Exception, SystemExit) as exc:
        result = {'ok': False, 'error': type(exc).__name__, 'hint': '检查输入邮件、FUNDA AI 库和既有 Gemini 环境；原件与已完成索引保留，可重跑 import-mbox/index。'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
