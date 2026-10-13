"""Button-driven Management / Training / Deployment console and separate chat."""
import streamlit as st
import ui
import model_views
import job_views
import chat_view
import detail_dialogs
from api import APIError

st.set_page_config(page_title='모델 플랫폼', page_icon='🧪', layout='wide')


def management():
    st.markdown('[모델](#models) · [배포된 모델](#deployments) · [데이터셋](#datasets) · [학습 작업](#training)')
    st.subheader('모델 목록', anchor='models')
    model_views.models()
    st.divider()
    st.subheader('배포된 모델', anchor='deployments')
    job_views.deployment_inventory('model_deployments')
    st.divider()
    st.subheader('데이터셋', anchor='datasets')
    model_views.datasets()
    st.divider()
    st.subheader('학습 작업', anchor='training')
    job_views.training()


def main():
    ui.init()
    try:
        ui.navigation()
        if ui.state()['page'] == 'chat':
            chat_view.page()
        else:
            management()
            if ui.state().get("show_add"):
                model_views.add_dialog_if_open()
            else:
                detail_dialogs.render()
    except APIError as exc:
        ui.error(exc)


if __name__ == '__main__':
    main()
