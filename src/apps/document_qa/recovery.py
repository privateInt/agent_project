"""Q07b local recovery projection. No transport, new intent, schema or inference."""
from .contracts import fingerprint


ACTIONS = {
    'retry-upload': ('원본 전달 이어가기', '서버에 보관된 같은 파일로 전달을 다시 시도합니다. 새 문서를 등록하지 않습니다.'),
    'dispatch-workflow': ('문서 처리 연결하기', '접수된 원본의 처리 흐름을 시작합니다. 원본 파일은 다시 올리지 않습니다.'),
    'retry-workflow': ('처리 연결 다시 확인하기', '접수 결과를 확인하지 못한 문서 처리 연결을 다시 요청합니다. 이미 확인된 결과는 재사용합니다.'),
    'dispatch-refinement-workflow': ('내용 정리 시작하기', '접수된 원본의 내용을 정리합니다. 검색 준비는 별도로 확인합니다.'),
    'retry-refinement-workflow': ('내용 정리 연결 다시 확인하기', '접수 결과를 확인하지 못한 내용 정리 연결을 다시 요청합니다. 같은 처리 요청을 유지합니다.'),
    'resume-refinement-workflow': ('내용 정리 이어가기', '중단된 내용 정리를 이어갑니다. 이미 접수된 정제와 확인된 결과는 그대로 사용합니다.'),
    'retry-refinement': ('내용 정리 접수 다시 확인하기', '접수 결과를 확인하지 못한 내용 정리를 다시 요청합니다. 같은 처리 요청을 유지합니다.'),
    'observe-refinement': ('정제 결과 확인하기', '접수된 정제 결과를 조회해 문서 상태를 갱신합니다. 정제 요청을 다시 전송하지 않습니다.'),
}
INTEGRITY_ERRORS = {
    'UPLOAD_TARGET_CHANGED', 'STORED_UPLOAD_CHANGED', 'REFINEMENT_TARGET_CHANGED',
    'REFINEMENT_INTENT_CHANGED', 'REFINEMENT_SOURCE_MISMATCH', 'REFINEMENT_PROVENANCE_MISMATCH',
    'CANONICAL_IDENTITY_MISMATCH', 'CANONICAL_BYTES_MISMATCH', 'CANONICAL_SOURCE_MISMATCH',
    'CANONICAL_PROCESSOR_MISMATCH', 'CANONICAL_STRUCTURE_INVALID', 'CANONICAL_QUALITY_INVALID',
    'CANONICAL_JSON_INVALID', 'CANONICAL_RESPONSE_INVALID', 'UNEXPECTED_OCR_RESULT',
    'HANDOFF_INTENT_CHANGED', 'HANDOFF_TARGET_CHANGED', 'WORKFLOW_SETUP_CHANGED',
    'REFINEMENT_FLOW_INTENT_CHANGED', 'REFINEMENT_FLOW_TARGET_CHANGED',
}


def projection(job, capabilities):
    """Offer only the earliest incomplete CPU stage, using existing adapter limits."""
    from .uploads import MAX_ATTEMPTS as UPLOAD_ATTEMPTS
    from .workflows import MAX_DELIVERIES
    from .refinement import MAX_ATTEMPTS as REFINEMENT_ATTEMPTS
    from .refinement_flow import MAX_GENERATIONS

    entry = job.get('workflow') or {}
    domain = job.get('refinement') or {}
    flow = job.get('refinement_workflow') or {}
    index = job.get('indexing') or {}
    indexing_flow = job.get('indexing_workflow') or {}
    selected, reason = [], 'ADMIN_REQUIRED'

    def add(action):
        title, detail = ACTIONS[action]
        selected.append({'id': action, 'title': title, 'detail': detail})

    def flow_action():
        if not capabilities['cpu_flow']:
            return 'SETUP_REQUIRED'
        delivery, state = flow.get('delivery_state'), flow.get('state', 'not_requested')
        if state == 'not_requested' or delivery == 'pending' and flow.get('attempts', 0) == 0:
            add('dispatch-refinement-workflow')
        elif delivery == 'confirmation_required' and flow.get('attempts', 0) < MAX_DELIVERIES:
            add('retry-refinement-workflow')
        elif delivery == 'accepted' and state in ('attention_required', 'expired') and flow.get('generation', MAX_GENERATIONS) < MAX_GENERATIONS:
            add('resume-refinement-workflow')
        else:
            return 'RUNNING' if state in ('running', 'awaiting_execution') and delivery == 'accepted' else 'ADMIN_REQUIRED'
        return 'EXPLICIT_ACTION'

    if any(value.get('error_code') in INTEGRITY_ERRORS for value in (job, entry, domain, flow)):
        reason = 'ADMIN_REQUIRED'
    elif job['state'] in ('upload_accepted', 'upload_confirmation_required'):
        if capabilities['upload'] and job.get('attempts', 0) < UPLOAD_ATTEMPTS:
            add('retry-upload')
            reason = 'EXPLICIT_ACTION'
    elif job['state'] == 'preparation_pending':
        if domain.get('state') == 'canonical_verified' or flow.get('state') == 'canonical_verified':
            reason = ('GPU_WAIT' if indexing_flow.get('error_code') == 'GPU_VERIFICATION_DEFERRED'
                      else 'SEARCH_READY' if index.get('state') == 'activated' else 'SEARCH_SETUP_REQUIRED')
        elif domain.get('terminal'):
            reason = 'TERMINAL_REVIEW'
        elif entry.get('state') != 'accepted':
            if capabilities['entry_flow']:
                if entry.get('state', 'not_requested') in ('not_requested', 'pending') and entry.get('attempts', 0) == 0:
                    add('dispatch-workflow')
                elif entry.get('state') == 'confirmation_required' and entry.get('attempts', 0) < MAX_DELIVERIES:
                    add('retry-workflow')
                reason = 'EXPLICIT_ACTION' if selected else 'ADMIN_REQUIRED'
            else:
                reason = 'SETUP_REQUIRED'
        elif domain.get('receipt'):
            if capabilities['refiner']:
                add('observe-refinement')
                reason = 'EXPLICIT_ACTION'
            # A known Data receipt is reused by the new CPU generation, never reposted.
            next_reason = flow_action()
            if not selected:
                reason = next_reason
        elif domain.get('attempts', 0) > 0:
            # A new CPU generation cannot resolve an unknown Data POST. Replay its
            # saved intent explicitly first; never create a different refine intent.
            if capabilities['refiner'] and domain.get('attempts', 0) < REFINEMENT_ATTEMPTS:
                add('retry-refinement')
                reason = 'EXPLICIT_ACTION'
        else:
            reason = flow_action()

    return {'schema': 1, 'job_id': job['job_id'],
            'revision': fingerprint({'job': job, 'capabilities': capabilities}),
            'actions': selected, 'reason': reason, 'inference_requested': False}
