"""Original previews and canonical reading/download views."""
import base64
import html
import json

import streamlit as st

from data_api import API, APIError, identifier
from data_preview import PreviewError, preview
from data_results import (
    markdown_document, plain_document, quality_messages, reading_html,
    recognition_summary, visible_blocks, timing_rows, page_timing_rows, image_summary, processing_summary, visual_summary,
)
from data_state import download_name, file_size, friendly_error, source_kind, source_name, source_type_label
from data_ui_actions import locked, start_refinement, state, refinement_available


def canonical_preview(raw, operation=None, source=None):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get('schema_version') != 'canonical-document-v1':
            raise ValueError()
        blocks, pages = value['blocks'], value['pages']
        if not isinstance(blocks, list) or not isinstance(pages, list): raise ValueError()
    except (ValueError, KeyError, TypeError):
        st.error('정리한 내용을 표시할 수 없습니다. 잠시 후 다시 확인해주세요.')
        return
    mode = processing_summary(value)
    if mode: st.caption(mode)
    tables = sum(bool(b.get('cells')) for b in blocks if isinstance(b, dict))
    texts = sum(bool(b.get('text')) and not b.get('cells') for b in blocks if isinstance(b, dict))
    st.caption('글 ' + str(texts) + '개 · 표 ' + str(tables) + '개' + (' · ' + str(len(pages)) + '페이지' if pages else ''))
    images = image_summary(blocks)
    if images: st.text(images)
    messages = quality_messages(value)
    recognized = recognition_summary(value)
    if recognized:
        st.info(recognized)
    timings, page_timings = timing_rows(value, operation), page_timing_rows(value)
    if timings or page_timings:
        with st.expander('처리 시간', expanded=False):
            if timings: st.dataframe(timings, hide_index=True)
            if page_timings: st.dataframe(page_timings, hide_index=True)
            st.caption('전체 시간과 세부 구간은 서로 포함 관계입니다. 인식 요청 시간에는 서버 대기·전송·응답 처리가 포함됩니다.')
    if messages:
        with st.expander('확인이 필요한 부분', expanded=True):
            for message in messages: st.text(message)
    visual = visual_summary(value)
    if visual: st.caption(visual)
    displayed = visible_blocks(blocks)
    if len(displayed) > 20:
        index = st.number_input('내용 나눠 보기', min_value=1, max_value=(len(displayed)+19)//20, value=1, step=1)
        st.caption('긴 결과는 한 번에 일부씩 보여줍니다. 내려받은 파일에는 전체 내용이 있습니다.')
    else: index = 1
    st.html('<div class="reading-preview">' + reading_html(displayed[(index-1)*20:index*20]) + '</div>')
    with st.popover('결과 다운로드', type='primary', key='result_download'):
        kind = st.selectbox('저장 형식', ['Markdown (.md)', '텍스트 (.txt)', '전체 구조 (.json)', '원본 파일'], key='result_format')
        if kind == '원본 파일':
            if not source or source.get('document_id') != (value.get('source') or {}).get('document_id'):
                st.info('이 결과의 원본 정보를 확인하지 못했습니다. 상태를 새로고침해주세요.')
                return
            original_download(source)
            return
        if kind == '전체 구조 (.json)':
            data, extension, mime = raw, 'json', 'application/json'
            st.caption('본문·표·페이지·좌표·처리 정보를 포함한 전체 결과입니다.')
        elif kind == '텍스트 (.txt)':
            data, extension, mime = plain_document(value).encode('utf-8'), 'txt', 'text/plain'
            st.caption('본문과 표의 값을 일반 텍스트로 저장합니다.')
        else:
            data, extension, mime = markdown_document(value).encode('utf-8'), 'md', 'text/markdown'
            st.caption('본문·표·그림에서 읽은 내용을 Markdown으로 저장합니다. 병합 셀은 값을 반복 표시합니다.')
        st.download_button('파일 저장', data, file_name='정리한-결과.' + extension, mime=mime,
                           on_click='ignore', key='save_result_file', type='primary')



def artifact(collection, id, metadata, representation='content', operation_id=None, source=None):
    s = state()
    key = (collection, id, representation, metadata.get('sha256'))
    cached = s.get('artifact')
    if (not cached or cached['key'] != key) and (s.get('read_error') or {}).get('key') != key:
        s.update(artifact=None, read_error=None)
        try:
            s['artifact'] = {'key': key, 'raw': API().download(collection, id, metadata, representation)}
        except (APIError, ValueError) as e:
            s['read_error'] = {'key': key, 'message': friendly_error(e)}
    if (s.get('read_error') or {}).get('key') == key:
        st.error(s['read_error']['message'])
        if st.button('결과 다시 불러오기', key='reread_result'):
            s.update(read_error=None, artifact=None)
            st.rerun()
        return
    operation = None
    if operation_id:
        try:
            operation = API().get('operations', operation_id)
        except (APIError, ValueError):
            pass  # Timing lookup must never prevent reading the saved result.
    canonical_preview(s['artifact']['raw'], operation, source)


def original_download(metadata):
    """Fetch on selection only; never evict the canonical result or rerun a Job."""
    s, api = state(), API()
    id = metadata['document_id']
    key = (api.base, id, metadata.get('sha256'), metadata.get('size_bytes'))
    cached = s.get('original_download')
    if not cached or cached['key'] != key:
        try:
            raw = api.download('source-documents', id, metadata)
            cached = {'key': key, 'raw': raw}
        except (APIError, ValueError) as e:
            cached = {'key': key, 'error': friendly_error(e)}
        s['original_download'] = cached
    if cached.get('error'):
        st.error(cached['error'])
        if st.button('원본 다시 불러오기', key='reload_original_download'):
            s['original_download'] = None
            st.rerun()
        return
    filename = download_name('source-documents', id, metadata)
    st.caption('내용 정리에 사용한 원본 파일입니다.')
    st.download_button('원본 다운로드', cached['raw'], file_name=filename, mime='application/octet-stream',
                       on_click='ignore', key='save_original_file', type='primary')


def source_row_download(metadata):
    if metadata.get('purge_state'):
        st.caption('파일 삭제됨' if metadata.get('purged_at') else '파일 삭제 중')
        return
    """Streamlit 1.63 deferred download: no bytes fetched just to render a list."""
    id = identifier(metadata['document_id'])
    # Freeze this row's origin and metadata; callbacks may run after navigation
    # and must not read session_state or close over a mutable loop variable.
    snapshot = json.loads(json.dumps(metadata))
    base = API().base
    filename = download_name('source-documents', id, snapshot)

    def fetch():
        try:
            return API(base).download('source-documents', id, snapshot)
        except (APIError, ValueError) as e:
            # Deferred callbacks cannot render UI. Fail the download instead of
            # returning error text or mismatched bytes under the original name.
            raise RuntimeError(friendly_error(e)) from None

    st.download_button('원본 다운로드', fetch, file_name=filename, mime='application/octet-stream',
                       on_click='ignore', key='row_download_' + id,
                       help='가져온 원본 파일을 그대로 저장합니다.', use_container_width=True)



def source_detail(value, *, allow_refine=True, options=None):
    if value.get('purge_state'):
        st.info('원본 파일을 영구 삭제했습니다.' if value.get('purged_at') else '파일 삭제를 완료하지 못했습니다. 삭제 버튼에서 같은 항목을 다시 시도해주세요.')
        return
    s, id = state(), value['document_id']
    st.html('<h3>' + html.escape(source_name(value)) + '</h3>')
    st.caption(source_type_label(value) + ' · ' + file_size(value.get('size_bytes')))
    if value.get('deleted_at'):
        st.info('목록에서 삭제한 자료입니다. 보관된 원본과 기존 정리 결과는 확인할 수 있습니다.')
    if value.get('source_url'):
        st.text(str(value['source_url'])[:1000])
    representation, metadata = 'content', value
    key = ('source-documents', id, representation, metadata.get('sha256'))
    cached = s.get('artifact')
    if (not cached or cached['key'] != key) and (s.get('read_error') or {}).get('key') != key:
        s.update(artifact=None, preview_cache=None, read_error=None)
        try:
            s['artifact'] = {'key': key, 'raw': API().download('source-documents', id, metadata, representation)}
        except (APIError, ValueError) as e:
            s['read_error'] = {'key': key, 'message': str(e)}
    if (s.get('read_error') or {}).get('key') == key:
        st.error('파일을 불러오지 못했습니다.')
        st.text(friendly_error(s['read_error']['message']))
        if st.button('파일 다시 읽기', key='reread_' + id):
            s.update(read_error=None, artifact=None, preview_cache=None)
            st.rerun()
        return
    raw = s['artifact']['raw']
    filename = download_name('source-documents', id, metadata, representation)
    with options if options is not None else st.popover('⋯ 더보기', key='source_more_' + id):
        st.download_button('원본 다운로드', raw, file_name=filename, mime='application/octet-stream',
                           key='source_save_' + id, on_click='ignore')
        st.caption('파일 번호: ' + id)
    kind = source_kind(metadata)
    render_preview(raw, kind, key, str(metadata.get('content_type') or ''))
    if allow_refine:
        st.caption('가져온 원본입니다. 내용을 정리하면 글자와 표를 읽기 쉽게 확인할 수 있습니다.')
        available = refinement_available(value)
        if st.button('내용 정리하기', type='primary', disabled=locked() or not available, key='refine_this_source'):
            start_refinement(value)



def render_preview(raw, kind, key, content_type):
    s = state()
    selector = 'preview_page_' + key[1] + '_' + key[2]
    index = max(0, int(st.session_state.get(selector, 1)) - 1)
    cache_key = (key, kind, index)
    cached = s.get('preview_cache')
    if not cached or cached['key'] != cache_key:
        try:
            result = preview(raw, kind, index, content_type)
        except PreviewError as e:
            result = {'error': str(e)}
        s['preview_cache'] = {'key': cache_key, 'result': result}
    result = s['preview_cache']['result']
    if result.get('error'):
        st.info('이 파일은 미리보기를 만들 수 없습니다. 파일을 다운로드해 확인할 수 있습니다.')
        return
    if result['kind'] == 'pdf':
        count = result['page_count']
        st.number_input('페이지', min_value=1, max_value=min(count, 300), value=1, step=1, key=selector)
        st.caption(str(count) + '페이지 PDF')
    elif result['kind'] == 'xlsx':
        names = result['sheets']
        st.number_input('시트 번호', min_value=1, max_value=len(names), value=1, step=1, key=selector)
        st.text(names[index])
    if result['kind'] in ('image', 'pdf'):
        st.image(base64.b64decode(result['png']), width='stretch')
    elif result['kind'] in ('html', 'docx'):
        if result.get('title'): st.text(result['title'])
        st.caption('본문·표 미리보기' + (' · 사이트의 디자인·외부 이미지는 생략합니다.' if kind == 'html' else ' · 페이지 배치는 원본과 다를 수 있습니다.'))
        st.html('<div class="reading-preview">' + result['html'] + '</div>')
    elif result['kind'] == 'xlsx':
        st.dataframe(result['rows'], hide_index=True, width='stretch')
        st.caption('파일에 저장된 값을 보여줍니다. 정확한 서식은 내려받은 원본에서 확인하세요.')
    elif result['kind'] == 'text':
        st.text(result['text'])
    else:
        st.info('이 형식은 파일을 다운로드해 확인할 수 있습니다.')
    if result.get('truncated'):
        st.caption('미리보기 한도 내에서 일부만 표시합니다. 전체 내용은 원본 파일에 있습니다.')
