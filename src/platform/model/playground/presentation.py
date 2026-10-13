"""Public labels and recorded model origin; never guess a Hugging Face revision."""
from api import APIError
from state import identifier

KINDS = {'llm': '대화·문장 생성', 'embedding': '문서 검색용 임베딩', 'reranker': '검색 결과 재정렬', 'ocr': '이미지에서 글자 읽기'}
STATES = {'ready': '사용 가능', 'importing': '가져오는 중', 'created': '실행 대기',
          'pending': '접수됨', 'queued': '실행 대기', 'dispatching': '실행 준비 중',
          'running': '실행 중', 'succeeded': '완료', 'completed': '완료',
          'failed': '실패 · 확인 필요', 'cancelling': '취소 중', 'cancelled': '취소됨',
          'preparing': '모델 준비 중', 'starting': '모델 불러오는 중',
          'waiting_resources': 'GPU 사용 순서 대기', 'stopping': '중지 중', 'stopped': '중지됨',
          'unknown': '상태 확인 중', 'unhealthy': '실행 상태 확인 필요', 'exited': '실행 종료',
          'available': '조회 가능', 'retained': '보관됨', 'none': '아직 없음',
          'not_requested': '평가하지 않음', 'not_evaluated': '평가하지 않음'}
PHASES = {'resolving_source': 'Hugging Face 원본을 확인하고 있습니다.',
          'transferring_files': '모델 파일을 가져오고 저장하고 있습니다.',
          'writing_manifest': '파일 저장을 마쳤습니다. 모델 등록을 마무리하고 있습니다.',
          'queued': '실행 순서를 기다리고 있습니다.', 'dispatching': '실행을 준비하고 있습니다.',
          'waiting_resources': '다른 작업이 GPU 사용을 마치기를 기다립니다.',
          'execution_started': '작업이 시작됐습니다.', 'rendering_axolotl_config': '학습 설정을 준비하고 있습니다.',
          'axolotl_preprocess_validation': '학습 데이터를 확인하고 있습니다.',
          'axolotl_training': '모델을 학습하고 있습니다.',
          'building_training_manifest': '학습 결과를 정리하고 있습니다.',
          'uploading_training_artifact': '학습 결과를 저장하고 있습니다.',
          'verifying_training_artifact': '저장된 학습 결과를 확인하고 있습니다.',
          'serving_starting': '대화할 모델을 준비하고 있습니다.',
          'completed': '작업이 완료됐습니다.', 'failed': '작업을 마치지 못했습니다. 상세 정보를 확인하세요.',
          'cancelling': '작업을 취소하고 있습니다.', 'cancelled': '작업이 취소됐습니다.'}


def state_label(value):
    return '전체' if value == '전체' else STATES.get(value, '상세 정보에서 확인')


def artifact_label(version):
    return {'adapter': '학습 결과', 'snapshot': '원본 모델'}.get(version.get('artifact_kind'), '모델 파일')


def version_label(version):
    value = version.get('version') or '이름 없는 버전'
    if version.get('artifact_kind') == 'adapter' and value.startswith('train-'):
        try:
            return '학습 결과 · ' + identifier(value[6:])[:8]
        except ValueError:
            pass
    return value


def origin(version, fetch):
    """Follow only explicit base links. Broken/cyclic lineage is never a snapshot."""
    derived = version.get('artifact_kind') == 'adapter' or version.get('source_type') == 'training'
    result = {'derived': derived, 'repo_id': '', 'requested_revision': '', 'resolved_revision': '', 'problem': ''}
    current, seen = version, set()
    try:
        for _ in range(8):
            current_id = identifier(current['model_version_id'])
            if current_id in seen:
                raise ValueError('cyclic origin')
            seen.add(current_id)
            if current.get('artifact_kind') != 'adapter' and current.get('source_type') != 'training':
                for key in ('repo_id', 'requested_revision', 'resolved_revision'):
                    result[key] = current.get(key) or ''
                if not result['repo_id']:
                    result['problem'] = '저장된 원본 정보가 없습니다.'
                return result
            parent_id = identifier(current['source_model_version_id'])
            current = fetch(parent_id)
            if identifier(current['model_version_id']) != parent_id:
                raise ValueError('wrong origin response')
        raise ValueError('origin depth exceeded')
    except (APIError, KeyError, TypeError, ValueError):
        result['problem'] = '기반 원본을 확인하지 못했습니다. 다시 조회해 주세요.'
    return result


def revision_label(info):
    requested = info.get('requested_revision') or '기록 없음'
    resolved = info.get('resolved_revision')
    return '요청 버전: ' + requested + ' · 확정 버전: ' + (resolved[:12] if resolved else '미확정')
