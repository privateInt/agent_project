"""One selected detail dialog; no details are rendered from stale URL IDs."""
import streamlit as st
import ui
from state import identifier


@st.dialog('상세 보기', width='large', on_dismiss=ui.close_detail)
def show(kind, resource_id):
    if not ui.detail_is_open(kind, resource_id):
        return
    if st.button('닫기', key='close_detail_dialog'):
        ui.close_detail()
        st.rerun()
    try:
        if kind == 'deployment':
            from job_views import deployment_detail
            deployment_detail(resource_id)
        elif kind == 'run':
            from job_views import training_detail
            training_detail(resource_id)
        elif kind == 'dataset':
            from model_views import dataset_detail
            dataset_detail(resource_id)
    except ui.api.APIError as exc:
        ui.error(exc)


def render():
    current = ui.state().get('detail_dialog')
    if current and current.get('kind') in ('deployment', 'run', 'dataset'):
        show(current['kind'], identifier(current['id']))
