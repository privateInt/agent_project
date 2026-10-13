// Optional Playwright test, driven by TestKnowledgeEndToEnd against isolated HTTP/SQL fixtures.
const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES?process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES+'/playwright':'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.AGENT_TEST_CHROMIUM?{executablePath:process.env.AGENT_TEST_CHROMIUM}:{}),args:['--no-sandbox','--disable-dev-shm-usage','--no-zygote','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader']});
 try{const page=await browser.newPage({viewport:{width:1365,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.AGENT_BROWSER_URL+'/console');await page.locator('#token').fill(process.env.AGENT_BROWSER_TOKEN);await page.locator('#login-submit').click();await page.waitForFunction(()=>document.querySelector('#connection-status').textContent==='연결됨');
 await page.locator('#new-tool').click();assert.equal(await page.locator('#knowledge-registration').isVisible(),true);await page.locator('#knowledge-connection-settings summary').click();
 await page.locator('#knowledge-url').fill(process.env.AGENT_FIXTURE_URL);await page.locator('#model-url').fill(process.env.AGENT_FIXTURE_URL);await page.locator('#knowledge-load').click();await page.waitForFunction(()=>document.querySelector('#knowledge-index').options.length>1&&!document.querySelector('#knowledge-load').disabled);
 await page.locator('#knowledge-index').selectOption({index:1});await page.locator('#knowledge-next').click();await page.locator('#knowledge-setup').waitFor({state:'visible'});await page.locator('#knowledge-name').fill('질문으로 지식 찾기');await page.locator('#knowledge-save').click();await page.locator('#view-detail').waitFor({state:'visible'});
 assert.equal(await page.locator('#arguments').isVisible(),false);assert.equal(await page.locator('#input-fields textarea').count(),1);
 await page.locator('#input-fields textarea').fill('  자연어 질문  ');await page.locator('#test-call').click();await page.waitForFunction(()=>document.querySelector('#call-state').textContent==='성공'&&!document.querySelector('#publish').disabled);
 assert.match(await page.locator('#knowledge-results').textContent(),/본문과 출처 보존/);assert.match(await page.locator('#knowledge-results').textContent(),/3쪽/);assert.equal(await page.locator('#result-data').isVisible(),false);
 const test=JSON.parse(await page.locator('#result').textContent());assert.equal(test.mode,'test');
 await page.locator('#publish').click();await page.waitForFunction(()=>document.querySelector('#draft-status').textContent.includes('사용 중')&&!document.querySelector('#run-call').disabled);
 await page.locator('#run-call').click();await page.waitForFunction(()=>JSON.parse(document.querySelector('#result').textContent).mode==='run'&&document.querySelector('#call-state').textContent==='성공'&&!document.querySelector('#run-call').disabled);
 const run=JSON.parse(await page.locator('#result').textContent());assert.notEqual(run.tool_call_id,test.tool_call_id);
 const screenshots=process.env.AGENT_TEST_SCREENSHOT_DIR;if(screenshots)await page.screenshot({path:path.join(screenshots,'knowledge-desktop.png'),fullPage:true});
 await page.setViewportSize({width:390,height:844});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'mobile overflow');if(screenshots)await page.screenshot({path:path.join(screenshots,'knowledge-mobile.png'),fullPage:true});
 await page.locator('#nav-results').click();await page.locator('#lookup-panel summary').click();await page.locator('#call-id').fill(test.tool_call_id);await page.locator('#lookup').click();await page.waitForFunction(id=>JSON.parse(document.querySelector('#result').textContent).tool_call_id===id,test.tool_call_id);
 assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);assert.deepEqual(errors,[]);
 console.log(JSON.stringify({result:'passed',checks:['real_browser_register','natural_question_form','actual_fixture_embedding_and_search','result_sources','publish','run','lookup','mobile_no_overflow','no_script_error','no_token_storage']}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
