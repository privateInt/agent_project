"""Console lists, progress polling and page composition; reruns perform GET only."""
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor

import streamlit as st
import data_style

from data_api import API, APIError, identifier
from data_state import COLLECTION, ID_FIELD, PAGES, PHASE, TERMINAL, file_size, friendly_error, label, source_name, source_type_label, navigation_page
from data_ui_actions import (
    close_dialog, error, go_page, init, locked, open_request, recovery,
    request_dialog, select, state, sync_url, recognition_connection,
    start_refinement, refinement_available,
)
from data_ui_documents import artifact, source_detail, source_row_download
from data_ui_deletion import delete_dialog, open_delete
from data_ui_groups import group_filter, group_actions, group_cell, group_dialog


def fetch_list(page, filters, cursor='', history=None):
    try:
        response = API().listing(COLLECTION[page], limit=10, cursor=cursor, **filters)
        state()['lists'][page] = {'filters': filters, 'cursor': cursor, 'history': history or [],
                                'data': response, 'at': time.strftime('%H:%M:%S UTC', time.gmtime())}
    except (APIError, ValueError) as e:
        # Never present a stale successful page under a newly submitted filter.
        state()['lists'].pop(page, None)
        error(e)



def list_toolbar(page):
    with st.container(key='data_toolbar_' + page):
        query, search, action = st.columns([1.45, 5.2, 1.65], vertical_alignment='center')
        clicked = query.button('내역 조회 / 새로고침' if page == 'crawl' else '조회 / 새로고침',
                               key='query_' + page, use_container_width=True)
        if page == 'operation':
            with search.popover('작업 필터', use_container_width=True):
                status = st.selectbox('작업 상태', ['', 'created', 'queued', 'running', 'succeeded', 'failed', 'cancelled'], format_func=lambda x: label(x) if x else '전체')
                kind = st.selectbox('종류', ['', 'collection.crawl', 'collection.browser', 'refinement.parse'], format_func=lambda x: {'': '전체', 'collection.crawl': '자료 가져오기', 'collection.browser': '웹 자료 가져오기', 'refinement.parse': '내용 정리'}[x])
            filters = {'status': status, 'kind': kind}
        else:
            title = '자료 이름·주소 검색' if page in ('source', 'crawl') else '자료 이름 검색'
            filters = {'q': search.text_input(title, key='q_' + page, max_chars=200,
                                             placeholder=title, label_visibility='collapsed')}
        if page == 'source' and action.button('자료 가져오기', key='open_intake', icon=':material/add:',
                                              type='primary', disabled=locked(), use_container_width=True):
            open_request('crawl')
        if page == 'refine' and action.button('정리할 자료 선택', key='choose_material',
                                              disabled=locked(), use_container_width=True):
            go_page('source')
    if page in ('source', 'refine') and st.checkbox('목록에서 삭제한 항목 포함', key='include_deleted_' + page):
        filters['include_deleted'] = 'true'
    return clicked, filters


def list_footer(page, cached):
    with st.container(key='data_footer_' + page):
        count, previous, number, following = st.columns([6, 1, .5, 1], vertical_alignment='center')
        count.caption('현재 페이지 ' + str(len(cached['data']['items'])) + '개')
        number.caption(str(len(cached['history']) + 1))
        if previous.button('이전', key='prev_' + page, disabled=not cached['history'], use_container_width=True):
            history = cached['history'][:]
            cursor = history.pop()
            fetch_list(page, cached['filters'], cursor, history)
            st.rerun()
        if following.button('다음', key='next_' + page, disabled=not cached['data'].get('next_cursor'), use_container_width=True):
            fetch_list(page, cached['filters'], cached['data']['next_cursor'], cached['history'] + [cached['cursor']])
            st.rerun()


def listing(page=None):
    s = state()
    page = page or s['page']
    if page == 'crawl':
        st.caption('웹 주소별 진행 상태와 실패 내역입니다. 상세에서 함께 가져온 첨부파일을 확인할 수 있습니다.')
    group_id = group_filter() if page == 'source' else ''
    search, filters = list_toolbar(page)
    if group_id: filters['group_id'] = group_id
    group_changed = page == 'source' and s.get('lists', {}).get(page, {}).get('filters', {}).get('group_id', '') != group_id
    refresh_filters = s.setdefault('list_refresh', {}).pop(page, None)
    if search or group_changed or page not in s['lists']:
        fetch_list(page, filters if search or group_changed or refresh_filters is None else refresh_filters)
        if page == 'source' and search:
            s['lists'].pop('crawl', None)
    cached = s['lists'].get(page)
    if not cached:
        return
    items = cached['data']['items']
    with st.container(key='data_table_' + page):
        data_style.heading(page)
        if not items:
            with st.container(key='data_row_empty_' + page):
                st.info('조회된 항목이 없습니다.')
        for item in items:
            id = item.get(ID_FIELD[page], '')
            try:
                identifier(id)
            except ValueError:
                st.error('일부 자료 정보를 불러오지 못했습니다. 목록을 새로고침해주세요.')
                continue
            if page == 'source':
                source_row(item)
                continue
            with st.container(key='data_row_' + page + '_' + id):
                title, status, created, action = st.columns(data_style.RESULT_COLUMNS, vertical_alignment='center')
                if page == 'crawl':
                    url = str(item.get('url') or item.get('name') or 'URL')
                    title.text(url + ' 수집 결과')
                    if item.get('name') and item.get('name') != url:
                        title.caption(data_style.text_label(str(item['name'])[:160]))
                    ids = item.get('output_document_ids') or ([item['output_document_id']] if item.get('output_document_id') else [])
                    title.caption('수집한 파일 ' + str(len(ids)) + '개' if ids else '결과를 기다리고 있습니다. 상세에서 진행 상태를 확인하세요.')
                else:
                    title.text(item.get('name') or ('내용 정리' if page == 'refine' or item.get('kind') == 'refinement.parse' else '자료 가져오기'))
                with status: data_style.status_badge(item)
                created.text(str(item.get('created_at') or '—')[:19].replace('T', ' '))
                with action, st.container(key='data_actions_' + page + '_' + id):
                    view, remove = st.columns([1.4, 1], gap='small')
                    if view.button('결과 보기', key='detail_' + id, use_container_width=True):
                        select(page, id)
                    if page == 'refine' and remove.button('삭제', key='row_delete_refine_' + id,
                                                         disabled=locked(), use_container_width=True):
                        open_delete(page, item)
        list_footer(page, cached)
    if page == 'source': group_actions()


def source_row(item):
    id = item['document_id']
    with st.container(key='data_row_source_' + id):
        info, groups, origin, kind, status, actions = st.columns(data_style.SOURCE_COLUMNS, vertical_alignment='center')
        with info, st.container(key='data_name_' + id):
            if st.button(data_style.text_label(source_name(item)), key='detail_' + id,
                         help='자료의 원본 내용을 확인합니다.', use_container_width=True):
                select('source', id)
            st.caption(file_size(item.get('size_bytes')))
        with groups: group_cell(item)
        origin.text(str(item.get('source_url') or '직접 업로드')[:200])
        kind.text(source_type_label(item))
        with status: data_style.status_badge(item)
        with actions, st.container(key='data_actions_' + id):
            refine, download, remove = st.columns([1.1, 1.5, .7], gap='small')
            available = refinement_available(item, show_hint=False)
            if refine.button('내용 정리', key='row_refine_' + id, disabled=locked() or not available,
                             use_container_width=True):
                start_refinement(item)
            with download:
                source_row_download(item)
            if remove.button('삭제', key='row_delete_source_' + id, disabled=locked(), use_container_width=True):
                open_delete('source', item)


def operation_status(value):
    st.metric('진행 상황', label(value.get('status')))
    if value.get('status') not in TERMINAL:
        st.info(PHASE.get(value.get('phase'), '처리 상태를 확인하고 있어요'))
    if value.get('error_code') or value.get('submission_error_code'):
        st.error(friendly_error(str(value.get('error_code') or value.get('submission_error_code')) + ' ' + str(value.get('error_message') or '')))



def collection_result():
    s, id = state(), state()['selected']
    if st.button('← 자료 목록', key='back_results'):
        close_dialog()
        st.rerun()
    @st.fragment(run_every=5 if not s.get('settled') else None)
    def content():
        with st.popover('⋯ 더보기', key='collection_more'):
            refresh = st.button('결과 새로고침', key='refresh_collection')
            options = st.container()
        try:
            value = API().get('crawl-runs', id)
        except (APIError, ValueError) as e:
            error(e)
            return
        if value.get('status') in TERMINAL and not s.get('settled'):
            s['settled'] = True
            s['lists'].pop('source', None)
            s['lists'].pop('crawl', None)
            st.rerun()
        url = str(value.get('url') or value.get('name') or 'URL')
        st.html('<h2>' + html.escape(url) + ' 수집 결과</h2>')
        status = value.get('status')
        with options:
            work_details(value)
        ids = value.get('output_document_ids') or ([value['output_document_id']] if value.get('output_document_id') else [])
        st.caption('현재 단계: 2. 내용 정리' if status == 'succeeded' and ids else '현재 단계: 1. 자료 수집')
        st.text(label(status) + ' · 수집된 파일 ' + str(len(ids)) + '개')
        if status not in TERMINAL:
            st.info(PHASE.get(value.get('phase'), '자료를 가져올 준비를 하고 있어요'))
        if value.get('error_code') or value.get('submission_error_code'):
            st.error('수집을 완료하지 못했습니다.')
            st.text(friendly_error(str(value.get('error_code') or value.get('submission_error_code')) + ' ' + str(value.get('error_message') or '')))
        if len(ids) > 20:
            st.error('수집 결과의 파일 개수가 지원 범위를 벗어납니다.')
            return
        cache = s.get('result_files')
        if refresh or not cache or cache['key'] != (id, tuple(ids)):
            def read(file_id):
                try: return {'id': file_id, 'value': API().get('source-documents', file_id)}
                except (APIError, ValueError) as e: return {'id': file_id, 'error': str(e)}
            with ThreadPoolExecutor(max_workers=4) as pool:
                entries = list(pool.map(read, ids))
            s['result_files'] = {'key': (id, tuple(ids)), 'entries': entries}
        entries = s['result_files']['entries']
        available = [e for e in entries if 'value' in e]
        if available and s.get('preview_file') not in [e['id'] for e in available]:
            s['preview_file'] = available[0]['id']
            s.update(artifact=None, preview_cache=None, read_error=None)
            sync_url()
        if ids:
            left, right = st.columns([1, 2.1], gap='large')
            with left:
                st.subheader('가져온 자료')
                for entry in entries:
                    if 'error' in entry:
                        st.warning('파일 정보를 불러오지 못했습니다.')
                        st.text(friendly_error(entry['error']))
                if available:
                    documents = {e['id']: e['value'] for e in available}
                    selector = 'collection_file_' + id
                    if st.session_state.get(selector) not in documents:
                        st.session_state[selector] = s['preview_file']
                    def file_label(file_id):
                        doc = documents[file_id]
                        role = {'seed': '시작 페이지', 'page': '연결된 페이지', 'attachment': '첨부파일'}.get(doc.get('crawl_role'), '수집 파일')
                        # Widget labels support Markdown. Treat filenames as text.
                        name = re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', html.escape(source_name(doc)))
                        return name + ' · ' + role
                    chosen = st.radio('파일 선택', list(documents), key=selector, label_visibility='collapsed',
                                      format_func=file_label)
                    if chosen != s.get('preview_file'):
                        s.update(preview_file=chosen, artifact=None, preview_cache=None, read_error=None)
                        sync_url()
                        st.rerun()
            with right:
                selected = next((e['value'] for e in available if e['id'] == s.get('preview_file')), None)
                if selected: source_detail(selected, options=options)
        elif status == 'succeeded':
            st.info('등록된 파일이 없습니다.')
        else:
            st.caption('수집이 끝나면 여기에 파일 목록과 미리보기가 표시됩니다.')
        if any(value.get(k) for k in ('document_limit_reached', 'depth_limit_reached', 'link_limit_reached')):
            st.warning('설정한 수집 범위 제한에 도달했습니다.')
    content()



def work_details(value):
    """Read-only diagnostics, always placed inside an optional details panel."""
    st.caption('조회 시각 ' + time.strftime('%H:%M:%S UTC', time.gmtime()))
    if value.get('status') not in TERMINAL:
        st.caption('처리가 끝날 때까지 진행 상황을 자동으로 확인합니다.')
    for key, title in (('operation_id', '작업 번호'), ('phase', '처리 단계'),
                       ('submission_status', '요청 전달 상태')):
        if value.get(key): st.text(title + ': ' + str(value[key]))



def run_detail(value, page, options):
    if value.get('purge_state'):
        st.info('정리한 파일을 영구 삭제했습니다.' if value.get('purged_at') else '파일 삭제를 완료하지 못했습니다. 삭제 버튼에서 같은 항목을 다시 시도해주세요.')
        return
    if value.get('deleted_at'):
        st.info('결과 목록에서 삭제한 작업입니다. 보관된 정리 결과와 원본은 확인할 수 있습니다.')
    st.caption('현재 단계: 3. 결과 확인' if value.get('output_document_id') else '현재 단계: 2. 내용 정리')
    operation_status(value)
    source = None
    if value.get('source_document_id'):
        try:
            source = API().get('source-documents', value['source_document_id'])
            st.subheader(source_name(source))
        except (APIError, ValueError):
            st.caption('원본 자료의 이름을 불러오지 못했습니다.')
    id = value.get('output_document_id')
    if id:
        metadata = API().get('canonical-documents', id)
        st.metric('결과 상태', label(metadata.get('state')))
        if metadata.get('state') == 'needs_review':
            st.warning('내용 정리가 끝났습니다. 정확한 내용은 원본과 함께 확인해주세요.')
        elif metadata.get('state') == 'needs_ocr':
            st.warning('일부 내용을 읽지 못했습니다. 확인할 수 있는 결과와 원본을 함께 보여드립니다.')
        with options:
            view = st.radio('원본과 비교', ['정리한 내용', '원본 자료'], key='result_view_' + id)
        if view == '원본 자료':
            st.caption('원본 자료를 보고 있습니다. 더보기에서 정리한 내용으로 돌아갈 수 있습니다.')
            if source: source_detail(source, allow_refine=False, options=options)
            else: st.info('원본을 불러올 수 없습니다. 자료 목록에서 다시 확인해주세요.')
        else:
            artifact('canonical-documents', id, metadata, operation_id=value.get('operation_id'), source=source)
    elif value.get('status') not in TERMINAL:
        st.caption('정리가 끝나면 이곳에서 내용을 바로 확인할 수 있습니다.')



def detail_dialog():
    s = state()
    page, id = s['page'], s['selected']
    if st.button('← 자료 목록' if page == 'source' else '← 결과 목록', key='close_detail'):
        close_dialog()
        st.rerun()
    st.text('자료 내용' if page == 'source' else PAGES[page])
    @st.fragment(run_every=5 if page != 'source' and not s.get('settled') else None)
    def content():
        with st.popover('⋯ 더보기', key='detail_more'):
            if st.button('상태 새로고침', key='refresh_detail'):
                s['settled'] = False
            options = st.container()
        try:
            value = API().get(COLLECTION[page], id)
            terminal = value.get('status') in TERMINAL
            if terminal and not s.get('settled'):
                s['settled'] = True
                st.rerun()
            with options:
                if page != 'source': work_details(value)
            if page == 'source':
                st.caption('현재 단계: 2. 내용 정리')
                source_detail(value, options=options)
            elif page == 'operation':
                operation_status(value)
                resource_page = {'crawl_run': 'crawl', 'refinement_run': 'refine'}.get(value.get('resource_type'))
                if resource_page and st.button('자료와 결과 보기'):
                    select(resource_page, value['resource_id'])
            else:
                run_detail(value, page, options)
        except (APIError, ValueError, KeyError) as e:
            error(e)
    content()



def render():
    s = init()
    if s.get('nav_target'):
        target = s.pop('nav_target')
        st.session_state.data_nav = navigation_page(target)
    data_style.apply()
    with st.container(key='top_nav'):
        brand, menu = st.columns([1.65, 5.5], vertical_alignment='center')
        brand.html('<div class="inventory-brand"><span></span>데이터 플랫폼</div>')
        with menu:
            page = st.radio('화면', ['source', 'refine'], format_func=PAGES.get, horizontal=True,
                            key='data_nav', label_visibility='collapsed')
    if page != navigation_page(s['page']):
        s.pop('deleting', None)
        s.pop('group_edit', None)
        s.update(page=page, selected='', dialog='', artifact=None, original_download=None, settled=False,
                 preview_cache=None, preview_file='', result_files=None, read_error=None)
        if not s.get('invalid_ticket'):
            sync_url()
    st.title(PAGES[navigation_page(s['page'])])
    st.caption('자료 가져오기 → 내용 정리 → 결과 확인' if navigation_page(s['page']) == 'source'
               else '정리한 내용과 원본을 비교하고 필요한 결과를 내려받으세요.')
    recognition_connection()
    if s['notice']:
        st.success(s.pop('notice'))
        s['notice'] = ''
    recovery()
    if s['page'] == 'crawl' and s['dialog'] == 'detail' and s['selected']:
        collection_result()
    elif s['dialog'] == 'detail' and s['selected']:
        detail_dialog()
    else:
        listing()
        if s['page'] == 'source':
            with st.expander('웹 가져오기 내역', expanded=False):
                listing('crawl')
    if s['dialog'] in ('crawl', 'upload', 'refine'):
        request_dialog(s['dialog'])
    elif s.get('deleting'):
        delete_dialog()
    elif s.get('group_edit'):
        group_dialog()
