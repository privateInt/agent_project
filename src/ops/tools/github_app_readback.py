"""One-off GitHub App metadata reads. Never a check emitter or merge gate.

Linux, Python 3.10+, stdlib and /usr/bin/openssl. No installation token is
created. Keep the App key local; use an outer timeout for the entire process.
"""

import argparse
import base64
import fcntl
import http.client
import json
import math
import os
import re
import ssl
import stat
import subprocess
import sys
import time


PERMISSIONS = {
    "checks": "write", "contents": "read",
    "pull_requests": "read", "metadata": "read",
}
MAX_BODY = 128 * 1024
MAX_KEY = 16384
OPENSSL = "/usr/bin/openssl"
OPENSSL_ENV = {
    "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
    "OPENSSL_CONF": "/dev/null",
}
_OWNER = re.compile(r"[A-Za-z0-9-]{1,39}")
_REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_SLUG = re.compile(r"[a-z0-9-]{1,100}")
_STAGES = frozenset({
    "ARGUMENTS", "KEY", "SIGN", "APP", "INSTALLATION", "REPOSITORY",
    "COMPLETE", "INTERNAL",
})
_REASONS = frozenset({
    "OBSERVED", "ARGUMENTS_INVALID", "KEY_INVALID", "SIGNING_FAILED",
    "READ_NOT_ALLOWED", "HTTP_STATUS_INVALID", "HTTP_TRANSPORT_FAILED",
    "RESPONSE_INVALID", "APP_MISMATCH", "INSTALLATION_MISMATCH",
    "REPOSITORY_INSTALLATION_MISMATCH", "UNEXPECTED_FAILURE",
})


class ProbeError(Exception):
    def __init__(self, stage, reason):
        super().__init__()
        self.stage = stage
        self.reason = reason


def _report(stage, reason, attempts=0, matched=False):
    if (type(stage) is not str or stage not in _STAGES
            or type(reason) is not str or reason not in _REASONS
            or type(attempts) is not int or not 0 <= attempts <= 3):
        stage, reason, attempts, matched = "INTERNAL", "UNEXPECTED_FAILURE", 0, False
    if matched is not False and not (
            matched is True and stage == "COMPLETE" and reason == "OBSERVED" and attempts == 3):
        stage, reason, matched = "INTERNAL", "UNEXPECTED_FAILURE", False
    return {
        "scope": "AUTHENTICATED_APP_METADATA_ONLY",
        "status": "OBSERVED" if matched else "BLOCKED",
        "stage": stage, "reason": reason, "get_attempts": attempts,
        "identity_metadata_verified": matched,
        "selected_repository_list_verified": False,
        "input_origin_verified": False,
        "trusted_gate_verified": False,
        "binding_verified": False,
        "native_ready": False,
        "merge_authorized": False,
        "independent_review_verified": False,
        "ui_observations_are_authenticated_evidence": False,
    }


def _positive_id(value):
    return type(value) is int and 1 <= value <= (1 << 63) - 1


def pins_checked(app_id, installation_id, owner, repository, slug):
    if not _positive_id(app_id) or not _positive_id(installation_id):
        raise ProbeError("ARGUMENTS", "ARGUMENTS_INVALID")
    if type(owner) is not str or _OWNER.fullmatch(owner) is None:
        raise ProbeError("ARGUMENTS", "ARGUMENTS_INVALID")
    if type(slug) is not str or _SLUG.fullmatch(slug) is None:
        raise ProbeError("ARGUMENTS", "ARGUMENTS_INVALID")
    if type(repository) is not str or repository.count("/") != 1:
        raise ProbeError("ARGUMENTS", "ARGUMENTS_INVALID")
    repo_owner, name = repository.split("/")
    if repo_owner != owner or _REPO.fullmatch(name) is None or name in (".", ".."):
        raise ProbeError("ARGUMENTS", "ARGUMENTS_INVALID")
    return {"app_id": app_id, "installation_id": installation_id,
            "owner": owner, "repository": repository, "slug": slug}


def open_key_checked(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                or info.st_nlink != 1 or not 1 <= info.st_size <= MAX_KEY):
            raise ValueError()
        # stdin=PIPE in the child must not replace a key opened as descriptor 0.
        if fd <= 2:
            reserved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
            os.close(fd)
            fd = reserved
        return fd
    except Exception:
        if fd is not None:
            os.close(fd)
        raise ProbeError("KEY", "KEY_INVALID") from None


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def sign_jwt(fd, app_id, now=None):
    try:
        if not _positive_id(app_id) or type(fd) is not int or fd < 3:
            raise ValueError()
        now = int(time.time()) if now is None else now
        if type(now) is not int or now < 60:
            raise ValueError()
        header = _b64(b'{"alg":"RS256","typ":"JWT"}')
        payload = _b64(json.dumps(
            {"iat": now - 60, "exp": now + 300, "iss": str(app_id)},
            sort_keys=True, separators=(",", ":"),
        ).encode("ascii"))
        message = (header + "." + payload).encode("ascii")
        signed = subprocess.run(
            [OPENSSL, "dgst", "-sha256", "-sign", "/proc/self/fd/" + str(fd),
             "-passin", "pass:", "-sigopt", "rsa_padding_mode:pkcs1"],
            input=message, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            pass_fds=(fd,), shell=False, timeout=5, check=False,
            env=dict(OPENSSL_ENV),
        )
        if signed.returncode != 0 or not 256 <= len(signed.stdout) <= 1024:
            raise ValueError()
        return message.decode("ascii") + "." + _b64(signed.stdout)
    except Exception:
        raise ProbeError("SIGN", "SIGNING_FAILED") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _bounded_integer(value):
    if len(value) > 20:
        raise ValueError()
    return int(value)


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError()
    return result


def _reject_constant(value):
    raise ValueError()


def _check_depth(value, depth=0):
    if type(value) in (dict, list):
        if depth >= 64:
            raise ValueError()
        children = value.values() if type(value) is dict else value
        for child in children:
            _check_depth(child, depth + 1)


class GitHubReader:
    def __init__(self, jwt, pins):
        pins = pins_checked(**pins)
        self._jwt = jwt
        self._paths = {
            "/app", "/app/installations/" + str(pins["installation_id"]),
            "/repos/" + pins["repository"] + "/installation",
        }
        self._visited = set()
        self.get_attempts = 0

    def get_json(self, path):
        if (type(path) is not str or path not in self._paths or path in self._visited
                or self.get_attempts >= 3):
            raise ProbeError("INTERNAL", "READ_NOT_ALLOWED")
        connection = None
        try:
            context = ssl.create_default_context()
            if context.check_hostname is not True or context.verify_mode != ssl.CERT_REQUIRED:
                raise ValueError()
            connection = http.client.HTTPSConnection(
                "api.github.com", timeout=10, context=context,
            )
            self._visited.add(path)
            self.get_attempts += 1
            connection.request("GET", path, headers={
                "Authorization": "Bearer " + self._jwt,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "agent-project-app-metadata-probe",
            })
            response = connection.getresponse()
            if response.status != 200:
                raise ProbeError("INTERNAL", "HTTP_STATUS_INVALID")
            raw = response.read(MAX_BODY + 1)
            if type(raw) is not bytes or len(raw) > MAX_BODY:
                raise ProbeError("INTERNAL", "RESPONSE_INVALID")
            try:
                value = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=_unique_object,
                    parse_int=_bounded_integer, parse_float=_finite_float,
                    parse_constant=_reject_constant,
                )
                _check_depth(value)
                if type(value) is not dict:
                    raise ValueError()
            except Exception:
                raise ProbeError("INTERNAL", "RESPONSE_INVALID") from None
            return value
        except ProbeError:
            raise
        except Exception:
            raise ProbeError("INTERNAL", "HTTP_TRANSPORT_FAILED") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


def _app_matches(value, pins):
    if type(value) is not dict:
        return False
    owner = value.get("owner")
    return (
        _positive_id(value.get("id")) and value["id"] == pins["app_id"]
        and type(owner) is dict and owner.get("login") == pins["owner"]
        and owner.get("type") == "User" and value.get("slug") == pins["slug"]
        and value.get("permissions") == PERMISSIONS
        and type(value.get("events")) is list and value["events"] == []
    )


def _installation_matches(value, pins):
    if type(value) is not dict:
        return False
    if value.get("permissions") != PERMISSIONS:
        return False
    account = value.get("account")
    return (
        _positive_id(value.get("id")) and value["id"] == pins["installation_id"]
        and _positive_id(value.get("app_id")) and value["app_id"] == pins["app_id"]
        and type(account) is dict and account.get("login") == pins["owner"]
        and account.get("type") == "User"
        and value.get("repository_selection") == "selected"
        and "suspended_at" in value and value["suspended_at"] is None
    )


def probe(pins, key_path):
    fd = None
    reader = None
    stage = "ARGUMENTS"
    try:
        if type(pins) is not dict or set(pins) != {
                "app_id", "installation_id", "owner", "repository", "slug"}:
            raise ProbeError("ARGUMENTS", "ARGUMENTS_INVALID")
        pins = pins_checked(**pins)
        stage = "KEY"
        fd = open_key_checked(key_path)
        stage = "SIGN"
        jwt = sign_jwt(fd, pins["app_id"])
        os.close(fd)
        fd = None
        reader = GitHubReader(jwt, pins)
        stage = "APP"
        if not _app_matches(reader.get_json("/app"), pins):
            raise ProbeError(stage, "APP_MISMATCH")
        stage = "INSTALLATION"
        if not _installation_matches(reader.get_json(
                "/app/installations/" + str(pins["installation_id"])), pins):
            raise ProbeError(stage, "INSTALLATION_MISMATCH")
        stage = "REPOSITORY"
        if not _installation_matches(reader.get_json(
                "/repos/" + pins["repository"] + "/installation"), pins):
            raise ProbeError(stage, "REPOSITORY_INSTALLATION_MISMATCH")
        return _report("COMPLETE", "OBSERVED", reader.get_attempts, True)
    except ProbeError as error:
        # Transport reports INTERNAL; the probe supplies the endpoint stage.
        error_stage = stage if error.stage == "INTERNAL" else error.stage
        return _report(error_stage, error.reason, reader.get_attempts if reader else 0)
    except Exception:
        return _report(stage, "UNEXPECTED_FAILURE", reader.get_attempts if reader else 0)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ProbeError("ARGUMENTS", "ARGUMENTS_INVALID")


def _cli_id(value):
    if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None:
        raise ValueError()
    number = int(value)
    if not _positive_id(number):
        raise ValueError()
    return number


def main(argv=None, output_stream=None):
    output_stream = sys.stdout if output_stream is None else output_stream
    report = _report("ARGUMENTS", "ARGUMENTS_INVALID")
    try:
        parser = _Parser(add_help=False, allow_abbrev=False)
        parser.add_argument("--app-id", type=_cli_id, required=True)
        parser.add_argument("--installation-id", type=_cli_id, required=True)
        parser.add_argument("--owner", required=True)
        parser.add_argument("--repository", required=True)
        parser.add_argument("--slug", required=True)
        parser.add_argument("--private-key", required=True)
        args = parser.parse_args(argv)
        pins = pins_checked(args.app_id, args.installation_id,
                            args.owner, args.repository, args.slug)
        report = probe(pins, args.private_key)
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
