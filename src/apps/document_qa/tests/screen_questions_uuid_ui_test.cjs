// Exercise screen code against real disposable QA HTTP responses supplied by Python.
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs');
const {boot}=require('./screen_ui_test.cjs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const V5='01851866-e7bd-5c5f-a405-2f949c22c60d',V4='41330cd4-618f-429e-949e-a8ef3b1337ab';
const policy={question_ready:true,question_inference_enabled:true,question_API_connected:true,head_active:true,head_revision:1,head_target_id:V5,policy_source:'explicit_server_opt_in'};
async function choose(a){await a.elements['answer-actions'].children[0].listeners.click();await a.settle();await a.event('question-confirm');}
async function main(){
  if(input.mode==='validate'){
    const a=boot(),Q=a.window.QAQuestions,U=a.window.QAUpload;
    Q.policy(policy);Q.policy({...policy,head_target_id:V4});
    const q={question_id:V5,question:'실제 UUID 계약',target_id:V5,agent_version_id:V4,head_revision:1,state:'pending',attempts:0,agent_run_id:null,error_code:null};
    Q.question(q);Q.pending({schema:1,kind:'action',library_id:V4,question_id:V5,operation:'observe'});
    const invalid=[null,4,'bad','00000000-0000-0000-0000-000000000000',V5.toUpperCase(),V5.replaceAll('-',''),' '+V5,V5+'\n'];
    for(const id of invalid){
      assert.throws(()=>Q.policy({...policy,head_target_id:id}));
      for(const field of ['question_id','target_id','agent_version_id'])assert.throws(()=>Q.question({...q,[field]:id}));
      assert.throws(()=>Q.pending({schema:1,kind:'action',library_id:V4,question_id:id,operation:'observe'}));
    }
    assert.throws(()=>Q.policy({...policy,question_API_connected:false}));
    assert.throws(()=>Q.policy({...policy,question_inference_enabled:false}));
    assert.throws(()=>Q.policy({...policy,head_revision:0}));
    assert.throws(()=>Q.pending({schema:1,kind:'accept',library_id:V4,key:V5,question:'키는 브라우저 v4'}));
    assert.equal(U.uid(V5),false);assert.equal(U.uid(U.uuid()),true);
    console.log(JSON.stringify({result:'passed',mode:input.mode,invalid_values:invalid.length,server_UUID_v4_and_v5:true,browser_key_v4:true}));return;
  }
  assert.match(input.origin,/^http:\/\/127\.0\.0\.1:\d+$/);
  let loss=input.mode==='lost-dispatch',accepted=null;
  const allRequests=[];
  function create(storage){const ui=boot({storage,onRequest:async(url,config)=>{
    allRequests.push({url,method:config.method});
    const response=await fetch(input.origin+url,{method:config.method,headers:{...config.headers,Authorization:'Bearer '+input.token},body:config.body,redirect:'error'});
    const value=await response.json();
    if(url==='/api/questions' && config.method==='POST')accepted=value;
    if(loss && url.endsWith('/dispatch')){loss=false;throw Error('ack lost after real QA HTTP response');}
    return {status:response.status,value};
  }});
    // Real HTTP/SQLite needs elapsed time; the in-memory fixture's immediate
    // event-loop count can expire before an actual request completes.
    ui.settle=async()=>{for(let i=0;i<500;i++){await new Promise(r=>setTimeout(r,10));if(!ui.elements.file.disabled && !ui.elements['login-submit'].disabled)return;}throw Error('HTTP screen did not settle');};
    ui.event=async(id,name='click')=>{if(ui.elements[id].disabled)return;await ui.elements[id].listeners[name]?.({preventDefault(){}});await ui.settle();};
    ui.login=async()=>{ui.elements.token.value='t'.repeat(43);await ui.event('login-form','submit');};
    return ui;
  }
  let a=create();await a.login();
  assert.equal(a.elements.workspace.hidden,false);
  if(input.mode==='paused'){
    assert.equal(a.elements['qa-status'].textContent,'준비 전');assert.equal(a.elements['question-submit'].disabled,true);
    assert.equal(allRequests.filter(r=>r.method==='POST').length,0);
  }else{
    assert.equal(a.elements['qa-status'].textContent,'질문 가능');assert.equal(a.elements['question-submit'].disabled,false);
    if(input.mode==='history'){
      await a.elements['question-list'].children[0].children[0].listeners.click();await a.settle();
      assert.match(a.elements['answer-info'].textContent,new RegExp(input.question_id));
      assert.equal(allRequests.filter(r=>r.method==='POST').length,0);
    }else{
      a.elements['question-input'].value='결론은 무엇인가요?';await a.event('question-form','submit');
      assert.equal(accepted.question_id[14],'5');assert.equal(accepted.target_id[14],'5');
      if(input.mode==='lost-dispatch'){
        const record=JSON.parse(a.storage.get('document-qa.q08.pending-question'));
        assert.equal(record.kind,'action');assert.equal(record.question_id,accepted.question_id);
        const before=allRequests.filter(r=>r.method==='POST').length;
        a=create(a.storage);await a.login();
        assert.equal(allRequests.filter(r=>r.method==='POST').length,before);assert.equal(a.storage.size,0);
        await a.elements['question-list'].children[0].children[0].listeners.click();await a.settle();
      }
      assert.equal(a.elements['answer-state'].textContent,'답변 준비 중');
      assert.equal(a.elements['answer-actions'].children[0].textContent,'답변 진행 확인');
      await choose(a);assert.equal(a.elements['answer-state'].textContent,'답변 완료');
      assert.match(a.elements['answer-text'].textContent,/검증된 사실입니다\. \[S1\]/);
      assert.match(a.elements['answer-sources'].textContent,/이 자료의 결론은 검증된 사실입니다/);
      assert.equal(a.elements['answer-links'].children[0].attributes.href,'#qa-source-'+accepted.question_id+'-S1');
      const before=allRequests.filter(r=>r.method==='POST').length;a.timers[0]();await a.settle();await a.event('refresh');
      assert.equal(allRequests.filter(r=>r.method==='POST').length,before);
      assert.equal(allRequests.filter(r=>r.url.endsWith('/dispatch')).length,1);
      assert.equal(allRequests.filter(r=>r.url.endsWith('/observe')).length,1);
    }
  }
  console.log(JSON.stringify({result:'passed',mode:input.mode,QA_POST_requests:allRequests.filter(r=>r.method==='POST').length,scope:'disposable_QA_HTTP_and_DOM_model_not_browser',production_API_requests:0,inference_requested:false,GPU_requested:false}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
