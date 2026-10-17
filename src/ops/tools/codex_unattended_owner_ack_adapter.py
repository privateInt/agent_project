"""Prepare local receipt arguments from an existing owner's decoded queue result.

No client or database is opened or called. A prepared result is neither proof of
native origin nor permission to dispatch or replay. Only pass the three payload
fields to record_queue_receipt when ready is True; rejected None fields are not
an absent remote result. Returned ACK bytes are transient and may contain text.
"""
from src.ops.tools import codex_unattended_queue_observer as _parser
from src.ops.tools import codex_unattended_quota_reader as _quota
from src.ops.tools import github_fixture_collector as _json


_MISSING = object()
_canonical = _json._canonical
_sha = _parser._sha
_FLAGS = (
    "remote_io_performed", "db_io_performed", "input_origin_verified",
    "client_identity_verified", "transport_pin_verified",
    "transport_id_matching_verified", "deadline_verified",
    "native_acceptance_verified", "remote_mutation_authorized",
    "dispatch_permitted", "replay_permitted", "native_ready",
)


class _Failure(Exception):
    def __init__(self, reason):
        self.reason = reason


def _need(condition, reason):
    if not condition:
        raise _Failure(reason)


def _shape(value, keys):
    _need(type(value) is dict and len(value) == len(keys), "RESULT_INVALID")
    _need(all(type(key) is str and len(key) <= 32 for key in value),
          "RESULT_INVALID")
    _need(set(value) == set(keys), "RESULT_INVALID")


def _encode_text(text):
    return text.encode("utf-8")


def _result(value):
    _shape(value, ("queuedSubmission",))
    item = value["queuedSubmission"]
    _shape(item, ("id", "clientUserMessageId", "input"))
    for name in ("id", "clientUserMessageId"):
        identifier = item[name]
        _need(type(identifier) is str and 1 <= len(identifier) <= 128
              and _parser._identifier(identifier), "RESULT_INVALID")
    inputs = item["input"]
    _need(type(inputs) is list and len(inputs) == 1, "RESULT_INVALID")
    text_item = inputs[0]
    _need(type(text_item) is dict and len(text_item) in (2, 3), "RESULT_INVALID")
    keys = ("type", "text") if len(text_item) == 2 else ("type", "text", "text_elements")
    _shape(text_item, keys)
    _need(type(text_item["type"]) is str and text_item["type"] == "text",
          "RESULT_INVALID")
    has_elements = "text_elements" in text_item
    if has_elements:
        _need(type(text_item["text_elements"]) is list
              and len(text_item["text_elements"]) == 0, "RESULT_INVALID")
    text = text_item["text"]
    _need(type(text) is str and 1 <= len(text) <= 16384, "RESULT_INVALID")
    try:
        text_utf8 = _encode_text(text)
    except UnicodeEncodeError:
        raise _Failure("RESULT_INVALID") from None
    _need(len(text_utf8) <= 16384, "TEXT_UTF8_LIMIT")
    copy = {"type": "text", "text": text}
    if has_elements:
        copy["text_elements"] = []
    return {"queuedSubmission": {"id": item["id"],
                                 "clientUserMessageId": item["clientUserMessageId"],
                                 "input": [copy]}}


def _error_code(error):
    # Never look up a key in the original dict: even dict.get can call a
    # colliding custom key's equality hook. Delegate only a clean plain proxy.
    code = None
    try:
        attributes = BaseException.__dict__["__dict__"].__get__(error)
        if type(attributes) is dict and len(attributes) <= 32:
            for key, value in attributes.items():
                if type(key) is str and len(key) == 4 and key == "code":
                    if type(value) is str and len(value) <= 64 and value in _quota._ERRORS:
                        code = value
                    break
    except Exception:
        code = None
    proxy = Exception()
    if code is not None:
        proxy.code = code
    return _quota._error_code(proxy)


def prepare_queue_ack(*, decoded_result=_MISSING, call_error=_MISSING):
    """Bound serialization and safe fixed-error conversion, without any IO.

    Omission of both arguments prepares explicit absence. Explicit None is an
    invalid supplied result/error. Always check ready before using the payload.
    Hashes cover canonical serialized decoded results, not wire response bytes.
    """
    report = {"scope": "OWNER_QUEUE_ACK_ADAPTER_ONLY", "ready": False,
              "status": "REJECTED", "reason": None, "ack_result_bytes": None,
              "expected_ack_sha256": None, "call_error_code": None}
    report.update({name: False for name in _FLAGS})
    try:
        has_result = decoded_result is not _MISSING
        has_error = call_error is not _MISSING
        _need(not (has_result and has_error), "INPUTS_MUTUALLY_EXCLUSIVE")
        if has_result:
            value = _result(decoded_result)
            raw = _canonical(value)
            _need(type(raw) is bytes and 1 <= len(raw) <= 262144, "RESULT_INVALID")
            pin = _sha(raw)
            report.update(ack_result_bytes=raw, expected_ack_sha256=pin,
                          reason="ACK_RESULT_PREPARED")
        elif has_error:
            _need(issubclass(type(call_error), Exception), "ERROR_INVALID")
            code = _error_code(call_error)
            report.update(call_error_code=code, reason="CALL_ERROR_PREPARED")
        else:
            report["reason"] = "ABSENCE_PREPARED"
        report.update(ready=True, status="PREPARED")
    except _Failure as error:
        report["reason"] = error.reason
    except Exception:
        report["reason"] = "PREPARATION_UNCONFIRMED"
    return report
