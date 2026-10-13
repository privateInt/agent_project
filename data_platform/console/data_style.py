"""Model-platform visual conventions, kept local for independent UI deployment."""
import re

import streamlit as st

from data_state import row_state

SOURCE_COLUMNS = [2.6, 3.0, 2.0, 1.1, 1.35, 3.85]
RESULT_COLUMNS = [4.5, 1.65, 2.2, 2.65]

CSS = """
<style>
:root { --inventory-blue:#1262ee; --inventory-line:#e1e7ef; --inventory-muted:#778397; }
.stApp { background:#fff; color:#182230; }
.stMainBlockContainer { max-width:1600px; padding:1rem 2.5rem 3rem; }
[data-testid="stHeader"] { height:0; background:transparent; }
h1 { font-size:2.15rem!important; font-weight:750!important; letter-spacing:-.045em; padding-top:1rem!important; }
[data-testid="stCaptionContainer"] { color:var(--inventory-muted); font-size:.875rem; }
[data-testid="stMetricValue"] { font-size:1.3rem; }
.st-key-top_nav { border-bottom:1px solid var(--inventory-line); padding:.25rem 0 .6rem; margin-bottom:1rem; }
.inventory-brand { font-size:1.3rem; font-weight:750; white-space:nowrap; display:flex; align-items:center; gap:.65rem; padding:.4rem 0; }
.inventory-brand span { display:inline-block; height:1.25rem; width:1.25rem; background:var(--inventory-blue); border-radius:3px; }
.st-key-top_nav [role="radiogroup"] { gap:1.6rem; }
.st-key-top_nav [data-baseweb="radio"] { margin:0!important; padding:.6rem .1rem; border-bottom:3px solid transparent; }
.st-key-top_nav [data-baseweb="radio"] > div:first-child { display:none; }
.st-key-top_nav [data-baseweb="radio"]:has(input:checked) { border-bottom-color:var(--inventory-blue); color:var(--inventory-blue); }
.st-key-top_nav [data-baseweb="radio"]:focus-within { outline:2px solid var(--inventory-blue); outline-offset:3px; }
.st-key-top_nav [data-baseweb="radio"] p { font-size:1rem; font-weight:600; }
[class*="st-key-data_toolbar_"] { padding:.3rem 0 .55rem; }
[class*="st-key-data_toolbar_"] button { min-height:2.9rem; }
[class*="st-key-data_toolbar_"] [data-baseweb="input"] { background:white; border-color:#ced8e5; }
.st-key-data_ocr_toolbar { background:#f8faff; border:1px solid var(--inventory-line); border-radius:7px; padding:.85rem 1rem; margin:.35rem 0 .8rem; }
.st-key-data_ocr_toolbar [data-testid="stVerticalBlock"] { gap:.4rem; }
.st-key-data_ocr_toolbar button { white-space:nowrap; }
[class*="st-key-data_table_"] { border:1px solid var(--inventory-line); border-radius:7px; gap:0!important; overflow:hidden; }
[class*="st-key-data_head_"] { background:#f5f7fa; padding:1rem 1.25rem; border-bottom:1px solid var(--inventory-line); }
[class*="st-key-data_head_"] p { font-size:.9rem; font-weight:650; color:#475468; margin:0; }
[class*="st-key-data_row_"] { padding:1.05rem 1.25rem; border-bottom:1px solid var(--inventory-line); min-height:92px; gap:.5rem!important; }
[class*="st-key-data_row_"]:hover { background:#fbfcfe; }
[class*="st-key-data_row_"] [data-testid="stVerticalBlock"] { gap:.3rem; }
[class*="st-key-data_row_"] [data-testid="stText"] { font-family:inherit; font-size:.95rem; overflow-wrap:anywhere; white-space:normal; }
[class*="st-key-data_name_"] button { border:0; background:transparent; padding:0; min-height:2.4rem; text-align:left; justify-content:flex-start; box-shadow:none; }
[class*="st-key-data_name_"] button p { font-size:1.03rem; font-weight:650; overflow-wrap:anywhere; }
[class*="st-key-data_name_"] button:hover { color:var(--inventory-blue); }
[class*="st-key-data_actions_"] [data-testid="stHorizontalBlock"] { gap:.45rem; flex-wrap:nowrap; }
[class*="st-key-data_actions_"] button { min-height:2.7rem; border-radius:5px; padding:.4rem .3rem; white-space:nowrap; }
[class*="st-key-row_refine_"] button:not(:disabled) { color:var(--inventory-blue); border-color:var(--inventory-blue); background:#fff; }
[class*="st-key-row_delete_"] button:not(:disabled) { color:#cf3434; }
[class*="st-key-data_actions_"] button:disabled { color:#a4afbc; border-color:#e1e7ef; background:#f4f6f8; }
[class*="st-key-data_footer_"] { padding:.7rem 1.25rem; }
[class*="st-key-data_footer_"] button { min-height:2.2rem; }
.collection-heading { font-size:1.15rem; font-weight:650; overflow-wrap:anywhere; margin-bottom:.4rem; }
.reading-preview { background:#fff; color:#20252b; padding:1.5rem; border:1px solid #dce3ea; border-radius:7px; line-height:1.8; max-height:700px; overflow:auto; overflow-wrap:anywhere; }
.reading-preview h1, .reading-preview h2 { font-size:1.4rem!important; margin:1rem 0; }
.reading-preview h3, .reading-preview h4 { font-size:1.15rem; }
.reading-preview table { border-collapse:collapse; margin:1rem 0; max-width:100%; }
.reading-preview th, .reading-preview td { border:1px solid #dce3ea; padding:.45rem .75rem; white-space:pre-wrap; }
.reading-preview th { background:#f4f7fa; }
.reading-preview pre { white-space:pre-wrap; }
@media (max-width:1100px) {
  .stMainBlockContainer { padding:1rem 1.1rem 2rem; }
  .st-key-top_nav [role="radiogroup"] { gap:.65rem; }
  [class*="st-key-data_row_"], [class*="st-key-data_head_"] { padding:.85rem .7rem; }
  [class*="st-key-data_actions_"] button p { font-size:.8rem; }
}
@media (max-width:740px) {
  [class*="st-key-data_head_"] { display:none; }
  .st-key-top_nav [role="radiogroup"] { flex-wrap:wrap; }
  [class*="st-key-data_actions_"] [data-testid="stColumn"] { min-width:0!important; flex:1 1 0!important; width:0!important; }
}
</style>
"""


def apply():
    # Fixed stylesheet only; no document names or API content enter this HTML.
    st.html(CSS)


def text_label(value):
    """Button labels accept Markdown; prevent names from becoming links/images."""
    return re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', str(value))


def heading(page):
    labels = ('자료 이름', '그룹', '가져온 곳', '파일 형식', '상태', '작업') if page == 'source' else (
        '웹 주소' if page == 'crawl' else '작업 이름', '상태', '요청 시각 (UTC)', '작업')
    with st.container(key='data_head_' + page):
        for column, title in zip(st.columns(SOURCE_COLUMNS if page == 'source' else RESULT_COLUMNS), labels):
            column.markdown(title)


def status_badge(value):
    if value.get('deleted_at'):
        st.badge('목록에서 삭제됨', color='gray')
        return
    status = value.get('status') or value.get('state') or value.get('submission_status')
    color = ('green' if status in ('ready', 'succeeded') else
             'orange' if status in ('created', 'queued', 'running', 'needs_review', 'needs_ocr', 'blocked') else
             'red' if status == 'failed' else 'gray')
    st.badge(row_state(value), color=color)
