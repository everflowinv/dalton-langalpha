"""Bounded, resumable image ingestion. Uses original authenticated sources only."""
import base64
import http.client
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile

from research_images import MAX_BYTES,atomic_json,sha,now


class Images(HTMLParser):
    def __init__(self):super().__init__();self.items=[]
    def handle_starttag(self,tag,attrs):
        if tag.lower()=='img':
            a=dict(attrs);src=a.get('src') or a.get('data-src')
            if src:self.items.append({'src':src,'name':a.get('alt',''),'width':a.get('width'),'height':a.get('height')})


def decoration(item):
    for k in ('width','height'):
        v=str(item.get(k) or '')
        if v.isdigit() and int(v)<=32:return True
    label=(item.get('name','')+' '+urllib.parse.urlsplit(item.get('src','')).path).lower()
    return bool(re.search(r'(?:^|[\W_])(logo|signature|spacer|tracking|pixel|beacon|unsubscribe)(?:[\W_]|$)',label))


def gmail_items(message):
    parts=[];html=[]
    def walk(node):
        body=node.get('body',{});mime=node.get('mimeType','')
        if mime.startswith('image/'):
            headers={h['name'].lower():h['value'] for h in node.get('headers',[])}
            parts.append({'kind':'gmail','name':node.get('filename',''),'mime':mime,'body':body,
                'cid':headers.get('content-id','').strip('<>'),'src':'cid:'+headers.get('content-id','').strip('<>')})
        elif mime=='text/html' and body.get('data'):
            html.append(base64.urlsafe_b64decode(body['data']).decode(errors='replace'))
        for child in node.get('parts',[]):walk(child)
    walk(message.get('payload',{}));parser=Images();parser.feed('\n'.join(html))
    bycid={i['src'][4:]:i for i in parser.items if i['src'].startswith('cid:')}
    for part in parts:
        tag=bycid.get(part['cid'],{})
        part.update({k:tag[k] for k in ('width','height') if k in tag})
        if tag.get('name'):part['name']+=' '+tag['name']
        part['locator']='mime:'+str(part['cid'] or part['body'].get('attachmentId') or sha(part['body'].get('data','').encode()))
    parts += [{'kind':'url',**i,'locator':'url:'+sha(i['src'].encode())} for i in parser.items if not i['src'].startswith('cid:')]
    return parts


def gws(*args):
    p=subprocess.run(['gws',*args],capture_output=True,text=True,timeout=35)
    if p.returncode:raise RuntimeError('gmail_read_failed')
    return json.loads(p.stdout)


def public_address(host,port):
    addresses=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('non_public_image_host')
    return addresses[0][4][0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        ip=public_address(self.host,self.port)
        sock=socket.create_connection((ip,self.port),timeout=self.timeout)
        self.sock=self._context.wrap_socket(sock,server_hostname=self.host)


def download_image(url):
    if url.startswith('data:image/'):
        head,data=url.split(',',1)
        if ';base64' not in head:raise ValueError('unsupported_data_image')
        value=base64.b64decode(data,validate=True)
        if len(value)>MAX_BYTES:raise ValueError('image_too_large')
        return value
    for _ in range(4):
        u=urllib.parse.urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (None,443):
            raise ValueError('unsupported_image_url')
        c=PinnedHTTPS(u.hostname,timeout=20,context=ssl.create_default_context())
        try:
            # Some original-source object stores send valid images as octet-stream.
            # Accept their actual media response; ImageStore validates decoded bytes.
            c.request('GET',urllib.parse.urlunsplit(('', '',u.path or '/',u.query,'')),headers={'User-Agent':'ResearchImageArchive/1.0','Accept':'*/*'})
            r=c.getresponse()
            if r.status in (301,302,303,307,308):
                url=urllib.parse.urljoin(url,r.getheader('Location',''));continue
            if r.status!=200:raise ValueError('image_http_'+str(r.status))
            mime=r.getheader('Content-Type','').lower().split(';')[0].strip()
            if not mime.startswith('image/') and mime!='application/octet-stream':raise ValueError('not_an_image_response')
            data=r.read(MAX_BYTES+1)
            if len(data)>MAX_BYTES:raise ValueError('image_too_large')
            return data
        finally:c.close()
    raise ValueError('too_many_image_redirects')


def mcp(name,args):
    req=urllib.request.Request('http://127.0.0.1:8950/mcp',data=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=35) as res:d=json.load(res)
    r=d.get('result',{})
    if r.get('isError') or 'error' in d:raise RuntimeError('alphaengine_source_unavailable')
    return r.get('structuredContent') or json.loads(r['content'][0]['text'])


def file_items(path,store,job):
    path=Path(path);items=[]
    if not path.is_file() or path.stat().st_size>100*1024*1024:raise ValueError('invalid_source_file')
    ext=path.suffix.lower()
    with path.open('rb') as f:head=f.read(20)
    folder=store.home/'extracted'/job['key'];folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    def save(data,locator,name=''):
        if len(data)>MAX_BYTES:raise ValueError('embedded_image_too_large')
        file=folder/(sha(data)+'.bin')
        if not file.exists():
            with file.open('xb') as f:
                os.chmod(file,0o600);f.write(data)
        items.append({'kind':'file','path':str(file),'locator':locator,'name':name})
    if ext in ('.jpg','.jpeg','.png','.webp','.gif'):
        return [{'kind':'file','path':str(path),'locator':'original','name':path.name}]
    if ext=='.pdf' or head.startswith(b'%PDF'):
        import fitz
        with fitz.open(path) as pdf:
            # Extraction is resumable by page; page assets are content-addressed.
            begin=job.get('page_cursor',0);end=min(len(pdf),begin+20)
            for n in range(begin,end):
                page=pdf[n];images=page.get_images(full=True)
                for info in images:
                    if info[2]>=100 and info[3]>=100:
                        save(pdf.extract_image(info[0])['image'],f'page:{n+1}:xref:{info[0]}',f'第 {n+1} 页图片')
                if len(page.get_drawings())>=5:
                    save(page.get_pixmap(matrix=fitz.Matrix(1.6,1.6),alpha=False).tobytes('png'),f'page:{n+1}:render',f'第 {n+1} 页（含矢量图）')
            job['next_page_cursor']=end if end<len(pdf) else None
        return items
    if ext=='.docx':
        with zipfile.ZipFile(path) as z:
            for f in z.infolist():
                if f.filename.startswith('word/media/') and f.filename.lower().endswith(('.png','.jpg','.jpeg','.gif','.webp')):
                    if f.file_size>MAX_BYTES:raise ValueError('embedded_image_too_large')
                    save(z.read(f),f.filename,Path(f.filename).name)
        return items
    if ext=='.eml':
        from email import policy
        from email.parser import BytesParser
        from funda_ai import mime_payload
        payload={'payload':mime_payload(BytesParser(policy=policy.default).parsebytes(path.read_bytes()))}
        return gmail_items(payload)
    if ext in ('.html','.htm') or b'<html' in head.lower() or b'<!doctype' in head.lower():
        parser=Images();parser.feed(path.read_text(errors='replace'))
        return [{'kind':'url',**i,'locator':'html:'+sha(i['src'].encode())} for i in parser.items]
    return []


def discover(job,store):
    if job['kind']=='gmail':return gmail_items(job['payload'])
    if job['kind']=='file':return file_items(job['payload']['path'],store,job)
    if job['kind']=='tmtb':
        node_bin=(
            os.environ.get('DALTON_NODE_BIN','').strip()
            or shutil.which('node')
            or ('/opt/homebrew/bin/node' if os.access('/opt/homebrew/bin/node',os.X_OK) else '')
            or ('/usr/local/bin/node' if os.access('/usr/local/bin/node',os.X_OK) else '')
            or str(Path.home()/'.openclaw/tools/node/bin/node')  # 兼容旧 node 路径
        )
        probe_env={**os.environ,'DALTON_RESEARCH_HOME':str(store.root)}
        probe=subprocess.run([node_bin,str(store.code_root/'scripts/alphaengine-mcp/export-image-source.mjs'),job['doc_key'].split(':',1)[-1]],capture_output=True,text=True,timeout=40,env=probe_env)
        details=json.loads(probe.stdout)
        if not details.get('ok'):raise RuntimeError('alphaengine_source_unavailable')
        if details.get('local_path'):
            original=Path(details['local_path']).resolve()
            if not original.is_relative_to((store.home/'upstream').resolve()):raise ValueError('unexpected_source_path')
            job['original_path']=str(original.relative_to(store.root))
            return file_items(original,store,job)
        if not details.get('attachment_available'):return []
        result=mcp('download_document',{'doc_id':job['doc_key'].split(':',1)[-1]})
        path=Path(result['local_path']).resolve()
        raw_dl_dir=os.environ.get('DALTON_ALPHAENGINE_DOWNLOAD_DIR','').strip()
        default_dl_dir=(Path.home()/'.openclaw/workspace/temp/alphaengine-mcp-downloads').resolve()  # 兼容旧服务默认下载目录
        allowed_dl_dirs=[
            Path(raw_dl_dir).expanduser().resolve() if raw_dl_dir else default_dl_dir,
            (store.root/'temp/alphaengine-mcp-downloads').resolve(),
            (store.code_root/'temp/alphaengine-mcp-downloads').resolve(),
        ]
        raw_ocw=os.environ.get('DALTON_OCW_ROOT','').strip()
        if raw_ocw:
            allowed_dl_dirs.append((Path(raw_ocw).expanduser()/'temp/alphaengine-mcp-downloads').resolve())
        if not any(path.is_relative_to(d) for d in allowed_dl_dirs):
            raise ValueError('unexpected_download_path')
        # Preserve original HTML/PDF as received, separately from image assets.
        original=store.home/'originals'/job['key']/path.name;original.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if not original.exists():
            shutil.copy2(path,original);os.chmod(original,0o600)
        job['original_path']=str(original.relative_to(store.root))
        return file_items(original,store,job)
    raise ValueError('unknown_image_job')


def fetch_item(item,job):
    if item['kind']=='file':return Path(item['path']).read_bytes()
    if item['kind']=='gmail':
        body=item['body']
        if body.get('data'):return base64.urlsafe_b64decode(body['data'])
        message_id=job.get('metadata',{}).get('gmail_message_id') or job['doc_key']
        result=gws('gmail','users','messages','attachments','get','--params',json.dumps({'userId':'me','messageId':message_id,'id':body['attachmentId']}))
        return base64.urlsafe_b64decode(result['data'])
    if item['kind']=='url':return download_image(item['src'])
    raise ValueError('unknown_image_item')


def process(store,source=None,seconds=120,max_images=48,retry=False):
    if not 1<=seconds<=600 or not 1<=max_images<=500:raise ValueError('invalid_worker_limits')
    import fcntl
    store.home.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=(store.home/'worker.lock').open('w')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close();return {'ok':True,'status':'pending','reason':'另一个图片 worker 正在处理，队列保留'}
    deadline=time.monotonic()+seconds;processed=0;indexed=0;skipped=0;errors=0;jobs=[]
    try:
        # New Gmail arrivals precede historical EML backfill, so history cannot
        # hold up the image evidence needed for the next market digest.
        files=sorted((store.home/'queue').glob('*.json'),key=lambda x:(
            json.loads(x.read_text()).get('kind')!='gmail',x.stat().st_mtime))
        for file in files:
            if time.monotonic()>=deadline or processed>=max_images:break
            job=json.loads(file.read_text())
            if (source and source!=job['source']) or job['status']=='complete':continue
            if job.get('attempts',0)>=3 and not retry:continue
            job['attempts']=job.get('attempts',0)+1
            try:
                if 'items' not in job:job['items']=discover(job,store)
                for item in job['items']:
                    locator=item['locator'];prev=job['results'].get(locator,{})
                    if prev.get('status') in ('indexed','skipped'):continue
                    if time.monotonic()>=deadline or processed>=max_images:break
                    if decoration(item):job['results'][locator]={'status':'skipped','reason':'decoration_or_tracking'};skipped+=1;continue
                    processed+=1
                    try:
                        data=fetch_item(item,job)
                        meta={**job['metadata'],'caption':item.get('name','')}
                        result=store.add(data,job['source'],job['doc_key'],locator,meta)
                        if result.get('skipped'):result={'status':'skipped','reason':result['skipped']};skipped+=1
                        else:indexed+=1
                        job['results'][locator]=result
                    except Exception as exc:
                        # Do not store auth exceptions, attachment URLs or signed queries in diagnostics.
                        job['results'][locator]={'status':'error','error':type(exc).__name__};errors+=1
                    atomic_json(file,job)
                done=all(job['results'].get(i['locator'],{}).get('status') in ('indexed','skipped') for i in job['items'])
                if done and job.get('next_page_cursor') is not None:
                    job['page_cursor']=job.pop('next_page_cursor');job.pop('items');job['status']='pending';job['attempts']=0
                else:job['status']='complete' if done else 'pending'
                if done:job.pop('error',None)
                # A size/time cap is normal backlog, not a failed attempt.
                if any(i['locator'] not in job['results'] for i in job.get('items',[])):job['attempts']=max(0,job['attempts']-1)
            except Exception as exc:job['status']='pending';job['error']=type(exc).__name__;errors+=1
            job['updated_at']=now();atomic_json(file,job)
            jobs.append({'source':job['source'],'doc_key':job['doc_key'],'status':job['status']})
        remaining=[]
        for file in (store.home/'queue').glob('*.json'):
            j=json.loads(file.read_text())
            if (not source or j['source']==source) and j['status']!='complete':remaining.append(j['key'])
        return {'ok':True,'status':'partial' if remaining else 'ok','indexed':indexed,'skipped':skipped,
                'errors':errors,'pending_jobs':len(remaining),'jobs':jobs,'ocr_calls':0}
    finally:lock.close()
