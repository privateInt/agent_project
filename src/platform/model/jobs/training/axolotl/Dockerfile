# syntax=docker/dockerfile:1
# Build with: docker buildx bake --builder default -f docker-bake.hcl --load training-job
# training_binary is linked to this source tree's training-binary target.
ARG AXOLOTL_IMAGE=axolotlai/axolotl:0.18.0
FROM ${AXOLOTL_IMAGE} AS training-job
LABEL model-platform.execution-gate="1"
LABEL model-platform.training-ddp="1"
ARG MLFLOW_CLIENT_VERSION=3.15.1
ARG NVIDIA_ML_PY_VERSION=13.610.43
WORKDIR /app
# The Axolotl image provides the trainer; add only the lightweight MLflow
# tracking client plus system-metric dependencies. The Tracking Server itself
# remains shared infrastructure (ops-mlflow).
RUN uv pip install --python /workspace/axolotl-venv/bin/python \
      "mlflow-skinny==${MLFLOW_CLIENT_VERSION}" \
      "psutil>=5.9,<8" \
      "nvidia-ml-py==${NVIDIA_ML_PY_VERSION}"
ENV AXOLOTL_DO_NOT_TRACK=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false
COPY --from=training_binary /out/training-axolotl /app/training-axolotl
COPY jobs/training/axolotl/config_adapter.py /app/config_adapter.py
COPY jobs/training/axolotl/distributed_train.py /app/distributed_train.py
COPY jobs/training/axolotl/checkpoint_store.py /app/checkpoint_store.py
COPY jobs/training/axolotl/checkpoint_train.py /app/checkpoint_train.py
COPY jobs/training/axolotl/evaluation_data.py /app/evaluation_data.py
COPY jobs/training/axolotl/evaluation_config.py /app/evaluation_config.py
COPY jobs/training/axolotl/evaluation_train.py /app/evaluation_train.py
# Override any base-image entrypoint; Dispatcher only selects this image.
ENTRYPOINT ["/app/training-axolotl"]

LABEL model-platform.training-output-preservation="1"

LABEL model-platform.training-checkpoints="1"

LABEL model-platform.training-evaluation="1"
