#!/usr/bin/env python3
"""Read-only deployment preflight. Does not start or modify services."""
import argparse
import json
from pathlib import Path
import socket
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"agent-postgres", "agent-service", "agent-runtime", "agent-gateway", "agent-db-checks", "agent-mcp"}


def command(args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise ValueError("preflight command failed: " + " ".join(args[:3]))
    return result.stdout


def verify_config(config, tracing=False):
    services = config["services"]
    if set(services) != EXPECTED:
        raise ValueError("unexpected service in Agent Compose")
    for name, service in services.items():
        mounts = service.get("tmpfs", [])
        if isinstance(mounts, str):
            mounts = [mounts]
        if not isinstance(mounts, list):
            raise ValueError("invalid tmpfs list in " + name)
        for mount in mounts:
            if not isinstance(mount, str) or not mount.partition(":")[0].startswith("/"):
                raise ValueError("tmpfs target must be an absolute container path in " + name)
        if name != "agent-gateway" and service.get("ports"):
            raise ValueError("only the gateway may publish a host port")
        if service.get("privileged") or service.get("devices") or service.get("gpus"):
            raise ValueError("Agent services must not receive host/GPU access")
        for volume in service.get("volumes", []):
            target = volume.get("target", "")
            if "docker.sock" in target:
                raise ValueError("Agent must not receive the Docker socket")
        for key in service.get("environment", {}):
            if key.startswith(("MODEL_", "DATA_", "KNOWLEDGE_", "MINIO_", "OPENSEARCH_")):
                raise ValueError("A04 does not need other platform credentials")
    if set(services["agent-postgres"]["networks"]) != {"agent-private"}:
        raise ValueError("Agent DB must stay on its private network")
    if set(services["agent-service"]["networks"]) != {"agent-private"}:
        raise ValueError("management service must stay on its private network")
    if set(services["agent-mcp"]["networks"]) != {"agent-private"}:
        raise ValueError("MCP adapter must stay on its private network")
    mcp_secrets = {s["source"] for s in services["agent-mcp"].get("secrets", [])}
    if mcp_secrets != {"agent-internal-token", "agent-api-token"}:
        raise ValueError("MCP adapter may not receive DB or target API credentials")
    if not config["networks"]["agent-private"].get("internal"):
        raise ValueError("Agent private network must be internal")
    runtime_secrets = {s["source"] for s in services["agent-runtime"].get("secrets", [])}
    allowed = {"agent-internal-token", "agent-tool-credentials"}
    if tracing:
        allowed.add("agent-langfuse-tracing")
        if services["agent-runtime"].get("environment", {}).get("AGENT_TRACE_CONFIG_FILE") != "/run/secrets/agent-langfuse-tracing":
            raise ValueError("unexpected tracing configuration path")
    if runtime_secrets != allowed:
        raise ValueError("runtime may not receive database credentials")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-port-check", action="store_true", help="use only after Agent is already running")
    parser.add_argument("--tracing", action="store_true", help="validate the optional runtime tracing overlay")
    args = parser.parse_args()
    files = ["-f", "docker-compose.yml", "-f", "runtime/compose.tracing.yml"] if args.tracing else []
    config = json.loads(command(["docker", "compose", *files, "--profile", "checks", "config", "--format", "json"]))
    verify_config(config, args.tracing)
    network = config["networks"]["agent-api"]["name"]
    command(["docker", "network", "inspect", network])
    for secret in config["secrets"].values():
        path = Path(secret["file"])
        if not path.is_file() or path.is_symlink():
            raise ValueError("missing or unsafe Agent secret file")
        if path.parent.stat().st_mode & 0o077:
            raise ValueError("Agent secret directory must have mode 0700")
        if path.stat().st_mode & 0o444 != 0o444:
            raise ValueError("Agent file secret must be readable by non-root containers inside the private directory")
    if not args.skip_port_check:
        for mapping in config["services"]["agent-gateway"]["ports"]:
            with socket.socket() as sock:
                sock.bind((mapping.get("host_ip", "0.0.0.0"), int(mapping["published"])))
    print("PASS: Compose boundaries, secrets, existing network" + ("; host port check skipped." if args.skip_port_check else " and requested host port."))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as exc:
        raise SystemExit("FAIL: " + str(exc))
