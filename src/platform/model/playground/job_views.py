"""Training and deployment inventories and explicit runtime control buttons."""
import streamlit as st
import math
import ui
import presentation as labels
import table_style
from state import identifier, can_chat, can_embed, can_rerank, inference_kind, status_text
import embedding_view
import reranking_view


def training():
    st.caption('학습 시작은 모델 관리에서 버전을 선택해 실행합니다. 여기서는 상태·로그·결과와 취소·복구를 확인합니다.')
    if st.button('모델에서 새 학습 선택'):
        ui.go('models')
    status = st.selectbox('학습 상태 필터', ['전체', 'created', 'running', 'cancelling', 'cancelled', 'failed', 'succeeded'], format_func=labels.state_label)
    rows = ui.listing('training', 'training-runs', '학습 작업 조회', {'status': '' if status == '전체' else status})
    names = {}
    for row in rows:
        info = ui.identity(row.get('source_model_version_id'))
        names[row['training_run_id']] = ui.identity_title(info) + ' · ' + labels.state_label(row['status']) + ' · ' + row['training_run_id'][:8]
        with st.container(border=True):
            if st.button(names[row['training_run_id']], key='run_' + row['training_run_id']):
                ui.go('training', run=row['training_run_id'])
            st.text('관리 이름: ' + (info['model'].get('name') or '조회하지 못함'))
            st.caption('학습에 사용한 플랫폼 버전: ' + labels.version_label(info['version']) + ' · ' + str((row.get('training') or {}).get('max_steps', '?')) + '단계')
            ui.origin_summary(info)
    if len(rows) >= 2:
        with st.expander('학습 평가 결과 비교'):
            chosen = st.multiselect('비교할 작업 (2~5개)', list(names), format_func=names.get, max_selections=5)
            if st.button('평가 비교 조회', disabled=len(chosen) < 2):
                ui.state()['results']['compare'] = ui.query('/api/training-runs/compare', training_run_ids=','.join(chosen))
            if ui.state()['results'].get('compare'):
                result = ui.state()['results']['compare']
                st.info('비교 가능' if result.get('comparable') else '직접 비교 불가: ' + result.get('reason', ''))
                st.json(result)


@st.fragment(run_every=5)
def training_detail(run_id):
    if not ui.detail_is_open('run', run_id):
        return
    try:
        run = ui.query('/api/training-runs/' + run_id)
        info = ui.identity(run.get('source_model_version_id'))
        ui.model_identity(info, '선택한 학습 · ' + ui.identity_title(info))
        st.info('학습 상태: ' + labels.state_label(run['status']))
        ui.operation(run)
        ui.details('학습 설정·상태 상세', run)
        if run['status'] == 'succeeded':
            st.success('학습을 마쳤습니다. 아래 학습 결과 모델 열기에서 배포할 수 있습니다.')
        elif run['status'] == 'created':
            st.caption('실행 순서를 기다리고 있습니다. 시작 버튼을 다시 누르지 않아도 됩니다.')
        if st.button('학습 상태 새로고침'): st.rerun()
        active = run['status'] in ('created', 'pending', 'running', 'cancelling')
        if st.button('학습 취소', disabled=not active or run['status'] == 'cancelling'):
            ui.direct_post('/api/training-runs/' + run_id + '/cancel'); st.rerun()
        with st.expander('학습 로그'):
            if st.button('로그 조회'):
                ui.state()['results']['logs_' + run_id] = ui.query('/api/training-runs/' + run_id + '/logs', tail_lines=200, max_bytes=65536)
            logs = ui.state()['results'].get('logs_' + run_id)
            if logs:
                st.caption('로그 상태: ' + logs.get('availability', '') + ' · ' + logs.get('source', ''))
                st.code(logs.get('stdout', ''), language='text')
                if logs.get('stderr'): st.code(logs['stderr'], language='text')
                if logs.get('truncated'): st.caption('최근 로그만 표시합니다.')
        with st.expander('평가 결과'):
            if st.button('평가 결과 조회'):
                ui.state()['results']['evaluation_' + run_id] = ui.query('/api/training-runs/' + run_id + '/evaluation')
            result = ui.state()['results'].get('evaluation_' + run_id)
            if result:
                st.caption('평가 상태: ' + labels.state_label(result.get('state', '')))
                scores = result.get('result') or {}
                if scores.get('loss') is not None:
                    st.metric('평가 손실', round(scores['loss'], 4))
                    st.caption('같은 평가 데이터와 조건에서 낮을수록 정답을 더 잘 예측했습니다. 일반적인 대화 품질 점수는 아닙니다.')
                ui.details('평가 수치 상세', result)
        with st.expander('체크포인트·출력 복구'):
            st.caption('체크포인트 재개는 남은 학습을 수행하는 새 작업입니다. 출력 업로드 재시도는 재학습하지 않습니다.')
            if st.button('체크포인트 조회'):
                ui.state()['results']['checkpoints_' + run_id] = ui.query('/api/training-runs/' + run_id + '/checkpoints')
            checkpoints = ui.state()['results'].get('checkpoints_' + run_id)
            if checkpoints:
                st.json(checkpoints)
                choices = {r['step']: r for r in checkpoints.get('checkpoints', []) if r.get('resumable')}
                if choices:
                    step = st.selectbox('재개할 체크포인트', list(choices), format_func=lambda k: str(k) + '단계')
                    if st.button('체크포인트에서 새 학습 시작', disabled=run['status'] not in ('failed', 'cancelled')):
                        ui.submit('resume', run_id, {'checkpoint_step': step})
            if st.button('출력 보존 상태 조회'):
                ui.state()['results']['output_' + run_id] = ui.query('/api/training-runs/' + run_id + '/output')
            output = ui.state()['results'].get('output_' + run_id)
            if output:
                st.json(output)
                if st.button('완료 출력 업로드 재시도', disabled=output.get('availability') != 'retained' or run['status'] != 'failed'):
                    ui.submit('retry_output', run_id)
        output_id = run.get('output_model_version_id') or run.get('recovered_model_version_id')
        if output_id and st.button('학습 결과 모델 열기'):
            version = ui.query('/api/model-versions/' + identifier(output_id))
            st.session_state.model_action = '상세'
            ui.state().pop('row_action_source', None)
            ui.go('models', model=version['model_id'], version=output_id)
    except ui.api.APIError as exc:
        ui.error(exc)


def refresh_deployment(dep_id):
    dep_id = identifier(dep_id)
    result = ui.query('/api/deployments/' + dep_id)
    if result.get('deployment_id') != dep_id:
        raise ui.api.APIError('배포 응답이 선택한 항목과 다릅니다.', code='INVALID_RESPONSE')
    for entry in ui.state()['lists'].values():
        items = entry.get('page', {}).get('items', [])
        for n, item in enumerate(items):
            if item.get('deployment_id') == dep_id:
                items[n] = result
    return result


def deployment_inventory(key):
    with st.container(key='deployment_toolbar_' + key):
        query, filter_col, space = st.columns([1.2, 1.8, 5.85], vertical_alignment='center')
        query_clicked = query.button('배포 조회', key='query_' + key, type='primary', use_container_width=True)
        status = filter_col.selectbox('배포 상태', ['전체', 'queued', 'waiting_resources', 'preparing', 'starting', 'ready', 'stopping', 'stopped', 'failed', 'unknown'],
                                      format_func=labels.state_label, key=key + '_filter', label_visibility='collapsed')
    rows = ui.listing(key, 'deployments', '배포 조회', {'runtime_state': '' if status == '전체' else status}, trigger=query_clicked, paginate=False)
    if key in ui.state()['lists']:
        with st.container(key='deployment_inventory_table_' + key):
            with st.container(key='deployment_table_head_' + key):
                for col, title in zip(st.columns(table_style.DEPLOYMENT_COLUMNS), ('등록 이름', 'Hugging Face 원본', '배포 버전', '상태', '작업')):
                    col.markdown(title)
            for row in rows:
                deployment_row(row)
            ui.list_footer(key)


def is_stopped_serving_deployment(row):
    return (not row.get('deleted_at') and row.get('status') == 'stopped'
            and row.get('runtime_state') == 'stopped' and row.get('desired_state') == 'stopped'
            and (row.get('serving') or {}).get('engine') == 'vllm')


def can_delete_deployment(row):
    return is_stopped_serving_deployment(row)


def redeployment_body(dep):
    """Copy a stopped deployment's persisted inputs, never display metadata."""
    if not is_stopped_serving_deployment(dep):
        raise ui.api.APIError('종료가 확인된 배포만 다시 배포할 수 있습니다.', code='FailedPrecondition')
    try:
        version = identifier(dep['model_version_id'])
        serving, resources = dep['serving'], dep['resources']
        length, memory = serving['max_model_len'], serving['gpu_memory_utilization']
        gpu_ids, node = resources['gpu_ids'], resources.get('node_name', '')
        profile = serving.get('profile', '')
        if (type(length) is not int or length <= 0 or type(memory) not in (int, float)
                or not math.isfinite(memory) or not 0 < memory < 1
                or not isinstance(profile, str) or not isinstance(node, str)
                or not isinstance(gpu_ids, list) or len(gpu_ids) != 1
                or type(gpu_ids[0]) is not int or gpu_ids[0] < 0
                or resources.get('gpu_count', 1) != 1):
            raise ValueError('incomplete deployment settings')
        return {'model_version_id': version,
                'serving': {'engine': 'vllm', 'profile': profile, 'max_model_len': length,
                            'gpu_memory_utilization': memory},
                'resources': {'gpu_ids': list(gpu_ids), 'gpu_count': 1, 'node_name': node}}
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ui.api.APIError('기존 배포 설정을 확인하지 못했습니다. 배포를 다시 조회하세요.', code='INVALID_RESPONSE') from None


def prepare_redeployment(dep_id, location):
    body = redeployment_body(refresh_deployment(dep_id))
    ui.state()['redeploy'] = {'deployment_id': dep_id, 'body': body, 'location': location,
                            'gateway': ui.client().base}
    ui.state().pop('stop_deployment', None)
    ui.state().pop('delete_deployment', None)
    if location == 'row':
        ui.close_detail()


def confirm_redeployment(saved):
    if saved['gateway'] != ui.client().base:
        raise ui.api.APIError('서버 연결이 바뀌었습니다.', code='SERVER_CHANGED')
    current = redeployment_body(refresh_deployment(saved['deployment_id']))
    if current != saved['body']:
        raise ui.api.APIError('배포 설정이 변경됐습니다. 다시 선택해 확인하세요.', code='REDEPLOY_CHANGED')
    ui.submit('deploy', current['model_version_id'], current)


def redeployment_confirmation(dep_id, location):
    saved = ui.state().get('redeploy')
    if not saved or saved['deployment_id'] != dep_id or saved['location'] != location:
        return
    body = saved['body']
    serving, resources = body['serving'], body['resources']
    st.info('배포 ' + dep_id[:8] + '의 모델 버전과 설정으로 새 배포를 만들까요?')
    st.caption('새 배포 ID가 발급됩니다. 기존 배포 이력은 보존되며, 연결 서비스에서는 새 ID를 선택해야 합니다.')
    st.caption('모델 버전 ID: ' + body['model_version_id'])
    st.caption('서빙: ' + (serving['profile'] or '기본 vLLM') + ' · GPU ' + str(resources['gpu_ids'][0])
               + ' · 최대 길이 ' + str(serving['max_model_len'])
               + ' · GPU 메모리 비율 ' + str(serving['gpu_memory_utilization'])
               + (' · 노드 ' + resources['node_name'] if resources['node_name'] else ''))
    pending = bool(ui.state().get('pending_payload'))
    if pending:
        st.caption('진행 중인 요청은 화면 위의 같은 요청 다시 확인으로 결과를 확인하세요.')
    yes, no = st.columns(2)
    if yes.button('같은 설정으로 새 배포', key='confirm_redeploy_' + location + '_' + dep_id, disabled=pending):
        try:
            confirm_redeployment(saved)
        except ui.api.APIError as exc:
            ui.error(exc)
    if no.button('취소', key='cancel_redeploy_' + location + '_' + dep_id):
        ui.state().pop('redeploy', None)
        st.rerun()


def remove_deployment(dep_id):
    client = ui.client()
    if ui.state().get('delete_deployment_gateway') != client.base:
        raise ui.api.APIError('서버 연결이 바뀌었습니다.', code='SERVER_CHANGED')
    result = client.call('DELETE', '/api/deployments/' + identifier(dep_id), timeout=100)
    if result.get('deployment_id') != dep_id or not result.get('deleted_at'):
        raise ui.api.APIError('배포 삭제 결과를 확인하지 못했습니다.', code='INVALID_RESPONSE')
    for entry in ui.state()['lists'].values():
        page = entry.get('page', {})
        if 'items' in page:
            page['items'] = [r for r in page['items'] if r.get('deployment_id') != dep_id]
    ui.state().pop('delete_deployment', None)
    ui.state().pop('stop_deployment', None)
    if (ui.state().get('redeploy') or {}).get('deployment_id') == dep_id:
        ui.state().pop('redeploy', None)
    ui.state()['display_cache'] = {}
    if ui.state()['ids'].get('deployment') == dep_id:
        ui.state()['ids'].pop('deployment', None)
        ui.state().pop('detail_dialog', None)
    ui.state()['notice'] = '배포 컨테이너와 전용 캐시를 삭제했습니다. 등록 모델은 다시 배포할 수 있습니다.'
    ui.sync_url()
    st.rerun()


def deployment_row(row):
    dep_id = identifier(row['deployment_id'])
    info = ui.identity(row.get('model_version_id'))
    with st.container(key='deployment_table_row_' + dep_id):
        title, origin, version, status_col, actions = st.columns(table_style.DEPLOYMENT_COLUMNS, vertical_alignment='center')
        title.text(info['model'].get('name') or ui.identity_title(info))
        title.caption('배포 ' + dep_id[:8])
        origin.text(ui.identity_title(info))
        origin.caption(table_style.revision_summary(info['origin']))
        version.text(labels.version_label(info['version']))
        with status_col:
            table_style.status_badge(row.get('runtime_state') or row.get('status'))
        with actions, st.container(key='table_actions_' + dep_id):
            inspect, chat, stop, delete = st.columns(4, gap='small')
            if inspect.button('조회', key='dep_' + dep_id, use_container_width=True):
                try:
                    refresh_deployment(dep_id)
                    ui.go('deployments', deployment=dep_id)
                except ui.api.APIError as exc:
                    ui.error(exc)
            kind = inference_kind(row, info)
            enabled = can_embed(row, info) if kind == 'embedding' else can_rerank(row, info) if kind == 'reranker' else can_chat(row, info)
            if chat.button({'embedding': '임베딩', 'reranker': '재정렬'}.get(kind, '채팅'), key='row_chat_' + dep_id, disabled=not enabled, use_container_width=True):
                ui.go('deployments' if kind in ('embedding', 'reranker') else 'chat', deployment=dep_id)
            if is_stopped_serving_deployment(row):
                if stop.button('재배포', key='row_redeploy_' + dep_id, use_container_width=True):
                    try: prepare_redeployment(dep_id, 'row')
                    except ui.api.APIError as exc: ui.error(exc)
            elif stop.button('종료', key='row_stop_' + dep_id,
                           disabled=row.get('runtime_state') == 'stopped' or row.get('desired_state') == 'stopped', use_container_width=True):
                ui.state()['stop_deployment'] = dep_id
            if delete.button('삭제', key='row_delete_deployment_' + dep_id, disabled=not can_delete_deployment(row), use_container_width=True, help='종료가 확인된 배포를 삭제합니다.'):
                ui.state()['delete_deployment'] = dep_id
                ui.state()['delete_deployment_gateway'] = ui.client().base
                ui.state().pop('stop_deployment', None)
                ui.state().pop('redeploy', None)
        redeployment_confirmation(dep_id, 'row')
        if ui.state().get('delete_deployment') == dep_id:
            st.warning('배포 ' + dep_id[:8] + '의 컨테이너와 전용 캐시를 삭제할까요?')
            st.caption('컨테이너 로그도 삭제됩니다. 등록 모델 파일과 작업 이력은 보존됩니다.')
            yes, no = st.columns(2)
            if yes.button('배포 삭제', key='confirm_delete_deployment_' + dep_id):
                try: remove_deployment(dep_id)
                except ui.api.APIError as exc:
                    ui.error(exc)
                    st.info('삭제 완료를 확인하지 못했습니다. 같은 삭제 버튼으로 다시 확인할 수 있습니다.')
            if no.button('취소', key='cancel_delete_deployment_' + dep_id):
                ui.state().pop('delete_deployment', None)
                st.rerun()
        if ui.state().get('stop_deployment') == dep_id:
            st.warning('“' + (info['model'].get('name') or ui.identity_title(info)) + '”의 배포 ' + dep_id[:8] + '을 종료할까요?')
            st.caption('이 배포를 사용하는 추론 요청이 중단됩니다. 모델 파일과 실행 이력은 보존됩니다.')
            yes, no = st.columns(2)
            if yes.button('배포 종료', key='confirm_stop_' + dep_id):
                try:
                    ui.direct_post('/api/deployments/' + dep_id + '/stop')
                    refresh_deployment(dep_id)
                    ui.state().pop('stop_deployment', None)
                    st.rerun()
                except ui.api.APIError as exc:
                    ui.error(exc)
                    st.info('최신 상태를 조회하거나 같은 종료 버튼으로 다시 확인하세요.')
            if no.button('취소', key='cancel_stop_' + dep_id):
                ui.state().pop('stop_deployment', None)
                st.rerun()


@st.fragment(run_every=3)
def deployment_detail(dep_id):
    if not ui.detail_is_open('deployment', dep_id):
        return
    try:
        dep = refresh_deployment(dep_id)
        info = ui.identity(dep.get('model_version_id'))
        ui.model_identity(info, '선택한 배포 · ' + ui.identity_title(info))
        title, text = status_text(dep, info=info)
        if can_chat(dep, info): st.success(title + ' · ' + text)
        else: st.info(title + ' · ' + text)
        ui.operation(dep)
        ui.details('배포 상세', dep)
        a, b, c = st.columns(3)
        if a.button('배포 상태 새로고침'): st.rerun()
        if is_stopped_serving_deployment(dep):
            if b.button('재배포', key='detail_redeploy_' + dep_id):
                prepare_redeployment(dep_id, 'detail')
        elif b.button('배포 중지', disabled=dep.get('runtime_state') == 'stopped' or dep.get('desired_state') == 'stopped'):
            ui.direct_post('/api/deployments/' + dep_id + '/stop'); st.rerun()
        if c.button('이 배포와 채팅', disabled=not can_chat(dep, info)):
            ui.go('chat', deployment=dep_id)
        st.caption('중지하면 이 배포를 사용하는 다른 서비스의 추론 요청도 처리할 수 없습니다.')
        redeployment_confirmation(dep_id, 'detail')
        if inference_kind(dep, info) == 'embedding':
            embedding_view.render(dep, info)
        elif inference_kind(dep, info) == 'reranker':
            reranking_view.render(dep, info)
    except ui.api.APIError as exc:
        ui.error(exc)
