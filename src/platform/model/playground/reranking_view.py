"""Explicit candidate scoring on an existing reranker deployment."""
import streamlit as st
import ui
from state import can_rerank
from reranking_validation import validate_profile, validate_result


def render(deployment, info):
    dep_id = deployment['deployment_id']
    st.markdown('**재정렬 점수 확인**')
    st.caption('질문과 비교할 후보를 입력하세요. 점수가 높을수록 관련성이 높으며 확률은 아닙니다.')
    with st.form('reranking_check_' + dep_id):
        query = st.text_area('질문', value='대한민국의 수도는 어디인가?', max_chars=4000)
        first = st.text_area('후보 1', value='대한민국의 수도는 서울이다.', max_chars=8000)
        second = st.text_area('후보 2', value='사과는 과일이다.', max_chars=8000)
        submitted = st.form_submit_button('점수 계산', disabled=not can_rerank(deployment, info))
    key = 'reranking_check_' + dep_id
    previous = ui.state()['results'].get(key)
    if previous and (not can_rerank(deployment, info) or previous['profile']['runtime_container_id'] != deployment.get('runtime_container_id')):
        ui.state()['results'].pop(key, None)
    if submitted:
        ui.state()['results'].pop(key, None)
        if not all(x.strip() for x in (query, first, second)):
            st.warning('질문과 두 후보를 모두 입력하세요.')
            return
        try:
            client = ui.client()
            profile = validate_profile(client.call('GET', '/api/deployments/' + dep_id + '/reranker-profile'), dep_id)
            candidates = [{'id': 'candidate-1', 'text': first}, {'id': 'candidate-2', 'text': second}]
            with st.spinner('질문과 후보의 관련성을 계산하고 있습니다…'):
                result = client.call('POST', '/api/deployments/' + dep_id + '/rerank', {
                    'expected_reranker_profile_id': profile['reranker_profile_id'], 'query': query,
                    'candidates': candidates}, timeout=135)
            rows = validate_result(result, profile, [c['id'] for c in candidates])
            ui.state()['results'][key] = {'profile': profile, 'rows': rows}
        except ui.api.APIError as exc:
            ui.error(exc)
        except (ValueError, KeyError, TypeError):
            ui.error(ui.api.APIError('재정렬 응답을 확인하지 못했습니다.', code='RERANK_INVALID_RESPONSE'))
    result = ui.state()['results'].get(key)
    if result:
        st.success('후보 2개의 점수를 확인했습니다.')
        st.table([{'후보': row['id'], '관련성 점수': row['score'], '질문+후보 토큰': row['input_tokens']}
                  for row in sorted(result['rows'], key=lambda row: (-row['score'], row['index']))])
        st.caption('마지막 명시적 실행 결과 · 점수 순으로 표시 · 검색 품질 평가와는 별도입니다.')
        ui.details('재정렬 모델·점수 정보', result['profile'])
