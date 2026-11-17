#!/usr/bin/env python3
"""Private image assets and model-versioned retrieval; no OCR during indexing."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

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
MODEL='gemini-embedding-2-preview'
DIMS=3072
SCHEMA_VERSION='image-only-v1'
SOURCES=('company-wiki','sales-notes','tmtb','funda-ai')
MAX_BYTES=20*1024*1024


def now():return datetime.now(timezone.utc).isoformat()
def sha(data):return hashlib.sha256(data).hexdigest()


def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    with tmp.open('w') as f:
        os.chmod(tmp,0o600);json.dump(data,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)


class ImageStore:
    def __init__(self, root=None, *, code_root=None):
        if root is not None:
            self.root = Path(root)
            self.code_root = Path(code_root) if code_root is not None else self.root
        else:
            self.root = DATA_ROOT
            self.code_root = Path(code_root) if code_root is not None else CODE_ROOT
        self.home=self.root/'state/research-images'
        self.db=self.home/'index.sqlite';self._client=None;self._query_vectors={};self._parents={}

    @contextlib.contextmanager
    def connect(self,write=False):
        if write:
            self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
            os.chmod(self.home,0o700)
            c=sqlite3.connect(self.db,timeout=20)
            os.chmod(self.db,0o600)
            c.executescript('''
            CREATE TABLE IF NOT EXISTS assets(sha TEXT PRIMARY KEY,path TEXT NOT NULL,preview_path TEXT NOT NULL,
                mime TEXT NOT NULL,width INTEGER,height INTEGER,bytes INTEGER,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vectors(sha TEXT,model TEXT,dimensions INTEGER,recipe TEXT,embedding BLOB,
                PRIMARY KEY(sha,model,dimensions,recipe));
            CREATE TABLE IF NOT EXISTS refs(id INTEGER PRIMARY KEY,source TEXT,doc_key TEXT,locator TEXT,
                asset_sha TEXT,title TEXT,date TEXT,metadata TEXT,created_at TEXT,
                UNIQUE(source,doc_key,locator));
            CREATE INDEX IF NOT EXISTS refs_source_date ON refs(source,date);
            CREATE TABLE IF NOT EXISTS descriptions(sha TEXT,model TEXT,prompt_sha TEXT,content TEXT,created_at TEXT,
                PRIMARY KEY(sha,model,prompt_sha));
            ''')
        else:
            c=sqlite3.connect(self.db.resolve().as_uri()+'?mode=ro',uri=True,timeout=20)
            c.execute('PRAGMA query_only=ON')
        c.row_factory=sqlite3.Row
        try:yield c
        finally:c.close()

    def client(self):
        if self._client is None:
            from google import genai
            from google.genai import types
            self._client=genai.Client(api_key=os.environ['GEMINI_API_KEY'],http_options=types.HttpOptions(timeout=30000))
        return self._client

    def embed(self,*,data=None,mime=None,query=None):
        if query is not None and query in self._query_vectors:return self._query_vectors[query].copy()
        from google.genai import types
        import numpy as np
        content=types.Content(parts=[types.Part.from_bytes(data=data,mime_type=mime)]) if data is not None else query
        r=self.client().models.embed_content(model=MODEL,contents=content,config={
            'task_type':'RETRIEVAL_DOCUMENT' if data is not None else 'RETRIEVAL_QUERY','output_dimensionality':DIMS})
        if len(r.embeddings)!=1:raise ValueError('图片 embedding 返回数量异常')
        vector=np.asarray(r.embeddings[0].values,dtype=np.float32)
        if vector.shape!=(DIMS,) or not np.isfinite(vector).all() or not np.linalg.norm(vector):
            raise ValueError('图片 embedding 维度或数值异常')
        if query is not None:self._query_vectors[query]=vector.copy()
        return vector

    def save(self,data):
        from PIL import Image,ImageOps
        if not data or len(data)>MAX_BYTES:raise ValueError('图片为空或超过 20MB')
        with Image.open(io.BytesIO(data)) as im:
            if im.width*im.height>50_000_000:raise ValueError('图片像素过大')
            im.load();fmt=im.format;w,h=im.size
            if fmt not in ('JPEG','PNG','WEBP','GIF'):raise ValueError('图片格式不支持')
            if min(w,h)<40 or w*h<16000:return {'skipped':'small_or_tracking','width':w,'height':h}
            ext={'JPEG':'.jpg','PNG':'.png','WEBP':'.webp','GIF':'.gif'}[fmt]
            digest=sha(data);folder=self.home/'assets'/digest[:2];folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            original=folder/(digest+ext)
            if not original.exists():
                with original.open('xb') as f:os.chmod(original,0o600);f.write(data)
            # Retrieval uses a deterministic image-only preview, never OCR text.
            im=ImageOps.exif_transpose(im).convert('RGB');im.thumbnail((2400,2400))
            out=io.BytesIO();im.save(out,format='JPEG',quality=90)
            preview=folder/(digest+'.preview.jpg')
            if not preview.exists():
                with preview.open('xb') as f:os.chmod(preview,0o600);f.write(out.getvalue())
        with self.connect(True) as c,c:
            c.execute('INSERT OR IGNORE INTO assets VALUES(?,?,?,?,?,?,?,?)',
                (digest,str(original.relative_to(self.root)),str(preview.relative_to(self.root)),Image.MIME[fmt],w,h,len(data),now()))
        return {'sha':digest,'preview':out.getvalue(),'width':w,'height':h}

    def add(self,data,source,doc_key,locator,metadata):
        if source not in SOURCES:raise ValueError('未知来源')
        saved=self.save(data)
        if 'skipped' in saved:return saved
        digest=saved['sha']
        # Save provenance BEFORE embedding, so failed API calls leave a recoverable asset.
        with self.connect(True) as c,c:
            c.execute('''INSERT INTO refs(source,doc_key,locator,asset_sha,title,date,metadata,created_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source,doc_key,locator) DO UPDATE SET
                asset_sha=excluded.asset_sha,title=excluded.title,date=excluded.date,metadata=excluded.metadata''',
                (source,doc_key,locator,digest,metadata.get('title',''),metadata.get('date',''),json.dumps(metadata,ensure_ascii=False),now()))
            rid=c.execute('SELECT id FROM refs WHERE source=? AND doc_key=? AND locator=?',(source,doc_key,locator)).fetchone()[0]
            present=c.execute('SELECT 1 FROM vectors WHERE sha=? AND model=? AND dimensions=? AND recipe=?',
                (digest,MODEL,DIMS,SCHEMA_VERSION)).fetchone()
        if not present:
            vector=self.embed(data=saved['preview'],mime='image/jpeg')
            with self.connect(True) as c,c:c.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?)',
                (digest,MODEL,DIMS,SCHEMA_VERSION,vector.tobytes()))
        return {'document_id':'image:'+str(rid),'asset_sha256':digest,'status':'indexed','reused_vector':bool(present)}

    def safe_asset(self,relative):
        path=(self.root/relative).resolve()
        if not path.is_relative_to((self.home/'assets').resolve()) or not path.is_file():
            raise ValueError('图片文件缺失或路径无效')
        return path

    def metadata(self,row):
        m=json.loads(row['metadata'])
        parent=m.get('parent_document_id')
        if not parent and row['source']=='sales-notes':
            key=row['doc_key']
            if key not in self._parents:
                path=self.root/'skills/market-digest/vectors.db'
                try:
                    with contextlib.closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as c:
                        found=c.execute('SELECT MIN(id) FROM email_chunks WHERE email_id=? OR message_id=?',(key,key)).fetchone()[0]
                    self._parents[key]='mail:'+str(found) if found is not None else None
                except sqlite3.Error:self._parents[key]=None
            parent=self._parents[key]
        return {
            'document_id':'image:'+str(row['id']),'canonical_id':'image-sha256:'+row['asset_sha'],
            'source':row['source'],'media_type':'image','title':row['title'],'date':row['date'],
            'date_basis':m.get('date_basis','继承原文档日期，非严格历史可见时间'),
            'parent_document_id':parent,'original_document_id':row['doc_key'],
            'original_source':m.get('original_source',row['source']),'location':row['path'],
            'asset_sha256':row['asset_sha'],'width':row['width'],'height':row['height'],
            'company':m.get('company'),'sector':m.get('sector'),'tags':m.get('tags',[]),
            'indexed_at':row['created_at'],'snippet':m.get('caption','')[:800],
            'evidence_role':'source_image','private':True,'embedding_model':MODEL,
            'embedding_dimensions':DIMS,'ocr_status':'not_required_for_retrieval'}

    def read(self,document,sources=None):
        if not document.startswith('image:') or not document[6:].isdigit():raise ValueError('需要 image:<id>')
        with self.connect() as c:
            row=c.execute('SELECT r.*,a.path,a.preview_path,a.width,a.height,a.mime FROM refs r JOIN assets a ON a.sha=r.asset_sha WHERE r.id=?',(int(document[6:]),)).fetchone()
        if row is None or (sources and row['source'] not in sources):raise ValueError('图片不存在或来源不符')
        path=self.safe_asset(row['path']);preview=self.safe_asset(row['preview_path'])
        if sha(path.read_bytes())!=row['asset_sha']:raise ValueError('原图哈希不符')
        result=self.metadata(row);result.update(local_path=str(path),preview_path=str(preview),preview_mime='image/jpeg',
            original_mime=row['mime'],
            note='请查看返回的原图／预览再引用具体数字；embedding 不提供 OCR 或数值保证。')
        return {'ok':True,'status':'ok','document':result}

    def queue_coverage(self,source,doc_key=None):
        counts={'tracked_jobs':0,'complete_jobs':0,'pending_jobs':0,'exhausted_jobs':0}
        for file in (self.home/'queue').glob('*.json'):
            job=json.loads(file.read_text())
            if job['source']!=source or (doc_key is not None and job['doc_key']!=doc_key):continue
            counts['tracked_jobs']+=1
            counts['complete_jobs' if job['status']=='complete' else 'pending_jobs']+=1
            if job['status']!='complete' and job.get('attempts',0)>=3:counts['exhausted_jobs']+=1
        return {**counts,'scope':'only_captured_assets; historical_documents_not_fully_backfilled'}

    def related(self,source,doc_key):
        images=[]
        if self.db.exists():
            with self.connect() as c:
                rows=c.execute('''SELECT r.*,a.path,a.width,a.height FROM refs r JOIN assets a ON a.sha=r.asset_sha
                    WHERE r.source=? AND r.doc_key=? ORDER BY r.id''',(source,doc_key)).fetchall()
                images=[self.metadata(row) for row in rows]
        return {'results':images,'coverage':self.queue_coverage(source,doc_key)}

    def search(self,source,query,start=None,end=None,limit=3,params=None,operation='search'):
        p=params or {}
        coverage=self.queue_coverage(source)
        warnings=[f"{coverage['pending_jobs']} 个图片任务待完成"] if coverage['pending_jobs'] else []
        if not self.db.exists():return {'ok':True,'status':'partial' if warnings else 'empty','results':[],
            'coverage':{**coverage,'indexed_images':0},'warnings':warnings}
        with self.connect() as c:
            rows=c.execute('''SELECT r.*,a.path,a.width,a.height,v.embedding,
                (SELECT group_concat(content,' ') FROM descriptions d WHERE d.sha=a.sha) AS extracted_text
                FROM refs r JOIN assets a ON a.sha=r.asset_sha
                LEFT JOIN vectors v ON v.sha=a.sha AND v.model=? AND v.dimensions=? AND v.recipe=?
                WHERE r.source=? ORDER BY r.date DESC,r.id''',(MODEL,DIMS,SCHEMA_VERSION,source)).fetchall()
        qvec=None
        if operation=='search' and p.get('search_mode')!='keyword' and rows:
            try:
                import numpy as np
                qvec=self.embed(query=query);qvec/=np.linalg.norm(qvec)
            except Exception:
                warnings.append('图片语义检索不可用，只匹配已有图片标题／图注')
        selected=[];pending=0;seen=set()
        for row in rows:
            meta=json.loads(row['metadata']);date=row['date']
            if (start and date<start) or (end and date>end):continue
            if any(p.get(k) and p[k]!=meta.get(k) for k in ('company','sector')):continue
            if p.get('tag') and p['tag'] not in meta.get('tags',[]):continue
            if p.get('sender') and p['sender'].lower() not in meta.get('original_source','').lower():continue
            if p.get('layer','raw')=='digest':continue
            if row['asset_sha'] in seen:continue
            if not row['embedding']:pending+=1
            terms=(query or '').casefold().split()
            kw=sum(t in (row['title']+' '+meta.get('caption','')+' '+(row['extracted_text'] or '')).casefold() for t in terms)
            score=None
            if qvec is not None and row['embedding']:
                vector=np.frombuffer(row['embedding'],dtype=np.float32)
                if vector.shape==(DIMS,) and np.isfinite(vector).all() and np.linalg.norm(vector):score=float(qvec@vector/np.linalg.norm(vector))
            if operation=='search' and score is None and not kw:continue
            item=self.metadata(row);item.update(score=score,keyword_hits=kw)
            rank=(score if score is not None else -1,kw) if qvec is not None else (kw,)
            if operation!='search':rank=(date,)
            selected.append((rank,item));seen.add(row['asset_sha'])
        if pending:warnings.append(f'{pending} 张已存原图的向量尚未完成')
        hits=[i for _,i in sorted(selected,key=lambda x:x[0],reverse=True)[:limit]]
        return {'ok':True,'status':'partial' if warnings else ('ok' if hits else 'empty'),'results':hits,'warnings':warnings,
            'coverage':{**coverage,'source_image_references':len(rows),'model':MODEL,'dimensions':DIMS}}

    def describe(self,document):
        result=self.read(document);d=result['document'];digest=d['asset_sha256']
        model='gemini-2.5-flash'
        prompt='逐字提取图中文字、表格和单位，并描述图表关系。逐项保留正负号、小数点、百分号及 bps 等单位，尤其核对靠近数字的细小负号；不要按文字颜色判断正负。区分原文与视觉估读；模糊数字或符号标为无法确认，不猜数。用原文语言和 Markdown 输出。'
        key=sha(prompt.encode())
        with self.connect() as c:old=c.execute('SELECT content FROM descriptions WHERE sha=? AND model=? AND prompt_sha=?',(digest,model,key)).fetchone()
        if old:return {**result,'description':old[0],'cached':True,'description_verified':False,
            'warning':'视觉提取可能漏读数字或正负号，引用前须核原图。'}
        from google.genai import types
        use_original=d['original_mime'] in ('image/png','image/jpeg','image/webp')
        data=Path(d['local_path'] if use_original else d['preview_path']).read_bytes()
        mime=d['original_mime'] if use_original else 'image/jpeg'
        res=self.client().models.generate_content(model=model,contents=types.Content(parts=[types.Part.from_bytes(data=data,mime_type=mime),types.Part(text=prompt)]))
        text=(res.text or '').strip()
        if not text:raise ValueError('识别结果为空；没有写缓存')
        with self.connect(True) as c,c:c.execute('INSERT OR REPLACE INTO descriptions VALUES(?,?,?,?,?)',(digest,model,key,text,now()))
        return {**result,'description':text,'cached':False,'description_verified':False,
            'warning':'视觉提取可能漏读数字或正负号，引用前须核原图。'}


def enqueue(source,doc_key,kind,payload,metadata,root=None):
    store=ImageStore(DATA_ROOT if root is None else root);key=sha((source+'\0'+doc_key).encode())
    store.home.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(store.home,0o700)
    file=store.home/'queue'/(key+'.json')
    if file.exists():return {'job':key,'status':'queued_or_existing'}
    atomic_json(file,{'key':key,'source':source,'doc_key':doc_key,'kind':kind,'payload':payload,
                     'metadata':metadata,'created_at':now(),'status':'queued','results':{},'attempts':0})
    return {'job':key,'status':'queued'}


def drain(source=None,seconds=120,max_images=48):
    """Called by existing ingestion CLIs; worker has Pillow/PyMuPDF in wiki venv."""
    import subprocess
    args=['bash',str(CODE_ROOT/'scripts/research_images.sh'),'process','--seconds',str(seconds),'--max-images',str(max_images)]
    if source:args+=['--source',source]
    try:
        p=subprocess.run(args,capture_output=True,text=True,timeout=seconds+80)
        return json.loads(p.stdout)
    except Exception as exc:return {'ok':False,'status':'partial','error':type(exc).__name__,'hint':'原文流程继续；图片队列保留，运行 research_images.sh process 续跑。'}


def main():
    ap=argparse.ArgumentParser(description=__doc__);sub=ap.add_subparsers(dest='command',required=True)
    p=sub.add_parser('process');p.add_argument('--source',choices=SOURCES);p.add_argument('--seconds',type=int,default=120);p.add_argument('--max-images',type=int,default=48);p.add_argument('--retry',action='store_true')
    for name in ('read','describe'):
        p=sub.add_parser(name);p.add_argument('--document',required=True)
    sub.add_parser('status')
    a=ap.parse_args();store=ImageStore()
    try:
        if a.command=='process':
            from research_image_ingest import process
            result=process(store,a.source,a.seconds,a.max_images,a.retry)
        elif a.command=='read':result=store.read(a.document)
        elif a.command=='describe':result=store.describe(a.document)
        else:
            counts={}
            if store.db.exists():
                with store.connect() as c:
                    counts={t:c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('assets','refs','vectors','descriptions')}
            jobs=[json.loads(f.read_text()) for f in (store.home/'queue').glob('*.json')]
            result={'ok':True,'counts':counts,'jobs':{s:sum(j['status']==s for j in jobs) for s in set(j['status'] for j in jobs)},'model':MODEL,'dimensions':DIMS}
    except Exception as exc:result={'ok':False,'status':'error','error':type(exc).__name__,'hint':'检查既有 Gemini 配置、图片队列和资产文件；错误未包含凭据或签名 URL。'}
    print(json.dumps(result,ensure_ascii=False));return 0 if result.get('ok') else 1


if __name__=='__main__':sys.exit(main())
