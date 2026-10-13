import streamlit as st

st.set_page_config(page_title='데이터 관리', page_icon='📄', layout='wide')

from data_ui import render
render()
