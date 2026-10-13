"""Show measured transfer progress without inventing an overall job percentage."""
from datetime import datetime, timezone
import streamlit as st


def byte_label(value):
    value = max(0, int(value))
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def transfer_display(op):
    progress = op.get('progress') or {}
    if not progress:
        return None
    done = max(0, int(progress.get('bytes_completed') or 0))
    total = max(0, int(progress.get('bytes_total') or 0))
    stage = progress.get('stage')
    ratio = min(done / total, 1.0) if stage == 'downloading' and total > 0 and op.get('status') == 'running' else None
    return {'file': progress.get('current_file') or '', 'ratio': ratio,
            'stage': {'downloading': '현재 파일 다운로드', 'uploading': '현재 파일 저장 중', 'stored': '파일 저장 완료'}.get(stage, '파일 전송'),
            'amount': byte_label(done) + (' / ' + byte_label(total) if total > 0 else (' · 전체 크기 확인 불가' if stage == 'downloading' else '')),
            'files': f"전체 {int(progress.get('files_total') or 0)}개 중 {int(progress.get('files_completed') or 0)}개 저장 완료"}


def compact_progress(op):
    """A current-file percentage, never a fabricated overall-import percentage."""
    p = op.get('progress') or {}
    if not p:
        return {'pending': '시작 대기', 'running': '가져오는 중', 'failed': '실패 처리 중',
                'cancelled': '취소 처리 중', 'succeeded': '완료 확인 중'}.get(op.get('status'), '진행 상태 확인 중')
    measured = transfer_display(op)
    if op.get('status') != 'running':
        return {'failed': '실패 처리 중', 'cancelled': '취소 처리 중', 'succeeded': '완료 확인 중'}.get(op.get('status'), '시작 대기')
    text = (f"현재 파일 {measured['ratio']:.0%}" if measured['ratio'] is not None else
            ('다운로드 ' + byte_label(p.get('bytes_completed') or 0) if p.get('stage') == 'downloading' else measured['stage']))
    total = max(0, int(p.get('files_total') or 0))
    done = max(0, int(p.get('files_completed') or 0))
    if total:
        text += f' · {done}/{total}개 저장'
    return text


def operation_progress(op):
    measured = transfer_display(op)
    if measured:
        st.text(measured['stage'] + ' · ' + measured['file'])
        if measured['ratio'] is not None:
            st.progress(measured['ratio'], text=f"현재 파일 다운로드 {measured['ratio']:.1%} · {measured['amount']}")
        else:
            st.caption(measured['amount'])
        st.caption(measured['files'])
        try:
            reported = datetime.fromisoformat(str(op['progress_updated_at']).replace('Z', '+00:00'))
            age = max(0, int((datetime.now(timezone.utc) - reported).total_seconds()))
            if op.get('status') == 'running':
                st.caption(f'전송량 마지막 갱신: {age}초 전')
        except (KeyError, TypeError, ValueError):
            st.caption('마지막으로 보고된 전송량입니다.')
    elif op.get('phase') == 'transferring_files':
        st.caption('파일을 가져오는 중입니다. 이 작업은 전송량 정보를 제공하지 않습니다.')
    if op.get('error_code'):
        message = str(op.get('error_message') or '')
        if op['error_code'] == 'GPU_BUSY' and op.get('status') not in ('failed', 'cancelled'):
            st.info('GPU 사용 순서를 기다리고 있습니다.')
        elif op.get('status') not in ('failed', 'cancelled'):
            st.info('작업 상태에 확인할 메시지가 있습니다. 작업 상세를 확인해 주세요.')
        elif op['error_code'] == 'TRANSFER_FAILED' and message.startswith('download ') and ('status 401' in message or 'status 403' in message):
            st.error('Hugging Face 다운로드 권한을 확인해 주세요. 모델 접근 승인을 받은 계정의 읽기 토큰이 필요합니다.')
        else:
            st.warning('작업을 마치지 못했습니다. 작업 상세에서 원인을 확인해 주세요.')
        st.caption('오류 코드: ' + op['error_code'])
