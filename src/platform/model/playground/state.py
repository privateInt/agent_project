"""Pure session helpers; query parameters retain a start request across reloads."""
import base64
import json
import uuid

from api import APIError


def identifier(value):
    parsed = uuid.UUID(str(value))
    if parsed.int == 0:
        raise ValueError("empty identifier")
    return str(parsed)


def new_intent(model_version_id, gpu_id):
    if type(gpu_id) is not int or not 0 <= gpu_id <= 63:
        raise ValueError("GPU ID must be 0..63")
    return {"v": 1, "key": str(uuid.uuid4()), "model_version_id": identifier(model_version_id), "gpu_id": gpu_id}


def encode_intent(intent):
    return base64.urlsafe_b64encode(json.dumps(intent, separators=(",", ":")).encode()).decode()


def decode_intent(value):
    if len(value) > 1024:
        raise ValueError("invalid start request")
    data = json.loads(base64.b64decode(value, altchars=b"-_", validate=True))
    if not isinstance(data, dict) or set(data) != {"v", "key", "model_version_id", "gpu_id"} or type(data["v"]) is not int or data["v"] != 1:
        raise ValueError("unsupported start request")
    checked = new_intent(data["model_version_id"], data["gpu_id"])
    checked["key"] = identifier(data["key"])
    return checked


def deployment_body(intent):
    # Keep v1 defaults fixed: a saved URL must replay the same fingerprint.
    return {"model_version_id": intent["model_version_id"],
            "serving": {"engine": "vllm", "max_model_len": 2048, "gpu_memory_utilization": 0.5},
            "resources": {"gpu_ids": [intent["gpu_id"]]}}


def accept_created(intent, result):
    # Uncertain or malformed success retains the original request for replay.
    try:
        return identifier(result["deployment_id"]), identifier(result["operation_id"])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise APIError("시작 요청의 결과를 확인하지 못했습니다.", code="INVALID_RESPONSE") from None


RERANKER_PROFILE = "ko-reranker-v1"
RERANKER_REPOSITORY = "Dongjin-kr/ko-reranker"

EMBEDDING_PROFILE = "snowflake-ko-v1"
EMBEDDING_REPOSITORY = "dragonkue/snowflake-arctic-embed-l-v2.0-ko"


def embedding_model(model, version):
    return (model.get('model_type') in ('embedding', 'llm')
            and version.get('repo_id') == EMBEDDING_REPOSITORY
            and version.get('artifact_kind') == 'snapshot')


def reranker_model(model, version):
    return (model.get('model_type') in ('reranker', 'llm')
            and version.get('repo_id') == RERANKER_REPOSITORY
            and version.get('artifact_kind') == 'snapshot')


def can_train_model(model, version):
    return model.get('model_type') == 'llm' and not embedding_model(model, version) and version.get('repo_id') != RERANKER_REPOSITORY


def can_deploy_model(model, version):
    if version.get('repo_id') == RERANKER_REPOSITORY:
        return reranker_model(model, version)
    return embedding_model(model, version) or reranker_model(model, version) or model.get('model_type') == 'llm'


def inference_kind(deployment, info=None):
    profile = (deployment.get('serving') or {}).get('profile', '')
    if profile == RERANKER_PROFILE:
        return 'reranker'
    if profile == EMBEDDING_PROFILE:
        return 'embedding'
    if profile == 'surya-ocr-v2':
        return 'ocr'
    if profile:
        return 'unknown'
    if info is not None:
        origin = info.get('origin') or {}
        if origin.get('repo_id') == RERANKER_REPOSITORY:
            return 'reranker'
        if origin.get('repo_id') == EMBEDDING_REPOSITORY:
            return 'embedding'
        return (info.get('model') or {}).get('model_type') or 'unknown'
    return 'llm'


def can_embed(deployment, info=None):
    return (inference_kind(deployment, info) == 'embedding' and not deployment.get('deleted_at')
            and deployment.get('status') == 'ready' and deployment.get('runtime_state') == 'ready'
            and deployment.get('desired_state') == 'running'
            and (deployment.get('serving') or {}).get('engine') == 'vllm')


def can_rerank(deployment, info=None):
    return (inference_kind(deployment, info) == 'reranker' and not deployment.get('deleted_at')
            and deployment.get('status') == 'ready' and deployment.get('runtime_state') == 'ready'
            and deployment.get('desired_state') == 'running'
            and (deployment.get('serving') or {}).get('engine') == 'vllm'
            and (deployment.get('serving') or {}).get('profile') == RERANKER_PROFILE)


def can_chat(deployment, info=None):
    serving = deployment.get("serving")
    return (not deployment.get("deleted_at") and deployment.get("status") == "ready" and deployment.get("runtime_state") == "ready"
            and deployment.get("desired_state") == "running" and isinstance(serving, dict)
            and serving.get("engine") == "vllm" and inference_kind(deployment, info) == "llm")


def status_text(deployment, operation=None, info=None):
    if deployment.get("deleted_at"): return "삭제됨", "배포 컨테이너와 전용 캐시를 삭제했습니다. 실행 이력은 보존됩니다."
    runtime = deployment.get("runtime_state") or "queued"
    if runtime == "stopped": return "종료됨", "모델 실행을 중지했습니다."
    if deployment.get("desired_state") == "stopped" or runtime == "stopping": return "종료 중", "모델을 중지하고 GPU를 반환하고 있습니다."
    if can_rerank(deployment, info): return "재정렬용", "질문과 검색 후보의 관련성 점수를 계산합니다. 아래에서 점수를 확인할 수 있습니다."
    if runtime == "ready" and inference_kind(deployment, info) == "reranker":
        return "재정렬 설정 필요", "이 배포에는 재정렬 전용 설정이 없습니다. 모델 목록에서 해당 버전을 새로 배포해주세요."
    if can_embed(deployment, info): return "임베딩용", "문서·질문을 검색용 벡터로 변환합니다. 아래에서 임베딩 API를 확인할 수 있습니다."
    if can_chat(deployment, info): return "대화 가능", "모델이 준비됐습니다. 채팅 페이지에서 질문할 수 있습니다."
    if runtime == "ready" and isinstance(deployment.get("serving"), dict) and deployment["serving"].get("profile") == "surya-ocr-v2":
        return "글자 인식용", "데이터 관리에서 OCR 모델로 선택해 사용할 수 있습니다."
    if deployment.get('runtime_error_code') == 'GPU_OUT_OF_MEMORY':
        return 'GPU 메모리 부족', '모델을 불러올 GPU 메모리가 부족합니다. 메모리 사용 비율을 낮추거나 다른 배포를 중지한 뒤 새로 배포해주세요. 자동 재시작하지 않습니다.'
    if deployment.get('runtime_error_code') == 'RUNTIME_HOST_OOM':
        return '서버 메모리 부족', '서버 메모리 제한으로 모델 실행이 종료됐습니다. 배포 오류를 확인해주세요.'
    if runtime in ("failed", "unhealthy", "exited") or deployment.get("status") == "failed" or deployment.get("submission_status") == "blocked":
        return "확인 필요", "모델을 실행하지 못했습니다. 배포 관리에서 중지한 뒤 오류를 확인해 주세요."
    if (operation or {}).get("status") in ("failed", "cancelled"):
        return "확인 필요", "시작 작업이 완료되지 않았습니다. 배포 관리에서 중지한 뒤 오류를 확인해 주세요."
    if runtime == "waiting_resources" or (operation or {}).get("phase") == "waiting_resources":
        return "GPU 대기", "다른 작업이 끝나기를 기다리고 있습니다. 배포 관리에서 대기를 중지할 수 있습니다."
    return {"queued": ("요청 전달 중", "모델 시작 요청을 전달하고 있습니다."),
            "preparing": ("모델 준비 중", "모델 파일을 준비하고 있습니다. 처음 실행하면 시간이 걸릴 수 있습니다."),
            "starting": ("모델 로딩 중", "모델을 GPU에 불러오고 있습니다."),
            "unknown": ("시작 확인 중", "모델의 시작 여부를 확인하고 있습니다.")}.get(runtime, ("상태 확인 중", "현재 실행 상태를 확인하고 있습니다."))


def error_text(error, starting=False):
    if starting and (not error.status or error.status >= 500):
        return "시작 요청의 결과를 확인하지 못했습니다. ‘같은 시작 요청 확인’을 눌러 이어서 확인해 주세요."
    return {"RERANK_NOT_READY": "재정렬 배포가 준비되지 않았거나 전용 설정으로 배포되지 않았습니다.",
            "RERANK_UNSUPPORTED_PROFILE": "모델·토크나이저 구성이 지원하는 재정렬 모델과 일치하지 않습니다.",
            "RERANK_PROFILE_MISMATCH": "재정렬 모델 정보가 달라졌습니다. 상태를 다시 조회해 주세요.",
            "RERANK_INPUT_TOO_LONG": "질문과 후보를 합친 길이가 512토큰을 넘었습니다. 내용을 줄여주세요. 자동으로 자르지 않습니다.",
            "RERANK_INPUT_TOO_LARGE": "질문·후보 텍스트가 너무 큽니다.",
            "RERANK_INVALID_REQUEST": "질문과 고유 ID를 가진 후보를 확인하세요.",
            "RERANK_INPUT_REJECTED": "모델이 입력을 거절했습니다. 길이와 배포 상태를 확인하세요.",
            "RERANK_INVALID_RESPONSE": "후보에 대응하는 정상 점수 응답을 확인하지 못했습니다.",
            "RERANK_UNAVAILABLE": "재정렬 모델에 연결하지 못했습니다.",
            "RERANK_TIMEOUT": "재정렬 요청 시간이 초과됐습니다. 자동 재시도하지 않았습니다.",
            "RERANK_IDENTITY_MISMATCH": "요청 중 배포 정보가 변경됐습니다.",
            "RERANK_TOKENIZER_UNAVAILABLE": "배포에서 토큰 수를 확인할 수 없습니다.",
            "EMBEDDING_NOT_READY": "임베딩 배포가 준비되지 않았거나 중지됐습니다.",
            "EMBEDDING_UNSUPPORTED_PROFILE": "이 배포의 모델·토크나이저 구성이 지원 profile과 일치하지 않습니다. 임베딩 확인 문서를 확인해 주세요.",
            "EMBEDDING_SPACE_MISMATCH": "임베딩 공간이 달라졌습니다. 사용할 모델을 확인하고 다시 조회해 주세요.",
            "EMBEDDING_INPUT_TOO_LONG": "입력이 모델의 토큰 한도를 넘었습니다. 내용을 줄여주세요. 자동으로 자르지 않습니다.",
            "EMBEDDING_INPUT_TOO_LARGE": "입력 텍스트가 너무 큽니다. 내용을 줄여주세요.",
            "EMBEDDING_INVALID_REQUEST": "역할과 입력 내용을 확인해주세요.",
            "EMBEDDING_INPUT_REJECTED": "모델이 입력을 처리하지 못했습니다. 입력 길이와 실행 상태를 확인해주세요.",
            "EMBEDDING_UNAVAILABLE": "임베딩 모델에 연결하지 못했습니다. 배포 상태를 확인해주세요.",
            "EMBEDDING_TIMEOUT": "임베딩 요청 시간이 초과됐습니다. 자동 재시도하지 않았습니다.",
            "EMBEDDING_INVALID_RESPONSE": "임베딩 응답의 차원·개수·정규화를 확인하지 못했습니다.",
            "EMBEDDING_TOKENIZER_UNAVAILABLE": "배포에서 토큰 수를 확인할 수 없어 요청을 중단했습니다.",
            "EMBEDDING_IDENTITY_MISMATCH": "요청 중 배포 정보가 변경됐습니다. 상태를 확인해주세요.",
            "DEPLOYMENT_NOT_READY": "모델이 아직 준비되지 않았거나 종료됐습니다. 상태를 다시 확인해 주세요.",
            "CHAT_CAPABILITY_REQUIRED": "이 배포는 채팅용 모델이 아닙니다. 배포 상세에서 용도에 맞는 기능을 선택하세요.",
            "CHAT_INPUT_REJECTED": "대화가 모델의 처리 한도를 넘었을 수 있습니다. 질문을 줄이거나 새 대화를 시작해 주세요.",
            "INVALID_CHAT": "질문이나 대화가 너무 깁니다. 질문을 줄이거나 새 대화를 시작해 주세요.",
            "INFERENCE_TIMEOUT": "답변을 기다리는 시간이 초과됐습니다. 자동으로 다시 보내지 않았습니다.",
            "INFERENCE_UNAVAILABLE": "모델에 연결하지 못했습니다. 실행 상태를 확인한 뒤 다시 보내 주세요.",
            "INVALID_INFERENCE_RESPONSE": "선택한 모델의 정상 응답을 확인하지 못했습니다.",
            "CONNECTION_ERROR": "서버에 연결하지 못했습니다. 잠시 후 다시 확인해 주세요.",
            "INVALID_RESPONSE": "서버 응답을 확인하지 못했습니다. 잠시 후 다시 확인해 주세요."}.get(error.code,
            "대상을 찾을 수 없습니다." if error.status == 404 else "요청을 처리하지 못했습니다. 상태를 확인한 뒤 다시 시도해 주세요.")


def answer_messages(history, prompt):
    messages = [{"role": m["role"], "content": m["content"]} for m in history] + [{"role": "user", "content": prompt}]
    if not prompt.strip() or len(messages) > 33 or sum(len(m["content"]) for m in messages) > 16000:
        raise APIError("대화 길이 한도입니다.", 400, "INVALID_CHAT")
    return messages


def accept_answer(deployment_id, response):
    if (response.get("deployment_id") != deployment_id or response.get("model") != "deployment-" + deployment_id
            or not isinstance(response.get("content"), str) or not response["content"].strip()
            or response.get("finish_reason") not in ("stop", "length")):
        raise APIError("선택한 모델의 응답을 확인하지 못했습니다.", 502, "INVALID_INFERENCE_RESPONSE")
    return {"role": "assistant", "content": response["content"], "truncated": response["finish_reason"] == "length"}
