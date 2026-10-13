'use strict';
(() => {
  const ids=new Set(['retry-upload','dispatch-workflow','retry-workflow','dispatch-refinement-workflow',
    'retry-refinement-workflow','resume-refinement-workflow','retry-refinement','observe-refinement']);
  const reasons={
    EXPLICIT_ACTION:'필요한 동작을 선택하면 최신 상태를 확인한 뒤 실행 내용을 보여드립니다.',
    RUNNING:'내용 정리가 진행 중입니다. 상태 조회는 처리 요청을 반복하지 않습니다.',
    GPU_WAIT:'GPU 사용을 기다리고 있습니다. 관리자가 GPU 준비를 확인한 뒤 검색 준비를 이어갑니다.',
    SEARCH_READY:'검색 자료 준비를 확인했습니다. 질문 연결은 자료함의 질문 상태에서 확인하세요.',
    SEARCH_SETUP_REQUIRED:'내용 정리를 완료했습니다. 관리자의 검색 준비와 질문 연결 확인이 필요합니다.',
    SETUP_REQUIRED:'처리 연결의 준비가 필요합니다. 관리자에게 확인을 요청하세요.',
    TERMINAL_REVIEW:'정제 결과에 확인할 문제가 있습니다. 관리자 확인 후 처리 방향을 정해야 합니다.',
    ADMIN_REQUIRED:'현재 상태에서는 화면에서 이어갈 수 없습니다. 관리자에게 자세한 확인 정보를 전달하세요.',
  };
  const uid=v=>typeof v==='string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
  const revision=v=>typeof v==='string' && /^[0-9a-f]{64}$/.test(v);
  function plan(value,jobId) {
    if(!value || value.schema!==1 || value.job_id!==jobId || !uid(jobId) || !revision(value.revision)
       || !Array.isArray(value.actions) || value.actions.length>2 || !Object.hasOwn(reasons,value.reason)
       || value.inference_requested!==false)throw Error('RECOVERY_RESPONSE_INVALID');
    for(const action of value.actions)if(!action || !ids.has(action.id) || typeof action.title!=='string'
      || !action.title.length || action.title.length>100 || typeof action.detail!=='string'
      || !action.detail.length || action.detail.length>400)throw Error('RECOVERY_RESPONSE_INVALID');
    if(new Set(value.actions.map(a=>a.id)).size!==value.actions.length)throw Error('RECOVERY_RESPONSE_INVALID');
    return value;
  }
  function pending(value) {
    if(!value || Object.keys(value).sort().join(',')!=='action,job_id,library_id,revision,schema'
       || value.schema!==1 || !uid(value.job_id) || !uid(value.library_id) || !ids.has(value.action)
       || !revision(value.revision))throw Error('RECOVERY_RECORD_INVALID');
    return value;
  }
  window.QARecovery=Object.freeze({plan,pending,reason:code=>reasons[code] || reasons.ADMIN_REQUIRED});
})();
