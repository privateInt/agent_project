"""Console navigation and explicit user requests, including safe request replay."""
import json
import os

import streamlit as st

from data_api import API, APIError, identifier
from data_requests import RequestStore, RequestStoreError
from data_state import (
    ID_FIELD, PAGES, collection_body, friendly_error, match, pack,
    refinement_body, source_name, source_kind, ticket, unpack, ocr_deployment_ready, navigation_page,
)


def state():
    return st.session_state.data_console



def sync_url():
    s = state()
    if s.get('invalid_ticket'):
        return  # Preserve a corrupt recovery URL instead of silently dropping it.
    q = {'page': s['page']}
    if s.get('selected'):
        q['id'] = s['selected']
        if s['page'] == 'crawl' and s.get('preview_file'):
            q['file'] = s['preview_file']
    if s.get('pending'):
        q['pending'] = pack(s['pending'])
    st.query_params.from_dict(q)



def init():
    if 'data_console' not in st.session_state:
        page = st.query_params.get('page', 'source')
        s = {'page': page if page in PAGES else 'source', 'lists': {}, 'pending': None, 'payload': None,
             'selected': '', 'dialog': '', 'artifact': None, 'notice': '', 'error': ''}
        raw = st.query_params.get('pending', '')
        if raw:
            try:
                s['pending'] = unpack(raw)
            except ValueError as e:
                s['invalid_ticket'] = str(e)
        try:
            s['selected'] = identifier(st.query_params.get('id', ''))
            s['dialog'] = 'detail'
        except ValueError:
            pass
        try:
            s['preview_file'] = identifier(st.query_params.get('file', ''))
        except ValueError:
            s['preview_file'] = ''
        if s['page'] == 'crawl' and not s['selected']:
            s['page'] = 'source'
        if s['pending']:
            try:
                saved = RequestStore().load(s['pending'], API().base)
                if saved:
                    s['payload'] = saved.get('payload')
                    s['accepted'] = saved.get('accepted')
            except (RequestStoreError, ValueError) as e:
                s['error'] = str(e)
        st.session_state.data_console = s
        st.session_state.data_nav = navigation_page(s['page'])
    return state()



def error(e):
    st.error('요청을 완료하지 못했습니다.')
    st.text(friendly_error(e))



def locked():
    return bool(state().get('pending') or state().get('invalid_ticket'))



def close_dialog():
    s = state()
    s.pop('deleting', None)
    s.update(dialog='', selected='', artifact=None, original_download=None, table_original_preview=None, preview_cache=None, preview_file='', result_files=None, read_error=None)
    if s['page'] == 'crawl': s['page'] = 'source'
    if not s.get('invalid_ticket'):
        sync_url()



def select(page, id):
    identifier(id)
    state().pop('deleting', None)
    state().update(page=page, selected=id, dialog='detail', artifact=None, original_download=None, table_original_preview=None, settled=False,
                   preview_file='', preview_cache=None, result_files=None, read_error=None)
    # Radio has already rendered in this run. Update its value before next render.
    state()['nav_target'] = page
    sync_url()
    st.rerun()



def open_request(kind, source=''):
    state().pop('deleting', None)
    if kind in ('crawl', 'upload'):
        st.session_state.request_method = kind
    state().update(dialog=kind, selected='', source=source, artifact=None, preview_cache=None, read_error=None, error='')
    sync_url()
    st.rerun()



def submit(kind, body, file=None):
    s, api = state(), API()
    try:
        if s.get('invalid_ticket'):
            raise ValueError(s['invalid_ticket'])
        if not s['pending']:
            s['pending'] = ticket(kind, body, api.base, file)
        match(s['pending'], kind, body, api.base, file)
        s['payload'] = (kind, json.loads(json.dumps(body)), file)
        s['definitive_rejection'] = False
        sync_url()  # Persist the key and input digest before attempting POST.
        accepted = RequestStore().prepare(s['pending'], kind, body, api.base, file)
        if accepted is None:
            with st.spinner('요청을 보내고 있습니다…'):
                accepted = api.create(kind, body, s['pending']['key'], file)
            try:
                RequestStore().complete(s['pending'], accepted)
            except RequestStoreError:
                pass  # Acceptance is known; a later replay still uses the same key.
    except (APIError, ValueError) as e:
        s['error'] = (getattr(e, 'code', '') + ': ' + str(e)).lstrip(': ')
        s['error_status'] = getattr(e, 'status', 0)
        s['definitive_rejection'] = isinstance(e, APIError) and e.status == 400
        return False
    accept_result(kind, accepted)



def accept_result(kind, accepted):
    s = state()
    page = 'source' if kind == 'upload' else kind
    payload = s.get('payload')
    mode = ('OCR 우선' if payload[1].get('ocr_deployment_id') else '파서만 사용') if kind == 'refine' and payload else ''
    s.update(pending=None, payload=None, error='', error_status=0, definitive_rejection=False,
             accepted=None, notice='파일을 등록했습니다.' if kind == 'upload' else ('내용 정리를 시작했습니다. 처리 방식: ' + mode if mode else '실행 요청을 보냈습니다. 이 화면에서 진행 상태를 확인하세요.'))
    s['lists'].pop(page, None)
    select(page, accepted[ID_FIELD[page]])



def new_request():
    s = state()
    kind = (s.get('pending') or {}).get('kind', 'crawl')
    target = (s.get('pending') or {}).get('target', '')
    payload = s.get('payload')
    if payload:
        body = payload[1]
        st.session_state['name_' + kind] = body['name']
        if kind == 'crawl':
            st.session_state.crawl_url = body['url']
            options = body['options']
            st.session_state.crawl_follow = options.get('follow_links', False)
            st.session_state.crawl_attachments = options.get('include_attachments', True)
            st.session_state.crawl_docs = options.get('max_documents', 10)
    s.update(pending=None, payload=None, accepted=None, error='', error_status=0, definitive_rejection=False)
    s.pop('invalid_ticket', None)
    s['notice'] = '새 요청을 작성합니다. 앞서 접수된 작업이 있다면 계속 실행됩니다.'
    open_request(kind, target)



def retry_request():
    s = state()
    if s.get('accepted'):
        accept_result(s['pending']['kind'], s['accepted'])
    elif s.get('payload'):
        submit(*s['payload'])
    st.rerun()



def request_error():
    s = state()
    if not s.get('error'):
        return
    if s.get('definitive_rejection') and (s.get('pending') or {}).get('kind') == 'upload':
        st.error('파일 업로드 요청이 거절되었습니다. 내용 정리는 아직 시작하지 않았습니다.')
    st.error(friendly_error(s['error']))
    with st.expander('문제 해결 정보'):
        st.caption('오류가 계속되면 이 내용을 복사하거나 캡처해서 전달해주세요.')
        # Plain code/text only. Never include request bodies, uploaded bytes or
        # connection configuration, and never render server errors as markup.
        status = s.get('error_status')
        details = ('HTTP ' + str(status) + '\n' if status else '') + str(s['error'])[:1000]
        st.code(details, language='text')



def pending_request(button_key):
    s = state()
    if s.get('payload'):
        kind, body, file = s['payload']
        st.text({'crawl': '웹에서 가져오기', 'upload': '파일 가져오기', 'refine': '내용 정리'}[kind] + ' · ' + body['name'])
        st.caption('입력한 내용은 보관되어 있습니다. 다시 입력할 필요가 없습니다.')
    if s.get('payload') and s['payload'][0] == 'refine':
        st.caption('이 요청의 처리 방식: ' + ('OCR 우선' if s['payload'][1].get('ocr_deployment_id') else '파서만 사용'))
    if s.get('accepted'):
        st.success('이미 접수된 요청입니다.')
        if st.button('실행 결과 보기', key=button_key, type='primary'):
            retry_request()
    elif s.get('payload') and not s.get('definitive_rejection'):
        if st.button('요청 다시 확인', key=button_key, type='primary'):
            retry_request()
    elif not s.get('payload'):
        st.info('이전 요청의 입력을 찾을 수 없습니다. 작업 현황을 확인하거나 새 요청을 작성하세요.')
    request_error()
    if s.get('definitive_rejection') or not s.get('payload'):
        if st.button('입력 수정' if s.get('definitive_rejection') else '새 요청 작성', key='new_' + button_key, type='primary'):
            new_request()
    else:
        with st.popover('⋯ 더보기', key='pending_more_' + button_key):
            if st.button('새 요청 작성', key='new_' + button_key):
                new_request()



def recovery():
    s = state()
    if s.get('invalid_ticket'):
        st.warning('이전 요청 정보를 불러올 수 없습니다.')
        if st.button('새 요청 작성', key='new_invalid_request'):
            new_request()
        return
    if not s['pending']:
        return
    if s['dialog'] not in ('crawl', 'upload', 'refine'):
        st.warning('요청의 접수 여부를 확인하지 못했습니다.' if not s.get('accepted') else '접수된 요청이 있습니다.')
        pending_request('retry_pending')



def go_page(page):
    close_dialog()
    state().update(page=page, nav_target=page)
    sync_url()
    st.rerun()



def query_ocr_models(cursor=''):
    """Explicit read-only discovery; each click fetches at most 100 deployments."""
    s = state()
    gateway = os.environ.get('MODEL_GATEWAY_URL', 'http://gateway:8000')
    old = s.get('ocr_catalog') or {}
    try:
        if cursor and (old.get('gateway') != gateway or cursor != old.get('next_cursor')):
            raise ValueError('changed catalog')
        response = API().deployment_listing(cursor)
        rows = response['items']
        ids = [identifier(row.get('deployment_id')) for row in rows]
        if len(ids) != len(set(ids)): raise ValueError('duplicate deployment')
        next_cursor = response.get('next_cursor', '')
        seen = list(old.get('cursors', [])) if cursor else []
        seen.append(cursor)
        if next_cursor and next_cursor in seen: raise ValueError('repeated cursor')
        items = dict(old.get('items', {})) if cursor else {}
        for row in rows:
            id = row['deployment_id']
            if ocr_deployment_ready(row, id): items[id] = row
        selected = s.get('ocr_selection') or os.environ.get('DATA_UI_OCR_DEPLOYMENT_ID', '').strip()
        selected = selected if selected in items else ''
        s['ocr_catalog'] = {'gateway': gateway, 'items': items, 'next_cursor': next_cursor, 'cursors': seen}
        s['ocr_selection'] = selected
        st.session_state.ocr_model_choice = selected
        s['ocr_connection'] = None
    except (APIError, ValueError, KeyError, TypeError, AttributeError):
        s['ocr_catalog'] = {'gateway': gateway, 'items': {}, 'error': 'OCR 모델 목록을 불러오지 못했습니다. 연결을 확인한 뒤 다시 조회해주세요.'}
        s['ocr_selection'] = ''
        st.session_state.ocr_model_choice = ''
        s['ocr_connection'] = None


def recognition_status(*, refresh=False):
    """Use the explicit selection; only job submission rechecks its live record."""
    s = state()
    deployment = s.get('ocr_selection', '')
    gateway = os.environ.get('MODEL_GATEWAY_URL', 'http://gateway:8000')
    catalog = s.get('ocr_catalog') or {}
    key = (deployment, gateway)
    result = {'key': key, 'ready': False, 'deployment': deployment,
              'detail': 'OCR 모델 조회를 누르고 사용할 글자 인식 모델을 선택해주세요.'}
    if not deployment or catalog.get('gateway') != gateway:
        return result
    cached = s.get('ocr_connection')
    if not refresh and cached and cached['key'] == key:
        return cached
    try:
        identifier(deployment)
        value = API().deployment(deployment) if refresh else catalog.get('items', {}).get(deployment, {})
        if value.get('deployment_id') != deployment:
            raise APIError('Deployment identity mismatch', code='INVALID_RESPONSE')
        ready = ocr_deployment_ready(value, deployment)
        result.update(ready=ready, detail='선택한 모델로 PDF·사진의 필요한 페이지를 읽습니다.' if ready else
                      '선택한 모델을 사용할 수 없습니다. OCR 모델을 다시 조회하거나 모델 관리에서 배포 상태를 확인해주세요.')
    except (APIError, ValueError):
        result['detail'] = '선택한 OCR 모델의 상태를 확인하지 못했습니다. 모델을 다시 조회하고 선택해주세요.'
    s['ocr_connection'] = result
    return result


def recognition_connection():
    s = state()
    gateway = os.environ.get('MODEL_GATEWAY_URL', 'http://gateway:8000')
    catalog = s.get('ocr_catalog')
    if catalog and catalog.get('gateway') != gateway:
        s.update(ocr_catalog=None, ocr_selection='', ocr_connection=None)
        st.session_state.ocr_model_choice = ''
    option, hint = st.columns([1.5, 6.8], vertical_alignment='center')
    s['fast_work'] = option.toggle('빠른 작업', key='fast_work',
                                  help='새 요청에서 OCR을 사용하지 않고 파서만 사용합니다. 이미 보낸 요청은 바뀌지 않습니다.')
    hint.caption('파서만 사용합니다. 스캔 PDF·사진의 글자는 읽지 못할 수 있습니다.' if s['fast_work'] else
                 '기본: 선택한 OCR 모델로 표를 우선 처리합니다. 사용할 모델이 없으면 파서만 사용합니다.')
    with st.container(key='data_ocr_toolbar'):
        query, selection, details = st.columns([1.45, 3.6, 3.25], vertical_alignment='center')
        if query.button('OCR 모델 조회', key='query_ocr_models', use_container_width=True):
            query_ocr_models()
        catalog = s.get('ocr_catalog')
        if not catalog:
            selection.caption('OCR 모델 미선택 · 현재는 파서만 사용합니다. OCR을 쓰려면 모델을 조회하고 선택하세요.')
            return
        if catalog.get('error'):
            selection.error(catalog['error'])
            return
        items = catalog['items']
        if items:
            choices = [''] + list(items)
            if st.session_state.get('ocr_model_choice') not in choices:
                st.session_state.ocr_model_choice = ''
            selected = selection.selectbox('사용할 OCR 모델', choices, key='ocr_model_choice',
                                            label_visibility='collapsed',
                                            help='새 PDF·사진 정리에 사용할 OCR 모델입니다.',
                                            format_func=lambda id: '모델을 선택하세요' if not id else 'Surya OCR · ' + id[:8])
            if selected != s.get('ocr_selection'):
                s.update(ocr_selection=selected, ocr_connection=None)
            result = recognition_status()
            if selected and not result['ready']: selection.warning(result['detail'])
            with details.expander('글자 인식 연결 정보', expanded=False):
                st.text(result['detail'])
                if selected: st.text('선택한 배포: ' + selected)
                st.caption('조회 당시 사용 가능한 배포입니다. 새 정리 요청 직전에 다시 확인하고 서버도 최신 관측·실행 대상을 검사합니다.')
                st.caption('선택은 현재 브라우저 세션에 적용됩니다. 이미 접수되었거나 확인 중인 요청의 모델은 바꾸지 않습니다.')
        elif catalog.get('next_cursor'):
            selection.info('이 목록에는 사용 가능한 OCR 모델이 없습니다. 다음 목록도 조회해주세요.')
        else:
            selection.info('사용 가능한 OCR 모델이 없어 파서만 사용합니다. OCR을 쓰려면 모델 관리에서 배포한 뒤 다시 조회해주세요.')
        if catalog.get('next_cursor') and details.button('다음 OCR 모델 조회', key='next_ocr_models'):
            query_ocr_models(catalog['next_cursor'])
            st.rerun()


def refinement_available(metadata, *, show_hint=True):
    if metadata.get('deleted_at') or metadata.get('purge_state'):
        if show_hint: st.info('목록에서 삭제한 자료입니다. 새 내용 정리는 시작할 수 없습니다.')
        return False
    if show_hint and source_kind(metadata) in ('pdf', 'png', 'jpg'):
        if state().get('fast_work'):
            st.caption('빠른 작업 · 파서만 사용합니다. 이미지로 된 내용은 미확인 상태로 남을 수 있습니다.')
        elif not recognition_status()['ready']:
            st.caption('사용할 OCR 모델이 없어 파서만 사용합니다. 필요한 경우 모델을 조회하고 선택해주세요.')
    return True



def start_refinement(metadata):
    try:
        if metadata.get('deleted_at') or metadata.get('purge_state'):
            raise ValueError('목록에서 삭제한 자료입니다. 자료 목록에서 다른 파일을 선택해주세요.')
        deployment = ''
        if not state().get('fast_work') and source_kind(metadata) in ('pdf', 'png', 'jpg'):
            result = recognition_status(refresh=True)
            if result['ready']:
                deployment = result['deployment']
        body = refinement_body(metadata, deployment)
    except ValueError as e:
        error(e)
        return
    submit('refine', body)
    st.rerun()



def request_form(kind):
    s = state()
    if s.get('pending'):
        pending_request('retry_request')
        return
    if kind == 'refine':
        if s.get('source'):
            try:
                doc = API().get('source-documents', s['source'])
                st.text(source_name(doc))
                st.caption('글자와 표를 읽기 쉬운 내용으로 정리합니다. 원본 파일은 그대로 보관합니다.')
                available = refinement_available(doc)
                if st.button('내용 정리하기', type='primary', key='submit_refine', disabled=not available):
                    start_refinement(doc)
            except (APIError, ValueError) as e:
                error(e)
        elif st.button('정리할 자료 선택', type='primary'):
            go_page('source')
        return
    file = None
    with st.form('request_' + kind):
        if kind == 'crawl':
            st.subheader('웹에서 자료 가져오기')
            url = st.text_input('웹페이지 주소', key='crawl_url', max_chars=4096, placeholder='https://example.com/document')
            with st.expander('가져올 범위'):
                attachments = st.checkbox('첨부파일도 함께 가져오기', value=True, key='crawl_attachments')
                follow = st.checkbox('연결된 같은 사이트의 페이지도 가져오기', key='crawl_follow')
                docs = st.number_input('최대 자료 수', min_value=1, max_value=20, value=10, step=1, key='crawl_docs')
                st.caption('시작 페이지와 첨부파일을 포함한 개수입니다. 연결된 페이지는 한 단계까지 가져옵니다.')
            body = collection_body(url, attachments, follow, docs)
            st.caption('자료를 가져온 뒤 파일을 선택해 내용을 정리할 수 있습니다.')
        else:
            st.subheader('내 파일 가져오기')
            uploaded = st.file_uploader('파일 선택 (최대 32 MB)', type=['txt', 'md', 'markdown', 'html', 'htm', 'pdf', 'png', 'jpg', 'jpeg', 'docx', 'xlsx'], key='source_file')
            file = uploaded.getvalue() if uploaded else None
            body = {'name': uploaded.name[:200] if uploaded else '', 'filename': uploaded.name if uploaded else ''}
            st.caption('문서·표·사진 파일을 가져올 수 있습니다.')
        submitted = st.form_submit_button('가져오기', type='primary')
    if submitted:
        submit(kind, body, file)
        st.rerun()
    request_error()



@st.dialog('새 요청', width='large', on_dismiss=close_dialog)
def request_dialog(kind):
    if kind in ('crawl', 'upload') and not state().get('pending'):
        kind = st.radio('가져올 자료', ['crawl', 'upload'], key='request_method', horizontal=True,
                        format_func=lambda x: '웹 주소' if x == 'crawl' else '파일 업로드')
        state()['dialog'] = kind
    request_form(kind)
    if st.button('닫기', key='close_request'):
        close_dialog()
        st.rerun()
