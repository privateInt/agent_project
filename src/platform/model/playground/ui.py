"""Shared console navigation, explicit queries and idempotent form submissions."""
import streamlit as st
import api
import actions
import time
import presentation as labels
from state import identifier, decode_intent, deployment_body, error_text

PAGES = {'models': '관리', 'chat': '채팅'}
MANAGEMENT_SECTIONS = {'models', 'datasets', 'training', 'deployments'}
ID_KEYS = ('model', 'version', 'dataset', 'run', 'deployment')


def client():
    return api.API()


def init():
    if 'console' not in st.session_state:
        state = {'page': 'models', 'ids': {}, 'lists': {}, 'pending': None, 'pending_payload': None,
                 'notice': '', 'messages': [], 'pending_prompt': None, 'chat_id': None, 'results': {}}
        try:
            state['page'] = st.query_params.get('page', 'chat' if st.query_params.get('deployment') else 'models')
            if state['page'] in MANAGEMENT_SECTIONS:
                state['section'] = state['page']
                state['page'] = 'models'
            if state['page'] not in PAGES:
                raise ValueError('unknown page')
            for key in ID_KEYS:
                if st.query_params.get(key):
                    state['ids'][key] = identifier(st.query_params[key])
            if st.query_params.get('pending'):
                state['pending'] = actions.decode(st.query_params['pending'])
                state['show_add'] = {'add_model': 'model', 'add_dataset': 'dataset'}.get(state['pending']['kind'])
            elif st.query_params.get('request'):
                # Keep old v1 deployment URLs usable without issuing a new request.
                legacy = decode_intent(st.query_params['request'])
                value = actions.payload('deploy', legacy['model_version_id'], deployment_body(legacy))
                state['pending'] = actions.ticket(value, legacy['key'])
                state['pending_payload'] = value
                state['page'] = 'models'
                state['section'] = 'deployments'
            st.session_state.console = state
            sync_url()
        except (ValueError, TypeError, KeyError, AttributeError):
            st.error('주소의 실행 정보를 읽을 수 없습니다. 주소의 ? 뒤를 지우고 다시 열어 주세요.')
            st.stop()
    if st.session_state.pop('clear_hf_token', False):
        st.session_state.pop('hf_token', None)
        st.session_state.pop('add_hf_token', None)
    if st.session_state.console['page'] in MANAGEMENT_SECTIONS:
        st.session_state.console['page'] = 'models'
    if st.session_state.get('console_nav') in MANAGEMENT_SECTIONS:
        st.session_state.console_nav = 'models'
    if 'next_console_nav' in st.session_state:
        st.session_state.console_nav = st.session_state.pop('next_console_nav')


def state():
    return st.session_state.console


def sync_url():
    values = {'page': state()['page'], **state()['ids']}
    if state()['pending']:
        values['pending'] = actions.encode(state()['pending'])
    st.query_params.from_dict(values)


def select(key, value):
    state()['ids'][key] = identifier(value)
    sync_url()


def go(page, **ids):
    state()['detail_dialog'] = None
    detail_key = {'datasets': 'dataset', 'training': 'run', 'deployments': 'deployment'}.get(page)
    if detail_key and ids.get(detail_key):
        state()['show_add'] = None
        state()['detail_dialog'] = {'kind': detail_key, 'id': identifier(ids[detail_key])}
    if page in MANAGEMENT_SECTIONS:
        state()['section'] = page
        page = 'models'
    state()['page'] = page
    for key, value in ids.items():
        if value is None:
            state()['ids'].pop(key, None)
        else:
            state()['ids'][key] = identifier(value)
    st.session_state['next_console_nav'] = page
    sync_url()
    st.rerun()


def navigation():
    import table_style
    table_style.apply()
    with st.container(key='top_nav'):
        brand, menu = st.columns([1.65, 5.5], vertical_alignment='center')
        with brand:
            st.html('<div class="inventory-brand"><span></span>모델 플랫폼</div>')
        with menu:
            if 'console_nav' not in st.session_state:
                st.session_state.console_nav = state()['page']
            page = st.radio('화면', list(PAGES), format_func=PAGES.get, key='console_nav',
                            horizontal=True, label_visibility='collapsed')
            if page != state()['page']:
                state()['detail_dialog'] = None
                state()['show_add'] = None
                state()['page'] = page
                sync_url()
    st.title(PAGES[state()['page']])
    if state()['notice']:
        st.success(state()['notice'])
    pending_panel()


def error(exc):
    messages = {'Aborted': '다른 곳에서 이름이 변경됐습니다. 이름 수정 창을 닫고 다시 열어 최신 이름을 확인하세요.',
                'SERVER_CHANGED': '서버 연결이 바뀌었습니다. 목록에서 다시 선택하세요.',
                'PENDING_INPUT_CHANGED': str(exc), 'INVALID_UPLOAD': str(exc),
                'REDEPLOY_CHANGED': str(exc),
                'InvalidArgument': '입력값을 확인해 주세요. 선택한 모델·데이터셋과 설정을 확인한 뒤 다시 요청하세요.',
                'AlreadyExists': '이미 같은 이름이나 버전이 존재합니다. 목록에서 기존 결과를 확인하세요.',
                'FailedPrecondition': '현재 상태에서는 실행할 수 없습니다. 최신 상태와 선택한 버전을 확인해 주세요.'}
    st.error(messages.get(exc.code, error_text(exc)))
    st.caption('오류 코드: ' + (str(exc.code) or str(exc.status) or 'UNKNOWN'))


def query(path, **filters):
    if filters:
        return client().listing(path.removeprefix('/api/'), **filters)
    return client().call('GET', path)


def details(title, value):
    with st.expander(title):
        st.json(value)


def _display_read(key, loader):
    # Session-only, bounded cache for labels. Runtime status always uses fresh GET.
    cache = state().setdefault('display_cache', {})
    now = time.monotonic()
    if key not in cache or now - cache[key][0] >= 60:
        try:
            value = loader()
        except api.APIError:
            value = None
        if len(cache) >= 256:
            cache.pop(next(iter(cache)))
        cache[key] = (now, value)
    return cache[key][1]


def version_metadata(version_id):
    version_id = identifier(version_id)
    value = _display_read('v:' + version_id, lambda: query('/api/model-versions/' + version_id))
    if not value or value.get('model_version_id') != version_id:
        raise api.APIError('원본 정보를 조회하지 못했습니다.', code='INVALID_RESPONSE')
    return value


def model_preview(model_id):
    """A model can contain many origins; label this single row as latest version."""
    page = _display_read('latest:' + model_id, lambda: client().listing('models/' + model_id + '/versions', limit=1))
    if page is None:
        return None, '최근 버전을 조회하지 못했습니다. 모델을 선택해 다시 확인하세요.'
    rows = page.get('items', [])
    if not rows:
        return None, '아직 가져온 버전이 없습니다. 모델을 선택해 원본을 가져오세요.'
    row = rows[0]
    if row.get('model_id') != model_id:
        return None, '최근 버전의 연결 정보를 확인하지 못했습니다.'
    return identity(row['model_version_id']), ''


def identity(version_id, version=None, model=None):
    """Display metadata is never used to choose the ID sent to a mutation."""
    try:
        version = version or version_metadata(version_id)
        if model is None:
            model_id = identifier(version['model_id'])
            model = _display_read('m:' + model_id, lambda: query('/api/models/' + model_id))
            if model and model.get('model_id') != model_id:
                model = None
        return {'version': version, 'model': model or {}, 'origin': labels.origin(version, version_metadata)}
    except (api.APIError, ValueError, KeyError, TypeError):
        return {'version': {'model_version_id': version_id}, 'model': model or {},
                'origin': {'problem': '모델의 원본 정보를 조회하지 못했습니다. 다시 조회해 주세요.'}}


def identity_title(info):
    return info['origin'].get('repo_id') or info['model'].get('name') or '원본 확인 필요'


def origin_summary(info):
    origin = info['origin']
    if origin.get('repo_id'):
        st.text(('기반 Hugging Face 원본: ' if origin.get('derived') else 'Hugging Face 원본: ') + origin['repo_id'])
        st.caption(labels.revision_label(origin))
    if origin.get('problem'):
        st.caption(origin['problem'])


def model_identity(info, heading=None):
    if heading:
        st.subheader(heading)
    st.text('관리 이름: ' + (info['model'].get('name') or '조회하지 못함'))
    st.text('플랫폼 버전: ' + labels.version_label(info['version']) + ' · ' + labels.artifact_label(info['version']))
    origin_summary(info)
    if info['origin'].get('resolved_revision'):
        with st.expander('원본 버전 전체 정보'):
            st.text('가져올 때 지정한 버전: ' + (info['origin'].get('requested_revision') or '기록 없음'))
            st.text('실제 다운로드한 확정 버전: ' + info['origin']['resolved_revision'])
            st.text('플랫폼에 등록된 버전 이름: ' + (info['version'].get('version') or '조회하지 못함'))
            st.caption('main 같은 이름은 이후 바뀔 수 있습니다. 확정 버전은 당시 가져온 원본을 구분합니다.')


def fetch_list(key, collection, filters, cursor='', history=None, items='items', next_key='next_cursor', cursor_key='cursor'):
    chosen = dict(filters)
    page = client().listing(collection, **chosen, limit=10, **{cursor_key: cursor})
    state()['lists'][key] = {'page': page, 'filters': chosen, 'cursor': cursor, 'history': history or [],
                            'collection': collection, 'items_key': items, 'next_key': next_key, 'cursor_key': cursor_key}
    state()['display_cache'] = {}


def list_footer(key):
    entry = state()['lists'].get(key)
    if not entry:
        return
    with st.container(key='table_footer_' + key):
        count, previous, number, following = st.columns([6, 1, .5, 1], vertical_alignment='center')
        count.caption('현재 페이지 ' + str(len(entry['page'].get(entry['items_key'], []))) + '개')
        number.caption(str(len(entry['history']) + 1))
        def load(cursor, history):
            fetch_list(key, entry['collection'], entry['filters'], cursor, history,
                       entry['items_key'], entry['next_key'], entry['cursor_key'])
            st.rerun()
        if previous.button('이전', key='prev_' + key, disabled=not entry['history'], use_container_width=True):
            load(entry['history'][-1], entry['history'][:-1])
        if following.button('다음', key='next_' + key, disabled=not entry['page'].get(entry['next_key']), use_container_width=True):
            load(entry['page'][entry['next_key']], entry['history'] + [entry['cursor']])


def listing(key, collection, label, filters=None, items='items', next_key='next_cursor', cursor_key='cursor', trigger=None, paginate=True):
    """Only explicit query/pagination clicks fetch a list; filter edits are inert."""
    if trigger is None:
        trigger = st.button(label, key='query_' + key, type='primary')
    if trigger:
        fetch_list(key, collection, filters or {}, items=items, next_key=next_key, cursor_key=cursor_key)
    if key not in state()['lists']:
        st.info('조회 버튼을 눌러 목록을 불러오세요.')
        return []
    entry = state()['lists'][key]
    for name, default in (('collection', collection), ('items_key', items), ('next_key', next_key), ('cursor_key', cursor_key)):
        entry.setdefault(name, default)
    if paginate:
        list_footer(key)
    rows = entry['page'].get(items, [])
    if not rows:
        st.info('조회 결과가 없습니다.')
    return rows


def operation(value):
    operation_id = value.get('operation_id')
    if operation_id:
        try:
            op = query('/api/operations/' + identifier(operation_id))
            st.caption(labels.PHASES.get(op.get('phase'), '진행 상태: ' + labels.state_label(op.get('status'))))
            from progress_view import operation_progress
            operation_progress(op)
            details('작업 상세', op)
        except api.APIError as exc:
            error(exc)


def submit(kind, target='', body=None, data=None):
    value = actions.payload(kind, target, body, data)
    if state()['pending'] is None:
        state()['pending'] = actions.ticket(value)
        state()['pending_gateway'] = client().base
        state().pop('addition_resource', None)
        sync_url()  # Ticket is available before a potentially ambiguous POST.
    actions.request_key(state()['pending'], value)
    state().setdefault('pending_gateway', client().base)
    state()['pending_payload'] = value
    perform(value)


def perform(value):
    if state().get('pending_gateway', client().base) != client().base:
        raise api.APIError('서버 연결이 바뀌었습니다.', code='SERVER_CHANGED')
    def registered(kind, resource_id):
        # Keep the confirmed resource visible even if the second HTTP call fails.
        # Never trust this URL ID to select the second call: execute replays the
        # original registration key and uses its confirmed response every time.
        state()['addition_resource'] = resource_id
        state()['ids'][kind] = resource_id
        if kind == 'model':
            state()['ids'].pop('version', None)
        sync_url()

    try:
        with st.spinner(actions.SPECS[value['kind']][0] + ' 요청 중…'):
            result = actions.execute(client(), state()['pending'], value, on_registered=registered)
    except api.APIError as exc:
        state()['pending_error'] = str(exc.code or exc.status)
        st.rerun()
    kind = value['kind']
    state()['pending'] = None
    state()['pending_payload'] = None
    state().pop('pending_gateway', None)
    state()['pending_error'] = ''
    state()['lists'].clear()
    state()['display_cache'] = {}
    state()['notice'] = actions.SPECS[kind][0] + ' 요청을 접수했습니다. 아래 상태를 확인하세요.'
    state()['last_result'] = result
    if kind in ('add_model', 'import', 'model'):
        st.session_state.model_action = '상세'
        state().pop('row_action_source', None)
    if kind in ('add_model', 'add_dataset'):
        state()['show_add'] = None
        state().pop('addition_resource', None)
    if kind == 'add_model':
        st.session_state.clear_hf_token = True
        state()['notice'] = '모델 추가 요청을 접수했습니다. 원본을 가져오는 동안 아래 상태를 확인하세요.'
        go('models', model=result['model_id'], version=result['model_version_id'])
    elif kind == 'add_dataset':
        state()['notice'] = '데이터셋과 첫 파일 버전을 추가했습니다. 학습 설정에서 선택할 수 있습니다.'
        go('datasets', dataset=result['dataset_id'])
    elif kind == 'import':
        st.session_state.clear_hf_token = True
        go('models', model=value['target'], version=result['model_version_id'])
    elif kind == 'model':
        go('models', model=result['model_id'], version=None)
    elif kind == 'dataset':
        go('datasets', dataset=result['dataset_id'])
    elif kind == 'upload':
        go('datasets', dataset=value['target'])
    elif kind in ('train', 'resume', 'retry_output'):
        go('training', run=result['training_run_id'])
    elif kind == 'deploy':
        source = state().pop('redeploy', None)
        if source and source['body'] == value['body']:
            state()['notice'] = '새 배포 요청을 접수했습니다. 기존 배포 ' + source['deployment_id'][:8] + '의 이력은 보존됩니다. 새 배포의 준비 상태를 확인하세요.'
        go('deployments', deployment=result['deployment_id'])


def pending_panel():
    saved = state()['pending']
    if not saved:
        return
    with st.container(border=True):
        st.warning(actions.SPECS[saved['kind']][0] + ': 요청 결과를 확인해야 합니다.')
        if state().get('addition_resource'):
            st.info('추가를 마치지 못했습니다. 이미 만든 항목에서 이어서 처리합니다.')
        if state().get('pending_error'):
            st.caption('최근 응답 코드: ' + state()['pending_error'])
        st.caption('새 요청을 추가하지 않고 같은 요청 키로 확인합니다. 페이지를 열거나 새로고침하는 것만으로 재전송하지 않습니다.')
        if state()['pending_payload']:
            label = '추가 이어서 진행' if saved['kind'] in ('add_model', 'add_dataset') else '같은 요청 다시 확인'
            if st.button(label, key='retry_action'):
                perform(state()['pending_payload'])
        else:
            st.info('새 세션에서는 원래 화면에서 같은 입력·파일·토큰으로 실행 버튼을 누르세요. 기존 요청 키를 재사용합니다.')
            if st.button('요청 화면으로 이동'):
                target_page = actions.SPECS[saved['kind']][1]
                state()['show_add'] = {'add_model': 'model', 'add_dataset': 'dataset'}.get(saved['kind'])
                if saved['kind'] in ('train', 'deploy', 'import'):
                    target_page = 'models'
                if saved['kind'] in ('resume', 'retry_output'):
                    go('training', run=saved['target'])
                elif saved['kind'] == 'upload':
                    go('datasets', dataset=saved['target'])
                else:
                    go(target_page)
        with st.expander('요청 기록 정리'):
            st.caption('결과를 확인하지 않고 기록을 닫은 뒤 새 요청을 보내면 중복 작업이 생길 수 있습니다.')
            checked = st.checkbox('관련 목록에서 기존 처리 결과를 확인했습니다.', key='checked_pending_' + saved['key'])
            if st.button('요청 기록 닫기', disabled=not checked):
                state()['pending'] = state()['pending_payload'] = None
                state().pop('pending_gateway', None)
                state().pop('addition_resource', None)
                st.session_state.clear_hf_token = True
                sync_url()
                st.rerun()


def direct_post(path):
    """Stop/cancel are idempotent by resource ID; never schedule a new Job."""
    result = client().call('POST', path)
    state()['notice'] = '요청을 접수했습니다. 완료 상태를 확인하세요.'
    return result


def close_detail():
    # Selection IDs can remain in links. Only an explicit click opens a dialog.
    state()['detail_dialog'] = None


def detail_is_open(kind, resource_id):
    return state()['page'] == 'models' and state().get('detail_dialog') == {'kind': kind, 'id': resource_id} and not state().get('show_add')
