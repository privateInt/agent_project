#!/usr/bin/env python3
"""Copy only shared connection settings from rendered Model Compose; no venv."""
import argparse
import json
import os
from pathlib import Path
import subprocess


def settings(config):
    services = config.get("services", {})
    operation = services.get("operation", {}).get("environment", {})
    training = services.get("training", {}).get("environment", {})
    result = {
        "DATA_POSTGRES_URL": operation.get("POSTGRES_URL"),
        "DATA_KAFKA_BROKERS": operation.get("KAFKA_BROKERS"),
        "DATA_MINIO_ENDPOINT": training.get("MINIO_ENDPOINT"),
        "DATA_MINIO_ACCESS_KEY": training.get("MINIO_ACCESS_KEY"),
        "DATA_MINIO_SECRET_KEY": training.get("MINIO_SECRET_KEY"),
        "DATA_MINIO_USE_SSL": training.get("MINIO_USE_SSL", "false"),
        "DATA_NETWORK": config.get("networks", {}).get("ops-infra", {}).get("name"),
        "DATA_GATEWAY_PORT": "14000",
    }
    for name, value in result.items():
        if not isinstance(value, str) or not value or any(c in value for c in "\r\n\x00"):
            raise ValueError("Missing or invalid source setting: " + name)
    if result["DATA_MINIO_USE_SSL"] not in ("true", "false"):
        raise ValueError("MINIO_USE_SSL must be true or false")
    return result


def dotenv(values):
    # Compose single-quoted values do not interpolate $variables. Escaping a
    # quote is defined by Compose's dotenv syntax, not shell quoting.
    return "# Generated shared infrastructure settings; keep this file private.\n" + "".join(
        key + "='" + value.replace("'", "\\'") + "'\n"
        for key, value in values.items()
    )


def create_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-platform", type=Path, default=Path("../model_platform"))
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / ".env"
    if target.exists():
        raise ValueError(".env already exists; it was not replaced")
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=args.model_platform.resolve(), capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise ValueError("Cannot read Model Compose configuration; check its path and permissions")
    values = settings(json.loads(result.stdout))
    create_private(target, dotenv(values))
    try:
        checked = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=target.parent, capture_output=True, text=True, timeout=60,
        )
        if checked.returncode:
            raise ValueError("Generated Data Compose settings could not be resolved")
        resolved = json.loads(checked.stdout)
        services = resolved["services"]
        actual = services["data-collection"]["environment"]
        for name in ("POSTGRES_URL", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_USE_SSL"):
            if actual.get(name) != values["DATA_" + name]:
                raise ValueError("Compose changed a connection value; check quoting or exported DATA variables")
        if services["data-operation"]["environment"].get("KAFKA_BROKERS") != values["DATA_KAFKA_BROKERS"]:
            raise ValueError("Compose changed Kafka connection settings")
        if resolved["networks"]["data-infra"].get("name") != values["DATA_NETWORK"]:
            raise ValueError("Compose changed the shared network name")
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        target.unlink()
        raise ValueError("Generated configuration verification failed; no services were changed and the new file was removed") from None
    print("Created data_platform/.env (0600); shared connection values were not printed.")
    print("Separate targets: data_operations, data_collection, data-sources, data.operations.requested.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        raise SystemExit("FAIL: " + str(exc))
