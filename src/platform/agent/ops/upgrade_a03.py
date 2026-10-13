#!/usr/bin/env python3
"""Prepare the A03 credential file; preserve all existing settings and secrets."""
import json
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]


def prepare(root):
    directory = root / ".secrets"
    if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mode & 0o077:
        raise ValueError("existing Agent secret directory must be regular and mode 0700")
    if not (root / ".env").is_file():
        raise ValueError("existing A02 settings required; use configure.py for a new install")
    path = directory / "tool-credentials.json"
    if path.is_symlink():
        raise ValueError("Tool credential file must not be a symlink")
    if path.exists():
        if not path.is_file() or path.stat().st_mode & 0o444 != 0o444:
            raise ValueError("Tool credential file must be regular and readable by the runtime")
        if path.stat().st_size > 65536 or not isinstance(json.loads(path.read_text()), dict):
            raise ValueError("invalid Tool credential registry")
        return "Existing Tool credential registry preserved."
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
    with os.fdopen(fd, "w") as out:
        out.write("{}\n")
    path.chmod(0o444)
    return "Created empty Tool credential registry; existing settings and secrets preserved."


if __name__ == "__main__":
    try:
        print(prepare(ROOT))
    except (ValueError, OSError) as exc:
        raise SystemExit("FAIL: " + str(exc))
