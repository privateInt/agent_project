"""Q01 pure contracts. No HTTP client, database, workflow or service control."""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Optional
from uuid import UUID, uuid5

MAX_BYTES = 32 << 20
MAX_DOCUMENTS = 20
MAX_CHUNKS = 20000
NAMESPACE = UUID("1892c2d1-c708-5e99-8a35-6a4c9976fe97")
OPERATIONS = {
    "data.upload": ("POST", "/api/source-documents", 200, "document_id"),
    "data.refine": ("POST", "/api/refinement-runs", 202, "refinement_run_id"),
    "knowledge.build": ("POST", "/api/indexes/{id}/builds", 202, "index_version_id"),
    "agent.tool_call": ("POST", "/api/tool-calls", 202, "tool_call_id"),
    "agent.run": ("POST", "/api/agent-runs", 202, "agent_run_id"),
}
KEYED_OPERATIONS = frozenset(OPERATIONS) | {
    "knowledge.create", "agent.connection.create", "agent.connection.version",
    "agent.tool.create", "agent.tool.version", "agent.tool.publish",
    "agent.agent.create", "agent.agent.version", "agent.agent.publish",
}


class ContractError(ValueError):
    """A receipt/state/identity cannot safely advance the document QA job."""


def require(condition, code):
    if not condition:
        raise ContractError(code)


def uid(value):
    try:
        parsed = UUID(value) if isinstance(value, str) else None
        return parsed is not None and parsed.int != 0 and str(parsed) == value
    except ValueError:
        return False


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def integer(value, minimum=0, maximum=None):
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def strict_json(raw):
    """Decode bounded JSON and reject duplicate keys, nonfinite numbers and trailing bytes."""
    require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES, "JSON_SIZE_INVALID")

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "JSON_DUPLICATE_KEY")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: require(False, "JSON_NONFINITE"))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ContractError("JSON_INVALID") from exc
    require(isinstance(value, dict), "JSON_OBJECT_REQUIRED")
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        require(depth <= 128, "JSON_DEPTH_EXCEEDED")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
            pending.extend((k, depth + 1) for k in item)
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise ContractError("JSON_INVALID_UTF8") from exc
    return value


def fingerprint(body):
    require(isinstance(body, dict), "REQUEST_OBJECT_REQUIRED")
    try:
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ContractError("REQUEST_INVALID") from exc
    return hashlib.sha256(raw).hexdigest()


def validate_request(operation, raw):
    """Validate the initial MVP subset, not a replacement for platform validators."""
    body = strict_json(raw)
    shapes = {
        "data.refine": ({"name", "document_id"}, {"rules_version", "ocr_deployment_id"}),
        "knowledge.build": (set(), {"chunking_run_id", "chunking", "canonical_ids", "parent_version_id", "embedding_deployment_id", "hnsw"}),
        "knowledge.activate": ({"index_version_id", "expected_active_version_id", "expected_revision"}, set()),
        "agent.tool_call": ({"tool_version_id", "arguments", "mode"}, {"caller_context"}),
        "agent.run": ({"agent_version_id", "input", "mode"}, {"caller_context"}),
    }
    require(operation in shapes, "REQUEST_CONTRACT_UNAVAILABLE")
    required, optional = shapes[operation]
    require(required <= body.keys() <= required | optional and all(v is not None for v in body.values()), "REQUEST_FIELDS_INVALID")
    if operation == "data.refine":
        require(isinstance(body["name"], str) and bool(body["name"].strip())
                and uid(body["document_id"]), "REFINEMENT_REQUEST_INVALID")
        require(all(isinstance(body[k], str) for k in optional if k in body), "REFINEMENT_REQUEST_INVALID")
        require(not body.get("ocr_deployment_id") or uid(body["ocr_deployment_id"]), "REFINEMENT_REQUEST_INVALID")
    elif operation == "knowledge.activate":
        require(uid(body["index_version_id"]) and (body["expected_active_version_id"] == "" or uid(body["expected_active_version_id"]))
                and integer(body["expected_revision"], 1), "ACTIVATION_REQUEST_INVALID")
    elif operation == "knowledge.build":
        run = body.get("chunking_run_id", "")
        require((uid(run) and "chunking" not in body) or (not run and isinstance(body.get("chunking"), dict)), "BUILD_INPUT_EXCLUSIVE")
        for k in ("parent_version_id", "embedding_deployment_id"):
            require(k not in body or uid(body[k]), "BUILD_REQUEST_INVALID")
        if "chunking" in body:
            require("canonical_ids" not in body, "BUILD_SUBSET_INVALID")
            chunking = body["chunking"]
            require(set(chunking) == {"name", "canonical_ids", "chunker"}
                    and isinstance(chunking["name"], str) and bool(chunking["name"].strip())
                    and isinstance(chunking["chunker"], dict)
                    and set(chunking["chunker"]) == {"id", "options"}
                    and isinstance(chunking["chunker"]["id"], str) and bool(chunking["chunker"]["id"])
                    and isinstance(chunking["chunker"]["options"], dict), "CHUNKING_REQUEST_INVALID")
            ids = chunking["canonical_ids"]
        else:
            ids = body.get("canonical_ids", [])
        require(isinstance(ids, list) and len(ids) <= MAX_DOCUMENTS and all(uid(i) for i in ids)
                and len(set(ids)) == len(ids) and ("chunking" not in body or len(ids) > 0), "BUILD_DOCUMENTS_INVALID")
    else:
        tool = operation == "agent.tool_call"
        version_field, input_field, question_field = ("tool_version_id", "arguments", "query") if tool else ("agent_version_id", "input", "question")
        inp = body[input_field]
        require(uid(body[version_field]) and isinstance(body["mode"], str) and body["mode"] in {"test", "run"}
                and isinstance(inp, dict) and set(inp) == {question_field}, "AGENT_REQUEST_INVALID")
        question = inp[question_field]
        require(isinstance(question, str) and bool(question.strip()) and "\0" not in question
                and len(question.encode("utf-8")) <= 8192, "QUESTION_INVALID")
        if "caller_context" in body:
            context = body["caller_context"]
            require(isinstance(context, dict) and set(context) <= {"system", "workflow_id", "execution_id", "node_id"}
                    and all(isinstance(v, str) for v in context.values()), "CALLER_CONTEXT_INVALID")
    return body


def upload_metadata(name, filename, size_bytes, sha256, reserved_document_count):
    """Pre-acceptance contract. Q03 must reserve ready+pending membership atomically."""
    require(isinstance(name, str) and bool(name.strip()) and "\0" not in name
            and len(name.encode("utf-8")) <= 800, "UPLOAD_NAME_INVALID")
    require(isinstance(filename, str) and bool(filename) and "\0" not in filename
            and "/" not in filename and "\\" not in filename
            and filename.rsplit(".", 1)[-1].lower() in {"pdf", "txt", "md", "markdown"}, "UPLOAD_FORMAT_UNSUPPORTED")
    require(integer(size_bytes, 1, MAX_BYTES) and digest(sha256), "UPLOAD_BYTES_INVALID")
    require(integer(reserved_document_count, 0, MAX_DOCUMENTS - 1), "DOCUMENT_CAPACITY_EXCEEDED")
    return {"name": name, "filename": filename, "size_bytes": size_bytes, "sha256": sha256}


@dataclass(frozen=True)
class Intent:
    job_id: str
    operation: str
    revision: int
    attempt: int
    request_sha256: str
    key: str


def request_intent(job_id, operation, revision, body, attempt=1):
    require(uid(job_id) and integer(revision, 1) and integer(attempt, 1), "INTENT_INVALID")
    require(operation in KEYED_OPERATIONS, "OPERATION_UNKNOWN")
    # Stable across process/n8n redelivery; payload changes must conflict, not mint a new key.
    key = str(uuid5(NAMESPACE, f"q01:{job_id}:{revision}:{operation}:{attempt}"))
    return Intent(job_id, operation, revision, attempt, fingerprint(body), key)


def reuse_intent(intent, body):
    require(fingerprint(body) == intent.request_sha256, "REQUEST_KEY_CONFLICT")
    return intent.key


@dataclass(frozen=True)
class Receipt:
    resource_id: str
    operation_id: str = ""
    version_id: str = ""
    status_url: str = ""


def acceptance(operation, http_status, raw, expected):
    require(operation in OPERATIONS, "OPERATION_UNKNOWN")
    _, _, wanted_status, field = OPERATIONS[operation]
    require(type(http_status) is int and http_status == wanted_status, "HTTP_RECEIPT_INVALID")
    body = strict_json(raw)
    resource = body.get(field)
    require(uid(resource), "RESOURCE_ID_INVALID")
    for key, value in expected.items():
        require(body.get(key) == value, "RECEIPT_IDENTITY_MISMATCH")
    if operation == "data.upload":
        require(body.get("state") == "ready" and digest(body.get("sha256"))
                and integer(body.get("size_bytes"), 1, MAX_BYTES), "UPLOAD_NOT_READY")
        require(not body.get("deleted_at") and not body.get("purge_state"), "SOURCE_UNAVAILABLE")
        return Receipt(resource)
    if operation == "data.refine":
        require(body.get("status") == "accepted" and uid(body.get("operation_id")), "REFINEMENT_NOT_ACCEPTED")
        return Receipt(resource, body["operation_id"])
    if operation == "knowledge.build":
        require(isinstance(body.get("state"), str) and body.get("state") in {"protecting", "queued", "indexing", "ready"}
                and uid(body.get("index_id")) and uid(body.get("operation_id"))
                and uid(body.get("chunking_run_id")), "BUILD_NOT_ACCEPTED")
        return Receipt(resource, body["operation_id"])
    version_field = "tool_version_id" if operation == "agent.tool_call" else "agent_version_id"
    url = ("/api/tool-calls/" if operation == "agent.tool_call" else "/api/agent-runs/") + resource
    require(body.get("state") == "accepted" and uid(body.get(version_field))
            and body.get("status_url") == url, "AGENT_NOT_ACCEPTED")
    return Receipt(resource, version_id=body[version_field], status_url=url)


def observe_refinement(receipt, body, source_id):
    require(body.get("refinement_run_id") == receipt.resource_id
            and body.get("operation_id") == receipt.operation_id
            and body.get("source_document_id") == source_id, "REFINEMENT_IDENTITY_MISMATCH")
    require(not body.get("deleted_at") and not body.get("purge_state"), "REFINEMENT_UNAVAILABLE")
    state = body.get("status")
    require(isinstance(state, str), "REFINEMENT_RESULT_INVALID")
    if state in {"created", "pending", "dispatching", "running", "retry_wait", "cancelling"}:
        return "waiting"
    if state in {"failed", "cancelled"}:
        return "failed"
    require(state == "succeeded" and not body.get("error_code")
            and uid(body.get("output_document_id")), "REFINEMENT_RESULT_INVALID")
    return "complete"  # Canonical metadata/content is still a separate gate.


@dataclass(frozen=True)
class CanonicalRef:
    canonical_id: str
    source_id: str
    sha256: str
    size_bytes: int


def canonical_reference(meta, raw, receipt, source_id, source_sha256, output_document_id):
    require(uid(output_document_id) and meta.get("document_id") == output_document_id
            and meta.get("refinement_run_id") == receipt.resource_id
            and meta.get("source_document_id") == source_id
            and meta.get("schema_version") == "canonical-document-v1", "CANONICAL_IDENTITY_MISMATCH")
    require(isinstance(raw, bytes) and integer(meta.get("size_bytes"), 1, MAX_BYTES)
            and len(raw) == meta["size_bytes"] and digest(meta.get("sha256"))
            and hashlib.sha256(raw).hexdigest() == meta["sha256"], "CANONICAL_BYTES_MISMATCH")
    body = strict_json(raw)
    source = body.get("source", {})
    quality = body.get("quality", {})
    require(body.get("schema_version") == "canonical-document-v1" and isinstance(source, dict)
            and source.get("document_id") == source_id and source.get("sha256") == source_sha256,
            "CANONICAL_CONTENT_IDENTITY_MISMATCH")
    require(isinstance(quality, dict) and quality.get("state") == meta.get("state")
            and isinstance(quality.get("issues"), list), "CANONICAL_QUALITY_INVALID")
    # Initial MVP stops on partial/needs-review results; no silent parser/OCR fallback.
    require(meta.get("state") == "ready"
            and all(isinstance(i, dict) and not i.get("requires_ocr") for i in quality["issues"]),
            "CANONICAL_ATTENTION_REQUIRED")
    blocks = body.get("blocks")
    require(isinstance(blocks, list) and any(isinstance(b, dict)
            and isinstance(b.get("text"), str) and b["text"].strip() for b in blocks), "CANONICAL_EMPTY")
    return CanonicalRef(meta["document_id"], source_id, meta["sha256"], meta["size_bytes"])


def document_snapshot(documents):
    require(isinstance(documents, tuple) and 1 <= len(documents) <= MAX_DOCUMENTS, "DOCUMENT_COUNT_INVALID")
    require(all(isinstance(d, CanonicalRef) and uid(d.canonical_id) and uid(d.source_id)
                and digest(d.sha256) and integer(d.size_bytes, 1, MAX_BYTES) for d in documents), "DOCUMENT_INVALID")
    require(len({d.canonical_id for d in documents}) == len(documents)
            and len({d.source_id for d in documents}) == len(documents), "DOCUMENT_DUPLICATE")
    require(sum(d.size_bytes for d in documents) <= MAX_BYTES, "DOCUMENT_BYTES_EXCEEDED")
    return tuple(sorted(documents, key=lambda d: d.canonical_id))


def observe_index(receipt, body, index_id, documents, deployment_id, space_id):
    documents = document_snapshot(documents)
    require(body.get("index_version_id") == receipt.resource_id and body.get("index_id") == index_id
            and body.get("operation_id") == receipt.operation_id, "INDEX_IDENTITY_MISMATCH")
    state = body.get("state")
    require(isinstance(state, str), "INDEX_NOT_READY")
    if state in {"protecting", "queued", "indexing"}:
        return "waiting"
    if state == "failed":
        return "failed"
    require(state == "ready" and not body.get("error_code") and body.get("reference_state") == "held"
            and integer(body.get("chunk_count"), 1, MAX_CHUNKS)
            and type(body.get("indexed_count")) is int and body["indexed_count"] == body["chunk_count"], "INDEX_NOT_READY")
    ids = body.get("canonical_ids")
    require(isinstance(ids, list) and all(uid(i) for i in ids)
            and sorted(ids) == [d.canonical_id for d in documents], "INDEX_DOCUMENTS_MISMATCH")
    profile = body.get("embedding_profile", {})
    require(isinstance(profile, dict) and profile.get("deployment_id") == deployment_id
            and profile.get("embedding_space_id") == space_id, "INDEX_EMBEDDING_MISMATCH")
    return "complete"


def observe_agent_run(receipt, body, mode):
    require(body.get("agent_run_id") == receipt.resource_id and body.get("agent_version_id") == receipt.version_id
            and body.get("mode") == mode and mode in {"test", "run"}, "AGENT_RUN_IDENTITY_MISMATCH")
    state = body.get("state")
    require(isinstance(state, str), "AGENT_RUN_STATE_INVALID")
    if state in {"accepted", "running"}:
        return "waiting"
    if state in {"failed", "interrupted"}:
        return "failed"
    require(state == "succeeded" and not body.get("error"), "AGENT_RUN_STATE_INVALID")
    result = body.get("result")
    require(isinstance(result, dict) and isinstance(result.get("outcome"), str) and result.get("outcome") in {"answered", "no_evidence"}
            and isinstance(result.get("answer"), str) and bool(result["answer"].strip())
            and isinstance(result.get("citations"), list) and isinstance(result.get("warnings"), list)
            and type(result.get("answer_truncated")) is bool and uid(result.get("tool_call_id"))
            and isinstance(result.get("identity"), dict), "AGENT_RESULT_INVALID")
    require(bool(result["citations"]) == (result["outcome"] == "answered"), "AGENT_CITATIONS_INVALID")
    return "complete"  # Transport/shape success does not claim semantic quality.


def recovery_action(operation, state, receipt_known=False):
    require(operation in KEYED_OPERATIONS or operation == "knowledge.activate", "OPERATION_UNKNOWN")
    require(state in {"complete", "waiting", "uncertain", "failed", "attention_required"}, "RECOVERY_STATE_INVALID")
    if state == "complete":
        return "reuse_completed_receipt"
    if state in {"failed", "attention_required"}:
        return "review_before_new_attempt"
    if operation == "knowledge.activate":
        return "read_index_and_compare_revision"
    return "get_existing_resource" if receipt_known else "replay_same_key_and_body"


@dataclass(frozen=True)
class QuestionHead:
    library_id: str
    revision: int
    documents: tuple
    index_id: str
    index_version_id: str
    embedding_deployment_id: str
    embedding_space_id: str
    tool_version_id: str
    agent_version_id: str


@dataclass(frozen=True)
class VerifiedHead:
    head: QuestionHead


def validate_head(head):
    require(isinstance(head, QuestionHead) and uid(head.library_id) and integer(head.revision, 1), "HEAD_INVALID")
    document_snapshot(head.documents)
    require(all(uid(v) for v in (head.index_id, head.index_version_id, head.embedding_deployment_id,
                                head.tool_version_id, head.agent_version_id))
            and isinstance(head.embedding_space_id, str) and head.embedding_space_id.startswith("es_")
            and digest(head.embedding_space_id[3:]), "HEAD_BINDING_INVALID")


def verify_bindings(head, index, version, tool, agent, tool_test, agent_test):
    validate_head(head)
    require(index.get("index_id") == head.index_id and index.get("state") == "active"
            and index.get("active_version_id") == head.index_version_id and integer(index.get("revision"), 1), "INDEX_ACTIVATION_UNVERIFIED")
    receipt = Receipt(head.index_version_id, version.get("operation_id", ""))
    require(uid(receipt.operation_id) and observe_index(receipt, version, head.index_id, head.documents,
            head.embedding_deployment_id, head.embedding_space_id) == "complete", "INDEX_COMPLETION_UNVERIFIED")
    binding = tool.get("binding", {})
    knowledge = binding.get("knowledge", {}) if isinstance(binding, dict) else {}
    require(tool.get("tool_version_id") == head.tool_version_id and tool.get("state") == "published"
            and tool.get("kind") == "knowledge_search_v1" and isinstance(knowledge, dict)
            and knowledge.get("index_id") == head.index_id and knowledge.get("index_version_id") == head.index_version_id
            and knowledge.get("embedding_deployment_id") == head.embedding_deployment_id
            and knowledge.get("embedding_space_id") == head.embedding_space_id, "TOOL_BINDING_UNVERIFIED")
    require(agent.get("agent_version_id") == head.agent_version_id and agent.get("state") == "published"
            and agent.get("template_id") == "knowledge_qa_v1" and agent.get("template_version") == 1
            and agent.get("tool_version_id") == head.tool_version_id, "AGENT_BINDING_UNVERIFIED")
    require(tool_test.get("tool_version_id") == head.tool_version_id and tool_test.get("mode") == "test"
            and uid(tool_test.get("tool_call_id")) and tool_test.get("state") == "succeeded"
            and not tool_test.get("error"), "TOOL_TEST_UNVERIFIED")
    result = tool_test.get("result", {})
    data = result.get("data", {}) if isinstance(result, dict) else {}
    metadata = result.get("metadata", {}) if isinstance(result, dict) else {}
    profile = metadata.get("embedding_profile", {}) if isinstance(metadata, dict) else {}
    require(isinstance(result, dict) and result.get("completion") == "completed" and isinstance(data, dict)
            and data.get("index_id") == head.index_id and data.get("index_version_id") == head.index_version_id
            and data.get("embedding_space_id") == head.embedding_space_id
            and isinstance(data.get("results"), list) and integer(data.get("returned_count"), 0, 50)
            and data["returned_count"] == len(data["results"])
            and isinstance(metadata, dict) and metadata.get("index_version_id") == head.index_version_id
            and metadata.get("adapter") == "knowledge_search_v1"
            and uid(binding.get("connection_version_id")) and uid(knowledge.get("model_connection_version_id"))
            and metadata.get("connection_version_id") == binding["connection_version_id"]
            and metadata.get("model_connection_version_id") == knowledge["model_connection_version_id"]
            and isinstance(profile, dict) and profile.get("deployment_id") == head.embedding_deployment_id
            and profile.get("embedding_space_id") == head.embedding_space_id, "TOOL_TEST_IDENTITY_MISMATCH")
    require(uid(agent_test.get("agent_run_id")), "AGENT_TEST_UNVERIFIED")
    receipt = Receipt(agent_test["agent_run_id"], version_id=head.agent_version_id)
    require(observe_agent_run(receipt, agent_test, "test") == "complete", "AGENT_TEST_UNVERIFIED")
    identity = agent_test["result"]["identity"]
    agent_metadata = identity.get("knowledge", {})
    agent_profile = agent_metadata.get("embedding_profile", {}) if isinstance(agent_metadata, dict) else {}
    require(identity.get("template_id") == "knowledge_qa_v1" and identity.get("template_version") == 1
            and identity.get("tool_version_id") == head.tool_version_id and isinstance(agent_metadata, dict)
            and all(agent_metadata.get(k) == metadata[k] for k in ("adapter", "connection_version_id", "model_connection_version_id", "index_version_id"))
            and isinstance(agent_profile, dict) and agent_profile.get("deployment_id") == head.embedding_deployment_id
            and agent_profile.get("embedding_space_id") == head.embedding_space_id,
            "AGENT_TEST_IDENTITY_MISMATCH")
    return VerifiedHead(head)


def switch_head(current: Optional[QuestionHead], verified, expected_revision):
    """Pure CAS decision. Caller must persist it in one transaction; this does not write a head."""
    require(isinstance(verified, VerifiedHead), "HEAD_NOT_VERIFIED")
    candidate = verified.head
    validate_head(candidate)
    require(integer(expected_revision) and (current.revision if current else 0) == expected_revision,
            "LIBRARY_REVISION_CONFLICT")
    if current:
        validate_head(current)
        require(current.library_id == candidate.library_id, "LIBRARY_IDENTITY_MISMATCH")
    require(candidate.revision == expected_revision + 1, "LIBRARY_REVISION_INVALID")
    return candidate


def question_target(head, exclusion_pending=False):
    require(type(exclusion_pending) is bool, "EXCLUSION_STATE_INVALID")
    if exclusion_pending:
        raise ContractError("DOCUMENT_EXCLUSION_PENDING")
    require(head is not None, "NO_READY_DOCUMENTS")
    validate_head(head)
    return head  # Frozen version reference; later uploads cannot rewrite earlier runs.


@dataclass(frozen=True)
class Job:
    job_id: str
    library_id: str
    revision: int
    stage: str = "upload"
    state: str = "pending"


def advance_job(job, stage, outcome):
    """Contract for ordered durable receipts; no storage or execution is provided here."""
    stages = ("upload", "refinement", "indexing", "binding")
    require(isinstance(job, Job) and uid(job.job_id) and uid(job.library_id)
            and integer(job.revision, 1) and job.stage in stages
            and job.state in {"pending", "running", "uncertain", "attention_required", "failed", "completed"}, "JOB_INVALID")
    require(stage == job.stage and job.state not in {"completed", "failed", "attention_required"}, "JOB_TRANSITION_INVALID")
    require(outcome in {"waiting", "uncertain", "attention_required", "failed", "complete"}, "JOB_OUTCOME_INVALID")
    if outcome != "complete":
        return Job(job.job_id, job.library_id, job.revision, stage,
                   "running" if outcome == "waiting" else outcome)
    position = stages.index(stage) + 1
    if position == len(stages):
        return Job(job.job_id, job.library_id, job.revision, stage, "completed")
    return Job(job.job_id, job.library_id, job.revision, stages[position], "pending")
