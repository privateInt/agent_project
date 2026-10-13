#!/usr/bin/env python3
"""Create Agent-only settings and secrets; never read another platform's config."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets

ROOT = Path(__file__).resolve().parents[1]


def configure(root, port, bind_host, network):
    if not 1024 <= port <= 65535:
        raise ValueError("port must be between 1024 and 65535")
    if ipaddress.ip_address(bind_host).version != 4:
        raise ValueError("Agent bind host must be an IPv4 address")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", network):
        raise ValueError("invalid Docker network name")
    target = root / ".env"
    directory = root / ".secrets"
    if target.exists() or target.is_symlink() or directory.exists() or directory.is_symlink():
        raise ValueError("Agent .env or secret directory already exists; nothing was replaced")
    directory.mkdir(mode=0o700)
    created = []
    try:
        for name in ("db-admin", "db-app", "internal-token", "api-token", "tool-credentials.json"):
            path = directory / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            created.append(path)
            with os.fdopen(fd, "w") as out:
                out.write("{}\n" if name == "tool-credentials.json" else secrets.token_hex(32) + "\n")
            # Compose file secrets preserve host permissions. 0444 is needed by
            # the non-root containers; the host parent directory remains 0700.
            path.chmod(0o444)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created.append(target)
        with os.fdopen(fd, "w") as out:
            out.write(f"AGENT_GATEWAY_PORT={port}\nAGENT_BIND_HOST={bind_host}\nAGENT_NETWORK={network}\n")
    except Exception:
        for path in reversed(created):
            path.unlink()
        directory.rmdir()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=16100)
    parser.add_argument("--bind-host", default="0.0.0.0")
    parser.add_argument("--network", default="ops-infra")
    args = parser.parse_args()
    configure(ROOT, args.port, args.bind_host, args.network)
    print("Created Agent-only settings and four independent secrets and an empty Tool credential registry. Existing settings were not read.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit("FAIL: " + str(exc))
