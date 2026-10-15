"""Observe one worker invocation's key-path open denial; never a trusted gate.

Linux/Python 3.10+, stdlib only. No key bytes or file metadata are read.
The CLI always exits 2. Use a reviewed, pinned copy with an outer timeout.
"""

import errno
import json
import os
import pwd
import re
import sys


_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_OPTIONS = frozenset({"--worker-user", "--key-owner", "--private-key"})


def _report(reason="ARGUMENTS_INVALID"):
    return {
        "scope": "WORKER_KEY_PATH_OPEN_ONLY",
        "status": "BLOCKED",
        "reason": reason,
        "worker_identity_verified": False,
        "control_open_verified": False,
        "key_open_attempts": 0,
        "key_path_open_denied": False,
        "key_bytes_read": 0,
        "key_presence_verified": False,
        "privilege_escalation_verified": False,
        "runtime_isolation_verified": False,
        "trusted_gate_verified": False,
        "native_ready": False,
        "merge_authorized": False,
    }


def _arguments_valid(worker_user, key_owner, key_path):
    if any(type(value) is not str or _USER.fullmatch(value) is None
           for value in (worker_user, key_owner)):
        return False
    if (type(key_path) is not str or not key_path.startswith("/")
            or key_path.startswith("//") or ".." in key_path.split("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in key_path)
            or os.path.normpath(key_path) != key_path):
        return False
    try:
        return len(key_path.encode("utf-8")) <= 4095
    except UnicodeError:
        return False


def _account_valid(account):
    return all(type(value) is int and value > 0
               for value in (account.pw_uid, account.pw_gid))


def _ids_match(ids, expected):
    return (type(ids) is tuple and len(ids) == 3
            and all(type(value) is int for value in ids)
            and ids == (expected, expected, expected))


def _close_once(fd):
    try:
        os.close(fd)
        return True
    except Exception:
        return False


def probe(worker_user, key_owner, key_path):
    report = _report()
    try:
        if not _arguments_valid(worker_user, key_owner, key_path):
            return report
        try:
            worker = pwd.getpwnam(worker_user)
            owner = pwd.getpwnam(key_owner)
            pins_valid = (_account_valid(worker) and _account_valid(owner)
                          and worker.pw_uid != owner.pw_uid)
        except Exception:
            pins_valid = False
        if not pins_valid:
            report["reason"] = "IDENTITY_PINS_INVALID"
            return report
        if (not _ids_match(os.getresuid(), worker.pw_uid)
                or not _ids_match(os.getresgid(), worker.pw_gid)):
            report["reason"] = "EXECUTION_IDENTITY_MISMATCH"
            return report
        report["worker_identity_verified"] = True
        control_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        key_flags = control_flags | os.O_NOFOLLOW
        try:
            control_fd = os.open("/dev/null", control_flags)
        except Exception:
            report["reason"] = "CONTROL_OPEN_FAILED"
            return report
        if not _close_once(control_fd):
            report["reason"] = "FD_CLOSE_FAILED"
            return report
        report["control_open_verified"] = True
        report["key_open_attempts"] = 1
        try:
            key_fd = os.open(key_path, key_flags)
        except OSError as error:
            if error.errno == errno.EACCES:
                report["status"] = "OBSERVED"
                report["reason"] = "KEY_PATH_OPEN_DENIED"
                report["key_path_open_denied"] = True
            else:
                report["reason"] = "KEY_PATH_UNCONFIRMED"
            return report
        except Exception:
            report["reason"] = "UNEXPECTED_FAILURE"
            return report
        report["reason"] = ("KEY_PATH_OPEN_ALLOWED" if _close_once(key_fd)
                            else "FD_CLOSE_FAILED")
        return report
    except Exception:
        report["reason"] = "UNEXPECTED_FAILURE"
        return report


def main(argv=None, output_stream=None):
    argv = sys.argv[1:] if argv is None else argv
    output_stream = sys.stdout if output_stream is None else output_stream
    report = _report()
    try:
        if (type(argv) is list and len(argv) == 6
                and all(type(value) is str for value in argv)):
            values = dict(zip(argv[::2], argv[1::2]))
            if len(values) == 3 and set(values) == _OPTIONS:
                report = probe(values["--worker-user"], values["--key-owner"],
                               values["--private-key"])
    except Exception:
        report = _report("UNEXPECTED_FAILURE")
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
