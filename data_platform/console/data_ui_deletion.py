"""Explicit single-row catalog removal; immutable artifacts stay available."""
import streamlit as st

from data_api import API, APIError, identifier
from data_state import COLLECTION, ID_FIELD, TERMINAL, friendly_error, source_name
from data_ui_actions import locked, state


def close_delete():
    state().pop('deleting', None)


def open_delete(page, item):
    if locked() or page not in ('source', 'refine'):
        return
    st.session_state.pop('confirm_purge_scope', None)
    id = identifier(item[ID_FIELD[page]])
    state()['deleting'] = {'page': page, 'id': id, 'gateway': API().base,
                          'name': source_name(item) if page == 'source' else str(item.get('name') or '내용 정리 결과')}
    st.rerun()


@st.dialog('목록에서 삭제', width='small', on_dismiss=close_delete)
def delete_dialog():
    target = state().get('deleting')
    if not target:
        return
    page, id = target['page'], target['id']
    st.text(target['name'])
    permanent = st.selectbox('삭제 방식', ['목록에서만 삭제', '파일까지 영구 삭제'], key='delete_mode') == '파일까지 영구 삭제'
    confirmed = True
    if permanent:
        st.warning('저장된 파일을 실제로 지우며 복구할 수 없습니다.')
        st.caption('원본 자료는 연결된 정리 결과를 먼저 영구 삭제해야 합니다.' if page == 'source' else '이 작업의 정리 결과 파일을 지웁니다. 원본 자료는 별도로 삭제합니다.')
        st.caption('중복 요청 방지와 작업 이력을 위한 식별자·해시·삭제 시각은 남깁니다.')
        confirmed = st.checkbox('파일을 복구할 수 없음을 확인했습니다.', key='confirm_purge_scope')
    else:
        st.write('이 항목을 목록에서 삭제할까요?')
        if page == 'source':
            st.caption('이 자료만 목록에서 삭제합니다. 원본 파일과 기존 정리 결과는 보관하며, 이 자료로 새 내용 정리는 시작할 수 없습니다. 이미 접수 중인 작업은 계속됩니다.')
        else:
            st.caption('이 정리 작업만 결과 목록에서 삭제합니다. 원본 자료와 정리한 파일은 보관합니다. 실행 중인 작업은 끝난 뒤 삭제할 수 있습니다.')
        st.caption('저장 공간을 비우는 영구 삭제가 아닙니다.')
    allowed = False
    api = API()
    try:
        if api.base != target['gateway']:
            raise APIError('서버 연결이 바뀌었습니다. 닫은 뒤 목록에서 다시 선택해주세요.', code='SERVER_CHANGED')
        current = api.get(COLLECTION[page], id)
        allowed = page == 'source' or current.get('status') in TERMINAL or bool(current.get('deleted_at'))
        if current.get('deleted_at'):
            st.info('목록에서 삭제된 항목입니다. 보관된 파일을 영구 삭제하거나 같은 삭제 상태를 다시 확인할 수 있습니다.')
        elif not allowed:
            st.info('아직 처리 중이거나 완료 상태를 확인하지 못했습니다. 작업이 끝난 뒤 다시 확인해주세요.')
    except (APIError, ValueError) as exc:
        st.error(str(exc) if getattr(exc, 'code', '') == 'SERVER_CHANGED' else friendly_error(exc))
    confirm, cancel = st.columns(2)
    if confirm.button('파일 영구 삭제' if permanent else '목록에서 삭제', key='confirm_delete', type='primary', disabled=not allowed or not confirmed or locked(), use_container_width=True):
        try:
            # The ID and server were captured when the user chose this row.
            (api.purge if permanent else api.delete)(COLLECTION[page], id)
        except (APIError, ValueError) as exc:
            st.error('삭제 완료를 확인하지 못했습니다. 같은 항목에서 다시 확인해주세요.')
            if getattr(exc, 'code', '') == 'FailedPrecondition':
                if 'SOURCE_HAS_RESULTS' in str(exc):
                    st.info('이 원본에 연결된 정리 결과가 남아 있습니다. 결과 확인에서 해당 결과를 먼저 영구 삭제해주세요. 목록에서 숨긴 결과도 포함됩니다.')
                else:
                    st.info('처리 중이거나 다른 작업에서 사용 중입니다. 작업이 끝난 뒤 다시 시도해주세요.')
            elif getattr(exc, 'code', '') == 'Unimplemented':
                st.info('삭제 기능이 있는 서버 버전으로 업데이트해야 합니다.')
            else:
                st.text(friendly_error(exc))
            return
        cached = state()['lists'].pop(page, None)
        if cached:
            state().setdefault('list_refresh', {})[page] = dict(cached['filters'])
        state().update(result_files=None, original_download=None)
        state()['notice'] = '파일을 영구 삭제했습니다.' if permanent else '목록에서 삭제했습니다. 저장된 원본과 정리한 파일은 보관됩니다.'
        close_delete()
        st.rerun()
    if cancel.button('취소', key='cancel_delete', use_container_width=True):
        close_delete()
        st.rerun()
