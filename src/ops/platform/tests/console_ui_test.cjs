/* Synthetic browser acceptance. Start serve_fixture.py on the same loopback network. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const url=process.env.OPS_TEST_URL||'http://localhost:17881';
const output=process.env.OPS_TEST_OUTPUT;
const control=process.env.OPS_TEST_CONTROL;
const KEY='ops-test-access-key-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ';
(async()=>{
 let browser;
 if(process.env.CHROMIUM_MODULE){const packaged=require(process.env.CHROMIUM_MODULE);browser=await chromium.launch({executablePath:await packaged.executablePath(),args:['--no-sandbox','--no-zygote','--single-process','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'],headless:true});}
 else browser=await chromium.launch({headless:true});
 const page=await browser.newPage({viewport:{width:1365,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('console',msg=>{if(msg.type()==='error'&&/Content Security Policy|Refused to/.test(msg.text()))errors.push(msg.text());});
 try{
  await page.goto(url);await page.locator('#login').waitFor({state:'visible'});
  assert.equal(await page.locator('#console').isVisible(),false);
  await page.locator('#password').fill(KEY);await page.locator('#login-button').click();
  await page.locator('.platform-card').first().waitFor();assert.equal(await page.locator('.platform-card').count(),4);
  assert.match(await page.locator('#overview').innerText(),/준비 상태는 제공하지 않습니다/);
  assert.equal(await page.evaluate(()=>localStorage.length+sessionStorage.length),0);
  if(output)await page.screenshot({path:output+'/overview.png',fullPage:true});
  await page.locator('[data-tab="runs"]').click();await page.locator('#records table').waitFor();
  await page.locator('#platform-select').selectOption('agent');await page.locator('#records table').waitFor();
  await page.locator('#records button').first().click();await page.locator('.steps').waitFor();
  assert.equal(await page.locator('.step').count(),5);assert.match(await page.locator('#detail').innerText(),/ANSWER_REFERENCE_INVALID/);
  assert.match(await page.locator('#detail').innerText(),/결과 품질 미평가/);
  await page.getByRole('button',{name:'추적 조회',exact:true}).click();await page.locator('.trace-row').first().waitFor();
  assert.equal(await page.locator('.trace-row').count(),10);assert.match(await page.locator('.trace').innerText(),/생성 토큰 176/);
  assert.doesNotMatch(await page.locator('body').innerText(),/PRIVATE_PROMPT|sk-lf-test|agent-test-key/);
  if(output)await page.screenshot({path:output+'/agent-detail.png',fullPage:true});
  await page.locator('#resource-select').selectOption('tool-calls');await page.getByText('전체 목록은 제공하지 않습니다.',{exact:false}).waitFor();
  await page.locator('[data-tab="resources"]').click();await page.locator('.metric-card').first().waitFor();
  assert.equal(await page.locator('.metric-card').count(),7);
  if(output)await page.screenshot({path:output+'/resources.png',fullPage:true});
  // Same container/Pod names in different namespaces retain visible scope.
  await page.route('**/api/resources',async route=>{
   const response=await route.fetch(),body=await response.json();
   const rows=['team-a','team-b'].map(namespace=>({labels:{container:'worker',pod:'same-pod',namespace},
    resource:{cluster:'cluster-one',node:'node-a',namespace,pod:'same-pod',container:'worker'},
    value:1024,value_status:'observed',unit:'bytes'}));
   body.metrics.container_memory.data.items=rows;
   await route.fulfill({response,json:body});
  });
  await page.locator('#resources-refresh').click();
  await page.getByText('cluster-one / node-a / team-a / same-pod',{exact:true}).waitFor();
  await page.getByText('cluster-one / node-a / team-b / same-pod',{exact:true}).waitFor();
  if(output)await page.screenshot({path:output+'/resource-scopes.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await page.setViewportSize({width:1365,height:1000});
  await page.unroute('**/api/resources');
  if(control){
   fs.writeFileSync(control,JSON.stringify({label:'<img src=x onerror="window.xss=true">'}));
   await page.locator('#resources-refresh').click();await page.getByText('<img src=x onerror="window.xss=true">',{exact:true}).first().waitFor();
   assert.equal(await page.evaluate(()=>window.xss),undefined);assert.equal(await page.locator('#metrics img').count(),0);
   fs.writeFileSync(control,JSON.stringify({empty:true}));await page.locator('#resources-refresh').click();await page.getByText('최근 관측값이 없습니다.',{exact:false}).first().waitFor();
   fs.writeFileSync(control,JSON.stringify({prom_failure:true}));await page.locator('#resources-refresh').click();await page.locator('#metrics .failure').first().waitFor();
   assert.equal(await page.locator('#metrics table').count(),0);
   assert.match(await page.locator('#metrics').innerText(),/마지막 성공 조회/);
   fs.writeFileSync(control,'{}');await page.locator('#resources-refresh').click();await page.locator('#metrics table').first().waitFor();
  }
  if(control){
   fs.writeFileSync(control,'{}');await page.locator('[data-tab="alerts"]').click();
   await page.locator('#alert-rules table').waitFor();
   assert.equal(await page.locator('#alert-rules tbody tr').count(),8);
   assert.match(await page.locator('#alert-summary').innerText(),/해당하는 대상이 없습니다/);
   fs.writeFileSync(control,JSON.stringify({alert_firing:true}));await page.locator('#alerts-refresh').click();
   await page.locator('#alert-routing table').waitFor();assert.match(await page.locator('#alert-rules').innerText(),/발생 중/);
   assert.doesNotMatch(await page.locator('#alerts').innerText(),/PRIVATE_PROMPT|http:\/\/private/);
   if(output)await page.screenshot({path:output+'/alerts.png',fullPage:true});
   fs.writeFileSync(control,JSON.stringify({alert_manager_failure:true}));await page.locator('#alerts-refresh').click();
   await page.locator('#alert-routing .failure').waitFor();assert.match(await page.locator('#alert-summary').innerText(),/확인하지 못했습니다/);
   assert.equal(await page.locator('#alert-routing table').count(),0);
   fs.writeFileSync(control,'{}');await page.locator('#alerts-refresh').click();
   await page.locator('#alert-routing .empty').waitFor();assert.match(await page.locator('#alert-summary').innerText(),/해당하는 대상이 없습니다/);
  }
  await page.setViewportSize({width:390,height:844});
  if(control){
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  }
  await page.locator('[data-tab="overview"]').click();await page.locator('.platform-card').first().waitFor();
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
  await page.evaluate(()=>{document.querySelector('#overview-stamp').dataset.checked='2020-01-01T00:00:00Z';updateAge();});
  assert.match(await page.locator('#overview-stamp').innerText(),/오래된 화면/);
  if(output)await page.screenshot({path:output+'/mobile.png',fullPage:true});
  await page.locator('#logout').click();await page.locator('#login').waitFor({state:'visible'});
  assert.equal(await page.locator('#platforms').innerText(),'');
  assert.equal(await page.evaluate(async()=>{const r=await fetch('/api/overview');return r.status;}),401);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({result:'passed',checks:['login/logout','four platforms','Agent failure and five steps','ten trace spans','metadata exclusion','unsupported list','seven resource groups','cluster/node/namespace/pod scope','XSS text rendering','empty vs outage','last success','mobile overflow','stale display','O04 eight rules','O04 firing and resolution','O04 outage is not empty','no browser storage or CSP errors']}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
