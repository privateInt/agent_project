"""Fixed natural-language search adapter; all outbound traffic uses pinned transport.

Discovery only reads public platform APIs. Executions never change deployment,
index or reference lifecycle, and never retry, trim, truncate or repair vectors.
"""
from . import observability as obs
import json
import math
import re
import time
import uuid
from . import http_tools
from .tools import ToolError


class Budget:
    def __init__(self, parent, seconds):
        self.parent = parent
        self.deadline = time.monotonic() + seconds

    def time_remaining(self):
        return max(0, min(self.deadline - time.monotonic(), self.parent.time_remaining() or 0))

    def is_active(self):
        return self.parent.is_active() and self.time_remaining() > 0


def valid_id(value):
    try:
        return uuid.UUID(value).int != 0 and str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def require(ok, code="TOOL_RESPONSE_INVALID"):
    if not ok:
        raise ToolError(code)


def identity(value, prefix):
    return isinstance(value, str) and re.fullmatch(prefix + r"[a-f0-9]{64}", value) is not None


def api(connection, path, context, body=None, query=None, seconds=5):
    # Every path is adapter-owned and every path ID is validated before use.
    args = {}
    if body is not None:
        args["body"] = body
    if query:
        args["query"] = query
    op = {"method": "GET" if body is None else "POST", "path": path,
          "parameters": [{"name": name, "in": "query", "required": True} for name in (query or {})],
          "input_schema": {"properties": {name: {} for name in args}}}
    binding = {"http": op, "connection_version_id": connection["connection_version_id"], "effect": "read"}
    try:
        result = http_tools.execute(binding, args, connection, Budget(context, seconds), "")
    except ToolError as exc:
        status = (exc.result or {}).get("status")
        code = ("KNOWLEDGE_RESOURCE_NOT_FOUND" if status == 404 else
                "KNOWLEDGE_NOT_READY" if status == 409 else
                "TOOL_RESPONSE_INVALID" if exc.code == "TOOL_ASYNC_UNSUPPORTED" else exc.code)
        raise ToolError(code) from None
    require(result.get("status") == 200 and isinstance(result.get("data"), dict))
    return result["data"]


def profile(p, deployment=None, space=None):
    require(isinstance(p, dict), "KNOWLEDGE_PROFILE_MISMATCH")
    require(valid_id(p.get("deployment_id")) and valid_id(p.get("model_version_id"))
            and identity(p.get("embedding_space_id"), "es_")
            and p.get("capability") == "embedding" and p.get("status") == "ready"
            and type(p.get("dimension")) is int and p["dimension"] == 1024
            and p.get("normalization") == "l2" and p.get("truncation") == "reject"
            and type(p.get("max_input_tokens")) is int and 1 <= p["max_input_tokens"] <= 8192
            and type(p.get("max_input_bytes")) is int and 1 <= p["max_input_bytes"] <= 131072
            and (deployment is None or p["deployment_id"] == deployment)
            and (space is None or p["embedding_space_id"] == space), "KNOWLEDGE_PROFILE_MISMATCH")
    return p


def index_version(connection, index_id, version_id, context):
    require(valid_id(index_id) and (not version_id or valid_id(version_id)), "TOOL_INPUT_INVALID")
    index = api(connection, "/api/indexes/" + index_id, context)
    require(index.get("index_id") == index_id)
    require(index.get("state") == "active", "KNOWLEDGE_NOT_READY")
    vid = version_id or index.get("active_version_id")
    require(valid_id(vid), "KNOWLEDGE_NOT_READY")
    version = api(connection, "/api/index-versions/" + vid, context)
    require(version.get("index_id") == index_id and version.get("index_version_id") == vid)
    require(version.get("state") == "ready" and version.get("reference_state") == "held"
            and type(version.get("chunk_count")) is int and version["chunk_count"] > 0
            and type(version.get("indexed_count")) is int and version["indexed_count"] == version["chunk_count"], "KNOWLEDGE_NOT_READY")
    profile(version.get("embedding_profile"))
    return index, version


def rerank_profile(connection, deployment, context, expected=None):
    require(valid_id(deployment), "TOOL_INPUT_INVALID")
    p = api(connection, "/api/deployments/" + deployment + "/reranker-profile", context)
    require(p.get("deployment_id") == deployment and identity(p.get("reranker_profile_id"), "rp_")
            and p.get("capability") == "reranker" and p.get("status") == "ready"
            and p.get("truncation") == "reject" and p.get("score_kind") == "logit"
            and p.get("score_direction") == "higher_is_better"
            and type(p.get("max_candidates")) is int and 1 <= p["max_candidates"] <= 100
            and (expected is None or p["reranker_profile_id"] == expected), "KNOWLEDGE_PROFILE_MISMATCH")
    return p


def valid_cursor(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", value) is not None


def inspect(req, knowledge, model, context):
    require(isinstance(req, dict) and set(req) <= {"action", "connection_version_id", "model_connection_version_id", "index_id", "index_version_id", "cursor", "rerank_deployment_id"}, "TOOL_INPUT_INVALID")
    for conn, key in ((knowledge, "connection_version_id"), (model, "model_connection_version_id")):
        require(isinstance(conn, dict) and valid_id(req.get(key)) and conn.get("connection_version_id") == req[key], "TOOL_INPUT_INVALID")
    action = req.get("action")
    if action == "indexes":
        require(not any(req.get(k) for k in ("index_id", "index_version_id", "rerank_deployment_id")), "TOOL_INPUT_INVALID")
        query = {"limit": 100}
        if req.get("cursor"):
            require(valid_cursor(req["cursor"]), "TOOL_INPUT_INVALID")
            query["cursor"] = req["cursor"]
        page = api(knowledge, "/api/indexes", context, query=query)
        require(isinstance(page.get("items"), list) and len(page["items"]) <= 100)
        items = []
        for item in page["items"]:
            require(isinstance(item, dict) and valid_id(item.get("index_id")) and isinstance(item.get("name"), str))
            items.append({"index_id": item["index_id"], "name": item["name"], "state": item.get("state"),
                          "selectable": item.get("state") == "active" and valid_id(item.get("active_version_id"))})
        cursor = page.get("next_cursor") or ""
        require(not cursor or valid_cursor(cursor))
        return {"items": items, "next_cursor": cursor}
    require(action == "resolve" and not req.get("cursor"), "TOOL_INPUT_INVALID")
    index, version = index_version(knowledge, req.get("index_id"), req.get("index_version_id"), context)
    pinned = version["embedding_profile"]
    current = profile(api(model, "/api/deployments/" + pinned["deployment_id"] + "/embedding-profile", context), pinned["deployment_id"], pinned["embedding_space_id"])
    k = {"model_connection_version_id": req["model_connection_version_id"], "index_id": index["index_id"],
         "index_version_id": version["index_version_id"], "embedding_space_id": pinned["embedding_space_id"],
         "embedding_deployment_id": current["deployment_id"], "top_k": 5, "candidate_k": 5}
    if req.get("rerank_deployment_id"):
        p = rerank_profile(model, req["rerank_deployment_id"], context)
        k["rerank"] = {"deployment_id": p["deployment_id"], "expected_reranker_profile_id": p["reranker_profile_id"]}
        k["top_k"] = min(5, p["max_candidates"])
        k["candidate_k"] = min(20, p["max_candidates"])
    return {"index_name": index["name"], "chunk_count": version["chunk_count"],
            "binding": {"connection_version_id": req["connection_version_id"], "knowledge": k}}


def inputs(binding, args, connections):
    require(isinstance(binding, dict) and set(binding) == {"connection_version_id", "knowledge"}, "TOOL_INPUT_INVALID")
    k = binding["knowledge"]
    require(isinstance(k, dict) and set(k) <= {"model_connection_version_id", "index_id", "index_version_id", "embedding_space_id", "embedding_deployment_id", "top_k", "candidate_k", "rerank"}, "TOOL_INPUT_INVALID")
    require(all(valid_id(k.get(key)) for key in ("model_connection_version_id", "index_id", "index_version_id", "embedding_deployment_id")) and identity(k.get("embedding_space_id"), "es_"), "TOOL_INPUT_INVALID")
    require(isinstance(connections, dict) and set(connections) == {"knowledge", "model"}, "TOOL_INPUT_INVALID")
    for key, expected in (("knowledge", binding.get("connection_version_id")), ("model", k["model_connection_version_id"])):
        require(valid_id(expected) and isinstance(connections[key], dict) and connections[key].get("connection_version_id") == expected, "TOOL_INPUT_INVALID")
    require(isinstance(args, dict) and set(args) <= {"query", "top_k", "candidate_k", "filters"}, "TOOL_INPUT_INVALID")
    query = args.get("query")
    require(isinstance(query, str) and query.strip() and "\0" not in query, "TOOL_INPUT_INVALID")
    try:
        require(len(query.encode("utf-8")) <= 8192, "TOOL_INPUT_INVALID")
    except UnicodeError:
        raise ToolError("TOOL_INPUT_INVALID") from None
    require(type(k.get("top_k")) is int and 1 <= k["top_k"] <= 50 and type(k.get("candidate_k")) is int and k["top_k"] <= k["candidate_k"] <= 100, "TOOL_INPUT_INVALID")
    top = args.get("top_k", k["top_k"])
    require(type(top) is int and 1 <= top <= 50, "TOOL_INPUT_INVALID")
    candidate = args.get("candidate_k", max(top, k["candidate_k"]))
    require(type(candidate) is int and top <= candidate <= 100, "TOOL_INPUT_INVALID")
    filters = args.get("filters", {})
    require(isinstance(filters, dict) and set(filters) <= {"source_document_ids", "canonical_ids", "kinds"}, "TOOL_INPUT_INVALID")
    for name, values in filters.items():
        require(isinstance(values, list) and 1 <= len(values) <= (3 if name == "kinds" else 20) and all(isinstance(v, str) for v in values), "TOOL_INPUT_INVALID")
        require(len(set(values)) == len(values) and all(v in ("text", "table", "code") if name == "kinds" else valid_id(v) for v in values), "TOOL_INPUT_INVALID")
    if "rerank" in k:
        r = k["rerank"]
        require(isinstance(r, dict) and set(r) == {"deployment_id", "expected_reranker_profile_id"} and valid_id(r["deployment_id"]) and identity(r["expected_reranker_profile_id"], "rp_"), "TOOL_INPUT_INVALID")
    return k, query, top, candidate, filters


def validate_search(result, k, top, candidate, filters):
    require(result.get("index_id") == k["index_id"] and result.get("index_version_id") == k["index_version_id"] and result.get("embedding_space_id") == k["embedding_space_id"])
    hits = result.get("results")
    require(isinstance(hits, list) and type(result.get("returned_count")) is int and result["returned_count"] == len(hits) and len(hits) <= top)
    require(type(result.get("top_k")) is int and result["top_k"] == top and type(result.get("candidate_k")) is int and result["candidate_k"] == candidate)
    require(type(result.get("candidate_count")) is int and len(hits) <= result["candidate_count"] <= candidate)
    require(result.get("filters") == {name: sorted(values) for name, values in filters.items()})
    r = result.get("rerank")
    require(isinstance(r, dict))
    if "rerank" not in k:
        require(r.get("requested") is False and r.get("applied") is False and r.get("status") == "disabled")
    else:
        pin = k["rerank"]
        require(r.get("requested") is True and r.get("deployment_id") == pin["deployment_id"] and r.get("reranker_profile_id") == pin["expected_reranker_profile_id"])
        require((bool(hits) and r.get("applied") is True and r.get("status") == "applied") or (not hits and r.get("applied") is False and r.get("status") == "skipped_no_candidates"))
    seen = set()
    for rank, hit in enumerate(hits, 1):
        require(isinstance(hit, dict) and identity(hit.get("chunk_id"), "") and hit["chunk_id"] not in seen)
        seen.add(hit["chunk_id"])
        require(valid_id(hit.get("source_document_id")) and valid_id(hit.get("canonical_id")) and hit.get("kind") in ("text", "table", "code") and isinstance(hit.get("text"), str) and hit["text"].strip())
        require(type(hit.get("rank")) is int and hit["rank"] == rank and type(hit.get("retrieval_rank")) is int and 1 <= hit["retrieval_rank"] <= candidate)
        score = hit.get("retrieval_score")
        require(type(score) in (float, int) and math.isfinite(score) and 0 <= score <= 1.000001)
        require(isinstance(hit.get("source_spans"), list) and bool(hit["source_spans"]) and "quality" in hit)
        if "rerank" in k:
            score = hit.get("rerank_score")
            require(type(score) in (float, int) and math.isfinite(score))
        for key, field in (("source_document_ids", "source_document_id"), ("canonical_ids", "canonical_id"), ("kinds", "kind")):
            require(key not in filters or hit[field] in filters[key])


def execute(binding, args, connections, context):
    stage, timings = "input", {}
    started = time.monotonic()
    def next_stage(name):
        nonlocal stage, started
        timings[stage] = int((time.monotonic() - started) * 1000)
        stage, started = name, time.monotonic()
        require(context.is_active() and (context.time_remaining() or 0) > 0, "TOOL_TIMEOUT")
    try:
        k, query, top, candidate, filters = inputs(binding, args, connections)
        knowledge, model = connections["knowledge"], connections["model"]
        next_stage("index")
        _, version = index_version(knowledge, k["index_id"], k["index_version_id"], context)
        profile(version["embedding_profile"], k["embedding_deployment_id"], k["embedding_space_id"])
        next_stage("embedding_profile")
        current = profile(api(model, "/api/deployments/" + k["embedding_deployment_id"] + "/embedding-profile", context), k["embedding_deployment_id"], k["embedding_space_id"])
        require(len(query.encode()) <= current["max_input_bytes"], "TOOL_INPUT_INVALID")
        if "rerank" in k:
            next_stage("rerank_profile")
            rp = rerank_profile(model, k["rerank"]["deployment_id"], context, k["rerank"]["expected_reranker_profile_id"])
            require(candidate <= rp["max_candidates"], "TOOL_INPUT_INVALID")
        next_stage("embedding")
        with obs.span('model.embedding', kind='embedding', deployment_id=k['embedding_deployment_id'], model_version_id=current['model_version_id']) as observation:
            receipt = api(model, "/api/deployments/" + k["embedding_deployment_id"] + "/embeddings", context, body={
                "expected_embedding_space_id": k["embedding_space_id"], "role": "query", "inputs": [{"id": "question-1", "text": query}]}, seconds=120)
            require(receipt.get("profile") == current and receipt.get("role") == "query")
            rows = receipt.get("data")
            require(isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict))
            row, vector = rows[0], rows[0].get("embedding")
            require(row.get("id") == "question-1" and type(row.get("index")) is int and row["index"] == 0
                    and type(row.get("input_tokens")) is int and 1 <= row["input_tokens"] <= current["max_input_tokens"]
                    and isinstance(vector, list) and len(vector) == 1024
                    and all(type(x) in (int, float) and math.isfinite(x) for x in vector))
            norm = math.sqrt(sum(x*x for x in vector))
            require(math.isfinite(norm) and abs(norm - 1) <= 0.001)
            observation.update(input_tokens=row['input_tokens'])
        next_stage("search")
        body = {"index_version_id": k["index_version_id"], "embedding_space_id": k["embedding_space_id"],
                "query_embedding": vector, "top_k": top, "candidate_k": candidate, "filters": filters}
        if "rerank" in k:
            body.update(query_text=query, rerank={"enabled": True, **k["rerank"]})
        with obs.span('knowledge.search', kind='retriever', index_id=k['index_id'], index_version_id=k['index_version_id']) as observation:
            result = api(knowledge, "/api/indexes/" + k["index_id"] + "/search", context, body=body, seconds=60 if "rerank" in k else 15)
            validate_search(result, k, top, candidate, filters)
            observation.update(returned_count=len(result['results']))
        timings[stage] = int((time.monotonic() - started) * 1000)
        out = {"data": result, "completion": "completed", "timings_ms": timings,
               "metadata": {"adapter": "knowledge_search_v1", "connection_version_id": binding["connection_version_id"],
                            "model_connection_version_id": k["model_connection_version_id"], "index_version_id": k["index_version_id"],
                            "embedding_profile": current, "embedding_input_tokens": row["input_tokens"], "embedding_calls": 1, "search_calls": 1}}
        if len(json.dumps(out, ensure_ascii=False, allow_nan=False).encode()) > http_tools.MAX_BODY:
            raise ToolError("TOOL_RESPONSE_TOO_LARGE")
        return out
    except (ToolError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        timings[stage] = int((time.monotonic() - started) * 1000)
        code = exc.code if isinstance(exc, ToolError) else "TOOL_RESPONSE_INVALID"
        raise ToolError(code, {"data": None, "completion": "failed", "stage": stage, "timings_ms": timings}) from None
