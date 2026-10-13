// Run with Node + Playwright/Chromium installed. All API calls are local stubs;
// this checks the shipped browser workflow, not Go/DB/Docker integration.
const {chromium}=require('playwright');
const fs=require('node:fs/promises');
const path=require('node:path');
const assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../gateway/web');
const source='11111111-1111-4111-8111-111111111111';
const canonical='22222222-2222-4222-8222-222222222222';
const runID='33333333-3333-4333-8333-333333333333';
const op='44444444-4444-4444-8444-444444444444';
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1200,height:1000}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  let submissions=[],dropFirst=true,registered=false,options;
  const run=()=>({chunking_run_id:runID,operation_id:op,name:'청크 미리보기',result_registered:registered,chunk_count:1,submission_status:'delivered',spec:{snapshot_id:'snapshot-a',chunker:{id:options?.chunker.id||'structure_v1',options:options?.chunker.options||{max_characters:1000,overlap_characters:100}},documents:[{canonical_id:canonical,source_document_id:source}]},summary:{documents:[{canonical_id:canonical,quality:{state:'needs_review'}}],exclusions:[{block_id:'b2',reason:'unread'}]}});
  await page.route('http://knowledge.test/**',async route=>{
   const req=route.request(),u=new URL(req.url()),p=u.pathname;
   const json=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
   if(p==='/api/chunkers')return json({items:['recursive_character_v1','structure_v1'].map(id=>({id,name:id,description:'설명',defaults:{max_characters:1000,overlap_characters:100}}))});
   if(p==='/api/source-groups')return json({items:[{group_id:'group-a',name:'안내 문서'}]});
   if(p==='/api/indexes'||p==='/console/model/deployments')return json({items:[]});
   if(p==='/api/sources')return json({items:[{document_id:source,name:'요금 안내'}]});
   if(p===`/api/sources/${source}/canonical-versions`)return json({items:[{source_document_id:source,output_document_id:canonical,name:'정제 완료',created_at:'2026-09-18T00:00:00Z'}]});
   if(p==='/api/chunking-runs'&&req.method()==='POST'){
    submissions.push({key:req.headers()['idempotency-key'],body:req.postData()});options=JSON.parse(req.postData());
    if(dropFirst){dropFirst=false;return route.abort('failed');}
    return json({chunking_run_id:runID,operation_id:op,status:'accepted'},202);
   }
   if(p==='/api/chunking-runs')return json({items:submissions.length?[run()]:[]});
   if(p===`/api/chunking-runs/${runID}`)return json(run());
   if(p===`/api/operations/${op}`)return json({status:registered?'succeeded':'running',phase:'chunking'});
   if(p===`/api/chunking-runs/${runID}/chunks`)return json({items:[{ordinal:0,kind:'text',text:'입장료는 무료입니다. <img src=x onerror="window.xss=true">',canonical_id:canonical,source_document_id:source,source_spans:[{block_id:'b1',start:0,end:11,metadata:{location:{page:1}}}],quality:{state:'needs_review'}}],next_cursor:''});
   const name=p==='/'?'index.html':p.slice(1);
   if(!['index.html','app.js','indexing.js','search.js','console.js','style.css'].includes(name))return route.fulfill({status:404,body:'not found'});
   return route.fulfill({contentType:name.endsWith('html')?'text/html':name.endsWith('js')?'text/javascript':'text/css',body:await fs.readFile(path.join(root,name))});
  });
  await page.goto('http://knowledge.test/');
  await page.waitForFunction(()=>document.querySelector('#chunker').options.length===2);
  assert.equal(await page.locator('#chunker').inputValue(),'structure_v1');
  await page.click('#newKnowledge');await page.getByRole('button',{name:'추가',exact:true}).click();await page.getByRole('button',{name:'이 정리 결과 추가',exact:true}).click();
  assert.equal(await page.locator('#selectionCount').textContent(),'1');
  await page.click('#stepNext');await page.locator('#previewPanel > summary').click();
  await page.click('#create');await page.waitForFunction(()=>document.querySelector('#createStatus').textContent.includes('미리보기 이어서 확인'));
  // A reload after lost acknowledgement must keep the original request/key.
  await page.reload();await page.waitForFunction(()=>!document.querySelector('#retry').hidden);
  await page.click('#retry');await page.waitForFunction(()=>!document.querySelector('#refresh').hidden);
  assert.equal(submissions.length,2);assert.deepEqual(submissions[0],submissions[1]);
  // Reload starts on the list. Enter the saved-preview view to inspect the replayed run.
  await page.click('#newKnowledge');await page.selectOption('#indexInput','saved');
  await page.selectOption('#runs',runID);await page.waitForFunction(()=>document.querySelector('#runStatus').textContent.includes('청크 미리보기'));
  registered=true;
  // Choose the same canonical input in the wizard to reach the optional preview panel.
  await page.selectOption('#indexInput','new');await page.getByRole('button',{name:'추가',exact:true}).click();await page.getByRole('button',{name:'이 정리 결과 추가',exact:true}).click();
  await page.click('#stepNext');await page.locator('#previewPanel > summary').click();
  await page.click('#refresh');await page.waitForSelector('#chunks article');
  assert.match(await page.locator('#chunks').textContent(),/입장료는 무료/);
  assert.equal(await page.locator('#chunks img').count(),0);assert.equal(await page.evaluate(()=>window.xss),undefined);
  await page.click('#compare');assert.equal(await page.locator('#chunker').inputValue(),'recursive_character_v1');assert.equal(await page.locator('#selectionCount').textContent(),'1');
  await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true);
  assert.deepEqual(errors,[]);
  console.log('PASS console: selection, two strategies, pending replay after reload, result/quality/source display, comparison, escaped content, mobile width');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
