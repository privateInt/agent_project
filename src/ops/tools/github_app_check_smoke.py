"""One-off, failure-only App check diagnostic. Never a required-check gate.

Python 3.10+, Linux, stdlib and the unchanged App readback auth helper.
Default lookup still issues and revokes a scoped installation token. Only the
explicit emit_once mode can create a check. The CLI always exits 2.
"""

from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import time

from src.ops.tools import github_app_readback as auth


CHECK_NAME = "codex/emitter-smoke"
TITLE = "Diagnostic smoke only"
SUMMARY = ("Intentional failure. This diagnostic does not approve required checks, "
           "independent review, policy, binding, runtime isolation, or merge.")
TOKEN_PERMISSIONS = {"checks": "write", "pull_requests": "read", "metadata": "read"}
DOMAIN = b"agent-project:github-app-check-smoke:v1\n"
MAX_BODY = 128 * 1024
WORK_SECONDS = 70
_SHA = re.compile(r"[0-9a-f]{40}")
_STAGES = frozenset({
    "ARGUMENTS", "KEY", "SIGN", "APP", "INSTALLATION", "REPOSITORY", "TOKEN",
    "TOKEN_SCOPE", "PR", "CHECK_LIST", "PR_RECHECK", "CHECK_CREATE",
    "CHECK_READBACK", "REVOKE", "COMPLETE", "INTERNAL",
})
_REASONS = frozenset({
    "OBSERVED", "ARGUMENTS_INVALID", "KEY_INVALID", "SIGNING_FAILED",
    "READ_NOT_ALLOWED", "HTTP_STATUS_INVALID", "HTTP_TRANSPORT_FAILED",
    "RESPONSE_INVALID", "APP_MISMATCH", "INSTALLATION_MISMATCH",
    "REPOSITORY_INSTALLATION_MISMATCH", "UNEXPECTED_FAILURE",
    "TOKEN_ISSUE_UNCONFIRMED", "TOKEN_SCOPE_INVALID", "PR_PINS_MISMATCH",
    "CHECK_LIST_INVALID", "CHECK_NOT_FOUND", "CHECK_CREATE_UNCONFIRMED",
    "CHECK_RECORD_MISMATCH", "TOKEN_REVOKE_UNCONFIRMED", "DEADLINE_EXCEEDED",
})


def _report():
    return {
        "scope": "APP_CHECK_SMOKE_ONLY", "status": "BLOCKED",
        "stage": "ARGUMENTS", "reason": "ARGUMENTS_INVALID",
        "identity_metadata_verified": False, "metadata_get_attempts": 0,
        "api_get_attempts": 0, "token_post_attempts": 0,
        "check_post_attempts": 0, "token_delete_attempts": 0,
        "check_run_id": None, "check_record_observed": False,
        "pr_pins_rechecked": False, "token_revoked_verified": False,
        "create_outcome_unconfirmed": False,
        "input_origin_verified": False, "independent_review_verified": False,
        "binding_verified": False, "trusted_gate_verified": False,
        "runtime_isolation_verified": False, "native_ready": False,
        "merge_authorized": False,
    }


def _fail(reason):
    raise auth.ProbeError("INTERNAL", reason)


def _error(report, stage, reason):
    if (type(stage) is not str or stage not in _STAGES
            or type(reason) is not str or reason not in _REASONS):
        stage, reason = "INTERNAL", "UNEXPECTED_FAILURE"
    report.update(status="BLOCKED", stage=stage, reason=reason)


def _deadline(started):
    if time.monotonic() - started >= WORK_SECONDS:
        _fail("DEADLINE_EXCEEDED")


def _basic_token(value):
    return (type(value) is str and 1 <= len(value) <= 4096
            and all(33 <= ord(char) <= 126 for char in value))


def _depth(value, depth=0):
    if type(value) in (dict, list):
        if depth >= 32:
            raise ValueError()
        for child in (value.values() if type(value) is dict else value):
            _depth(child, depth + 1)


def _external_id(pins, pr_number, base_sha, head_sha):
    scope = {"repository": pins["repository"], "pr_number": pr_number,
             "base_sha": base_sha, "head_sha": head_sha,
             "app_id": pins["app_id"], "installation_id": pins["installation_id"]}
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True).encode("ascii")
    return "agent-project-emitter-smoke-v1:" + hashlib.sha256(DOMAIN + encoded).hexdigest()


def _check_payload(head_sha, external_id):
    return {
        "name": CHECK_NAME, "head_sha": head_sha, "status": "completed",
        "conclusion": "failure",
        "external_id": external_id,
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "output": {"title": TITLE, "summary": SUMMARY},
    }


class _Client:
    """Fixed operations, credentials, bodies, order and per-operation limits."""

    def __init__(self, jwt, pins, pr_number, head_sha, external_id, emit_once, started):
        self._jwt, self._token = jwt, None
        self._pins, self._head, self._external = pins, head_sha, external_id
        self._emit, self._started, self._check_id = emit_once, started, None
        self.counts = {op: 0 for op in (
            "TOKEN", "REPOS", "PR_INITIAL", "LIST", "PR_PRECREATE", "CREATE",
            "GET_CHECK", "PR_FINAL", "REVOKE",
        )}
        self._next = {"TOKEN"}
        repo = "/repos/" + pins["repository"]
        pr = repo + "/pulls/" + str(pr_number)
        self._routes = {
            "TOKEN": ("POST", "/app/installations/" + str(pins["installation_id"])
                      + "/access_tokens", 201),
            "REPOS": ("GET", "/installation/repositories?per_page=100&page=1", 200),
            "PR_INITIAL": ("GET", pr, 200), "PR_PRECREATE": ("GET", pr, 200),
            "PR_FINAL": ("GET", pr, 200),
            "LIST": ("GET", repo + "/commits/" + head_sha + "/check-runs"
                     + "?check_name=codex%2Femitter-smoke&filter=all&per_page=100&page=1"
                     + "&app_id=" + str(pins["app_id"]), 200),
            "CREATE": ("POST", repo + "/check-runs", 201),
            "REVOKE": ("DELETE", "/installation/token", 204),
        }

    def set_token(self, token):
        if not _basic_token(token) or self._token is not None:
            _fail("TOKEN_ISSUE_UNCONFIRMED")
        self._token = token

    def set_check_id(self, check_id):
        if not auth._positive_id(check_id) or self._check_id is not None:
            _fail("CHECK_RECORD_MISMATCH")
        self._check_id = check_id

    def call(self, operation):
        if (type(operation) is not str or operation not in self._next
                or self.counts.get(operation) != 0
                or (operation in {"PR_PRECREATE", "CREATE"} and not self._emit)
                or (operation != "TOKEN" and not _basic_token(self._token))):
            _fail("READ_NOT_ALLOWED")
        if operation != "REVOKE":
            _deadline(self._started)
        if operation == "GET_CHECK":
            if not auth._positive_id(self._check_id):
                _fail("READ_NOT_ALLOWED")
            route = ("GET", "/repos/" + self._pins["repository"] + "/check-runs/"
                     + str(self._check_id), 200)
        else:
            route = self._routes[operation]
        self._next = {
            "TOKEN": {"REPOS", "REVOKE"}, "REPOS": {"PR_INITIAL", "REVOKE"},
            "PR_INITIAL": {"LIST", "REVOKE"},
            "LIST": {"PR_PRECREATE", "GET_CHECK", "REVOKE"},
            "PR_PRECREATE": {"CREATE", "REVOKE"},
            "CREATE": {"GET_CHECK", "REVOKE"},
            "GET_CHECK": {"PR_FINAL", "REVOKE"}, "PR_FINAL": {"REVOKE"},
            "REVOKE": set(),
        }[operation]
        self.counts[operation] += 1
        method, path, expected_status = route
        body = None
        if operation == "TOKEN":
            payload = {"repositories": [self._pins["repository"].split("/")[1]],
                       "permissions": dict(TOKEN_PERMISSIONS)}
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
        elif operation == "CREATE":
            body = json.dumps(_check_payload(self._head, self._external), sort_keys=True,
                              separators=(",", ":")).encode("ascii")
        connection = None
        try:
            context = ssl.create_default_context()
            if context.check_hostname is not True or context.verify_mode != ssl.CERT_REQUIRED:
                raise ValueError()
            connection = http.client.HTTPSConnection("api.github.com", timeout=5, context=context)
            credential = self._jwt if operation == "TOKEN" else self._token
            headers = {"Authorization": "Bearer " + credential,
                       "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2026-03-10",
                       "User-Agent": "agent-project-app-check-smoke"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != expected_status:
                _fail("HTTP_STATUS_INVALID")
            if operation == "REVOKE":
                return None
            if operation in {"REPOS", "LIST"} and response.getheader("Link"):
                _fail("RESPONSE_INVALID")
            raw = response.read(MAX_BODY + 1)
            if type(raw) is not bytes or len(raw) > MAX_BODY:
                _fail("RESPONSE_INVALID")
            try:
                value = json.loads(raw.decode("utf-8"), object_pairs_hook=auth._unique_object,
                                   parse_int=auth._bounded_integer, parse_float=auth._finite_float,
                                   parse_constant=auth._reject_constant)
                _depth(value)
                if type(value) is not dict:
                    raise ValueError()
            except Exception:
                _fail("RESPONSE_INVALID")
            return value
        except auth.ProbeError:
            raise
        except Exception:
            _fail("HTTP_TRANSPORT_FAILED")
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def _token_repo_id(value, pins):
    repos = value.get("repositories")
    expires = value.get("expires_at")
    if (value.get("permissions") != TOKEN_PERMISSIONS
            or value.get("repository_selection") != "selected"
            or type(repos) is not list or len(repos) != 1 or type(repos[0]) is not dict
            or not auth._positive_id(repos[0].get("id"))
            or repos[0].get("full_name") != pins["repository"]
            or type(expires) is not str):
        _fail("TOKEN_SCOPE_INVALID")
    try:
        parsed = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != expires or parsed.timestamp() <= time.time():
            raise ValueError()
    except (ValueError, OverflowError):
        _fail("TOKEN_SCOPE_INVALID")
    return repos[0]["id"]


def _repos_match(value, pins, repo_id):
    repos = value.get("repositories")
    return (type(value.get("total_count")) is int and value["total_count"] == 1
            and type(repos) is list and len(repos) == 1 and type(repos[0]) is dict
            and auth._positive_id(repos[0].get("id")) and repos[0]["id"] == repo_id
            and repos[0].get("full_name") == pins["repository"])


def _pr_matches(value, pins, repo_id, pr_number, base_sha, head_sha):
    if (not auth._positive_id(value.get("number")) or value["number"] != pr_number
            or value.get("state") != "open" or value.get("draft") is not True
            or value.get("merged") is not False):
        return False
    for side, sha in (("base", base_sha), ("head", head_sha)):
        row = value.get(side)
        if type(row) is not dict or row.get("sha") != sha:
            return False
        repo = row.get("repo")
        if (type(repo) is not dict or repo.get("full_name") != pins["repository"]
                or not auth._positive_id(repo.get("id")) or repo["id"] != repo_id):
            return False
    return True


def _record_matches(value, pins, head_sha, external_id, expected_id=None):
    if type(value) is not dict or not auth._positive_id(value.get("id")):
        return False
    app, output = value.get("app"), value.get("output")
    return (type(app) is dict and auth._positive_id(app.get("id"))
            and app.get("id") == pins["app_id"]
            and app.get("slug") == pins["slug"]
            and (expected_id is None or value["id"] == expected_id)
            and value.get("name") == CHECK_NAME and value.get("head_sha") == head_sha
            and value.get("external_id") == external_id
            and value.get("status") == "completed" and value.get("conclusion") == "failure"
            and type(output) is dict and output.get("title") == TITLE
            and output.get("summary") == SUMMARY)


def smoke(pins, key_path, pr_number, base_sha, head_sha, emit_once=False):
    report, fd, reader, client = _report(), None, None, None
    started = time.monotonic()
    try:
        if (type(pins) is not dict or set(pins) != {
                "app_id", "installation_id", "owner", "repository", "slug"}
                or not auth._positive_id(pr_number) or type(emit_once) is not bool
                or type(base_sha) is not str or _SHA.fullmatch(base_sha) is None
                or type(head_sha) is not str or _SHA.fullmatch(head_sha) is None
                or type(key_path) is not str or not key_path):
            _fail("ARGUMENTS_INVALID")
        pins = auth.pins_checked(**pins)
        external_id = _external_id(pins, pr_number, base_sha, head_sha)
        report["stage"] = "KEY"
        fd = auth.open_key_checked(key_path)
        report["stage"] = "SIGN"
        jwt = auth.sign_jwt(fd, pins["app_id"])
        closing_fd, fd = fd, None
        try:
            os.close(closing_fd)
        except OSError:
            raise auth.ProbeError("KEY", "KEY_INVALID") from None
        reader = auth.GitHubReader(jwt, pins)
        for stage, path, matches, reason in (
            ("APP", "/app", auth._app_matches, "APP_MISMATCH"),
            ("INSTALLATION", "/app/installations/" + str(pins["installation_id"]),
             auth._installation_matches, "INSTALLATION_MISMATCH"),
            ("REPOSITORY", "/repos/" + pins["repository"] + "/installation",
             auth._installation_matches, "REPOSITORY_INSTALLATION_MISMATCH"),
        ):
            report["stage"] = stage
            _deadline(started)
            if not matches(reader.get_json(path), pins):
                _fail(reason)
        report["identity_metadata_verified"] = True
        client = _Client(jwt, pins, pr_number, head_sha, external_id, emit_once, started)
        report["stage"] = "TOKEN"
        try:
            token_row = client.call("TOKEN")
        except auth.ProbeError as error:
            if error.reason == "DEADLINE_EXCEEDED":
                raise
            _fail("TOKEN_ISSUE_UNCONFIRMED")
        except Exception:
            _fail("TOKEN_ISSUE_UNCONFIRMED")
        client.set_token(token_row.get("token"))
        report["stage"] = "TOKEN_SCOPE"
        repo_id = _token_repo_id(token_row, pins)
        if not _repos_match(client.call("REPOS"), pins, repo_id):
            _fail("TOKEN_SCOPE_INVALID")
        report["stage"] = "PR"
        if not _pr_matches(client.call("PR_INITIAL"), pins, repo_id, pr_number, base_sha, head_sha):
            _fail("PR_PINS_MISMATCH")
        report["stage"] = "CHECK_LIST"
        listing = client.call("LIST")
        count, rows = listing.get("total_count"), listing.get("check_runs")
        if (type(count) is not int or type(rows) is not list or not 0 <= count <= 100
                or count != len(rows) or count > 1):
            _fail("CHECK_LIST_INVALID")
        if rows:
            if not _record_matches(rows[0], pins, head_sha, external_id):
                _fail("CHECK_LIST_INVALID")
            check_id = rows[0]["id"]
        else:
            if not emit_once:
                _fail("CHECK_NOT_FOUND")
            report["stage"] = "PR_RECHECK"
            if not _pr_matches(client.call("PR_PRECREATE"), pins, repo_id,
                               pr_number, base_sha, head_sha):
                _fail("PR_PINS_MISMATCH")
            report["stage"] = "CHECK_CREATE"
            try:
                created = client.call("CREATE")
                if not _record_matches(created, pins, head_sha, external_id):
                    _fail("CHECK_RECORD_MISMATCH")
            except auth.ProbeError as error:
                report["create_outcome_unconfirmed"] = client.counts["CREATE"] == 1
                if error.reason == "DEADLINE_EXCEEDED":
                    raise
                _fail("CHECK_CREATE_UNCONFIRMED")
            except Exception:
                report["create_outcome_unconfirmed"] = client.counts["CREATE"] == 1
                _fail("CHECK_CREATE_UNCONFIRMED")
            check_id = created["id"]
        report["check_run_id"] = check_id
        client.set_check_id(check_id)
        report["stage"] = "CHECK_READBACK"
        if not _record_matches(client.call("GET_CHECK"), pins, head_sha, external_id, check_id):
            _fail("CHECK_RECORD_MISMATCH")
        report["check_record_observed"] = True
        report["stage"] = "PR_RECHECK"
        if not _pr_matches(client.call("PR_FINAL"), pins, repo_id, pr_number, base_sha, head_sha):
            _fail("PR_PINS_MISMATCH")
        report["pr_pins_rechecked"] = True
        report.update(stage="COMPLETE", reason="OBSERVED")
    except auth.ProbeError as error:
        _error(report, report["stage"] if error.stage == "INTERNAL" else error.stage, error.reason)
    except Exception:
        _error(report, report["stage"], "UNEXPECTED_FAILURE")
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if client is not None and client._token is not None:
            try:
                client.call("REVOKE")
                report["token_revoked_verified"] = True
            except Exception:
                if report["stage"] == "COMPLETE":
                    _error(report, "REVOKE", "TOKEN_REVOKE_UNCONFIRMED")
            client._token, client._jwt = None, None
    if reader is not None:
        count = reader.get_attempts
        if type(count) is int and 0 <= count <= 3:
            report["metadata_get_attempts"] = count
        else:
            _error(report, "INTERNAL", "UNEXPECTED_FAILURE")
    if client is not None:
        counts = client.counts
        report.update(
            api_get_attempts=sum(counts[op] for op in (
                "REPOS", "PR_INITIAL", "LIST", "PR_PRECREATE", "GET_CHECK", "PR_FINAL")),
            token_post_attempts=counts["TOKEN"], check_post_attempts=counts["CREATE"],
            token_delete_attempts=counts["REVOKE"],
        )
    if (report["stage"] == "COMPLETE" and report["reason"] == "OBSERVED"
            and all(report[key] is True for key in (
                "identity_metadata_verified", "check_record_observed",
                "pr_pins_rechecked", "token_revoked_verified"))):
        report["status"] = "OBSERVED"
    return report


def _cli(argv):
    if type(argv) not in (list, tuple) or len(argv) not in (18, 19):
        _fail("ARGUMENTS_INVALID")
    flags = {"--app-id", "--installation-id", "--owner", "--repository", "--slug",
             "--private-key", "--pr-number", "--base-sha", "--head-sha"}
    values, emit = {}, False
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag == "--emit-once" and not emit:
            emit, index = True, index + 1
            continue
        if (type(flag) is not str or flag not in flags or flag in values
                or index + 1 >= len(argv) or type(argv[index + 1]) is not str
                or argv[index + 1].startswith("--")):
            _fail("ARGUMENTS_INVALID")
        values[flag], index = argv[index + 1], index + 2
    if set(values) != flags:
        _fail("ARGUMENTS_INVALID")
    pins = auth.pins_checked(auth._cli_id(values["--app-id"]),
                             auth._cli_id(values["--installation-id"]), values["--owner"],
                             values["--repository"], values["--slug"])
    return (pins, values["--private-key"], auth._cli_id(values["--pr-number"]),
            values["--base-sha"], values["--head-sha"], emit)


def main(argv=None, output_stream=None):
    output_stream = sys.stdout if output_stream is None else output_stream
    report = _report()
    try:
        argv = sys.argv[1:] if argv is None else argv
        report = smoke(*_cli(argv))
    except Exception:
        pass
    try:
        output_stream.write(json.dumps(report, sort_keys=True) + "\n")
        output_stream.flush()
    except Exception:
        pass
    return 2


if __name__ == "__main__":
    exit_code = main()
    try:
        sys.stdout.close()
    except Exception:
        pass
    sys.exit(exit_code)
