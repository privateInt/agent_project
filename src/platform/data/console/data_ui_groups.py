"""Small catalog groups: GET on render, membership writes on explicit actions only."""
import uuid
import streamlit as st
from data_api import API, APIError, identifier
from data_state import friendly_error, source_name
from data_ui_actions import state, locked


def load_groups():
    s, api = state(), API()
    cached = s.get('groups')
    if cached and cached['base'] == api.base:
        return cached
    try:
        groups, cursor, seen = [], '', set()
        for _ in range(10):
            page = api.listing('source-groups', limit=100, cursor=cursor)
            for row in page['items']:
                api.group_response(row)
                if row['group_id'] in seen: raise ValueError('그룹 목록이 중복되었습니다.')
                seen.add(row['group_id']); groups.append(row)
            next_cursor = page.get('next_cursor', '')
            if not next_cursor: break
            if next_cursor == cursor: raise ValueError('그룹 목록을 끝까지 확인하지 못했습니다.')
            cursor = next_cursor
        else:
            raise ValueError('화면의 그룹 조회 한도(1,000개)를 넘었습니다.')
        cached = {'base': api.base, 'items': groups}
    except (APIError, ValueError) as e:
        cached = {'base': api.base, 'items': [], 'error': friendly_error(e)}
    s['groups'] = cached
    return cached


def group_filter():
    groups = load_groups()
    left, right = st.columns([5, 1])
    if right.button('그룹 새로고침', key='reload_groups', use_container_width=True):
        refresh_groups()
        st.rerun()
    if groups.get('error'):
        st.warning('그룹 목록을 불러오지 못했습니다. 그룹 새로고침을 눌러주세요.')
        # Do not silently switch a failed selected group to the complete catalog.
        return state().get('source_group_id', '')
    by_id = {g['group_id']: g for g in groups['items']}
    if 'source_group_filter' not in st.session_state:
        st.session_state['source_group_filter'] = state().get('source_group_id', '')
    if st.session_state.get('source_group_filter', '') not in ('', *by_id):
        st.warning('선택한 그룹을 찾을 수 없습니다. 전체 자료를 표시합니다.')
        st.session_state['source_group_filter'] = ''
    selected = left.selectbox('자료 그룹', ['', *by_id], key='source_group_filter',
                              format_func=lambda id: by_id[id]['name'] if id else '전체 자료')
    state()['source_group_id'] = selected
    return selected


def group_actions():
    """Keep infrequent name editing folded; membership lives beside each source."""
    s, groups = state(), load_groups()
    if groups.get('error') or not groups['items']: return
    by_id = {g['group_id']: g for g in groups['items']}
    with st.expander('그룹 이름 관리', expanded=False):
        target = st.selectbox('이름을 바꿀 그룹', list(by_id), key='rename_group_target',
                              format_func=lambda id: by_id[id]['name'])
        row = by_id[target]
        with st.form('rename_source_group_' + target):
            name = st.text_input('변경할 이름', value=row['name'], max_chars=200,
                                 key='rename_group_name_' + target + '_' + str(row['version']))
            change = st.form_submit_button('이름 변경', disabled=locked())
        if change:
            try:
                API().rename_group(target, name.strip(), row['version'])
                refresh_groups()
                s['notice'] = '그룹 이름을 변경했습니다.'
                st.rerun()
            except (APIError, ValueError) as e:
                st.error('그룹 이름이 중복되거나 다른 화면에서 변경되었습니다.'
                         if getattr(e, 'status', 0) == 409 else friendly_error(e))
                st.caption('그룹 새로고침 후 다시 확인해주세요.')


def document_groups(item):
    rows = item.get('groups')
    if not isinstance(rows, list):
        raise APIError('그룹 정보를 확인하지 못했습니다. 목록을 새로고침해주세요.', code='INVALID_RESPONSE')
    groups = {}
    for row in rows:
        API.group_response(row)
        if row['group_id'] in groups: raise ValueError('그룹 정보가 중복되었습니다.')
        groups[row['group_id']] = row
    return groups


def refresh_groups():
    s = state()
    s.pop('groups', None)
    cached = s['lists'].pop('source', None)
    if cached: s.setdefault('list_refresh', {})['source'] = dict(cached['filters'])


def close_group():
    state().pop('group_edit', None)


def open_group(item, mode):
    if locked(): return
    groups = document_groups(item)
    if mode == 'remove' and not groups: return
    st.session_state.pop('assign_group_name', None)
    st.session_state.pop('remove_group_target', None)
    state()['group_edit'] = {'id': identifier(item['document_id']), 'name': source_name(item),
                            'gateway': API().base, 'mode': mode, 'groups': groups, 'keys': {}}
    st.rerun()


def group_cell(item):
    try:
        groups = document_groups(item)
    except (APIError, ValueError):
        st.text('그룹 조회 필요')
        st.caption('목록을 새로고침하거나 서버 업데이트를 확인해주세요.')
        return
    st.text(', '.join(g['name'] for g in groups.values()) if groups else '미지정')
    with st.container(key='data_actions_groups_' + item['document_id']):
        assign, remove = st.columns(2, gap='small')
        unavailable = bool(item.get('deleted_at') or item.get('purge_state'))
        if assign.button('그룹 지정', key='row_group_assign_' + item['document_id'],
                         disabled=locked() or unavailable, use_container_width=True):
            open_group(item, 'assign')
        if remove.button('그룹 해제', key='row_group_remove_' + item['document_id'],
                         disabled=locked() or not groups, use_container_width=True):
            open_group(item, 'remove')


@st.dialog('자료 그룹', width='small', on_dismiss=close_group)
def group_dialog():
    target = state().get('group_edit')
    if not target: return
    st.text(target['name'])
    api = API()
    allowed = False
    try:
        if api.base != target['gateway']:
            raise APIError('서버 연결이 바뀌었습니다. 닫은 뒤 자료를 다시 선택해주세요.', code='SERVER_CHANGED')
        current = api.get('source-documents', target['id'])
        document_groups(current)
        allowed = target['mode'] == 'remove' or not (current.get('deleted_at') or current.get('purge_state'))
        if not allowed: st.info('삭제되었거나 삭제 중인 자료에는 그룹을 지정할 수 없습니다.')
    except (APIError, ValueError) as e:
        st.error(str(e) if getattr(e, 'code', '') == 'SERVER_CHANGED' else friendly_error(e))
    name, group_id, clicked = '', '', False
    if target['mode'] == 'assign':
        st.caption('그룹 이름이 있으면 해당 그룹에 포함하고, 없으면 새로 만들어 포함합니다.')
        with st.form('assign_document_group'):
            name = st.text_input('그룹명', key='assign_group_name', max_chars=200, placeholder='예: 프로젝트 A')
            clicked = st.form_submit_button('지정', type='primary', disabled=not allowed or locked())
    else:
        groups = target['groups']
        st.caption('선택한 그룹과의 연결만 해제합니다. 원본과 정리 결과는 유지됩니다.')
        group_id = st.selectbox('해제할 그룹', list(groups), key='remove_group_target',
                                format_func=lambda id: groups[id]['name'])
        clicked = st.button('해제', key='confirm_group_remove', type='primary', disabled=not allowed or locked())
    if clicked:
        try:
            if target['mode'] == 'assign':
                name = name.strip()
                if not name: raise ValueError('그룹명을 입력해주세요.')
                key = target['keys'].setdefault(name, str(uuid.uuid4()))
                api.assign_group(target['id'], name, key)
                notice = '그룹을 지정했습니다.'
            else:
                api.group_membership(group_id, target['id'], False)
                notice = '그룹 연결을 해제했습니다. 원본과 정리 결과는 유지됩니다.'
            refresh_groups()
            state()['notice'] = notice
            close_group()
            st.rerun()
        except (APIError, ValueError) as e:
            refresh_groups()
            st.error(str(e) if isinstance(e, ValueError) else friendly_error(e))
            st.caption('입력과 선택은 유지됩니다. 같은 버튼을 다시 눌러 처리 상태를 확인할 수 있습니다.')
    if st.button('닫기', key='close_group_dialog'):
        close_group()
        st.rerun()
