import test from 'node:test';
import assert from 'node:assert/strict';
import { searchLibrary, listFilters, listRecordingTasks, getRecordingTask, getDocumentInfo,
  compactSearchResult, documentMetadata, resultMatches, requireDatePair } from './server.mjs';

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
