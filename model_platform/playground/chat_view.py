"""Chat connects to a selected ready deployment and never starts one."""
import streamlit as st
import ui
import presentation as labels
from api import APIError
from state import identifier, can_chat, status_text, answer_messages, accept_answer, error_text


def page():
    st.caption('배포된 대화용 LLM을 선택합니다. OCR 모델은 데이터 관리에서 사용합니다.')
    rows = ui.listing('chat_deployments', 'deployments', '대화 가능한 배포 조회', {'runtime_state': 'ready'})
    shown = 0
    for row in rows:
        dep_id = row['deployment_id']
        info = ui.identity(row.get('model_version_id'))
        if not can_chat(row, info): continue
        shown += 1
        name = ui.identity_title(info) + ' · ' + labels.version_label(info['version'])
        with st.container(border=True):
            if st.button(name + ' · ' + dep_id[:8], key='chat_dep_' + dep_id):
                ui.select('deployment', dep_id); st.rerun()
            st.text('관리 이름: ' + (info['model'].get('name') or '조회하지 못함'))
            ui.origin_summary(info)
    if rows and not shown:
        st.info('조회된 목록에 대화 가능한 LLM이 없습니다. 다른 목록이나 배포 상태를 확인해주세요.')
    with st.expander('답변 설정'):
        st.selectbox('답변 길이', [128, 256, 512], index=1, format_func=lambda n: {128: '짧게', 256: '보통', 512: '길게'}[n], key='answer_length')
        st.slider('답변 다양성', 0.0, 1.2, 0.7, 0.1, key='temperature')
    if ui.state()['ids'].get('deployment'): conversation()


def send(prompt, dep_id):
    ui.state()['pending_prompt'] = prompt
    try:
        messages = answer_messages(ui.state()['messages'], prompt)
        with st.spinner('답변을 작성하고 있습니다…'):
            result = ui.client().call('POST', '/api/deployments/' + dep_id + '/chat',
                                    {'messages': messages, 'max_tokens': st.session_state.get('answer_length', 256),
                                     'temperature': st.session_state.get('temperature', 0.7)}, timeout=135)
        answer = accept_answer(dep_id, result)
    except APIError as exc:
        ui.state()['chat_error'] = error_text(exc); return
    ui.state()['messages'].extend([{'role': 'user', 'content': prompt}, answer])
    ui.state()['pending_prompt'] = None; ui.state()['chat_error'] = ''


@st.fragment(run_every=3)
def conversation():
    dep_id = ui.state()['ids']['deployment']
    if ui.state()['chat_id'] != dep_id:
        ui.state()['chat_id'] = dep_id
        ui.state()['messages'] = []; ui.state()['pending_prompt'] = None; ui.state()['chat_error'] = ''
    try:
        dep = ui.query('/api/deployments/' + dep_id)
        if dep.get('deployment_id') != dep_id:
            raise APIError('배포 응답이 다릅니다.', code='INVALID_RESPONSE')
        info = ui.identity(dep.get('model_version_id'))
        chat_allowed = can_chat(dep, info) and info['model'].get('model_type') == 'llm'
        ui.model_identity(info, '대화 · ' + ui.identity_title(info))
        title, explanation = status_text(dep, info=info)
        if chat_allowed: st.success(explanation)
        elif can_chat(dep, info): st.info('대화용 LLM인지 확인하지 못했습니다. 모델 관리에서 모델 종류를 확인해주세요.')
        else: st.info(title + ' · ' + explanation)
        a, b = st.columns(2)
        if a.button('새 대화', disabled=not ui.state()['messages'] and not ui.state()['pending_prompt']):
            ui.state()['messages'] = []; ui.state()['pending_prompt'] = None; ui.state()['chat_error'] = ''; st.rerun()
        if b.button('관리에서 배포 보기'): ui.go('deployments', deployment=dep_id)
        for message in ui.state()['messages']:
            with st.chat_message(message['role']):
                st.markdown(message['content'])
                if message.get('truncated'): st.caption('설정한 답변 길이에 도달했습니다.')
        pending = ui.state()['pending_prompt']
        if pending:
            with st.chat_message('user'): st.markdown(pending)
            st.warning(ui.state().get('chat_error') or '전송 결과를 확인하지 못했습니다.')
            if st.button('같은 질문 다시 보내기', disabled=not chat_allowed):
                send(pending, dep_id); st.rerun()
        elif chat_allowed and not ui.state()['messages']:
            with st.expander('예제 질문', expanded=True):
                for i, prompt in enumerate(['인공지능을 쉽게 설명해 줘.', '짧은 업무 이메일 인사말을 작성해 줘.', 'Say hello in one short sentence.']):
                    if st.button(prompt, key='example_' + str(i)):
                        send(prompt, dep_id); st.rerun()
        prompt = st.chat_input('질문을 입력하세요', max_chars=4000, disabled=not chat_allowed or bool(pending))
        if prompt: send(prompt, dep_id); st.rerun()
        st.caption('대화 기록은 현재 브라우저 세션에만 보관됩니다. 모델 실행은 배포 관리에서 중지하세요.')
    except APIError as exc:
        ui.error(exc)
