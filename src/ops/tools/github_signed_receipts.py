"""Verify detached Ed25519 receipts under caller-supplied pins, never a gate.

Python 3.10+, Linux, OpenSSL 3. No signing, CLI, network or private-key input.
Artifact bytes are opaque: signatures do not prove execution or independence.
"""

import fcntl
import hashlib
import json
import os
import re
import subprocess

from src.ops.tools.github_check_audit import canonical_binding_digest


MAX_ARTIFACT_BYTES = 1024 * 1024
_ROLES = ("tests", "reviewer")
_DOMAIN = b"agent-project:signed-receipt:v1\n"
_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
_ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HEX = re.compile(r"[0-9a-f]+")


def _result(reason):
    matched = reason == "CRYPTOGRAPHIC_MATCH"
    return {
        "scope": "RECEIPT_PAIR_VERIFICATION_ONLY",
        "status": "CRYPTOGRAPHIC_MATCH" if matched else "BLOCKED",
        "reason": reason,
        "signature_pair_verified": matched,
        "artifact_hashes_verified": matched,
        "binding_fields_satisfied": matched,
        "input_origin_verified": False,
        "signer_roster_origin_verified": False,
        "issuer_environment_verified": False,
        "runtime_isolation_verified": False,
        "independent_review_verified": False,
        "trusted_gate_verified": False,
        "binding_verified": False,
        "native_ready": False,
        "merge_authorized": False,
    }


def _shape(value, keys):
    return (type(value) is dict and len(value) == len(keys)
            and all(type(key) is str for key in value) and set(value) == set(keys))


def _hex_string(value, length):
    return (type(value) is str and len(value) == length
            and _HEX.fullmatch(value) is not None)


def _actor(value):
    return (type(value) is str and len(value) <= 128
            and _ACTOR.fullmatch(value) is not None)


def _signer_pins(signers):
    if not _shape(signers, ("author",) + _ROLES):
        return None
    author = signers["author"]
    if (not _shape(author, ("actor", "key_sha256"))
            or not _actor(author["actor"])
            or not _hex_string(author["key_sha256"], 64)):
        return None
    actors = {author["actor"]}
    fingerprints = {author["key_sha256"]}
    checked = {}
    for role in _ROLES:
        row = signers[role]
        if (not _shape(row, ("actor", "public_key_hex"))
                or not _actor(row["actor"])
                or not _hex_string(row["public_key_hex"], 64)):
            return None
        public_key = bytes.fromhex(row["public_key_hex"])
        fingerprint = hashlib.sha256(public_key).hexdigest()
        if row["actor"] in actors or fingerprint in fingerprints:
            return None
        actors.add(row["actor"])
        fingerprints.add(fingerprint)
        checked[role] = (row["actor"], fingerprint, public_key)
    return checked


def _verify_ed25519(public_key, message, signature):
    """Verify only public buffers; failures disclose no backend diagnostics."""
    fds = []
    cleanup_ok = True
    try:
        for data in (_SPKI_PREFIX + public_key, message, signature):
            fd = os.memfd_create("receipt-public", os.MFD_CLOEXEC)
            fds.append(fd)
            if fd < 3:
                reserved = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
                fds.append(reserved)
                # A failed close may already have released the descriptor.
                # Never retry that number after another thread can reuse it.
                fds.remove(fd)
                os.close(fd)
                fd = reserved
            view = memoryview(data)
            offset = 0
            while offset < len(view):
                written = os.write(fd, view[offset:])
                if written <= 0:
                    raise OSError("public buffer write failed")
                offset += written
            os.lseek(fd, 0, os.SEEK_SET)
        completed = subprocess.run(
            ["/usr/bin/openssl", "pkeyutl", "-verify", "-pubin", "-keyform", "DER",
             "-rawin", "-inkey", "/proc/self/fd/" + str(fds[0]),
             "-in", "/proc/self/fd/" + str(fds[1]),
             "-sigfile", "/proc/self/fd/" + str(fds[2])],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, pass_fds=tuple(fds), close_fds=True,
            shell=False, check=False, timeout=5,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin", "OPENSSL_CONF": "/dev/null"},
        )
    except Exception:
        return False
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except Exception:
                cleanup_ok = False
    if not cleanup_ok:
        return False
    return completed.returncode == 0


def _verify_pair(expected_binding, expected_signers, receipts, artifacts):
    if canonical_binding_digest(expected_binding) is None:
        return _result("EXPECTED_BINDING_INVALID")
    signers = _signer_pins(expected_signers)
    if signers is None:
        return _result("SIGNER_PINS_INVALID")
    if (not _shape(artifacts, _ROLES)
            or any(type(artifacts[role]) is not bytes
                   or not 1 <= len(artifacts[role]) <= MAX_ARTIFACT_BYTES
                   for role in _ROLES)):
        return _result("ARTIFACTS_INVALID")
    hashes = {role: hashlib.sha256(artifacts[role]).hexdigest() for role in _ROLES}
    if hashes["tests"] != expected_binding["evidence_sha256"]:
        return _result("ARTIFACT_HASH_MISMATCH")
    if not _shape(receipts, _ROLES):
        return _result("RECEIPTS_INVALID")
    verified_inputs = []
    for role in _ROLES:
        receipt = receipts[role]
        if (not _shape(receipt, ("payload", "signature_hex"))
                or not _hex_string(receipt["signature_hex"], 128)):
            return _result("RECEIPTS_INVALID")
        payload = receipt["payload"]
        actor, fingerprint, public_key = signers[role]
        if (not _shape(payload, ("schema", "role", "actor", "key_sha256",
                                "binding", "artifact_sha256"))
                or type(payload["schema"]) is not int or payload["schema"] != 1
                or type(payload["role"]) is not str or payload["role"] != role
                or type(payload["actor"]) is not str or payload["actor"] != actor
                or not _hex_string(payload["key_sha256"], 64)
                or payload["key_sha256"] != fingerprint
                or not _hex_string(payload["artifact_sha256"], 64)
                or canonical_binding_digest(payload["binding"]) is None):
            return _result("RECEIPTS_INVALID")
        if payload["binding"] != expected_binding:
            return _result("BINDING_MISMATCH")
        if payload["artifact_sha256"] != hashes[role]:
            return _result("ARTIFACT_HASH_MISMATCH")
        message = _DOMAIN + json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii")
        verified_inputs.append((public_key, message,
                                bytes.fromhex(receipt["signature_hex"])))
    # Validate both roles before invoking either subprocess. Opaque artifact
    # contents and caller-controlled pin origins remain outside this component.
    for public_key, message, signature in verified_inputs:
        if not _verify_ed25519(public_key, message, signature):
            return _result("SIGNATURE_NOT_VERIFIED")
    return _result("CRYPTOGRAPHIC_MATCH")


def verify_receipt_pair(expected_binding, expected_signers, receipts, artifacts):
    """Compare two receipts to supplied pins; never authenticate pin origins.

    Every failure clears all three narrow result flags. All nine trust/merge
    flags remain false even for authentic signatures under the supplied keys.
    """
    try:
        return _verify_pair(expected_binding, expected_signers, receipts, artifacts)
    except Exception:
        return _result("UNEXPECTED_FAILURE")
