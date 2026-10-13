// Focused guards with DOM/API doubles; real browser/SQL coverage is agent_browser_test.cjs.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {boot,id}=require('./console_ui_test.cjs');
const a=boot(),proto=a.get('workflow-edit').constructor.prototype;
proto.getAttribute=function(k){return this.attrs[k]??null;};
Object.defineProperty(proto,'options',{get(){return this.children;},configurable:true});
Object.defineProperty(proto,'href',{get(){return this.attrs.href||'';},set(v){this.attrs.href=v;},configurable:true});
for(const file of ['agents.js','workflows.js'])a.run(fs.readFileSync(path.join(__dirname,'../gateway/app/web',file),'utf8'));
a.run("state.connected=true;state.token='fixture';location.hostname='agent.example.test';controls();");
const agent={agent_id:id(201),display_name:'문서 Agent <script>bad</script>',enabled:true,versions:[]};
const version={agent_version_id:id(202),agent_id:agent.agent_id,state:'published',version:2};agent.versions=[version];
const link={workflow_link_id:id(203),display_name:'자동화 <img src=x>',agent_version_id:version.agent_version_id,workflow_id:'flow-one',instance_url:'http://127.0.0.1:16200',edit_url:'javascript:alert(1)',revision:1,enabled:true};
const run={agent_run_id:id(204),agent_version_id:version.agent_version_id,agent_id:agent.agent_id,agent_version:2,question:'자료 질문',created_at:'2026-09-24T00:00:00Z',mode:'run',state:'failed',caller_context:{system:'n8n',workflow_id:'flow-one'},error:{code:'ANSWER_REFERENCE_INVALID',stage:'finalize'}};
a.run(`const fixture={agent:${JSON.stringify(agent)},version:${JSON.stringify(version)},link:${JSON.stringify(link)},runs:${JSON.stringify([run,{...run,agent_run_id:id(205),caller_context:{}},{...run,agent_run_id:id(206),agent_version_id:id(999)},{...run,agent_run_id:id(207),caller_context:{system:'n8n',workflow_id:'other'}}])},requests:[]};
api=async(path,opt={})=>{fixture.requests.push({path,method:opt.method||'GET',body:opt.body});if(path.startsWith('/api/workflow-links/'))return structuredClone(fixture.link);if(path.startsWith('/api/agent-versions/'))return structuredClone(fixture.version);if(path.startsWith('/api/agents/'))return structuredClone(fixture.agent);if(path.startsWith('/api/agent-runs/')&&path.endsWith('/steps'))return {items:[]};if(path.startsWith('/api/agent-runs/'))return {...fixture.runs[0],state:'running',input:{question:'계속 실행 중'}};throw Error('unexpected fixture route');};
all=async path=>path==='/api/agents'?[fixture.agent]:path==='/api/workflow-links'?[fixture.link]:fixture.runs;`);
// VM fixture clone without relying on globals absent in the old DOM harness.
a.run('function structuredClone(v){return JSON.parse(JSON.stringify(v));}');
(async()=>{
 let checks=0;
 for(const url of ['javascript:alert(1)','https://x/workflow/../evil','https://u:p@x/workflow/a','https://x/workflow/a?secret=value','https://x/workflow/a#frag','https://x/workflow/%61','https://x\\@evil/workflow/a'])assert.throws(()=>a.run(`parseWorkflowURL(${JSON.stringify(url)})`));
 assert.equal(a.run("parseWorkflowURL('https://n8n.example.test/workflow/Ab_12-').workflow_id"),'Ab_12-');checks++;
 await a.run(`openWorkflow('${link.workflow_link_id}')`);assert.equal(a.get('workflow-edit').getAttribute('href'),null);assert.match(a.get('workflow-address-help').textContent,/서버 내부 주소/);assert.equal(a.get('workflow-address-panel').open,true);checks++;
 a.get('workflow-browser-origin').value='https://flows.example.test';a.run('workflowEditAddress()');assert.equal(a.get('workflow-edit').href,'https://flows.example.test/workflow/flow-one');assert.equal(a.run('fixture.link.instance_url'),'http://127.0.0.1:16200');assert(!a.get('workflow-edit').href.includes('fixture'));checks++;
 a.run('fixture.agent.enabled=false');await a.run(`openWorkflow('${link.workflow_link_id}')`);a.run('controls()');assert.equal(a.get('workflow-ask').disabled,true);assert.match(a.get('workflow-status-help').textContent,/새 질문에 사용할 수 없습니다/);a.run("fixture.agent.enabled=true;fixture.version.state='retired'");await a.run(`openWorkflow('${link.workflow_link_id}')`);a.run('controls()');assert.equal(a.get('workflow-ask').disabled,true);checks++;
 a.run("fixture.version.state='published'");await a.run(`openWorkflow('${link.workflow_link_id}')`);await a.run('workflowHistory()');assert.equal(a.get('workflow-history').children.length,1);assert.match(a.get('workflow-history').textContent,/ANSWER_REFERENCE_INVALID/);assert.equal(a.run("fixture.requests.filter(r=>r.method!=='GET').length"),0);checks++;
 a.run('fixture.link.revision=2');await assert.rejects(a.run("changeWorkflow({display_name:'my change'})"),/다른 작업/);assert.equal(a.run('workflowUI.patch'),null);assert.equal(a.run("fixture.requests.filter(r=>r.method==='PATCH').length"),0);assert.equal(a.get('workflow-name').textContent,link.display_name);checks++;
 a.run(`showAgentRun({...fixture.runs[0],input:{question:'질문'}},[{step_id:'finalize',state:'failed',duration_ms:100}]);`);assert.match(a.get('agent-run-message').textContent,/인용이 없거나 인용 형식/);assert.equal(a.get('agent-answer').textContent,'');assert.match(a.get('agent-steps').textContent,/0.1초/);checks++;
 const promise=a.run(`waitAgentRun('${run.agent_run_id}')`);await new Promise(r=>setTimeout(r,10));a.get('agent-poll-stop').onclick();await promise;assert.equal(a.run('agentUI.pending'),run.agent_run_id);assert.equal(a.run('agentUI.polling'),false);assert.equal(a.run("fixture.requests.filter(r=>r.method!=='GET').length"),0);assert.match(a.get('notice').textContent,/서버 실행은 계속/);checks++;
 a.run('resetAgents();resetWorkflows()');for(const name of ['agent-run-json','agent-run-question','agent-run-message','agent-steps','question-history','workflow-definition','workflow-name'])assert.equal(a.get(name).textContent,'');assert.equal(a.get('workflow-edit').getAttribute('href'),null);checks++;
 console.log(JSON.stringify({result:'passed',checks,scope:'URL boundary, remote loopback guidance, retired/disabled Agent, exact workflow history, revision conflict, failed citation display, stopped polling, cleared session; DOM/API doubles'}));
})().catch(e=>{console.error(e);process.exitCode=1;});
