"""Model selection leads to import/training/deployment forms; dataset uploads."""
import os
import streamlit as st
import ui
import presentation as labels
import table_style
from progress_view import compact_progress
from api import APIError
from state import identifier, embedding_model, can_train_model, can_deploy_model, EMBEDDING_PROFILE, reranker_model, RERANKER_PROFILE

SAMPLE = ('{"messages":[{"role":"user","content":"안녕"},{"role":"assistant","content":"안녕하세요!"}]}\n'
          '{"messages":[{"role":"user","content":"너는 무엇을 하니?"},{"role":"assistant","content":"질문에 답하는 AI입니다."}]}\n').encode()


def gpu_input(key):
    try:
        default = int(os.environ.get('MODEL_PLAYGROUND_GPU_ID', '0'))
        if not 0 <= default <= 63:
            raise ValueError()
    except ValueError:
        raise APIError('MODEL_PLAYGROUND_GPU_ID 설정을 확인하세요.', 400, 'INVALID_GPU') from None
    return st.number_input('사용할 GPU 번호', min_value=0, max_value=63, value=default, step=1, key=key)


def add_model_form():
    with st.container(border=True):
        st.subheader('새 모델 추가')
        with st.form('add_model_form'):
            name = st.text_input('등록 이름', max_chars=200, help='내 모델 목록에서 구분할 이름입니다. 예: 업무용 Gemma')
            repo = st.text_input('Hugging Face 저장소', placeholder='google/gemma-3-1b-it', key='add_repo')
            token = st.text_input('HF 토큰 (필요한 경우)', type='password', key='add_hf_token', help='접근 승인이 필요한 모델은 승인받은 계정의 읽기 토큰을 입력하세요.')
            with st.expander('버전·설명 등 추가 옵션'):
                kind = st.selectbox('모델 종류', ['llm', 'embedding', 'reranker', 'ocr'], format_func=labels.KINDS.get)
                version = st.text_input('등록할 버전 이름', value='base-v1', max_chars=128)
                revision = st.text_input('가져올 원본 버전 (revision)', value='main')
                description = st.text_area('설명', max_chars=2000)
            st.caption('추가하면 원본을 가져오기 시작합니다. 파일이 준비되면 학습하거나 배포할 수 있습니다.')
            sent = st.form_submit_button('모델 추가하기', type='primary')
        if sent:
            if not name.strip() or not repo.strip() or not version.strip():
                st.error('등록 이름과 Hugging Face 저장소, 버전 이름을 입력하세요.')
            else:
                ui.submit('add_model', body={
                    'registration': {'name': name.strip(), 'model_type': kind, 'description': description.strip()},
                    'version': {'version': version.strip(), 'repo_id': repo.strip(), 'revision': revision.strip() or 'main', 'hf_token': token.strip()}})
        if st.button('닫기', key='close_add_model'):
            dismiss_add_dialog()
            st.rerun()


def add_dataset_form():
    with st.container(border=True):
        st.subheader('새 데이터셋 추가')
        st.caption('대화 예시가 담긴 JSONL 파일을 선택하세요. 파일 형식은 아래 예제를 참고할 수 있습니다.')
        st.download_button('예제 JSONL 다운로드', SAMPLE, 'chat-example.jsonl', 'application/jsonl')
        with st.form('add_dataset_form'):
            name = st.text_input('데이터셋 이름', max_chars=200)
            uploaded = st.file_uploader('학습 대화 파일 (JSONL)', type=['jsonl'], max_upload_size=64, key='add_dataset_file')
            with st.expander('버전·설명 추가 옵션'):
                version = st.text_input('데이터셋 버전 이름', value='v1', max_chars=128)
                description = st.text_area('데이터셋 설명', max_chars=2000)
            sent = st.form_submit_button('데이터셋 추가하기', type='primary')
        if sent:
            if not name.strip() or not version.strip() or uploaded is None:
                st.error('데이터셋 이름과 파일, 버전 이름을 입력하세요.')
            else:
                data = uploaded.getvalue()
                if not data or len(data) > 64 * 1024 * 1024:
                    st.error('1바이트 이상, 64 MiB 이하 파일을 선택하세요.')
                else:
                    ui.submit('add_dataset', body={
                        'registration': {'name': name.strip(), 'description': description.strip()},
                        'version': {'version': version.strip()}}, data=data)
        if st.button('닫기', key='close_add_dataset'):
            dismiss_add_dialog()
            st.rerun()


def open_model_action(model_id, version_id, action):
    # Refresh the exact selected version before opening its execution form.
    model = ui.query('/api/models/' + identifier(model_id))
    version = ui.query('/api/model-versions/' + identifier(version_id))
    if model.get('deleted_at') or version.get('model_id') != model_id or version.get('state') != 'ready':
        raise APIError('선택한 모델의 최신 상태를 확인하고 다시 조회하세요.', code='FailedPrecondition')
    if not (can_train_model(model, version) if action == '학습' else can_deploy_model(model, version)) or version.get('artifact_kind') not in (('snapshot',) if action == '학습' else ('snapshot', 'adapter')):
        raise APIError('이 버전에서는 선택한 작업을 실행할 수 없습니다.', code='FailedPrecondition')
    st.session_state.model_action = action
    ui.state()['row_action_source'] = model_id
    ui.go('models', model=model_id, version=version_id)


def remove_model(model_id, permanent=False):
    client = ui.client()
    if permanent and ui.state().get('delete_model_gateway') != client.base:
        raise APIError('서버 연결이 바뀌었습니다. 목록에서 다시 선택해주세요.', code='SERVER_CHANGED')
    result = client.call('POST', '/api/models/' + identifier(model_id) + '/purge', timeout=100) if permanent else client.call('DELETE', '/api/models/' + identifier(model_id))
    if permanent and (result.get('purge_state') != 'purged' or not result.get('purged_at')):
        raise APIError('파일 삭제 완료를 확인하지 못했습니다. 같은 항목에서 다시 확인하세요.', code='INVALID_RESPONSE')
    if result.get('model_id') != model_id or not result.get('deleted_at'):
        raise APIError('삭제 결과를 확인하지 못했습니다. 같은 항목에서 다시 확인하세요.', code='INVALID_RESPONSE')
    for key in list(ui.state()['lists']):
        if key == 'models':
            entry = ui.state()['lists'][key]
            entry['page']['items'] = [r for r in entry['page']['items'] if r['model_id'] != model_id]
        elif key == 'versions_' + model_id:
            del ui.state()['lists'][key]
    ui.state()['display_cache'] = {}
    ui.state().pop('delete_model', None)
    if ui.state()['ids'].get('model') == model_id:
        ui.state()['ids'].pop('model', None)
        ui.state()['ids'].pop('version', None)
    ui.state()['notice'] = '모델 파일과 해당 배포 캐시를 영구 삭제했습니다.' if permanent else '모델을 목록에서 삭제했습니다. 기존 배포는 아래 목록에서 종료할 수 있습니다.'
    ui.sync_url()
    st.rerun()


def delete_confirmation(row):
    model_id = row['model_id']
    st.text(row['name'])
    permanent = st.selectbox('삭제 방식', ['목록에서만 삭제', '파일까지 영구 삭제'], key='model_delete_mode_' + model_id) == '파일까지 영구 삭제'
    confirmed = True
    if permanent:
        st.warning('이 모델의 모든 버전 파일과 배포 캐시를 실제로 지웁니다. 복구할 수 없습니다.')
        st.caption('배포를 모두 중지하고 가져오기·학습·결과 복구가 끝난 뒤 삭제할 수 있습니다. 학습 데이터와 별도 보관된 체크포인트·학습 작업 파일은 이 삭제 대상에 포함되지 않습니다.')
        confirmed = st.checkbox('이 모델의 모든 버전 파일을 영구 삭제하는 데 동의합니다.', key='model_purge_confirm_' + model_id)
    else:
        st.warning('이 모델의 모든 버전을 관리 목록에서 삭제할까요?')
        st.caption('저장 파일과 학습 이력은 보존됩니다. 실행 중인 학습·배포는 계속되며, 배포 종료는 아래 목록에서 할 수 있습니다.')
    yes, no = st.columns(2)
    if yes.button('파일 영구 삭제' if permanent else '목록에서 삭제', key='confirm_delete_' + model_id, disabled=not confirmed):
        try:
            remove_model(model_id, permanent=permanent)
        except APIError as exc:
            ui.error(exc)
            if permanent and exc.code == 'FailedPrecondition':
                st.info('배포를 모두 중지하고 모델 가져오기·학습·결과 복구가 끝났는지 확인해주세요.')
            st.info('아직 삭제 완료를 확인하지 못했습니다. 같은 삭제 버튼으로 다시 확인할 수 있습니다.')
    if no.button('취소', key='cancel_delete_' + model_id):
        ui.state().pop('delete_model', None)
        st.rerun()


def row_action_panel(model_id):
    action = st.session_state.get('model_action', '상세')
    if action not in ('배포', '학습'):
        model_detail()
        return
    version_id = ui.state()['ids'].get('version')
    model = ui.query('/api/models/' + model_id)
    version = ui.query('/api/model-versions/' + identifier(version_id))
    if model.get('deleted_at') or version.get('model_id') != model_id:
        st.info('목록에서 삭제됐거나 선택한 모델과 연결되지 않은 버전입니다.')
        return
    st.caption(row_action_title(model, version, action))
    if st.button('설정 닫기', key='close_model_action'):
        ui.go('models', model=None, version=None)
    if not (can_train_model(model, version) if action == '학습' else can_deploy_model(model, version)) or version.get('state') != 'ready':
        st.info('이 버전은 아직 실행할 수 없습니다. 모델 목록을 다시 조회하세요.')
    elif action == '학습' and version.get('artifact_kind') == 'snapshot':
        training_form(version_id)
    elif action == '배포' and version.get('artifact_kind') in ('snapshot', 'adapter'):
        deployment_form(version_id, embedding_model(model, version), reranker_model(model, version))
    else:
        st.info('선택한 버전에서는 이 작업을 실행할 수 없습니다.')


def row_action_title(model, version, action):
    return model['name'] + ' · ' + labels.version_label(version) + ' · ' + action


def open_rename(model_id, version_id):
    client = ui.client()
    model = client.call('GET', '/api/models/' + identifier(model_id))
    version = client.call('GET', '/api/model-versions/' + identifier(version_id)) if version_id else None
    if model.get('deleted_at') or model.get('purge_state') or (version and version.get('model_id') != model_id):
        raise APIError('모델의 최신 상태를 확인하세요.', code='FailedPrecondition')
    ui.state()['rename_model'] = {'model': model, 'version': version, 'gateway': client.base}
    # Fresh editor values, with a frozen version UUID even if the row selector changes.
    st.session_state['rename_name_' + model_id] = model['name']
    if version:
        st.session_state['rename_version_' + version['model_version_id']] = version['version']


def save_label(editor, is_version, value):
    client = ui.client()
    if editor['gateway'] != client.base:
        raise APIError('서버 연결이 바뀌었습니다.', code='SERVER_CHANGED')
    obj = editor['version'] if is_version else editor['model']
    field, id_field, collection = ('version', 'model_version_id', 'model-versions') if is_version else ('name', 'model_id', 'models')
    value = value.strip()
    if not value:
        raise APIError('이름을 입력하세요.', code='InvalidArgument')
    result = client.call('PATCH', '/api/' + collection + '/' + identifier(obj[id_field]),
                         {field: value, 'expected_' + field: obj[field]})
    if result.get(id_field) != obj[id_field] or result.get(field) != value:
        raise APIError('이름 변경 결과를 확인하지 못했습니다.', code='INVALID_RESPONSE')
    obj[field] = value
    for entry in ui.state()['lists'].values():
        for row in entry.get('page', {}).get('items', []):
            if row.get(id_field) == obj[id_field]:
                row[field] = value
    ui.state()['display_cache'] = {}
    ui.state()['notice'] = ('버전 이름' if is_version else '등록 이름') + '을 변경했습니다.'
    st.rerun()


def rename_panel(editor):
    model, version = editor['model'], editor['version']
    st.caption('등록 이름과 버전 이름만 변경합니다. 원본 저장소와 모델 파일은 그대로 사용합니다.')
    with st.form('rename_model_form_' + model['model_id']):
        name = st.text_input('등록 이름', key='rename_name_' + model['model_id'], max_chars=200)
        sent = st.form_submit_button('등록 이름 저장')
    if sent:
        try: save_label(editor, False, name)
        except APIError as exc: ui.error(exc)
    if version:
        st.caption('수정할 버전: ' + version['version'] + ' · ' + version['model_version_id'][:8])
        with st.form('rename_version_form_' + version['model_version_id']):
            label = st.text_input('버전 이름', key='rename_version_' + version['model_version_id'], max_chars=128)
            sent = st.form_submit_button('버전 이름 저장')
        if sent:
            try: save_label(editor, True, label)
            except APIError as exc: ui.error(exc)
    if st.button('이름 수정 닫기', key='close_rename_' + model['model_id']):
        ui.state().pop('rename_model', None)
        st.rerun()


@st.fragment(run_every=3)
def import_row_status(snapshot):
    # Poll the selected version, not a job inferred from a name or list order.
    try:
        version = ui.query('/api/model-versions/' + identifier(snapshot['model_version_id']))
        if version.get('model_version_id') != snapshot['model_version_id']:
            raise APIError('버전 응답이 다릅니다.', code='INVALID_RESPONSE')
    except APIError:
        table_style.status_badge(snapshot.get('state'))
        st.caption('진행률 조회 지연')
        return
    table_style.status_badge(version.get('state'))
    if version.get('state') != 'importing':
        ui.state()['display_cache'] = {}
        if not (ui.state().get('rename_model') or ui.state().get('show_add') or ui.state().get('detail_dialog') or ui.state().get('row_action_source')):
            st.rerun()
        return
    if version.get('operation_id'):
        try:
            op = ui.query('/api/operations/' + identifier(version['operation_id']))
            if op.get('operation_id') != version['operation_id']:
                raise APIError('작업 응답이 다릅니다.', code='INVALID_RESPONSE')
            st.caption(compact_progress(op))
        except APIError:
            st.caption('진행률 조회 지연')
    else:
        st.caption('작업 등록 중')


def model_row(row):
    model_id = identifier(row['model_id'])
    page = ui._display_read('row_versions:' + model_id,
                            lambda: ui.client().listing('models/' + model_id + '/versions', limit=100))
    versions = {r['model_version_id']: r for r in (page or {}).get('items', []) if r.get('model_id') == model_id}
    with st.container(key='model_table_row_' + model_id):
        title, origin, selection, status, actions = st.columns(table_style.MODEL_COLUMNS, vertical_alignment='center')
        with title, st.container(key='model_name_' + model_id):
            if st.button(row['name'], key='model_' + model_id, use_container_width=True):
                st.session_state.model_action = '상세'
                ui.state().pop('row_action_source', None)
                ui.go('models', model=model_id, version=None)
        with selection:
            selected = st.selectbox('사용할 버전', list(versions), format_func=lambda k: labels.version_label(versions[k]),
                                    key='row_version_' + model_id, disabled=not versions, placeholder='등록된 버전 없음',
                                    label_visibility='collapsed')
        if (ui.state().get('row_action_source') == model_id and ui.state()['ids'].get('model') == model_id
                and ui.state()['ids'].get('version') != selected):
            ui.state().pop('row_action_source', None)
            ui.go('models', model=None, version=None)
        current = versions.get(selected, {})
        with origin:
            if selected:
                info = ui.identity(selected, model=row)
                source = info['origin']
                if source.get('repo_id'):
                    st.text(source['repo_id'])
                    st.caption(table_style.revision_summary(source))
                if source.get('problem'):
                    st.caption(source['problem'])
            else:
                st.caption('버전을 조회하지 못했습니다.' if page is None else '원본 버전 없음')
        with status:
            if current.get('state') == 'importing':
                import_row_status(current)
            else:
                table_style.status_badge(current.get('state'))
        if title.button('이름 수정', key='row_rename_' + model_id, disabled=bool(row.get('deleted_at') or row.get('purge_state'))):
            try: open_rename(model_id, selected)
            except APIError as exc: ui.error(exc)
        usable = not row.get('deleted_at') and not row.get('purge_state') and current.get('state') == 'ready'
        with actions, st.container(key='table_actions_' + model_id):
            deploy, train, delete = st.columns(3, gap='small')
            if deploy.button('배포', key='row_deploy_' + model_id, disabled=not (usable and can_deploy_model(row, current) and current.get('artifact_kind') in ('snapshot', 'adapter')), use_container_width=True):
                open_model_action(model_id, selected, '배포')
            if train.button('학습', key='row_train_' + model_id, disabled=not (usable and can_train_model(row, current) and current.get('artifact_kind') == 'snapshot'), use_container_width=True):
                open_model_action(model_id, selected, '학습')
            if delete.button('삭제', key='row_delete_' + model_id, use_container_width=True):
                ui.state()['delete_model'] = model_id
                ui.state()['delete_model_gateway'] = ui.client().base
                st.session_state.pop('model_purge_confirm_' + model_id, None)
        if (page or {}).get('next_cursor'):
            st.caption('최근 100개 버전입니다. 이전 버전은 모델 이름을 눌러 조회하세요.')
        if reranker_model(row, current):
            st.caption('검색 결과 재정렬 · 배포 후 질문과 후보의 점수를 확인할 수 있습니다.')
        elif embedding_model(row, current):
            st.caption('문서 검색용 임베딩 · 배포 후 임베딩 API로 사용할 수 있습니다.')
        elif row.get('model_type') != 'llm':
            st.caption(labels.KINDS.get(row.get('model_type'), '모델') + ' · 현재 엔진의 학습·배포 대상이 아닙니다.')
        editor = ui.state().get('rename_model')
        if editor and editor['model']['model_id'] == model_id:
            with st.container(key='detail_panel_rename_' + model_id):
                rename_panel(editor)
        if ui.state().get('delete_model') == model_id:
            with st.container(key='detail_panel_delete_' + model_id):
                delete_confirmation(row)
        if ui.state()['ids'].get('model') == model_id:
            with st.container(key='detail_panel_model_' + model_id):
                row_action_panel(model_id)


def models():
    st.caption('모델을 선택하고 배포하거나 학습해 보세요.')
    with st.container(key='model_toolbar'):
        query, search_col, filter_col, spacer, add = st.columns([1.2, 3.8, .9, 1.7, 1.25], vertical_alignment='center')
        query_clicked = query.button('모델 조회', key='query_models', type='primary', use_container_width=True)
        search = search_col.text_input('모델 검색', placeholder='모델 이름 검색', label_visibility='collapsed')
        with filter_col.popover('필터', use_container_width=True):
            include_deleted = st.checkbox('목록에서 삭제한 항목 포함', key='model_include_deleted')
            kind_filter = st.selectbox('종류 필터', ['전체', 'llm', 'embedding', 'reranker', 'ocr'], format_func=lambda k: labels.KINDS.get(k, k))
        if add.button('모델 추가', key='open_add_model', icon=':material/add:', use_container_width=True):
            ui.close_detail()
            ui.state()['show_add'] = 'model'
            st.rerun()
    rows = ui.listing('models', 'models', '모델 조회', {'q': search.strip(), 'model_type': '' if kind_filter == '전체' else kind_filter, **({'include_deleted': 'true'} if include_deleted else {})},
                      trigger=query_clicked, paginate=False)
    if 'models' in ui.state()['lists']:
        with st.container(key='model_table'):
            table_style.model_heading()
            for row in rows:
                model_row(row)
            ui.list_footer('models')
    model_id = ui.state()['ids'].get('model')
    if model_id and not any(r['model_id'] == model_id for r in rows):
        with st.container(key='detail_panel_linked'):
            row_action_panel(model_id)


def model_detail():
    model_id = ui.state()['ids'].get('model')
    if not model_id:
        return
    model = ui.query('/api/models/' + model_id)
    st.divider()
    st.subheader('선택한 모델 · ' + model['name'])
    st.caption(model.get('description', ''))
    if st.button('상세 닫기'):
        ui.go('models', model=None, version=None)
    ui.details('모델 상세', model)
    if model.get('purge_state'):
        st.info('모델 파일을 영구 삭제했습니다.' if model.get('purged_at') else '파일 삭제가 완료되지 않았습니다. 같은 모델에서 다시 시도해주세요.')
        return
    if model.get('deleted_at'):
        st.info('목록에서 삭제된 모델입니다. 기존 작업과 원본 기록은 보존됩니다.')
        return
    with st.expander('이 모델에 다른 버전 추가'):
        with st.form('import_model_' + model_id):
            label = st.text_input('등록할 버전 이름', value='base-v1', max_chars=128, help='플랫폼 안에서 구분할 이름입니다. 아래 원본 버전과 별개입니다.')
            repo = st.text_input('Hugging Face 저장소', placeholder='google/gemma-3-1b-it')
            revision = st.text_input('가져올 원본 버전 (revision)', value='main', help='Hugging Face의 브랜치·태그·커밋을 입력합니다. 가져오면 실제 받은 확정 버전도 표시됩니다.')
            token = st.text_input('HF 토큰 (필요한 경우)', type='password', key='hf_token')
            imported = st.form_submit_button('모델 버전 추가하기')
        if imported:
            if not label.strip() or not repo.strip():
                st.error('버전 이름과 저장소를 입력하세요.')
            else:
                ui.submit('import', model_id, {'version': label.strip(), 'repo_id': repo.strip(), 'revision': revision.strip() or 'main', 'hf_token': token.strip()})
    st.caption('이 모델에 등록된 버전마다 원본이나 학습 결과가 다를 수 있습니다. 사용할 버전을 골라 주세요.')
    versions = ui.listing('versions_' + model_id, 'models/' + model_id + '/versions', '버전 조회')
    for row in versions:
        with st.container(border=True):
            if st.button(labels.version_label(row), key='version_' + row['model_version_id'], use_container_width=True):
                ui.select('version', row['model_version_id'])
                st.session_state.model_action = '상세'
                st.rerun()
            st.caption(labels.state_label(row.get('state')) + ' · ' + labels.artifact_label(row))
            ui.origin_summary(ui.identity(row['model_version_id'], model=model))
    version_id = ui.state()['ids'].get('version')
    if not version_id:
        return
    version = ui.query('/api/model-versions/' + version_id)
    if version.get('model_id') != model_id:
        st.warning('현재 모델의 버전을 다시 선택하세요.')
        return
    ui.model_identity(ui.identity(version_id, version, model), '선택한 버전 · ' + labels.version_label(version))
    version_status(version_id)
    ui.details('버전 상세', version)
    llm = can_train_model(model, version)
    deployable = can_deploy_model(model, version)
    ready = version.get('state') == 'ready'
    a, b = st.columns(2)
    if a.button('학습 설정', disabled=not (llm and ready and version.get('artifact_kind') == 'snapshot'), use_container_width=True):
        st.session_state.model_action = '학습'
    a.caption('내 데이터를 사용해 새로운 학습 결과를 만듭니다.')
    if b.button('배포 설정', disabled=not (deployable and ready and version.get('artifact_kind') in ('snapshot', 'adapter')), use_container_width=True):
        st.session_state.model_action = '배포'
    b.caption('선택한 버전의 추론 API를 사용할 수 있게 준비합니다.')
    if not ready:
        st.info('모델 상태가 사용 가능으로 바뀌면 학습·배포를 선택할 수 있습니다.')
    elif llm and version.get('artifact_kind') == 'adapter':
        st.info('이미 학습한 결과입니다. 배포 설정을 눌러 대화해 보세요. 새 학습은 원본 모델 버전에서 시작합니다.')
    if embedding_model(model, version):
        st.info('지원하는 임베딩 모델입니다. 배포 후 문서·질문을 벡터로 변환할 수 있습니다.')
    elif reranker_model(model, version):
        st.info('지원하는 재정렬 모델입니다. 배포 후 질문과 후보의 관련성 점수를 확인할 수 있습니다.')
    elif not llm:
        st.info('현재 학습·배포 엔진은 LLM용입니다. 이 모델 종류는 등록·조회·가져오기를 사용할 수 있습니다.')
    action = st.session_state.get('model_action')
    if action == '학습' and llm and ready and version.get('artifact_kind') == 'snapshot':
        training_form(version_id)
    elif action == '배포' and deployable and ready and version.get('artifact_kind') in ('snapshot', 'adapter'):
        deployment_form(version_id, embedding_model(model, version), reranker_model(model, version))


def dataset_picker(prefix, label):
    st.markdown('**' + label + '**')
    q = st.text_input('데이터셋 이름 검색', key=prefix + '_search')
    rows = ui.listing(prefix + '_datasets', 'datasets', '데이터셋 목록 조회', {'q': q.strip()},
                      items='datasets', next_key='next_after_id', cursor_key='after_id')
    if not rows:
        return None
    by_id = {r['dataset_id']: r for r in rows}
    selected = st.selectbox('데이터셋 선택', list(by_id), format_func=lambda k: by_id[k]['name'], key=prefix + '_dataset')
    versions = ui.listing(prefix + '_versions_' + selected, 'datasets/' + selected + '/versions', '데이터셋 버전 조회',
                          items='versions', next_key='next_after_id', cursor_key='after_id')
    ready = {r['dataset_version_id']: r for r in versions if r.get('state') == 'ready'}
    if not ready:
        return None
    version = st.selectbox('데이터셋 버전 선택', list(ready), format_func=lambda k: ready[k]['version'] + ' · ' + str(ready[k]['record_count']) + '개', key=prefix + '_version_' + selected)
    st.caption('선택한 버전의 데이터를 그대로 사용합니다. 업로드한 뒤 내용이 바뀌지 않습니다.')
    return ready[version]


def training_form(version_id):
    st.subheader('학습 실행')
    source = st.radio('학습 데이터', ['등록된 데이터셋', '내장 테스트 데이터'], key='training_source')
    st.caption('처음 실행을 확인하려면 내장 테스트 데이터를 선택하세요. 내 데이터를 학습하려면 데이터셋 화면에서 먼저 파일을 등록하세요.')
    dataset = dataset_picker('train', '학습용 데이터셋') if source == '등록된 데이터셋' else None
    evaluate = st.checkbox('별도 데이터셋으로 평가', disabled=source != '등록된 데이터셋')
    evaluation = dataset_picker('eval', '평가용 데이터셋') if evaluate and source == '등록된 데이터셋' else None
    with st.form('training_form_' + version_id):
        steps = st.number_input('학습 단계 수', min_value=1, max_value=100000, value=1, step=1, help='학습을 몇 번 진행할지 정합니다. 기본 1단계는 실행 확인용이며 학습 품질을 보장하지 않습니다.')
        with st.expander('고급 학습 설정 · 필요할 때만 변경'):
            st.caption('기본값으로 실행할 수 있습니다. 고급 설정을 접어도 변경한 값은 유지됩니다.')
            sequence = st.selectbox('입력 길이', [256, 512, 1024, 2048, 4096], index=0)
            gpu = gpu_input('training_gpu')
            batch = st.number_input('배치 크기', min_value=1, max_value=16, value=1, step=1)
            accumulation = st.number_input('누적 단계', min_value=1, max_value=128, value=1, step=1)
            lr = st.number_input('학습률', min_value=0.000001, max_value=0.01, value=0.0002, format='%.6f', help='한 번에 모델을 얼마나 바꿀지 정하는 값입니다. 처음에는 기본값을 사용하세요.')
            rank = st.selectbox('LoRA rank', [8, 16, 32, 64], help='추가로 학습할 가중치의 크기입니다. 처음에는 기본값 8을 사용하세요.')
            alpha = st.number_input('LoRA alpha', min_value=1, max_value=256, value=16, step=1)
            checkpoint = st.number_input('체크포인트 저장 간격 (0: 끔)', min_value=0, max_value=100000, value=0, step=1)
        st.caption('실행하면 GPU와 저장 공간을 사용합니다. 시작 후 학습 작업 화면에서 진행 상태와 결과를 확인할 수 있습니다.')
        start = st.form_submit_button('학습 시작', type='primary')
    if start:
        if source == '등록된 데이터셋' and dataset is None:
            st.error('학습 데이터셋 버전을 선택하세요.'); return
        if evaluate and source == '등록된 데이터셋' and (evaluation is None or evaluation['sha256'] == dataset['sha256']):
            st.error('학습 데이터와 내용이 다른 평가 데이터셋 버전을 선택하세요.'); return
        if checkpoint and checkpoint > steps:
            st.error('저장 간격은 학습 단계 수 이하여야 합니다.'); return
        spec = lambda d: {'source_type': 'version', 'format': 'chat_messages', 'dataset_version_id': identifier(d['dataset_version_id'])}
        body = {'source_model_version_id': version_id, 'dataset': spec(dataset) if dataset else {'source_type': 'synthetic', 'format': 'chat_messages'},
                'training': {'method': 'qlora', 'sequence_length': sequence, 'max_steps': steps, 'micro_batch_size': batch,
                             'gradient_accumulation_steps': accumulation, 'learning_rate': lr, 'lora': {'r': rank, 'alpha': alpha, 'dropout': 0.0},
                             'sample_packing': False, 'checkpoint_steps': checkpoint}, 'resources': {'gpu_ids': [gpu]}}
        if evaluate and evaluation:
            body['evaluation'] = {'dataset': spec(evaluation)}
        ui.submit('train', version_id, body)


def deployment_form(version_id, embedding=False, reranker=False):
    st.subheader('배포 실행')
    st.caption('질문과 후보의 관련성 점수를 계산할 모델을 준비합니다.' if reranker else '문서·질문을 검색용 벡터로 변환할 모델을 준비합니다.' if embedding else '선택한 모델 버전을 대화할 수 있는 상태로 준비합니다. 준비가 끝나면 채팅 페이지로 이동하세요.')
    with st.form('deployment_form_' + version_id):
        with st.expander('고급 배포 설정 · 필요할 때만 변경'):
            gpu = gpu_input('deployment_gpu')
            length = st.selectbox('질문+후보 최대 길이' if reranker else '최대 입력·응답 길이', [512] if reranker else [512, 1024, 2048, 4096], index=0 if reranker else 2)
            memory = st.slider('GPU 메모리 사용 비율', 0.1, 0.9, 0.5, 0.05)
        st.caption('같은 GPU에 여러 모델을 배포할 수 있습니다. 메모리 사용 비율은 GPU 전체 용량 기준이며, 남은 메모리에 맞게 낮춰주세요. 모델 로딩에 실패하면 해당 배포만 실패 처리합니다.')
        start = st.form_submit_button('배포 시작', type='primary')
    if start:
        ui.submit('deploy', version_id, {'model_version_id': version_id,
                  'serving': {'engine': 'vllm', **({'profile': RERANKER_PROFILE} if reranker else {'profile': EMBEDDING_PROFILE} if embedding else {}), 'max_model_len': length, 'gpu_memory_utilization': memory}, 'resources': {'gpu_ids': [gpu]}})


def datasets():
    st.caption('데이터셋 추가에서 이름과 파일을 함께 입력하세요. 추가한 데이터는 학습 설정에서 선택할 수 있습니다.')
    if st.button('데이터셋 추가', key='open_add_dataset', type='primary'):
        ui.close_detail()
        ui.state()['show_add'] = 'dataset'
    q = st.text_input('데이터셋 검색')
    rows = ui.listing('dataset_catalog', 'datasets', '데이터셋 조회', {'q': q.strip()}, items='datasets', next_key='next_after_id', cursor_key='after_id')
    for row in rows:
        if st.button(row['name'], key='ds_' + row['dataset_id']):
            ui.go('datasets', dataset=row['dataset_id'])


def dataset_detail(dataset_id):
    if not ui.detail_is_open('dataset', dataset_id):
        return
    dataset = ui.query('/api/datasets/' + dataset_id)
    st.subheader(dataset['name'])
    ui.details('데이터셋 상세', dataset)
    versions = ui.listing('dataset_versions_' + dataset_id, 'datasets/' + dataset_id + '/versions', '버전 목록 조회',
                          items='versions', next_key='next_after_id', cursor_key='after_id')
    if versions:
        st.dataframe([{'버전': v['version'], '상태': labels.state_label(v['state']), '대화 예시 수': v['record_count'], '파일 크기 (KB)': round(v['size_bytes'] / 1024, 1)} for v in versions], hide_index=True)
        ui.details('버전 상세', versions)
    with st.expander('이 데이터셋에 새 파일 버전 추가'):
        st.download_button('예제 JSONL 다운로드', SAMPLE, 'chat-example.jsonl', 'application/jsonl')
        with st.form('upload_' + dataset_id):
            version = st.text_input('데이터셋 버전 이름', value='v1', max_chars=128)
            uploaded = st.file_uploader('JSONL 파일', type=['jsonl'], max_upload_size=64)
            sent = st.form_submit_button('데이터 버전 추가하기')
        if sent:
            if uploaded is None or not version.strip():
                st.error('버전 이름과 파일을 선택하세요.')
            else:
                data = uploaded.getvalue()
                if not data or len(data) > 64 * 1024 * 1024:
                    st.error('1바이트 이상, 64 MiB 이하 파일을 선택하세요.'); return
                ui.submit('upload', dataset_id, {'version': version.strip()}, data)


def dismiss_add_dialog():
    ui.state()['show_add'] = None
    st.session_state.clear_hf_token = True


@st.dialog('모델 추가', width='medium', on_dismiss=dismiss_add_dialog)
def model_add_dialog():
    try:
        add_model_form()
    except APIError as exc:
        ui.error(exc)


@st.dialog('데이터셋 추가', width='medium', on_dismiss=dismiss_add_dialog)
def dataset_add_dialog():
    try:
        add_dataset_form()
    except APIError as exc:
        ui.error(exc)


def add_dialog_if_open():
    if ui.state().get('show_add') == 'model':
        model_add_dialog()
    elif ui.state().get('show_add') == 'dataset':
        dataset_add_dialog()


@st.fragment(run_every=3)
def version_status(version_id):
    try:
        version = ui.query('/api/model-versions/' + identifier(version_id))
        current = version.get('state')
        observed = ui.state().setdefault('observed_versions', {})
        previous = observed.get(version_id)
        observed[version_id] = current
        if previous == 'importing' and current in ('ready', 'failed'):
            ui.state()['display_cache'] = {}
            # Let an open addition form finish without a full-page rerun.
            if not ui.state().get('show_add') and not ui.state().get('detail_dialog'):
                st.rerun()
        st.caption('상태: ' + labels.state_label(current))
        st.caption('작업 상태는 3초마다 갱신됩니다.')
        ui.operation(version)
        if st.button('버전 상태 새로고침'):
            ui.state()['display_cache'] = {}
            st.rerun()
    except APIError as exc:
        ui.error(exc)
