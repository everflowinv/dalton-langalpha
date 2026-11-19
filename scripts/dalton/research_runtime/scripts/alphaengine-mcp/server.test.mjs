import test from 'node:test';
import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { searchLibrary, listFilters, listRecordingTasks, getRecordingStatus, getRecordingTask, getDocumentInfo,
  compactSearchResult, documentMetadata, resultMatches, requireDatePair,
  createAccountManager, createMacOsKeychain, isValidKeychainAccount, quoteSecurityInteractiveArg,
  classifyUpstreamError, isQuotaOrRateLimitError,
  resolveEndpointQuotaType, formatSanitizedErrorSnippet, fetchWithToken, apiRequest, listAttachments,
  downloadDocument, authStatus, sendDefaultOpsAlert } from './server.mjs';

const row = (id, extra = {}) => ({id,doc_id:id,code:'summary',title:`Meeting ${id}`,publish_time:'2026-10-28 08:00:00',
  rank_date:'2026-10-28 14:56:43',institution_meeting_flag:true,personal_meeting_flag:false,
  owner_institution_id:null,type_show_name:'分析师纪要',...extra});
function pagingRequest(rows) {
  const requests=[];
  const request=async (path,options)=>{
    assert.equal(path,'/summary/search/list/new');
    const p=JSON.parse(options.body);requests.push(p);
    const start=p.search_after?.[0]||0,end=Math.min(rows.length,start+20);
    return {results:rows.slice(start,end),total:rows.length,search_after:end<rows.length?[end]:null,has_next_page:end<rows.length};
  };
  return {request,requests};
}

test('institution category keeps summary documents, original titles and provenance', async()=>{
  const r=row('290001790949577_inst_134249',{title:'GS Expert Call on Core Implications of Frontier Foundation Models on Robotics AI',institution_name:['Goldman Sachs']});
  const {request,requests}=pagingRequest([r]);
  const x=await searchLibrary({document_categories:['institution_meetings'],limit:5},request);
  assert.equal(x.returned,1);assert.equal(x.results[0].title,r.title);
  assert.equal(x.results[0].institution_meeting_flag,true);assert.equal(x.results[0].owner_institution_id,null);
  assert.equal(x.results[0].type_name,'分析师纪要');assert.deepEqual(x.results[0].sources,['Goldman Sachs']);
  assert.equal(x.results[0].document_code,'summary');assert.equal(requests[0].code,'institutionMeeting');
  assert.equal(requests[0].order_by,'-time');assert.equal(x.has_more,false);
});

test('paging different client limits preserves all rows and duplicate cursor requests are repeatable',async()=>{
  const rows=Array.from({length:1762},(_,i)=>row(`${i}_inst_134249`));
  const {request,requests}=pagingRequest(rows);let out=[],cursor;
  do {
    const args={meeting_scope:'institution',limit:out.length?50:5,...(cursor?{cursor}:{})};
    const page=await searchLibrary(args,request);out.push(...page.results.map(r=>r.doc_id));
    if (out.length===55) {
      const replay=await searchLibrary(args,request);
      assert.deepEqual(replay.results,page.results);
    }
    cursor=page.cursor;
    if(!page.has_more)break;
  }while(out.length<=1762);
  assert.deepEqual(out,rows.map(r=>r.doc_id));
  assert.equal(new Set(out).size,1762);
  assert.ok(requests.some(r=>r.distinct_ids.length===20));
});

test('mismatched filters do not consume a cursor and older publish_time does not reorder it',async()=>{
  const rows=[row('new',{rank_date:'2026-10-28 15:00:00',publish_time:'2026-10-27 08:00:00'}),row('old')];
  const {request}=pagingRequest(rows);
  const first=await searchLibrary({meeting_scope:'institution',limit:1},request);
  assert.equal(first.results[0].doc_id,'new');
  await assert.rejects(searchLibrary({meeting_scope:'personal',cursor:first.cursor},request),/FILTER_MISMATCH/);
  const second=await searchLibrary({meeting_scope:'institution',cursor:first.cursor,limit:1},request);
  assert.equal(second.results[0].doc_id,'old');
});

test('query sort safety, scope conflicts and title/company filters',async()=>{
  await assert.rejects(searchLibrary({query:'Tesla',sort:'newest'}),/E_QUERY_SORT/);
  await assert.rejects(searchLibrary({meeting_scope:'institution',document_categories:['personal_meetings']}),/SCOPE_CONFLICT/);
  await assert.rejects(searchLibrary({meeting_scope:'institution',document_categories:['foreign_report']}),/SCOPE_CONFLICT/);
  const {request,requests}=pagingRequest([]);
  await searchLibrary({query:'Tesla',search_in:'title',company_names:['Tesla, Inc.'],first_regions:['北美']},request);
  assert.equal(requests[0].query_type,'1');assert.equal(requests[0].order_by,'comprehensive');
  assert.deepEqual(requests[0].company_name,['Tesla, Inc.']);assert.deepEqual(requests[0].first_region,['北美']);
});

test('raw scope code and personal scope cannot leak unrelated records',async()=>{
  const {request}=pagingRequest([row('inst'),row('personal',{institution_meeting_flag:false,personal_meeting_flag:true})]);
  const x=await searchLibrary({document_codes:['personalMeeting']},request);
  assert.deepEqual(x.results.map(r=>r.doc_id),['personal']);
});

test('source date and hierarchical type checks still work',()=>{
  const filters={codes:['summary'],typeIds:['PARENT'],sourceNames:['Goldman Sachs'],markets:[],dateFrom:'2026-10-28'};
  const r=row('x',{type_id:'CHILD',type_full_id:'PARENT:CHILD',institution_name:['Goldman Sachs']});
  assert.equal(resultMatches(r,filters),true);
  assert.equal(resultMatches({...r,publish_time:'2026-10-27'},filters),false);
  assert.equal(resultMatches({...r,institution_name:['Other']},filters),false);
});

test('bad dates and non-advancing upstream cursors fail visibly',async()=>{
  assert.throws(()=>requireDatePair('2026-02-30','2026-03-01'),/BAD_DATE/);
  assert.throws(()=>requireDatePair('2026-10-01',undefined),/BAD_DATE_RANGE/);
  await assert.rejects(searchLibrary({meeting_scope:'institution'},async()=>({results:[],has_next_page:true,search_after:null})),/UPSTREAM_CURSOR/);
});

test('filters paginate and expose account categories/industry/company dictionaries',async()=>{
  const request=async path=>{
    if(path==='/summary/search/category')return [{key:'institutionMeeting',show:true}];
    if(path.startsWith('/summary/industry/list'))return [{industry_id:'A',industry_name:'机械',child:[{industry_id:'B',industry_name:'机器人'}]}];
    if(path.startsWith('/api/v1/kmpadmin/dic/company/list?keyword='))return [{company_name:'Tesla, Inc.',company_code:'TSLA'}];
    throw new Error(path);
  };
  const one=await listFilters({filter_type:'industries',limit:1},request);
  assert.equal(one.next_offset,1);assert.equal(one.total,2);
  const two=await listFilters({filter_type:'industries',offset:one.next_offset,limit:1},request);
  assert.equal(two.industries[0].industry_name,'机器人');assert.equal(two.next_offset,null);
  assert.equal((await listFilters({filter_type:'upstream_categories'},request)).upstream_categories[0].key,'institutionMeeting');
  assert.equal((await listFilters({filter_type:'companies',query:'Tesla'},request)).companies[0].company_code,'TSLA');
});

test('recording task page mode ignores false cursor-mode has_next_page',async()=>{
  let sent;
  const out=await listRecordingTasks({page:2,limit:5,states:[3]},async(path,options)=>{
    sent=JSON.parse(options.body);
    return {current_page_num:2,total:1879,has_next_page:false,results:[{roadshow_id:'x',transcribe_state:3}]};
  });
  assert.equal(out.has_more,true);assert.equal(out.next_page,3);assert.equal(out.results[0].state_name,'录制失败');
  assert.equal(sent.page_num,2);assert.deepEqual(sent.transcribe_state,[3]);
});

test('task detail encodes IDs and suppresses credentials/signed links',async()=>{
  let path;
  const out=await getRecordingTask({roadshow_id:'a:2/b',wechat_group:'a&b'},async p=>{path=p;return {title:'test',token:'secret',password:'secret',file_url:'https://example.test/?signature=secret'};});
  assert.match(path,/a%3A2%2Fb\?wechatGroup=a%26b$/);assert.equal(out.detail.token,undefined);assert.equal(out.detail.password,undefined);
  assert.equal(out.detail.file_url,'[link omitted]');
});

test('base metadata does not fetch full text and legacy metadata remains additive',async()=>{
  let path;
  const out=await getDocumentInfo({doc_id:'123_inst_1'},async p=>{path=p;return {read:false,document_type_id:'foreign_summary_roadshow'};});
  assert.match(path,/\/summary\/doc\/base\?doc_id=/);assert.equal(out.read,false);
  const r=row('x',{original_text:'text',author_list:['Author'],institution_name:['Source']});
  const meta=documentMetadata(r);assert.equal(meta.type_name,'分析师纪要');assert.equal(meta.publish_date,'2026-10-28');
  assert.deepEqual(meta.authors,['Author']);assert.equal(meta.institution_meeting_flag,true);
  assert.equal(compactSearchResult(r,false).snippet,undefined);
});

test('separate transcript/digest fields and Office attachments stay accessible without audio confusion',async()=>{
  const {selectDocumentText,documentFileCandidates,extensionFor,listAttachments}=await import('./server.mjs');
  const detail={content:'legacy text',original_text:'full transcript',digest:'long summary'};
  assert.equal(selectDocumentText(detail),'legacy text');assert.equal(selectDocumentText(detail,'original_text'),'full transcript');
  assert.equal(selectDocumentText(detail,'digest'),'long summary');
  const files={file_url:'https://x.test/audio.mp3',original_pdf_path:'https://x.test/report.pdf',attachment_path:'https://x.test/model.xlsx'};
  assert.equal(documentFileCandidates(files).length,2);
  assert.equal(extensionFor('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','https://x.test/file',Buffer.from('PK')),'.xlsx');
  const out=await listAttachments({doc_id:'x'},async()=>[{file_path:'https://x.test/model.xlsx',file_name:'model.xlsx'}]);
  assert.equal(out.attachments[0].attachment_index,0);assert.equal(out.attachments[0].downloadable_document,true);
  assert.equal(out.attachments[0].file_path,'[link omitted]');
});

test('image exporter document IDs include institution suffixes but exclude filesystem paths',async()=>{
  const {isValidDocumentId}=await import('./server.mjs');
  for(const id of ['123','290001790954279_inst_134249'])assert.equal(isValidDocumentId(id),true);
  for(const id of ['',null,'../x','x/y','a\\b','a?b','a\n'])assert.equal(isValidDocumentId(id),false);
});

test('watchlist dictionaries resolve search group IDs without mutating groups',async()=>{
  let called;
  const request=async(path,opts)=>{called=path;assert.equal(opts,undefined);return [{id:49987,name:'自选股',code:'TSLA.O'}];};
  const groups=await listFilters({filter_type:'watchlist_groups'},request);
  assert.equal(groups.watchlist_groups[0].id,49987);
  const members=await listFilters({filter_type:'watchlist_stocks',group_id:'49987',query:'TSLA'},request);
  assert.match(called,/groupId=49987$/);assert.equal(members.returned,1);
  await assert.rejects(listFilters({filter_type:'watchlist_stocks'},request),/group_id/);
});

function makeFakeJwt(payload) {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256', typ: 'JWT' })).toString('base64url');
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  return `${header}.${body}.fakesig`;
}

function makeTestHarness(initialNowMs = Date.parse('2026-11-19T02:00:00Z'), cooldownMs = 6 * 3600 * 1000) {
  const dir = mkdtempSync(path.join(tmpdir(), 'ae-multi-account-'));
  const stateFile = path.join(dir, 'alphaengine_accounts_state.json');
  const keychainStore = new Map();
  let currentNowMs = initialNowMs;
  let desktopState = { appRunning: true, candidates: [] };
  const alertsSent = [];
  const keychainCalls = [];

  const keychain = {
    save(userId, bundle) {
      keychainCalls.push({ op: 'save', userId });
      keychainStore.set(String(userId), {
        userId: String(userId),
        token: bundle.token,
        refreshToken: bundle.refreshToken ?? bundle.refresh_token ?? null,
        exp: bundle.exp,
        nickname: bundle.nickname ?? null,
        phoneLast4: bundle.phoneLast4 ?? null,
      });
      return true;
    },
    load(userId) {
      keychainCalls.push({ op: 'load', userId });
      return keychainStore.get(String(userId)) || null;
    },
  };

  const manager = createAccountManager({
    stateFile,
    cooldownMs,
    nowMs: () => currentNowMs,
    readDesktopSnapshot: () => desktopState,
    keychain,
    sendOpsAlert: async (alert) => {
      alertsSent.push(alert);
      return true;
    },
  });

  return {
    dir,
    stateFile,
    keychainStore,
    keychainCalls,
    alertsSent,
    manager,
    setNowMs: (ms) => { currentNowMs = ms; },
    advanceMs: (ms) => { currentNowMs += ms; },
    setDesktop: (next) => { desktopState = next; },
    cleanup: () => rmSync(dir, { recursive: true, force: true }),
  };
}

test('quota error classifier matches explicit structured signals, treats ambiguous permission-or-quota as retry-only, and ignores echoed params', () => {
  assert.equal(classifyUpstreamError(new Error('E_UPSTREAM_API: 无查看权限或额度已达上限')), 'ambiguous_retry');
  assert.equal(isQuotaOrRateLimitError(new Error('E_UPSTREAM_API: 无查看权限或额度已达上限')), false);
  assert.equal(isQuotaOrRateLimitError(new Error('E_UPSTREAM_API: 额度已达上限')), true);
  assert.equal(isQuotaOrRateLimitError(new Error('E_UPSTREAM_HTTP: HTTP 429: {"message":"Too Many Requests"}')), true);
  assert.equal(isQuotaOrRateLimitError(new Error('E_UPSTREAM_API: quota_exceeded')), true);
  assert.equal(isQuotaOrRateLimitError({ message: 'rate_limited', httpStatus: 200 }), true);
  assert.equal(isQuotaOrRateLimitError(new Error('E_BAD_DATE: 429')), false);
  assert.equal(isQuotaOrRateLimitError(new Error('E_UPSTREAM_HTTP: HTTP 401: 登录已过期')), false);
  assert.equal(isQuotaOrRateLimitError(new Error('E_UPSTREAM_API: 数据不存在')), false);

  // Echoed request params containing "quota_exceeded" in HTTP 400 must NOT classify as quota
  assert.equal(
    classifyUpstreamError({
      httpStatus: 400,
      message: 'E_UPSTREAM_HTTP: HTTP 400: {"message":"invalid query","params":{"query":"quota_exceeded"}}',
      upstreamBody: { message: 'invalid query', params: { query: 'quota_exceeded' } },
    }),
    'none',
  );
  // HTTP 200 with top-level status_code: 429 must classify as quota
  assert.equal(
    classifyUpstreamError({
      httpStatus: 429,
      message: 'E_UPSTREAM_HTTP: HTTP 429',
      upstreamBody: { status_code: 429, message: 'slow down' },
    }),
    'quota',
  );
});

test('A search quota exhausted -> switches to B and succeeds; search exhaustion does not affect read quota; no token leakage', async () => {
  const h = makeTestHarness();
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: '研报账号A', mobile: '13800001111', exp: nowSec + 20 * 86400 });
    const tokenB = makeFakeJwt({ user_id: 'acc_b', name: '研报账号B', phone: '13900002222', exp: nowSec + 25 * 86400 });
    const refreshA = 'refresh-secret-a-999';
    const refreshB = 'refresh-secret-b-888';

    // Account B logged in earlier, saved to keychain
    h.setDesktop({
      appRunning: true,
      candidates: [{ userId: 'acc_b', token: tokenB, refreshToken: refreshB, exp: nowSec + 25 * 86400, nickname: '研报账号B', phoneLast4: '2222' }],
    });
    h.manager.syncAccounts();

    // Now Account A is active in the Desktop App
    h.setDesktop({
      appRunning: true,
      candidates: [{ userId: 'acc_a', token: tokenA, refreshToken: refreshA, exp: nowSec + 20 * 86400, nickname: '研报账号A', phoneLast4: '1111' }],
    });
    h.manager.syncAccounts();

    assert.ok(h.keychainStore.has('acc_a'));
    assert.ok(h.keychainStore.has('acc_b'));

    const stdoutChunks = [];
    const stderrChunks = [];
    const origStdout = process.stdout.write.bind(process.stdout);
    const origStderr = process.stderr.write.bind(process.stderr);
    process.stdout.write = (chunk, ...rest) => { stdoutChunks.push(String(chunk)); return origStdout(chunk, ...rest); };
    process.stderr.write = (chunk, ...rest) => { stderrChunks.push(String(chunk)); return origStderr(chunk, ...rest); };

    try {
      const seenTokens = [];
      const searchResult = await h.manager.executeWithFailover('search', async (token) => {
        seenTokens.push(token);
        if (token === tokenA) {
          throw new Error(`E_UPSTREAM_API: 额度已达上限 (leaked=${tokenA})`);
        }
        return { ok: true, results: [{ doc_id: 'doc_1' }] };
      });
      assert.deepEqual(seenTokens, [tokenA, tokenB]);
      assert.equal(searchResult.ok, true);

      // Verify search exhaustion on A does NOT prevent A from serving 'read'
      const readTokens = [];
      const readResult = await h.manager.executeWithFailover('read', async (token) => {
        readTokens.push(token);
        return { doc_id: 'doc_1', content: 'full text' };
      });
      assert.deepEqual(readTokens, [tokenA]);
      assert.equal(readResult.doc_id, 'doc_1');

      const status = await authStatus({}, h.manager);
      assert.equal(status.status, 'ready');
      assert.equal(status.authenticated, true);
      assert.equal(status.active_search_user_id, 'acc_b');
      assert.equal(status.active_read_user_id, 'acc_a');
      assert.equal(status.accounts.length, 2);

      const accAStatus = status.accounts.find((a) => a.user_id === 'acc_a');
      const accBStatus = status.accounts.find((a) => a.user_id === 'acc_b');
      assert.equal(accAStatus.nickname, '研报账号A');
      assert.equal(accAStatus.phone_last4, '1111');
      assert.equal(accAStatus.search_quota.status, 'exhausted');
      assert.equal(accAStatus.search_quota.label, '已用完');
      assert.ok(accAStatus.search_quota.exhausted_at);
      assert.equal(accAStatus.read_quota.status, 'available');
      assert.equal(accAStatus.read_quota.label, '可用');
      assert.equal(accBStatus.nickname, '研报账号B');
      assert.equal(accBStatus.phone_last4, '2222');
      assert.equal(accBStatus.search_quota.status, 'available');

      // Check 0600 file permissions and zero token leakage in state file, status output, and stdio
      const mode = statSync(h.stateFile).mode & 0o777;
      assert.equal(mode, 0o600);
      const diskState = readFileSync(h.stateFile, 'utf8');
      const combinedText = [
        diskState,
        JSON.stringify(status),
        JSON.stringify(searchResult),
        stdoutChunks.join(''),
        stderrChunks.join(''),
      ].join('\n');
      for (const secret of [tokenA, tokenB, refreshA, refreshB]) {
        assert.equal(combinedText.includes(secret), false, 'secret leaked in output or disk state');
      }
    } finally {
      process.stdout.write = origStdout;
      process.stderr.write = origStderr;
    }
  } finally {
    h.cleanup();
  }
});

test('both accounts exhausted returns original quota error; cooled account retries after cooldown and clears mark on success', async () => {
  const h = makeTestHarness(Date.parse('2026-11-19T02:00:00Z'), 6 * 3600 * 1000);
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: '账号A', exp: nowSec + 20 * 86400 });
    const tokenB = makeFakeJwt({ user_id: 'acc_b', nickname: '账号B', exp: nowSec + 20 * 86400 });

    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: nowSec + 20 * 86400, nickname: '账号B' }] });
    h.manager.syncAccounts();
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400, nickname: '账号A' }] });
    h.manager.syncAccounts();

    // Both A and B return quota errors -> must throw A's original error
    await assert.rejects(
      h.manager.executeWithFailover('search', async (token) => {
        if (token === tokenA) throw new Error(`E_UPSTREAM_API: 额度已达上限 [orig-A] token=${tokenA}`);
        throw new Error('E_UPSTREAM_HTTP: HTTP 429: {"message":"Too Many Requests"}');
      }),
      (err) => {
        assert.match(err.message, /额度已达上限 \[orig-A\]/);
        assert.equal(err.message.includes(tokenA), false);
        assert.equal(err.message.includes(tokenB), false);
        return true;
      },
    );

    // While both are in 6h cooldown, upstream is NOT called
    let upstreamCalls = 0;
    h.advanceMs(5 * 3600 * 1000);
    await assert.rejects(
      h.manager.executeWithFailover('search', async () => {
        upstreamCalls += 1;
        return { ok: true };
      }),
      /quota_exceeded/,
    );
    assert.equal(upstreamCalls, 0);

    // After 6h cooldown elapses, next request retries App-preferred account A and clears mark on success
    h.advanceMs(1 * 3600 * 1000 + 1000);
    const retriedTokens = [];
    const recovered = await h.manager.executeWithFailover('search', async (token) => {
      retriedTokens.push(token);
      return { ok: true, recovered: true };
    });
    assert.deepEqual(retriedTokens, [tokenA]);
    assert.equal(recovered.recovered, true);

    const statusAfter = await authStatus({}, h.manager);
    const accA = statusAfter.accounts.find((a) => a.user_id === 'acc_a');
    assert.equal(accA.search_quota.status, 'available');
    assert.equal(accA.search_quota.exhausted_at, null);
  } finally {
    h.cleanup();
  }
});

test('expiring account (< 3 days) sends ops alert once per day per account and keychain fallback works when app is closed', async () => {
  const startMs = Date.parse('2026-11-19T02:00:00Z');
  const h = makeTestHarness(startMs);
  try {
    const nowSec = Math.floor(startMs / 1000);
    const tokenExpiring = makeFakeJwt({ user_id: 'acc_exp', mobile: '13812345678', exp: nowSec + 2 * 86400 });
    const tokenFresh = makeFakeJwt({ user_id: 'acc_fresh', nickname: '长期号', exp: nowSec + 20 * 86400 });

    h.setDesktop({
      appRunning: true,
      candidates: [{ userId: 'acc_exp', token: tokenExpiring, exp: nowSec + 2 * 86400, phoneLast4: '5678' }],
    });
    h.manager.syncAccounts();

    h.setDesktop({
      appRunning: true,
      candidates: [{ userId: 'acc_fresh', token: tokenFresh, exp: nowSec + 20 * 86400, nickname: '长期号' }],
    });
    h.manager.syncAccounts();

    // App is now closed -> should still be ready using keychain accounts and trigger alert for acc_exp only
    h.setDesktop({ appRunning: false, candidates: [] });
    const s1 = await authStatus({}, h.manager);
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(s1.status, 'ready');
    assert.equal(s1.authenticated, true);
    assert.equal(s1.accounts.length, 2);
    assert.equal(h.alertsSent.length, 1);
    assert.equal(h.alertsSent[0].code, 'alphaengine_token_expiring');
    assert.equal(h.alertsSent[0].detail, '尾号5678');
    assert.equal(h.alertsSent[0].message, '请在 AlphaEngine App 里登录账号 尾号5678');

    // Second check on the same day must NOT alert again
    await authStatus({}, h.manager);
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(h.alertsSent.length, 1);

    // Next calendar day triggers again once
    h.advanceMs(24 * 3600 * 1000);
    await authStatus({}, h.manager);
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(h.alertsSent.length, 2);
  } finally {
    h.cleanup();
  }
});

test('1) error sanitization runs BEFORE truncation so secrets crossing the 600-char boundary never leak fragments', async () => {
  const secretToken = 'secret-jwt-token-prefix-0123456789-middle-abcdef-tail-xyz987';
  const refreshSecret = 'refresh-secret-crossing-600-char-boundary-0123456789-abcdef-ghijkl';
  const padding = 'x'.repeat(565);
  const payload = {
    message: `${padding}${refreshSecret}`,
    refresh_token: refreshSecret,
    authorization: `Bearer ${secretToken}`,
    nested: { access_token: secretToken },
  };
  const snippet = formatSanitizedErrorSnippet(payload, [secretToken, refreshSecret], 600);
  assert.equal(snippet.length, 600);
  // Not even a 12-char prefix of refreshSecret or secretToken may survive in the truncated error
  assert.equal(snippet.includes(refreshSecret.slice(0, 12)), false);
  assert.equal(snippet.includes(secretToken.slice(0, 12)), false);

  const origFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify(payload), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    });
  try {
    await assert.rejects(
      fetchWithToken('/summary/search/list/new', { method: 'POST', body: '{}' }, secretToken, [refreshSecret]),
      (err) => {
        assert.equal(err.message.includes(refreshSecret.slice(0, 12)), false);
        assert.equal(err.message.includes(secretToken.slice(0, 12)), false);
        assert.equal(JSON.stringify(err.upstreamBody).includes(refreshSecret.slice(0, 12)), false);
        return true;
      },
    );
  } finally {
    globalThis.fetch = origFetch;
  }
});

test('2) keychain stores only token in base64 (no refresh_token) and skips commands exceeding 4000 bytes with token-free warning', () => {
  const securityCalls = [];
  const warnings = [];
  const store = new Map();
  const kc = createMacOsKeychain({
    service: 'dalton-alphaengine-test',
    logWarning: (msg) => warnings.push(msg),
    runSecurity: (args, input) => {
      if (args[0] === '-i') {
        securityCalls.push({ args, input, bytes: Buffer.byteLength(input, 'utf8') });
        const m = input.match(/add-generic-password -U -a (\S+) -s (\S+) -w (\S+)/);
        assert.ok(m);
        store.set(`${m[2]}:${m[1]}`, m[3]);
        return '';
      }
      if (args[0] === 'find-generic-password') {
        const key = `${args[2]}:${args[4]}`;
        const val = store.get(key);
        if (!val) throw new Error('not found');
        return val;
      }
      throw new Error('unexpected security args');
    },
  });

  const normalToken = makeFakeJwt({ user_id: 'u1', exp: 2000000000 });
  const ok = kc.save('u1', { token: normalToken, refreshToken: 'must-not-save-refresh', exp: 2000000000 });
  assert.equal(ok, true);
  assert.equal(securityCalls.length, 1);
  assert.ok(securityCalls[0].bytes <= 4000);
  const rawStored = store.get('dalton-alphaengine-test:u1');
  const decodedJson = JSON.parse(Buffer.from(rawStored, 'base64').toString('utf8'));
  assert.equal(decodedJson.token, normalToken);
  assert.equal('refresh_token' in decodedJson, false);
  assert.equal(kc.load('u1').token, normalToken);

  // Oversized token (> 4000 bytes security -i command) must NOT be written and must log a token-free warning
  const hugeToken = `eyJhbGciOiJIUzI1NiJ9.${'a'.repeat(3200)}.sig`;
  const skipped = kc.save('u_huge', { token: hugeToken, exp: 2000000000 });
  assert.equal(skipped, false);
  assert.equal(securityCalls.length, 1);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /exceeds 4000-byte limit/);
  assert.equal(warnings[0].includes(hugeToken.slice(0, 20)), false);
});

test('3) HTTP 200 with body status_code 429 switches & marks; ambiguous permission-or-quota retries once without marking', async () => {
  const h = makeTestHarness();
  const origFetch = globalThis.fetch;
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: 'A', exp: nowSec + 20 * 86400 });
    const tokenB = makeFakeJwt({ user_id: 'acc_b', nickname: 'B', exp: nowSec + 20 * 86400 });
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();

    // Ambiguous "无查看权限或额度已达上限": retries with B, if B fails too returns original error AND marks neither account
    const ambigTokens = [];
    await assert.rejects(
      h.manager.executeWithFailover('read', async (token) => {
        ambigTokens.push(token);
        const msg = token === tokenA ? '无查看权限或额度已达上限 [A]' : '无查看权限或额度已达上限 [B]';
        const err = new Error(`E_UPSTREAM_API: ${msg}`);
        err.httpStatus = 200;
        err.upstreamBody = { success: false, message: msg };
        throw err;
      }),
      /无查看权限或额度已达上限 \[A\]/,
    );
    assert.deepEqual(ambigTokens, [tokenA, tokenB]);
    const stAfterAmbig = await authStatus({}, h.manager);
    assert.equal(stAfterAmbig.accounts.find((a) => a.user_id === 'acc_a').read_quota.status, 'available');
    assert.equal(stAfterAmbig.accounts.find((a) => a.user_id === 'acc_b').read_quota.status, 'available');

    // HTTP 200 with top-level status_code: 429 triggers switch & marks A
    const authHeaders = [];
    globalThis.fetch = async (_url, init) => {
      authHeaders.push(init.headers.Authorization);
      if (init.headers.Authorization === `Bearer ${tokenA}`) {
        return new Response(JSON.stringify({ status_code: 429, message: 'Too Many Requests' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ success: true, data: { results: [] } }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };
    const res = await h.manager.executeWithFailover('search', (token, _acc, secrets) =>
      fetchWithToken('/summary/search/list/new', { method: 'POST', body: '{}' }, token, secrets),
    );
    assert.deepEqual(res, { results: [] });
    assert.deepEqual(authHeaders, [`Bearer ${tokenA}`, `Bearer ${tokenB}`]);
    const stAfter429 = await authStatus({}, h.manager);
    assert.equal(stAfter429.accounts.find((a) => a.user_id === 'acc_a').search_quota.status, 'exhausted');
  } finally {
    globalThis.fetch = origFetch;
    h.cleanup();
  }
});

test('4) concurrency orders: late success does not clear newer mark; retry re-checks state marks and 30s expiry', async () => {
  // Order 1: Req1 starts at t=100, Req2 marks A at t=102, Req1 finishes at t=103 -> must NOT clear A's mark
  const h1 = makeTestHarness(100000);
  try {
    const nowSec = Math.floor(100000 / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', exp: nowSec + 86400 });
    h1.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 86400 }] });
    h1.manager.syncAccounts();

    let releaseReq1;
    const req1Gate = new Promise((resolve) => { releaseReq1 = resolve; });
    const req1 = h1.manager.executeWithFailover('search', async () => {
      await req1Gate;
      return { ok: true };
    });

    h1.setNowMs(102000);
    await assert.rejects(
      h1.manager.executeWithFailover('search', async () => {
        throw new Error('E_UPSTREAM_HTTP: HTTP 429');
      }),
      /429/,
    );

    h1.setNowMs(103000);
    releaseReq1();
    await req1;
    const st1 = await authStatus({}, h1.manager);
    assert.equal(st1.accounts.find((a) => a.user_id === 'acc_a').search_quota.status, 'exhausted');
  } finally {
    h1.cleanup();
  }

  // Order 2 & Order 3: while Req1 awaits A, B gets marked exhausted OR B enters 30s expiry window -> retry skips B
  for (const mode of ['concurrent_mark_b', 'b_expires_during_a']) {
    const startMs = 100000;
    const h2 = makeTestHarness(startMs);
    try {
      const startSec = Math.floor(startMs / 1000);
      const tokenA = makeFakeJwt({ user_id: 'acc_a', exp: startSec + 86400 });
      const bExp = mode === 'b_expires_during_a' ? startSec + 40 : startSec + 86400;
      const tokenB = makeFakeJwt({ user_id: 'acc_b', exp: bExp });
      h2.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: bExp }] });
      h2.manager.syncAccounts();
      h2.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: startSec + 86400 }] });
      h2.manager.syncAccounts();

      let releaseA;
      const aGate = new Promise((resolve) => { releaseA = resolve; });
      const called = [];
      const req1 = h2.manager.executeWithFailover('search', async (token) => {
        called.push(token);
        if (token === tokenA) {
          await aGate;
          throw new Error('E_UPSTREAM_HTTP: HTTP 429: A hit 429');
        }
        return { ok: true };
      });

      if (mode === 'concurrent_mark_b') {
        h2.setNowMs(102000);
        // Mark B exhausted concurrently while Req1 is awaiting A
        h2.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: bExp }] });
        await assert.rejects(
          h2.manager.executeWithFailover('search', async () => {
            throw new Error('E_UPSTREAM_HTTP: HTTP 429: B hit 429');
          }),
          /429/,
        );
        h2.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: startSec + 86400 }] });
      } else {
        // Advance clock by 15s -> B has 25s left (<= 30s buffer)
        h2.setNowMs(115000);
      }

      releaseA();
      await assert.rejects(req1, /A hit 429/);
      assert.deepEqual(called, [tokenA]);
    } finally {
      h2.cleanup();
    }
  }
});

test('5) stale renderer token does not overwrite newer keychain token and newer keychain token is preferred', async () => {
  const startMs = Date.parse('2026-11-19T02:00:00Z');
  const h = makeTestHarness(startMs);
  try {
    const nowSec = Math.floor(startMs / 1000);
    const newerToken = makeFakeJwt({ user_id: 'acc_a', nickname: '新窗口', exp: nowSec + 20 * 86400 });
    const olderToken = makeFakeJwt({ user_id: 'acc_a', nickname: '旧窗口', exp: nowSec - 60 });

    // Newer module tab saved to keychain
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: newerToken, exp: nowSec + 20 * 86400, nickname: '新窗口' }] });
    h.manager.syncAccounts();

    // Newer tab closed; ps only shows stale expired renderer for the same user_id
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: olderToken, exp: nowSec - 60, nickname: '旧窗口' }] });
    h.manager.syncAccounts();

    // Keychain must still keep newerToken, and request/authStatus must use newerToken
    assert.equal(h.keychainStore.get('acc_a').token, newerToken);
    const used = [];
    await h.manager.executeWithFailover('search', async (token) => {
      used.push(token);
      return { ok: true };
    });
    assert.deepEqual(used, [newerToken]);
    const st = await authStatus({}, h.manager);
    assert.equal(st.status, 'ready');
    assert.equal(st.authenticated, true);
  } finally {
    h.cleanup();
  }
});

test('6) endpoint quota mapping routes attachments to read quota and warns when unmapped path defaults to search', async () => {
  const warnings = [];
  assert.equal(resolveEndpointQuotaType('/summary/detail/123/true?system=false', (m) => warnings.push(m)), 'read');
  assert.equal(resolveEndpointQuotaType('/summary/private/detail/123', (m) => warnings.push(m)), 'read');
  assert.equal(resolveEndpointQuotaType('/summary/attachment/list/123', (m) => warnings.push(m)), 'read');
  assert.equal(resolveEndpointQuotaType('/summary/doc/base?doc_id=123', (m) => warnings.push(m)), 'read');
  assert.equal(resolveEndpointQuotaType('/summary/search/list/new', (m) => warnings.push(m)), 'search');
  assert.equal(warnings.length, 0);
  assert.equal(resolveEndpointQuotaType('/summary/unknown/new_endpoint?foo=bar', (m) => warnings.push(m)), 'search');
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /unmapped endpoint "\/summary\/unknown\/new_endpoint"/);

  // With single account whose search quota is exhausted, listAttachments & downloadDocument (attachment) still run via read quota
  const h = makeTestHarness();
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', exp: nowSec + 20 * 86400 });
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();

    await assert.rejects(
      h.manager.executeWithFailover('search', async () => { throw new Error('E_UPSTREAM_HTTP: HTTP 429'); }),
      /429/,
    );

    const requestViaManager = (apiPath, opts = {}) =>
      h.manager.executeWithFailover(
        opts.quotaType || resolveEndpointQuotaType(apiPath, (m) => warnings.push(m)),
        async () => [{ file_path: 'https://example.test/att.pdf', file_name: 'att.pdf' }],
      );
    const attOut = await listAttachments({ doc_id: 'doc_1' }, requestViaManager);
    assert.equal(attOut.returned, 1);
    assert.equal(attOut.attachments[0].downloadable_document, true);

    // downloadDocument with invalid attachment index fails cleanly AFTER fetching attachment list via read quota
    await assert.rejects(downloadDocument({ doc_id: 'doc_1', attachment_index: 5 }, requestViaManager), /E_BAD_ATTACHMENT/);
  } finally {
    h.cleanup();
  }
});

test('7) unwritable state file logs warning without token and continues serving valid desktop/keychain tokens', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'ae-unwritable-state-'));
  const blockingFile = path.join(dir, 'not-a-dir');
  writeFileSync(blockingFile, 'blocker', 'utf8');
  const unwritableStateFile = path.join(blockingFile, 'alphaengine_accounts_state.json');
  const warnings = [];
  const kcMap = new Map();
  const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
  const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: '正常号', exp: nowSec + 20 * 86400 });

  const mgr = createAccountManager({
    stateFile: unwritableStateFile,
    nowMs: () => Date.parse('2026-11-19T02:00:00Z'),
    readDesktopSnapshot: () => ({
      appRunning: true,
      candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400, nickname: '正常号' }],
    }),
    keychain: {
      save: (uid, b) => { kcMap.set(uid, b); return true; },
      load: (uid) => kcMap.get(uid) || null,
    },
    logWarning: (msg) => warnings.push(msg),
  });

  try {
    const res = await mgr.executeWithFailover('search', async (tok) => ({ tokUsed: tok === tokenA }));
    assert.equal(res.tokUsed, true);
    const st = await authStatus({}, mgr);
    assert.equal(st.status, 'ready');
    assert.equal(st.authenticated, true);
    assert.ok(warnings.length >= 1);
    assert.equal(warnings.join('\n').includes(tokenA), false);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('8) partial delivery is marked as alerted and concurrent managers share daily lock file without duplicate alerts', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'ae-cross-proc-alert-'));
  const stateFile = path.join(dir, 'alphaengine_accounts_state.json');
  const nowMs = Date.parse('2026-11-19T02:00:00Z');
  const nowSec = Math.floor(nowMs / 1000);
  const expToken = makeFakeJwt({ user_id: 'acc_exp', nickname: '到期号', exp: nowSec + 86400 });

  // Simulate two independent processes sharing the same stateFile/markerDir
  let sendCount = 0;
  const makeMgr = () =>
    createAccountManager({
      stateFile,
      nowMs: () => nowMs,
      readDesktopSnapshot: () => ({
        appRunning: true,
        candidates: [{ userId: 'acc_exp', token: expToken, exp: nowSec + 86400, nickname: '到期号' }],
      }),
      keychain: { save: () => true, load: () => null },
      sendOpsAlert: async () => {
        sendCount += 1;
        await new Promise((r) => setTimeout(r, 20));
        return true;
      },
    });

  try {
    const m1 = makeMgr();
    const m2 = makeMgr();
    await Promise.all([m1.checkExpiringAccountsAndAlert(), m2.checkExpiringAccountsAndAlert()]);
    assert.equal(sendCount, 1);

    // Verify sendDefaultOpsAlert behavior against HTTP 502 partial delivery (any_succeeded / results ok:true)
    const envFile = path.join(dir, 'gw.env');
    writeFileSync(envFile, 'INTERNAL_SERVICE_TOKEN=test-gw-token\n', 'utf8');
    const origFetch = globalThis.fetch;
    let gwCalls = 0;
    globalThis.fetch = async () => {
      gwCalls += 1;
      return new Response(
        JSON.stringify({
          ok: false,
          any_succeeded: true,
          results: [
            { target: 'feishu:user:a', ok: true, message_id: 'om_1' },
            { target: 'feishu:user:b', ok: false, error: 'Error' },
          ],
        }),
        { status: 502, headers: { 'Content-Type': 'application/json' } },
      );
    };
    try {
      const mPartial = createAccountManager({
        stateFile: path.join(dir, 'partial_state.json'),
        nowMs: () => nowMs,
        readDesktopSnapshot: () => ({
          appRunning: true,
          candidates: [{ userId: 'acc_exp2', token: expToken, exp: nowSec + 86400, nickname: '部分成功号' }],
        }),
        keychain: { save: () => true, load: () => null },
        sendOpsAlert: ({ code, detail }) =>
          sendDefaultOpsAlert({ code, detail, envFile, gatewayUrl: 'http://127.0.0.1:8962/internal/ops-alerts' }),
      });
      await mPartial.checkExpiringAccountsAndAlert();
      await mPartial.checkExpiringAccountsAndAlert();
      assert.equal(gwCalls, 1);
    } finally {
      globalThis.fetch = origFetch;
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('GLM-1 & GLM-2) security timeout (5s) warns without blocking, account whitelist rejects unsafe user_id, and quoteSecurityInteractiveArg handles quotes/backslashes/newlines', () => {
  assert.equal(quoteSecurityInteractiveArg('123456'), '123456');
  assert.equal(quoteSecurityInteractiveArg('acc_a-1:test@x.y'), 'acc_a-1:test@x.y');
  assert.equal(quoteSecurityInteractiveArg('has space'), '"has space"');
  assert.equal(quoteSecurityInteractiveArg('a"b\\c'), '"a\\"b\\\\c"');
  assert.throws(() => quoteSecurityInteractiveArg('bad\nline'), /E_KEYCHAIN_INVALID_ARG/);
  assert.throws(() => quoteSecurityInteractiveArg('bad\rline'), /E_KEYCHAIN_INVALID_ARG/);
  assert.throws(() => quoteSecurityInteractiveArg('bad\0nul'), /E_KEYCHAIN_INVALID_ARG/);

  assert.equal(isValidKeychainAccount('123456789'), true);
  assert.equal(isValidKeychainAccount('acc_a-1:test@x.y'), true);
  assert.equal(isValidKeychainAccount(''), false);
  assert.equal(isValidKeychainAccount('bad user'), false);
  assert.equal(isValidKeychainAccount('bad"quote'), false);
  assert.equal(isValidKeychainAccount('bad\\slash'), false);
  assert.equal(isValidKeychainAccount('bad\nline'), false);
  assert.equal(isValidKeychainAccount('x'.repeat(129)), false);

  const warnings = [];
  const seenTimeouts = [];
  let securityInvoked = 0;
  const kc = createMacOsKeychain({
    service: 'dalton-alphaengine-test',
    timeoutMs: 5000,
    logWarning: (m) => warnings.push(m),
    runSecurity: (_args, _input, timeout) => {
      securityInvoked += 1;
      seenTimeouts.push(timeout);
      throw Object.assign(new Error('ETIMEDOUT'), { code: 'ETIMEDOUT', killed: true, signal: 'SIGTERM' });
    },
  });

  const tok = makeFakeJwt({ user_id: '12345', exp: 2000000000 });
  // Unsafe account ID must NOT call security CLI
  assert.equal(kc.save('bad"account', { token: tok, exp: 2000000000 }), false);
  assert.equal(kc.load('bad\naccount'), null);
  assert.equal(securityInvoked, 0);

  // Timeout on save and load degrades cleanly with token-free warnings and passes 5000ms timeout
  assert.equal(kc.save('12345', { token: tok, exp: 2000000000 }), false);
  assert.equal(kc.load('12345'), null);
  assert.deepEqual(seenTimeouts, [5000, 5000]);
  assert.ok(warnings.some((w) => w.includes('timeout') && !w.includes(tok)));
  assert.ok(warnings.some((w) => w.includes('timed out') && !w.includes(tok)));
});

test('GLM-3 & GLM-4) authStatus does not block on slow alerts, and accounts expired > 7 days stop daily reminders', async () => {
  const startMs = Date.parse('2026-11-19T02:00:00Z');
  const h = makeTestHarness(startMs);
  try {
    const nowSec = Math.floor(startMs / 1000);
    const oldExpiredToken = makeFakeJwt({ user_id: 'acc_old_exp', nickname: '废弃号', exp: nowSec - 8 * 86400 });
    const soonExpiringToken = makeFakeJwt({ user_id: 'acc_soon', nickname: '临期号', exp: nowSec + 2 * 86400 });

    h.setDesktop({
      appRunning: true,
      candidates: [
        { userId: 'acc_old_exp', token: oldExpiredToken, exp: nowSec - 8 * 86400, nickname: '废弃号' },
        { userId: 'acc_soon', token: soonExpiringToken, exp: nowSec + 2 * 86400, nickname: '临期号' },
      ],
    });
    h.manager.syncAccounts();

    // Slow alert sender (60ms) must NOT block authStatus()
    let releaseAlert;
    const slowMgr = createAccountManager({
      stateFile: h.stateFile,
      nowMs: () => startMs,
      readDesktopSnapshot: () => ({
        appRunning: true,
        candidates: [
          { userId: 'acc_old_exp', token: oldExpiredToken, exp: nowSec - 8 * 86400, nickname: '废弃号' },
          { userId: 'acc_soon', token: soonExpiringToken, exp: nowSec + 2 * 86400, nickname: '临期号' },
        ],
      }),
      keychain: { save: () => true, load: () => null },
      sendOpsAlert: async (alert) => {
        h.alertsSent.push(alert);
        await new Promise((r) => { releaseAlert = r; setTimeout(r, 60); });
        return true;
      },
    });

    const t0 = Date.now();
    const st = await authStatus({}, slowMgr);
    const elapsed = Date.now() - t0;
    assert.ok(elapsed < 40, `authStatus blocked for ${elapsed}ms`);
    assert.equal(st.accounts.find((a) => a.user_id === 'acc_old_exp').token_expired, true);

    await new Promise((r) => setTimeout(r, 80));
    if (releaseAlert) releaseAlert();
    // Only acc_soon should be alerted; acc_old_exp (> 7 days expired) must NOT alert
    assert.equal(h.alertsSent.length, 1);
    assert.equal(h.alertsSent[0].userId, 'acc_soon');
  } finally {
    h.cleanup();
  }
});

test('GLM-6) classifyUpstreamError upstreamBody/httpStatus branches, single-account compatibility, and corrupt state file degradation', async () => {
  // upstreamBody / httpStatus branches
  assert.equal(classifyUpstreamError({ httpStatus: 429, upstreamBody: { message: 'any' } }), 'quota');
  assert.equal(classifyUpstreamError({ httpStatus: 200, upstreamBody: { status_code: 429 } }), 'quota');
  assert.equal(classifyUpstreamError({ httpStatus: 400, upstreamBody: { status: 'quota_exceeded' } }), 'quota');
  assert.equal(classifyUpstreamError({ httpStatus: 400, upstreamBody: { error_code: 'rate_limited' } }), 'quota');
  assert.equal(classifyUpstreamError({ httpStatus: 200, upstreamBody: { code: 'quota_exceeded' } }), 'quota');
  assert.equal(classifyUpstreamError({ httpStatus: 200, upstreamBody: { error: { status_code: 429 } } }), 'quota');
  assert.equal(classifyUpstreamError({ httpStatus: 200, upstreamBody: { error: { error_code: 'rate_limited' } } }), 'quota');
  assert.equal(classifyUpstreamError({ httpStatus: 200, upstreamBody: { error: { message: '额度不足' } } }), 'quota');
  assert.equal(classifyUpstreamError({ httpStatus: 200, upstreamBody: { message: '无查看权限或额度已达上限' } }), 'ambiguous_retry');
  assert.equal(classifyUpstreamError({ httpStatus: 400, upstreamBody: { error_code: 'E_BAD_ARGS', message: 'quota_exceeded' } }), 'none');

  // Single-account compatibility + corrupt state file degradation
  const startMs = Date.parse('2026-11-19T02:00:00Z');
  const h = makeTestHarness(startMs, 6 * 3600 * 1000);
  try {
    // 1. Corrupt state file on disk
    writeFileSync(h.stateFile, '{corrupt json!!!', 'utf8');

    // No app running and no keychain -> app_not_running
    h.setDesktop({ appRunning: false, candidates: [] });
    const stEmpty = await authStatus({}, h.manager);
    assert.equal(stEmpty.status, 'app_not_running');
    assert.equal(stEmpty.authenticated, false);

    // App running without module page -> no_module_page
    h.setDesktop({ appRunning: true, candidates: [] });
    const stNoPage = await authStatus({}, h.manager);
    assert.equal(stNoPage.status, 'no_module_page');

    // Single valid account in App -> works despite earlier corrupt state file
    const nowSec = Math.floor(startMs / 1000);
    const tokenSingle = makeFakeJwt({ user_id: '998877', nickname: '单账号', exp: nowSec + 20 * 86400 });
    h.setDesktop({ appRunning: true, candidates: [{ userId: '998877', token: tokenSingle, exp: nowSec + 20 * 86400, nickname: '单账号' }] });

    const okRes = await h.manager.executeWithFailover('search', async (tok) => ({ ok: tok === tokenSingle }));
    assert.equal(okRes.ok, true);

    // Quota exhaustion on single account marks it and rethrows original error
    await assert.rejects(
      h.manager.executeWithFailover('search', async () => {
        const err = new Error('E_UPSTREAM_HTTP: HTTP 429: single-limit');
        err.httpStatus = 429;
        err.upstreamBody = { status_code: 429, message: 'single-limit' };
        throw err;
      }),
      /single-limit/,
    );
    const stExhausted = await authStatus({}, h.manager);
    assert.equal(stExhausted.accounts.length, 1);
    assert.equal(stExhausted.accounts[0].search_quota.status, 'exhausted');
    assert.equal(stExhausted.accounts[0].read_quota.status, 'available');

    // State file was overwritten with valid JSON
    const parsedDisk = JSON.parse(readFileSync(h.stateFile, 'utf8'));
    assert.equal(parsedDisk.accounts['998877'].quota.search.exhausted_at !== null, true);
  } finally {
    h.cleanup();
  }
});

test('R4-1) concurrent state writes merge per-account/per-quota without overwriting another process cooldown', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'ae-r4-merge-'));
  const stateFile = path.join(dir, 'alphaengine_accounts_state.json');
  const nowMs = Date.parse('2026-11-19T02:00:00Z');
  const nowSec = Math.floor(nowMs / 1000);
  const tokenA = makeFakeJwt({ user_id: '1001', nickname: '号A', exp: nowSec + 20 * 86400 });
  const tokenB = makeFakeJwt({ user_id: '1002', nickname: '号B', exp: nowSec + 20 * 86400 });
  const kc = new Map([
    ['1001', { userId: '1001', token: tokenA, exp: nowSec + 20 * 86400 }],
    ['1002', { userId: '1002', token: tokenB, exp: nowSec + 20 * 86400 }],
  ]);

  let m2;
  let injectedDuringKeychainLoad = false;
  // Process 1 reads state, then pauses inside keychain.load while Process 2 writes a 429 cooldown for 1001.search
  const m1 = createAccountManager({
    stateFile,
    nowMs: () => nowMs + 5000,
    readDesktopSnapshot: () => ({
      appRunning: true,
      candidates: [{ userId: '1002', token: tokenB, exp: nowSec + 20 * 86400, nickname: '号B' }],
    }),
    keychain: {
      save: () => true,
      load: (uid) => {
        if (!injectedDuringKeychainLoad && uid === '1002') {
          injectedDuringKeychainLoad = true;
          // Process 2 marks 1001 search quota exhausted on disk while Process 1 is mid-sync
          m2.executeWithFailover('search', async () => {
            const err = new Error('E_UPSTREAM_HTTP: HTTP 429');
            err.httpStatus = 429;
            err.upstreamBody = { status_code: 429 };
            throw err;
          }).catch(() => {});
        }
        return kc.get(uid) || null;
      },
    },
  });

  m2 = createAccountManager({
    stateFile,
    nowMs: () => nowMs + 2000,
    readDesktopSnapshot: () => ({
      appRunning: true,
      candidates: [{ userId: '1001', token: tokenA, exp: nowSec + 20 * 86400, nickname: '号A' }],
    }),
    keychain: { save: () => true, load: (uid) => kc.get(uid) || null },
  });

  try {
    // Seed both accounts on disk first
    m2.syncAccounts();
    // Process 1 runs syncAccounts(); mid-way Process 2 writes 1001.search exhaustion to disk
    m1.syncAccounts();
    // Wait a tick for m2's async rejection handler if needed
    await new Promise((r) => setTimeout(r, 10));

    // Process 1's save MUST NOT erase Process 2's 1001.search exhaustion mark
    const finalDisk = JSON.parse(readFileSync(stateFile, 'utf8'));
    assert.ok(finalDisk.accounts['1001'].quota.search.exhausted_at, '1001 search exhaustion was overwritten');
    assert.equal(finalDisk.accounts['1001'].quota.read.exhausted_at, null);
    assert.ok(finalDisk.accounts['1002'].updated_at);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('R4-2) searchLibrary switches account on HTTP 200 with {"error":{"status_code":429}} instead of returning empty results', async () => {
  const h = makeTestHarness();
  const origFetch = globalThis.fetch;
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: 'A', exp: nowSec + 20 * 86400 });
    const tokenB = makeFakeJwt({ user_id: 'acc_b', nickname: 'B', exp: nowSec + 20 * 86400 });
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();

    const authHeaders = [];
    globalThis.fetch = async (url, init) => {
      authHeaders.push({ url: String(url), auth: init.headers.Authorization });
      if (init.headers.Authorization === `Bearer ${tokenA}`) {
        return new Response(JSON.stringify({ error: { status_code: 429, message: 'nested rate limit' } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(
        JSON.stringify({
          success: true,
          data: {
            results: [{ doc_id: 'doc_from_b', title: 'NVDA Earnings', publish_time: '2026-11-18 10:00:00', code: 'summary' }],
            total: 1,
            has_next_page: false,
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    };

    const res = await searchLibrary({ query: 'NVDA' }, (apiPath, opts) => apiRequest(apiPath, opts, h.manager));
    assert.equal(res.returned, 1);
    assert.equal(res.results[0].doc_id, 'doc_from_b');
    assert.deepEqual(
      authHeaders.map((x) => x.auth),
      [`Bearer ${tokenA}`, `Bearer ${tokenB}`],
    );

    const st = await authStatus({}, h.manager);
    assert.equal(st.accounts.find((a) => a.user_id === 'acc_a').search_quota.status, 'exhausted');
  } finally {
    globalThis.fetch = origFetch;
    h.cleanup();
  }
});

test('R4-3) readable-only state file keeps in-memory cooldown so second search skips exhausted account directly', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'ae-r4-readonly-'));
  const stateFile = path.join(dir, 'alphaengine_accounts_state.json');
  const nowMs = Date.parse('2026-11-19T02:00:00Z');
  const nowSec = Math.floor(nowMs / 1000);
  const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: 'A', exp: nowSec + 20 * 86400 });
  const tokenB = makeFakeJwt({ user_id: 'acc_b', nickname: 'B', exp: nowSec + 20 * 86400 });
  const kc = new Map([
    ['acc_a', { userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400 }],
    ['acc_b', { userId: 'acc_b', token: tokenB, exp: nowSec + 20 * 86400 }],
  ]);
  const warnings = [];
  const mgr = createAccountManager({
    stateFile,
    nowMs: () => nowMs,
    readDesktopSnapshot: () => ({
      appRunning: true,
      candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400, nickname: 'A' }],
    }),
    keychain: { save: () => true, load: (uid) => kc.get(uid) || null },
    logWarning: (m) => warnings.push(m),
  });

  try {
    // Seed state file with both accounts writable first, then lock file + directory read-only
    writeFileSync(
      stateFile,
      JSON.stringify({
        version: 1,
        last_used_user_id: null,
        accounts: {
          acc_a: { user_id: 'acc_a', exp: nowSec + 20 * 86400, quota: { search: { exhausted_at: null }, read: { exhausted_at: null } } },
          acc_b: { user_id: 'acc_b', exp: nowSec + 20 * 86400, quota: { search: { exhausted_at: null }, read: { exhausted_at: null } } },
        },
      }),
      { mode: 0o600 },
    );
    chmodSync(stateFile, 0o444);
    chmodSync(dir, 0o555);

    const req1Tokens = [];
    const r1 = await mgr.executeWithFailover('search', async (tok) => {
      req1Tokens.push(tok);
      if (tok === tokenA) {
        const err = new Error('E_UPSTREAM_HTTP: HTTP 429');
        err.httpStatus = 429;
        err.upstreamBody = { status_code: 429 };
        throw err;
      }
      return { ok: true, run: 1 };
    });
    assert.equal(r1.ok, true);
    assert.deepEqual(req1Tokens, [tokenA, tokenB]);

    // Second search reads the still-stale read-only disk file, merges memoryState, and goes directly to B!
    const req2Tokens = [];
    const r2 = await mgr.executeWithFailover('search', async (tok) => {
      req2Tokens.push(tok);
      return { ok: true, run: 2 };
    });
    assert.equal(r2.ok, true);
    assert.deepEqual(req2Tokens, [tokenB]);
    assert.ok(warnings.some((w) => w.includes('failed to persist account state')));
  } finally {
    try { chmodSync(dir, 0o755); } catch {}
    try { chmodSync(stateFile, 0o644); } catch {}
    rmSync(dir, { recursive: true, force: true });
  }
});

test('R4-4) getRecordingStatus runs counts and preview on the same account and fails over the whole pair on 429', async () => {
  const h = makeTestHarness();
  const origFetch = globalThis.fetch;
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: 'A', exp: nowSec + 20 * 86400 });
    const tokenB = makeFakeJwt({ user_id: 'acc_b', nickname: 'B', exp: nowSec + 20 * 86400 });
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();

    const calls = [];
    globalThis.fetch = async (url, init) => {
      const u = String(url);
      const auth = init.headers.Authorization;
      calls.push({ url: u, auth });
      if (auth === `Bearer ${tokenA}` && u.includes('/booked/preview')) {
        return new Response(JSON.stringify({ status_code: 429, message: 'Too Many Requests' }), {
          status: 429,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      const tag = auth === `Bearer ${tokenA}` ? 'A' : 'B';
      if (u.includes('/booked/count/list')) {
        return new Response(JSON.stringify({ success: true, data: { source_account: tag, total: 3 } }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      return new Response(JSON.stringify({ success: true, data: { source_account: tag, items: [] } }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const statusRes = await getRecordingStatus({ meeting_scope: 'personal' }, apiRequest, h.manager);
    // Both counts and preview must come from account B (never mixed A+B)
    assert.equal(statusRes.counts.source_account, 'B');
    assert.equal(statusRes.preview.source_account, 'B');
    assert.equal(calls.filter((c) => c.auth === `Bearer ${tokenA}`).length, 2);
    assert.equal(calls.filter((c) => c.auth === `Bearer ${tokenB}`).length, 2);
  } finally {
    globalThis.fetch = origFetch;
    h.cleanup();
  }
});

test('R5-1) getRecordingStatus fails over as soon as one request hits quota, even while the other is still pending', async () => {
  const h = makeTestHarness();
  const hanging = [];
  const origFetch = globalThis.fetch;
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: 'A', exp: nowSec + 20 * 86400 });
    const tokenB = makeFakeJwt({ user_id: 'acc_b', nickname: 'B', exp: nowSec + 20 * 86400 });
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();

    globalThis.fetch = async (url, init) => {
      const u = String(url);
      const auth = init.headers.Authorization;
      if (auth === `Bearer ${tokenA}`) {
        if (u.includes('/booked/preview')) {
          return new Response(JSON.stringify({ status_code: 429, message: 'Too Many Requests' }), {
            status: 429,
            headers: { 'Content-Type': 'application/json' },
          });
        }
        // Account A's count request stays pending until the test releases it.
        return new Promise((_resolve, reject) => {
          hanging.push(() => reject(new Error('released')));
        });
      }
      const body = u.includes('/booked/count/list') ? { source_account: 'B', total: 1 } : { source_account: 'B', items: [] };
      return new Response(JSON.stringify({ success: true, data: body }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const started = Date.now();
    const statusRes = await getRecordingStatus({ meeting_scope: 'personal' }, apiRequest, h.manager);
    assert.ok(Date.now() - started < 5000, 'failover must not wait for the hanging request');
    assert.equal(statusRes.counts.source_account, 'B');
    assert.equal(statusRes.preview.source_account, 'B');
  } finally {
    for (const release of hanging) release();
    globalThis.fetch = origFetch;
    h.cleanup();
  }
});

test('R4-5) gateway alert timeout keeps daily marker (no duplicate alert); definitive failure deletes marker to allow retry', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'ae-r4-alert-timeout-'));
  const nowMs = Date.parse('2026-11-19T02:00:00Z');
  const nowSec = Math.floor(nowMs / 1000);
  const expToken = makeFakeJwt({ user_id: '5001', nickname: '超时号', exp: nowSec + 86400 });
  const envFile = path.join(dir, 'gw.env');
  writeFileSync(envFile, 'INTERNAL_SERVICE_TOKEN=tok-123\n', 'utf8');
  const origFetch = globalThis.fetch;

  try {
    // 1. Timeout -> outcome: 'uncertain' -> marker kept -> second check does NOT re-send
    const stateTimeout = path.join(dir, 'timeout', 'state.json');
    let timeoutFetches = 0;
    globalThis.fetch = async () => {
      timeoutFetches += 1;
      throw Object.assign(new Error('The operation was aborted due to timeout'), { name: 'TimeoutError' });
    };
    const mTimeout = createAccountManager({
      stateFile: stateTimeout,
      nowMs: () => nowMs,
      readDesktopSnapshot: () => ({
        appRunning: true,
        candidates: [{ userId: '5001', token: expToken, exp: nowSec + 86400, nickname: '超时号' }],
      }),
      keychain: { save: () => true, load: () => null },
      sendOpsAlert: ({ code, detail }) =>
        sendDefaultOpsAlert({ code, detail, envFile, gatewayUrl: 'http://127.0.0.1:8962/internal/ops-alerts' }),
    });
    await mTimeout.checkExpiringAccountsAndAlert();
    await mTimeout.checkExpiringAccountsAndAlert();
    assert.equal(timeoutFetches, 1);
    const markerFile = path.join(path.dirname(stateTimeout), 'alphaengine_alert_markers', '5001_2026-11-19.lock');
    assert.equal(existsSync(markerFile), true);

    // 2. Definitive complete failure (502 any_succeeded: false) -> marker deleted -> next check retries and succeeds
    const stateFail = path.join(dir, 'fail', 'state.json');
    let failAttempt = 0;
    globalThis.fetch = async () => {
      failAttempt += 1;
      if (failAttempt === 1) {
        return new Response(
          JSON.stringify({ ok: false, any_succeeded: false, results: [{ target: 'a', ok: false }] }),
          { status: 502, headers: { 'Content-Type': 'application/json' } },
        );
      }
      return new Response(JSON.stringify({ ok: true, any_succeeded: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };
    const mFail = createAccountManager({
      stateFile: stateFail,
      nowMs: () => nowMs,
      readDesktopSnapshot: () => ({
        appRunning: true,
        candidates: [{ userId: '5001', token: expToken, exp: nowSec + 86400, nickname: '失败重试号' }],
      }),
      keychain: { save: () => true, load: () => null },
      sendOpsAlert: ({ code, detail }) =>
        sendDefaultOpsAlert({ code, detail, envFile, gatewayUrl: 'http://127.0.0.1:8962/internal/ops-alerts' }),
    });
    const first = await mFail.checkExpiringAccountsAndAlert();
    assert.equal(first.length, 0);
    const failMarker = path.join(path.dirname(stateFail), 'alphaengine_alert_markers', '5001_2026-11-19.lock');
    assert.equal(existsSync(failMarker), false);

    const second = await mFail.checkExpiringAccountsAndAlert();
    assert.equal(second.length, 1);
    assert.equal(failAttempt, 2);
  } finally {
    globalThis.fetch = origFetch;
    rmSync(dir, { recursive: true, force: true });
  }
});

test('R8-1) a malformed field in an error body is skipped, not allowed to hide the quota signal', () => {
  assert.equal(
    classifyUpstreamError({ httpStatus: 200, upstreamBody: { message: '额度不足', error_code: { toString: null } } }),
    'quota',
  );
  assert.equal(classifyUpstreamError({ httpStatus: { toString: null }, upstreamBody: { status_code: { valueOf: null } } }), 'none');
});

test('R9-1) a 200 body with an object status_code and a quota message still switches accounts through the request path', async () => {
  const h = makeTestHarness();
  const origFetch = globalThis.fetch;
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: 'A', exp: nowSec + 20 * 86400 });
    const tokenB = makeFakeJwt({ user_id: 'acc_b', nickname: 'B', exp: nowSec + 20 * 86400 });
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();

    globalThis.fetch = async (url, init) => {
      const u = String(url);
      if (init.headers.Authorization === `Bearer ${tokenA}`) {
        return new Response(JSON.stringify({ status_code: { toString: null }, message: '额度不足' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      const body = u.includes('/booked/count/list') ? { source_account: 'B', total: 1 } : { source_account: 'B', items: [] };
      return new Response(JSON.stringify({ success: true, data: body }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const statusRes = await getRecordingStatus({ meeting_scope: 'personal' }, apiRequest, h.manager);
    assert.equal(statusRes.counts.source_account, 'B');
    assert.equal(statusRes.preview.source_account, 'B');
  } finally {
    globalThis.fetch = origFetch;
    h.cleanup();
  }
});

test('R10-1) through the request path, a body status_code of "0" keeps the HTTP 429, so the request switches accounts', async () => {
  const h = makeTestHarness();
  const origFetch = globalThis.fetch;
  try {
    const nowSec = Math.floor(Date.parse('2026-11-19T02:00:00Z') / 1000);
    const tokenA = makeFakeJwt({ user_id: 'acc_a', nickname: 'A', exp: nowSec + 20 * 86400 });
    const tokenB = makeFakeJwt({ user_id: 'acc_b', nickname: 'B', exp: nowSec + 20 * 86400 });
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_b', token: tokenB, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();
    h.setDesktop({ appRunning: true, candidates: [{ userId: 'acc_a', token: tokenA, exp: nowSec + 20 * 86400 }] });
    h.manager.syncAccounts();

    globalThis.fetch = async (url, init) => {
      const u = String(url);
      if (init.headers.Authorization === `Bearer ${tokenA}`) {
        return new Response(JSON.stringify({ status_code: '0', error: { status_code: 403 } }), {
          status: 429,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      const body = u.includes('/booked/count/list') ? { source_account: 'B', total: 1 } : { source_account: 'B', items: [] };
      return new Response(JSON.stringify({ success: true, data: body }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    const statusRes = await getRecordingStatus({ meeting_scope: 'personal' }, apiRequest, h.manager);
    assert.equal(statusRes.counts.source_account, 'B');
    assert.equal(statusRes.preview.source_account, 'B');
  } finally {
    globalThis.fetch = origFetch;
    h.cleanup();
  }
});
