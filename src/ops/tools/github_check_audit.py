"""Offline check-run comparison; never an authenticated gate or merge approval.

Python 3.10+, stdlib only. The caller supplies comparison pins separately from
the REST-shaped snapshot. Neither input is authenticated here. For the CLI use
a complete input file: timeout 5s python3 -B github_check_audit.py < input.json
The CLI always exits 2, including when the supplied structures match.
"""

import json
import math
import re
import sys


REQUIRED_CONTEXTS = (
    "codex/required-tests",
    "codex/independent-review",
    "codex/policy",
)
MAX_RUNS = 1000
MAX_INPUT_BYTES = 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")


def _result(reason, context=None, structural_satisfied=False):
    return {
        "scope": "OFFLINE_ONLY",
        "status": "STRUCTURAL_MATCH" if structural_satisfied else "BLOCKED",
        "structural_satisfied": structural_satisfied,
        "reason": reason,
        "context": context,
        "input_origin_verified": False,
        "trusted_gate_verified": False,
        "binding_verified": False,
        "native_ready": False,
        "merge_authorized": False,
    }


def _positive_int(value):
    return type(value) is int and value > 0


def _canonical_sha(value):
    return type(value) is str and _SHA.fullmatch(value) is not None


def audit_check_runs(expected_head_sha, expected_apps, snapshot):
    """Compare a supplied snapshot without authenticating its origin or pins.

    Counts only establish internal consistency. Repository/PR/base/rules/plan/
    evidence bindings, collection completeness and reviewer independence remain
    unverified. Duplicate required names are ambiguous; no latest run is chosen.
    """
    if not _canonical_sha(expected_head_sha):
        return _result("EXPECTED_HEAD_INVALID")
    if (
        type(expected_apps) is not dict
        or any(type(key) is not str for key in expected_apps)
        or set(expected_apps) != set(REQUIRED_CONTEXTS)
        or not all(_positive_int(v) for v in expected_apps.values())
    ):
        return _result("EXPECTED_APPS_INVALID")
    if type(snapshot) is not dict:
        return _result("SNAPSHOT_INVALID")
    runs, count = snapshot.get("check_runs"), snapshot.get("total_count")
    if type(runs) is not list or type(count) is not int or count < 0:
        return _result("SNAPSHOT_INVALID")
    if len(runs) > MAX_RUNS or count > MAX_RUNS:
        return _result("SNAPSHOT_LIMIT_EXCEEDED")
    if count != len(runs):
        return _result("SNAPSHOT_INCOMPLETE")

    selected = {context: [] for context in REQUIRED_CONTEXTS}
    run_ids = set()
    for row in runs:
        if type(row) is not dict:
            return _result("RUN_INVALID")
        run_id, name = row.get("id"), row.get("name")
        if (
            not _positive_int(run_id)
            or type(name) is not str
            or not name
            or len(name) > 256
        ):
            return _result("RUN_INVALID")
        if run_id in run_ids:
            return _result("RUN_ID_DUPLICATE")
        run_ids.add(run_id)
        if name in selected:
            selected[name].append(row)

    for context in REQUIRED_CONTEXTS:
        matches = selected[context]
        if not matches:
            return _result("REQUIRED_CHECK_MISSING", context)
        if len(matches) != 1:
            return _result("REQUIRED_CHECK_AMBIGUOUS", context)
        row = matches[0]
        app = row.get("app")
        if type(app) is not dict or not _positive_int(app.get("id")):
            return _result("REQUIRED_RUN_INVALID", context)
        app_id = app["id"]
        if app_id != expected_apps[context]:
            return _result("REQUIRED_APP_MISMATCH", context)
        if not _canonical_sha(row.get("head_sha")):
            return _result("REQUIRED_RUN_INVALID", context)
        if row["head_sha"] != expected_head_sha:
            return _result("REQUIRED_HEAD_MISMATCH", context)
        status, conclusion = row.get("status"), row.get("conclusion")
        if (
            type(status) is not str
            or type(conclusion) is not str
            or status != "completed"
            or conclusion != "success"
        ):
            return _result("REQUIRED_CHECK_NOT_SUCCESS", context)

    return _result("REQUIRED_CHECKS_STRUCTURALLY_MATCH", structural_satisfied=True)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _bounded_integer(value):
    if len(value) > 20:
        raise ValueError("integer token too long")
    return int(value)


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite number")
    return result


def _reject_constant(value):
    raise ValueError("nonfinite constant")


def main(argv=None, input_stream=None, output_stream=None):
    argv = sys.argv[1:] if argv is None else argv
    input_stream = sys.stdin.buffer if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    report = _result("INPUT_INVALID")
    try:
        if argv:
            raise ValueError("arguments unsupported")
        raw = input_stream.read(MAX_INPUT_BYTES + 1)
        if type(raw) is not bytes or len(raw) > MAX_INPUT_BYTES:
            raise ValueError("invalid input size or stream")
        envelope = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_int=_bounded_integer,
            parse_float=_finite_float,
            parse_constant=_reject_constant,
        )
        if type(envelope) is not dict or set(envelope) != {
            "expected_head_sha", "expected_apps", "snapshot"
        }:
            raise ValueError("invalid envelope")
        report = audit_check_runs(
            envelope["expected_head_sha"], envelope["expected_apps"], envelope["snapshot"]
        )
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    try:
        output_stream.write(json.dumps(report, sort_keys=True) + "\n")
        output_stream.flush()
    except (OSError, ValueError):
        # An unusable output channel cannot safely carry even an error report.
        pass
    return 2


if __name__ == "__main__":
    exit_code = main()
    try:
        sys.stdout.close()
    except (OSError, ValueError):
        # Prevent a second, unmasked shutdown flush after a broken stdout pipe.
        pass
    sys.exit(exit_code)
