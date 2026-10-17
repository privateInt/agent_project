"""Offline check-run comparison; never an authenticated gate or merge approval.

Python 3.10+, stdlib only. The caller supplies comparison pins separately from
the REST-shaped snapshot. Neither input is authenticated here. For the CLI use
a complete input file: timeout 5s python3 -B github_check_audit.py < input.json
The CLI always exits 2, including when the supplied structures match.
"""

import hashlib
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
BINDING_DOMAIN = b"agent-project:github-evidence-binding:v1\n"
_BINDING_KEYS = frozenset({
    "repository", "pr_number", "base_sha", "head_sha",
    "release_sha256", "plan_sha256", "evidence_sha256",
})
_BINDING_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


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


def canonical_binding_digest(binding):
    """Hash seven validated scalar claims using this protocol's v1 encoding.

    This is an unkeyed digest, not a signature or an authenticated GitHub claim.
    evidence_sha256 names a separate claimed artifact hash; artifact bytes are
    neither supplied to nor read by this function. Invalid input returns None.
    """
    if (
        type(binding) is not dict
        or len(binding) != len(_BINDING_KEYS)
        or any(type(key) is not str for key in binding)
        or set(binding) != _BINDING_KEYS
    ):
        return None
    repository = binding["repository"]
    if (
        type(repository) is not str
        or len(repository) > 201
        or _BINDING_REPOSITORY.fullmatch(repository) is None
        or any(token in {".", ".."} for token in repository.split("/"))
    ):
        return None
    pr_number = binding["pr_number"]
    if type(pr_number) is not int or not 1 <= pr_number <= 2**63 - 1:
        return None
    if not all(_canonical_sha(binding[key]) for key in ("base_sha", "head_sha")):
        return None
    for key in ("release_sha256", "plan_sha256", "evidence_sha256"):
        value = binding[key]
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            return None
    encoded = json.dumps(
        binding, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(BINDING_DOMAIN + encoded).hexdigest()


def _bound_result(reason):
    report = _result(reason)
    report["binding_fields_satisfied"] = False
    return report


def audit_bound_checks(expected_binding, expected_apps, evidence):
    """Compare binding claims, then reuse the offline required-check audit.

    The expected pins, observed claims, digest and snapshot are unauthenticated.
    Matching fields do not establish live freshness, artifact integrity, reviewer
    independence or merge permission. All five trust flags remain false.
    """
    expected_digest = canonical_binding_digest(expected_binding)
    if expected_digest is None:
        return _bound_result("EXPECTED_BINDING_INVALID")
    if (
        type(evidence) is not dict
        or len(evidence) != 3
        or any(type(key) is not str for key in evidence)
        or set(evidence) != {"binding", "binding_digest", "snapshot"}
    ):
        return _bound_result("BINDING_EVIDENCE_INVALID")
    observed_digest = canonical_binding_digest(evidence["binding"])
    if observed_digest is None:
        return _bound_result("OBSERVED_BINDING_INVALID")
    claimed_digest = evidence["binding_digest"]
    if type(claimed_digest) is not str or _SHA256.fullmatch(claimed_digest) is None:
        return _bound_result("CLAIMED_BINDING_DIGEST_INVALID")
    if claimed_digest != observed_digest:
        return _bound_result("CLAIMED_BINDING_DIGEST_MISMATCH")
    if expected_digest != observed_digest:
        return _bound_result("BINDING_FIELDS_MISMATCH")
    report = audit_check_runs(expected_binding["head_sha"], expected_apps, evidence["snapshot"])
    report["binding_fields_satisfied"] = True
    return report


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
