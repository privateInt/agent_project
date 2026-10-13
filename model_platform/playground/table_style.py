"""Presentation only: native Streamlit widgets styled as a model inventory."""
import streamlit as st

MODEL_COLUMNS = [2.2, 2.6, 1.6, 1.25, 2.45]
DEPLOYMENT_COLUMNS = [2.2, 2.6, 1.6, 1.25, 2.45]

CSS = """
<style>
:root { --inventory-blue:#1262ee; --inventory-line:#e1e7ef; --inventory-muted:#778397; }
.stApp { background:#fff; color:#182230; }
.stMainBlockContainer { max-width:1600px; padding:1rem 2.5rem 3rem; }
[data-testid="stHeader"] { height:0; background:transparent; }
h1 { font-size:2.15rem!important; font-weight:750!important; letter-spacing:-.045em; padding-top:1rem!important; }
[data-testid="stCaptionContainer"] { color:var(--inventory-muted); font-size:.875rem; }
.st-key-top_nav { border-bottom:1px solid var(--inventory-line); padding:.25rem 0 .6rem; margin-bottom:1rem; }
.inventory-brand { font-size:1.3rem; font-weight:750; white-space:nowrap; display:flex; align-items:center; gap:.65rem; padding:.4rem 0; }
.inventory-brand span { display:inline-block; height:1.25rem; width:1.25rem; background:var(--inventory-blue); border-radius:3px; }
.st-key-top_nav [role="radiogroup"] { gap:1.6rem; }
.st-key-top_nav [data-baseweb="radio"] { margin:0!important; padding:.6rem .1rem; border-bottom:3px solid transparent; }
.st-key-top_nav [data-baseweb="radio"] > div:first-child { display:none; }
.st-key-top_nav [data-baseweb="radio"]:has(input:checked) { border-bottom-color:var(--inventory-blue); color:var(--inventory-blue); }
.st-key-top_nav [data-baseweb="radio"]:focus-within { outline:2px solid var(--inventory-blue); outline-offset:3px; }
.st-key-top_nav [data-baseweb="radio"] p { font-size:1rem; font-weight:600; }
.st-key-model_toolbar, [class*="st-key-deployment_toolbar_"] { padding:.3rem 0 .55rem; }
.st-key-model_toolbar button, [class*="st-key-deployment_toolbar_"] button { min-height:2.9rem; }
.st-key-model_toolbar [data-baseweb="input"] { background:white; border-color:#ced8e5; }
.st-key-open_add_model button { color:var(--inventory-blue); border-color:var(--inventory-blue); }
.st-key-model_table, [class*="st-key-deployment_inventory_table_"] { border:1px solid var(--inventory-line); border-radius:7px; gap:0!important; overflow:hidden; }
.st-key-model_table_head, [class*="st-key-deployment_table_head_"] { background:#f5f7fa; padding:1rem 1.25rem; border-bottom:1px solid var(--inventory-line); }
.st-key-model_table_head p, [class*="st-key-deployment_table_head_"] p { font-size:.9rem; font-weight:650; color:#475468; margin:0; }
[class*="st-key-model_table_row_"], [class*="st-key-deployment_table_row_"] { padding:1.1rem 1.25rem; border-bottom:1px solid var(--inventory-line); min-height:92px; gap:.55rem!important; }
[class*="st-key-model_table_row_"]:hover, [class*="st-key-deployment_table_row_"]:hover { background:#fbfcfe; }
[class*="st-key-model_table_row_"] [data-testid="stVerticalBlock"], [class*="st-key-deployment_table_row_"] [data-testid="stVerticalBlock"] { gap:.3rem; }
[class*="st-key-model_table_row_"] [data-testid="stText"], [class*="st-key-deployment_table_row_"] [data-testid="stText"] { font-family:inherit; font-size:.95rem; overflow-wrap:anywhere; white-space:normal; }
[class*="st-key-model_name_"] button { border:0; background:transparent; padding:0; min-height:2.4rem; text-align:left; justify-content:flex-start; box-shadow:none; }
[class*="st-key-model_name_"] button p { font-size:1.06rem; font-weight:650; overflow-wrap:anywhere; }
[class*="st-key-model_name_"] button:hover { color:var(--inventory-blue); }
[class*="st-key-table_actions_"] [data-testid="stHorizontalBlock"] { gap:.45rem; flex-wrap:nowrap; }
[class*="st-key-table_actions_"] button { min-height:2.7rem; border-radius:5px; padding:.4rem .3rem; white-space:nowrap; }
[class*="st-key-row_deploy_"] button:not(:disabled), [class*="st-key-row_chat_"] button:not(:disabled) { color:var(--inventory-blue); border-color:var(--inventory-blue); background:#fff; }
[class*="st-key-row_delete_"] button, [class*="st-key-row_stop_"] button:not(:disabled) { color:#cf3434; }
[class*="st-key-table_actions_"] button:disabled { color:#a4afbc; border-color:#e1e7ef; background:#f4f6f8; }
[class*="st-key-table_footer_"] { padding:.7rem 1.25rem; }
[class*="st-key-table_footer_"] button { min-height:2.2rem; }
[class*="st-key-detail_panel_"] { border:1px solid var(--inventory-line); background:#f8faff; padding:1.2rem; border-radius:6px; margin:.5rem 0; }
@media (max-width:1100px) {
  .stMainBlockContainer { padding:1rem 1.1rem 2rem; }
  .st-key-top_nav [role="radiogroup"] { gap:.65rem; }
  [class*="st-key-model_table_row_"], [class*="st-key-deployment_table_row_"] { padding:.85rem .7rem; }
  .st-key-model_table_head, [class*="st-key-deployment_table_head_"] { padding:.85rem .7rem; }
  [class*="st-key-table_actions_"] button p { font-size:.8rem; }
}
@media (max-width:740px) {
  .st-key-model_table_head, [class*="st-key-deployment_table_head_"] { display:none; }
  .st-key-top_nav [role="radiogroup"] { flex-wrap:wrap; }
  [class*="st-key-table_actions_"] [data-testid="stColumn"] { min-width:0!important; flex:1 1 0!important; width:0!important; }
}
</style>
"""


def apply():
    # Fixed stylesheet only. Never interpolate model names or API data into HTML.
    st.html(CSS)


def model_heading():
    with st.container(key='model_table_head'):
        for column, label in zip(st.columns(MODEL_COLUMNS), ('등록 이름', 'Hugging Face 원본', '사용할 버전', '상태', '작업')):
            column.markdown(label)


def status_badge(value):
    import presentation as labels
    if not value:
        st.caption('버전 없음')
        return
    color = 'green' if value in ('ready', 'succeeded') else 'orange' if value in ('importing', 'queued', 'waiting_resources', 'preparing', 'starting', 'unknown') else 'red' if value in ('failed', 'unhealthy') else 'gray'
    st.badge(labels.state_label(value), color=color)


def revision_summary(source):
    fixed = (source.get('resolved_revision') or '')[:12] or '미확정'
    if source.get('derived'):
        return '기반 원본 · ' + fixed
    return (source.get('requested_revision') or '요청 버전 기록 없음') + ' · ' + fixed
