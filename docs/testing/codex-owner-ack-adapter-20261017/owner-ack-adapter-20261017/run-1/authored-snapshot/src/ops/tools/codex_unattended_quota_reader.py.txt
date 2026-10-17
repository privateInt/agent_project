"""Observe quota through one fresh caller-owned client, without granting work.

The caller owns connection creation, transport pins, deadlines, exclusive attempt
receipts and closure. No retry, reconnect, thread or queue operation exists here.
"""
import hashlib


_ERRORS = frozenset((
    "RPC_REQUEST_TIMEOUT", "RPC_WIRE_LIMIT", "RPC_LINE_LIMIT", "RPC_STDOUT_LIMIT",
    "RPC_CONNECT_FAILED", "RPC_DEPENDENCY_UNSUPPORTED", "RPC_UPGRADE_REJECTED",
    "RPC_SEND_FAILED", "RPC_RECEIVE_FAILED", "RPC_REMOTE_ERROR", "RPC_RESPONSE_INVALID",
    "RPC_TEXT_REQUIRED", "RPC_NONFINITE_JSON", "RPC_SERVER_REQUEST_FORBIDDEN",
    "RPC_NOTIFICATION_LIMIT", "RPC_DUPLICATE_RESPONSE", "RPC_RESPONSE_ID_MISMATCH",
    "RPC_RESULT_INVALID", "RPC_CLIENT_CLOSED", "RPC_METHOD_FORBIDDEN",
    "RPC_PARAMS_FORBIDDEN", "RPC_REQUEST_TOO_LARGE",
))
_REACHED = frozenset((
    "rate_limit_reached", "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted", "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
))
_MISSING = object()


def _need(value):
    if not value:
        raise ValueError()


def _identifier_hash(value):
    _need(type(value) is str)
    raw = value.encode("utf-8")
    _need(1 <= len(raw) <= 256)
    return hashlib.sha256(raw).hexdigest()


def _window(value):
    if value is None:
        return None
    _need(type(value) is dict)
    used = value.get("usedPercent")
    _need(type(used) is int and 0 <= used <= 100)
    answer = {"usedPercent": used}
    for field, minimum in (("windowDurationMins", 1), ("resetsAt", 0)):
        number = value.get(field)
        _need(number is None or type(number) is int and minimum <= number <= 2**63 - 1)
        answer[field] = number
    return answer


def _bucket(value, identifier):
    _need(type(value) is dict)
    primary, secondary = _window(value.get("primary")), _window(value.get("secondary"))
    _need(primary is not None or secondary is not None)
    reached = value.get("rateLimitReachedType", _MISSING)
    _need(reached is _MISSING or reached is None
          or type(reached) is str and reached in _REACHED)
    return {"limit_id_sha256": identifier,
            "reached_type_is_null": None if reached is _MISSING else reached is None,
            "primary": primary, "secondary": secondary}


def _snapshot(value):
    _need(type(value) is dict and type(value.get("rateLimits")) is dict)
    buckets = value.get("rateLimitsByLimitId")
    if buckets is None:
        bucket = value["rateLimits"]
        identifier = bucket.get("limitId")
        return [_bucket(bucket, None if identifier is None else _identifier_hash(identifier))]
    _need(type(buckets) is dict and 1 <= len(buckets) <= 16)
    return [_bucket(bucket, _identifier_hash(identifier)) for identifier, bucket in buckets.items()]


def _error_code(error):
    # Bypass an exception subclass's __getattribute__ / code / __dict__ properties.
    # Never stringify the error, copy arbitrary attrs, or compare a hostile object.
    try:
        attributes = BaseException.__dict__["__dict__"].__get__(error)
        code = attributes.get("code") if type(attributes) is dict else None
        if type(code) is str and code in _ERRORS:
            return code
    except Exception:
        pass
    return "UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR"


def read_quota_only(client):
    """At most initialize + initialized + quota read, once on a borrowed client."""
    report = {
        "scope": "QUOTA_ONLY_READER", "status": "UNCONFIRMED", "phase": "PRECHECK",
        "reason": "CLIENT_INTERFACE_INVALID", "error_code": "CLIENT_INTERFACE_INVALID",
        "requests": [], "initialized_call_entered": False,
        "initialized_notification_completed": False, "retries": 0,
        "ordinaryUsageAllowed": None, "ordinary_usage_observed_allowed": False, "windows": [],
        "transport_pin_verified": False, "wire_send_verified": False,
        "full_quota_admission_verified": False, "direct_external_io_performed": False,
        "thread_operations": 0, "settings_changed": False, "queue_changed": False,
        "services_changed": False, "remote_mutation_authorized": False,
        "native_acceptance_verified": False, "replay_permitted": False, "native_ready": False,
    }
    try:
        call = getattr(client, "call", None)
        initialized = getattr(client, "notify_initialized", None)
        if not callable(call) or not callable(initialized):
            return report
    except Exception:
        return report

    def invoke(method, params):
        request = {"method": method, "call_entered": True, "response_observed": False}
        report["requests"].append(request)
        value = call(method, params)
        request["response_observed"] = True
        return value

    try:
        report["phase"] = "INITIALIZE"
        value = invoke("initialize", {
            "clientInfo": {"name": "agent_project_quota_reader", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        if type(value) is not dict:
            report.update(reason="INITIALIZE_RESULT_INVALID", error_code="INITIALIZE_RESULT_INVALID")
            return report
        report["phase"] = "INITIALIZED_NOTIFICATION"
        report["initialized_call_entered"] = True
        initialized()
        report["initialized_notification_completed"] = True
        report["phase"] = "RATE_LIMITS_READ"
        value = invoke("account/rateLimits/read", {})
    except Exception as error:
        report.update(reason="CLIENT_CALL_UNCONFIRMED", error_code=_error_code(error))
        return report

    report["phase"] = "SANITIZE"
    if type(value) is dict:
        ordinary = value.get("ordinaryUsageAllowed")
        report["ordinaryUsageAllowed"] = ordinary if type(ordinary) is bool else None
    else:
        ordinary = None
    try:
        report["windows"] = _snapshot(value)
    except Exception:
        report.update(status="OBSERVED_UNKNOWN", reason="QUOTA_SNAPSHOT_INVALID",
                      error_code="QUOTA_RESULT_INVALID")
        return report
    report.update(phase="COMPLETE", error_code=None)
    if ordinary is True:
        report.update(status="OBSERVED_ALLOWED", reason="ORDINARY_ALLOWED_OBSERVED",
                      ordinary_usage_observed_allowed=True)
    elif ordinary is False:
        report.update(status="OBSERVED_BLOCKED", reason="ORDINARY_BLOCKED_OBSERVED")
    else:
        report.update(status="OBSERVED_UNKNOWN", reason="ORDINARY_USAGE_UNKNOWN")
    return report
