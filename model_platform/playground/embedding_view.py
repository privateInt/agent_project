"""Explicit embedding check on an existing deployment; no automatic inference."""
import math
import streamlit as st
import ui
from state import can_embed


def render(deployment, info):
    dep_id = deployment['deployment_id']
    st.markdown('**임베딩 확인**')
    st.caption('문서 또는 질문 원문을 입력하세요. 질문에 필요한 접두어는 서버에서 적용합니다.')
    with st.form('embedding_check_' + dep_id):
        role = st.selectbox('입력 용도', ['query', 'document'], format_func=lambda v: '질문' if v == 'query' else '문서')
        text = st.text_area('변환할 내용', value='대한민국의 수도는 어디인가?', max_chars=16000)
        submitted = st.form_submit_button('임베딩 실행', disabled=not can_embed(deployment, info))
    key = 'embedding_check_' + dep_id
    if submitted:
        ui.state()['results'].pop(key, None)
        if not text.strip():
            st.warning('변환할 내용을 입력하세요.')
            return
        try:
            client = ui.client()
            profile = client.call('GET', '/api/deployments/' + dep_id + '/embedding-profile')
            if profile.get('deployment_id') != dep_id or profile.get('capability') != 'embedding' or not profile.get('embedding_space_id'):
                raise ui.api.APIError('profile 응답을 확인하지 못했습니다.', code='INVALID_RESPONSE')
            with st.spinner('임베딩을 계산하고 있습니다…'):
                result = client.call('POST', '/api/deployments/' + dep_id + '/embeddings', {
                    'expected_embedding_space_id': profile['embedding_space_id'], 'role': role,
                    'inputs': [{'id': 'preview-1', 'text': text}]}, timeout=135)
            rows = result.get('data') or []
            if (len(rows) != 1 or rows[0].get('id') != 'preview-1' or rows[0].get('index') != 0
                    or result.get('role') != role or result.get('profile', {}).get('embedding_space_id') != profile['embedding_space_id']):
                raise ui.api.APIError('입력에 대응하는 결과가 아닙니다.', code='EMBEDDING_INVALID_RESPONSE')
            vector = rows[0].get('embedding') or []
            if len(vector) != profile.get('dimension') or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vector):
                raise ui.api.APIError('벡터 응답을 확인하지 못했습니다.', code='EMBEDDING_INVALID_RESPONSE')
            norm = math.sqrt(sum(x*x for x in vector))
            if abs(norm - 1) > 0.001:
                raise ui.api.APIError('벡터 정규화를 확인하지 못했습니다.', code='EMBEDDING_INVALID_RESPONSE')
            ui.state()['results'][key] = {'dimension': len(vector), 'input_tokens': rows[0]['input_tokens'],
                                          'norm': round(norm, 6), 'role': role, 'profile': result['profile']}
        except ui.api.APIError as exc:
            ui.error(exc)
    result = ui.state()['results'].get(key)
    if result:
        st.success(f"임베딩 응답 확인 · {result['dimension']}차원 · {result['input_tokens']}토큰")
        st.caption(('질문' if result['role'] == 'query' else '문서') + f" 입력 · L2 norm {result['norm']} · 마지막 명시적 실행 결과")
        ui.details('임베딩 공간·실행 모델 정보', result['profile'])
