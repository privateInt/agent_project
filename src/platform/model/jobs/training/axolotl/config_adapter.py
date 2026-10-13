from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def yaml_scalar(value: str) -> str:
    # JSON strings are valid YAML scalars and avoid quoting/path edge cases.
    return json.dumps(value, ensure_ascii=False)


def yaml_bool(value: bool) -> str:
    return "true" if value else "false"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return data


def detect_chat_template(model_dir: Path) -> tuple[str, list[str]]:
    config_path = model_dir / "config.json"
    if not config_path.exists():
        return "tokenizer_default", []
    config = load_json(config_path)
    values: list[str] = []
    model_type = config.get("model_type")
    if isinstance(model_type, str):
        values.append(model_type.lower())
    architectures = config.get("architectures")
    if isinstance(architectures, list):
        values.extend(str(item).lower() for item in architectures)
    text_config = config.get("text_config")
    if isinstance(text_config, dict) and isinstance(text_config.get("model_type"), str):
        values.append(str(text_config["model_type"]).lower())

    if any("gemma3" in value or "gemma_3" in value for value in values):
        return "gemma3", ["<end_of_turn>"]
    return "tokenizer_default", []


def validate_chat_messages_jsonl(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError(f"dataset file does not exist: {path}")
    seen = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            seen += 1
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSONL at line {line_number}: {exc}") from exc
            messages = record.get("messages") if isinstance(record, dict) else None
            if not isinstance(messages, list) or not messages:
                raise RuntimeError(f"line {line_number}: messages must be a non-empty list")
            for index, message in enumerate(messages):
                if not isinstance(message, dict):
                    raise RuntimeError(f"line {line_number}: messages[{index}] must be an object")
                if not isinstance(message.get("role"), str) or not isinstance(message.get("content"), str):
                    raise RuntimeError(
                        f"line {line_number}: messages[{index}] requires string role/content"
                    )
            # Structural validation only needs a small sample. Axolotl preprocess
            # remains the authoritative tokenizer/template validation step.
            if seen >= 20:
                break
    if seen == 0:
        raise RuntimeError("dataset contains no JSONL records")


def materialize_dataset(spec: dict[str, Any], workspace: Path) -> tuple[Path, str]:
    dataset = spec["dataset"]
    source_type = dataset["source_type"]
    dataset_format = dataset["format"]
    if dataset_format != "chat_messages":
        raise RuntimeError(f"unsupported dataset format: {dataset_format}")

    if source_type == "synthetic":
        dataset_path = workspace / "synthetic-train.jsonl"
        # Axolotl 0.18.0 debug preprocessing samples with randrange(0, len(dataset)-1).
        # A one-record dataset therefore raises randrange(0, 0). Keep at least two
        # deterministic records for this integration-only validation dataset.
        dataset_records = [
            {
                "messages": [
                    {"role": "user", "content": "Return the sum of 2 and 3."},
                    {"role": "assistant", "content": "5"},
                ]
            },
            {
                "messages": [
                    {"role": "user", "content": "Return the product of 2 and 4."},
                    {"role": "assistant", "content": "8"},
                ]
            },
        ]
        dataset_path.write_text(
            "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in dataset_records),
            encoding="utf-8",
        )
        return dataset_path, "synthetic_validation_only"

    if source_type in ("s3", "version"):
        local_path = Path(dataset.get("local_path") or "").resolve()
        validate_chat_messages_jsonl(local_path)
        return local_path, "registered_version_jsonl" if source_type == "version" else "s3_jsonl"

    raise RuntimeError(f"unsupported dataset source_type: {source_type}")


def render_config(spec: dict[str, Any], workspace: Path) -> dict[str, object]:
    model_dir = Path(spec["model_dir"]).resolve()
    if not model_dir.is_dir():
        raise RuntimeError(f"model directory does not exist: {model_dir}")

    training = spec["training"]
    if training["method"] != "qlora":
        raise RuntimeError(f"unsupported training method: {training['method']}")

    world_size = int(spec.get('world_size', 1))
    strategy = training.get('strategy') or ('ddp' if world_size > 1 else 'single')
    if not 1 <= world_size <= 16 or strategy != ('ddp' if world_size > 1 else 'single'):
        raise RuntimeError('strategy and local GPU count disagree')
    # World size comes from torchrun; preprocessing remains a single process.
    find_unused = world_size == 1
    checkpoint_options = '' if world_size == 1 else 'gradient_checkpointing_kwargs:\n  use_reentrant: false\n'

    dataset_path, dataset_kind = materialize_dataset(spec, workspace)
    prepared_dir = workspace / "prepared"
    output_dir = Path(spec["output_dir"]).resolve() if spec.get("output_dir") else workspace / "output"
    config_path = workspace / "axolotl.yml"
    chat_template, eot_tokens = detect_chat_template(model_dir)

    eot_block = ""
    if eot_tokens:
        eot_block = "eot_tokens:\n" + "".join(f"  - {yaml_scalar(token)}\n" for token in eot_tokens)

    tracking = spec.get("tracking") or {}
    mlflow_block = ""
    if bool(tracking.get("enabled")):
        tracking_uri = str(tracking.get("tracking_uri") or "").strip()
        experiment_name = str(tracking.get("experiment_name") or "").strip()
        run_name = str(tracking.get("run_name") or "").strip()
        if not tracking_uri or not experiment_name or not run_name:
            raise RuntimeError("MLflow tracking requires tracking_uri, experiment_name, and run_name")
        # Metrics/params only. Training artifacts are persisted by the platform's
        # dedicated MinIO lifecycle, so do not duplicate checkpoints into MLflow.
        mlflow_block = f"""\
use_mlflow: true
mlflow_tracking_uri: {yaml_scalar(tracking_uri)}
mlflow_experiment_name: {yaml_scalar(experiment_name)}
mlflow_run_name: {yaml_scalar(run_name)}
hf_mlflow_log_artifacts: false
"""

    checkpoint_block = ""
    checkpoint_steps = training.get("checkpoint_steps", 0)
    if checkpoint_steps:
        if (type(checkpoint_steps) is not int or checkpoint_steps < 1 or
                checkpoint_steps > int(training['max_steps']) or world_size != 1 or
                training['sample_packing'] or spec['dataset']['source_type'] not in ('synthetic', 'version') or
                not spec.get('output_dir')):
            raise RuntimeError('checkpoint training requires local single GPU, stable dataset and unpacked samples')
        from checkpoint_store import prepare_context
        resume_path = prepare_context(spec, workspace, dataset_path, model_dir)
        checkpoint_block = f"""save_strategy: steps
save_steps: {checkpoint_steps}
save_total_limit: 2
save_only_model: false
seed: 42
data_seed: 42
dataloader_num_workers: 0
ignore_data_skip: false
auto_resume_from_checkpoints: false
"""
        if resume_path:
            checkpoint_block += f"resume_from_checkpoint: {yaml_scalar(resume_path)}\n"
    elif spec.get('resume'):
        raise RuntimeError('resume requires checkpoint training')

    lora = training["lora"]
    config = f"""\
base_model: {yaml_scalar(str(model_dir))}
load_in_4bit: true

ddp_find_unused_parameters: {yaml_bool(find_unused)}
chat_template: {yaml_scalar(chat_template)}
{eot_block}
datasets:
  - path: {yaml_scalar(str(dataset_path))}
    ds_type: json
    type: chat_template
    field_messages: messages
    roles_to_train:
      - assistant

dataset_prepared_path: {yaml_scalar(str(prepared_dir))}
val_set_size: 0.0
output_dir: {yaml_scalar(str(output_dir))}

adapter: qlora
sequence_len: {int(training['sequence_length'])}
sample_packing: {yaml_bool(bool(training['sample_packing']))}
lora_r: {int(lora['r'])}
lora_alpha: {int(lora['alpha'])}
lora_dropout: {float(lora['dropout'])}
lora_target_linear: true

gradient_accumulation_steps: {int(training['gradient_accumulation_steps'])}
micro_batch_size: {int(training['micro_batch_size'])}
max_steps: {int(training['max_steps'])}
optimizer: adamw_bnb_8bit
lr_scheduler: cosine
learning_rate: {float(training['learning_rate'])}
bf16: true
tf32: true
gradient_checkpointing: true
{checkpoint_options}\
logging_steps: 1
warmup_steps: 0
weight_decay: 0.0
{checkpoint_block}{mlflow_block}"""
    config_path.write_text(config, encoding="utf-8")

    return {
        "engine": "axolotl",
        "method": training["method"],
        "config_path": str(config_path),
        "dataset_path": str(dataset_path),
        "output_dir": str(output_dir),
        "dataset_kind": dataset_kind,
        "chat_template": chat_template,
        "sequence_len": int(training["sequence_length"]),
        "micro_batch_size": int(training["micro_batch_size"]),
        "max_steps": int(training["max_steps"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate Model Platform TrainingSpec into Axolotl YAML")
    parser.add_argument("--spec", required=True, help="platform-training-spec.json")
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    spec = load_json(Path(args.spec).resolve())

    print(json.dumps(render_config(spec, workspace), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
