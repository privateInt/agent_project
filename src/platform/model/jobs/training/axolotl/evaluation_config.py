"""Add pinned evaluation without changing the existing Axolotl/checkpoint config."""
import argparse
import json
from pathlib import Path
from config_adapter import load_json, render_config
from evaluation_data import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    parser.add_argument('--workspace', required=True)
    args = parser.parse_args()
    spec = load_json(Path(args.spec))
    workspace = Path(args.workspace).resolve()
    # Fail before preprocessing/training if raw training/evaluation prompts overlap.
    prepare(spec, workspace)
    result = render_config(spec, workspace)
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))


if __name__ == '__main__':
    main()
