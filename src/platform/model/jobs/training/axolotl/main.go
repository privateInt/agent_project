package main

import (
 "model-platform/internal/operationreport"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/artifacts"
 "model-platform/internal/trainingoutput"
 "model-platform/internal/trainingcheckpoint"
 "model-platform/internal/trainingevaluation"
	"model-platform/internal/storage"
	"model-platform/internal/datasets"
    "model-platform/internal/trainingparallel"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type adapterResult struct {
 Evaluation *trainingevaluation.Result `json:"-"`
 Checkpoint trainingcheckpoint.Evidence `json:"-"`
    Parallelism trainingparallel.Result `json:"-"`
	Engine         string `json:"engine"`
	Method         string `json:"method"`
	ConfigPath     string `json:"config_path"`
	DatasetPath    string `json:"dataset_path"`
	OutputDir      string `json:"output_dir"`
	DatasetKind    string `json:"dataset_kind"`
	ChatTemplate   string `json:"chat_template"`
	SequenceLen    int    `json:"sequence_len"`
	MicroBatchSize int    `json:"micro_batch_size"`
	MaxSteps       int    `json:"max_steps"`
}

type trainingOutputSummary struct {
	FileCount int      `json:"file_count"`
	Bytes     int64    `json:"bytes"`
	Files     []string `json:"files"`
}

type adapterInput struct {
 Evaluation *evaluationInput `json:"evaluation,omitempty"`
 CheckpointMetadata map[string]any `json:"checkpoint_metadata,omitempty"`
 Resume *trainingv1.TrainingResume `json:"resume,omitempty"`
 ResumeDirectory string `json:"resume_directory,omitempty"`
 OutputDir string `json:"output_dir,omitempty"`
    WorldSize int `json:"world_size"`
	ModelDir string               `json:"model_dir"`
	Dataset  adapterDatasetInput  `json:"dataset"`
	Training adapterTrainingInput `json:"training"`
	Tracking adapterTrackingInput `json:"tracking"`
}

type adapterTrackingInput struct {
	Enabled        bool   `json:"enabled"`
	TrackingURI    string `json:"tracking_uri,omitempty"`
	ExperimentName string `json:"experiment_name,omitempty"`
	RunName        string `json:"run_name,omitempty"`
}

type mlflowTracking struct {
	Enabled        bool
	TrackingURI    string
	PublicURI      string
	ExperimentName string
	ExperimentID   string
	RunName        string
	RunID          string
}

type adapterDatasetInput struct {
	SourceType string `json:"source_type"`
	SourceURI  string `json:"source_uri,omitempty"`
	LocalPath  string `json:"local_path,omitempty"`
	Format     string `json:"format"`
}

type adapterTrainingInput struct {
 CheckpointSteps int32 `json:"checkpoint_steps,omitempty"`
    Strategy string `json:"strategy,omitempty"`
	Method                    string           `json:"method"`
	SequenceLength            int32            `json:"sequence_length"`
	MaxSteps                  int32            `json:"max_steps"`
	MicroBatchSize            int32            `json:"micro_batch_size"`
	GradientAccumulationSteps int32            `json:"gradient_accumulation_steps"`
	LearningRate              float64          `json:"learning_rate"`
	Lora                      adapterLoraInput `json:"lora"`
	SamplePacking             bool             `json:"sample_packing"`
}

type adapterLoraInput struct {
	R       int32   `json:"r"`
	Alpha   int32   `json:"alpha"`
	Dropout float64 `json:"dropout"`
}

func main() {
	// Axolotl is the only Training compute engine in the current project.
	// The public API stays framework-neutral: TrainingSpec + DatasetSpec are
	// translated here into an Axolotl config by config_adapter.py.
	ctx := context.Background()
	operationID := required("OPERATION_ID")
	trainingRunID := required("RESOURCE_ID")

	opConn, err := grpc.NewClient(env("OPERATION_GRPC_ADDRESS", "operation:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatal(err)
	}
	defer opConn.Close()
	ops := operationreport.New(operationv1.NewOperationServiceClient(opConn))
    if err := ops.Authorize(ctx,operationID,trainingRunID,"training.run"); err != nil { log.Printf("execution not authorized; process stopped: %v",err); return }
	mlflow := mlflowTrackingFromEnv(trainingRunID)

	trainingConn, err := grpc.NewClient(env("TRAINING_GRPC_ADDRESS", "training:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		failOperation(ctx, ops, operationID, "TRAINING_GRPC_CONNECT_FAILED", err.Error())
		return
	}
	defer trainingConn.Close()
	training := trainingv1.NewTrainingServiceClient(trainingConn)

	managementConn, err := grpc.NewClient(env("MANAGEMENT_GRPC_ADDRESS", "management:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "MANAGEMENT_GRPC_CONNECT_FAILED", err.Error())
		return
	}
	defer managementConn.Close()
	management := managementv1.NewManagementServiceClient(managementConn)

	if _, err := ops.MarkRunning(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "resolving_training_input"}); err != nil { log.Printf("job stopped: cannot mark operation running: %v", err); return }

	run, err := training.GetTrainingRun(ctx, &trainingv1.GetTrainingRunRequest{TrainingRunId: trainingRunID})
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "TRAINING_RUN_LOOKUP_FAILED", err.Error())
		return
	}
	sourceModelVersionID := strings.TrimSpace(run.GetSourceModelVersionId())
	if sourceModelVersionID == "" {
		failTraining(ctx, ops, training, operationID, trainingRunID, "SOURCE_MODEL_MISSING", "training run has no source_model_version_id")
		return
	}

	modelVersion, err := management.GetModelVersion(ctx, &managementv1.GetModelVersionRequest{ModelVersionId: sourceModelVersionID})
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "MODEL_VERSION_LOOKUP_FAILED", err.Error())
		return
	}
	if modelVersion.GetState() != "ready" {
		failTraining(ctx, ops, training, operationID, trainingRunID, "MODEL_VERSION_NOT_READY", fmt.Sprintf("model version state=%s", modelVersion.GetState()))
		return
	}
	if modelVersion.GetArtifactKind() == "adapter" {
		failTraining(ctx, ops, training, operationID, trainingRunID, "UNSUPPORTED_SOURCE_ARTIFACT", "adapter continuation is not implemented")
		return
	}
	modelMeta, err := management.GetModel(ctx, &managementv1.GetModelRequest{ModelId: modelVersion.GetModelId()})
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "MODEL_LOOKUP_FAILED", err.Error())
		return
	}
	if modelMeta.GetModelType() != "llm" {
		failTraining(ctx, ops, training, operationID, trainingRunID, "UNSUPPORTED_MODEL_TYPE", fmt.Sprintf("Axolotl currently supports model_type=llm, got %s", modelMeta.GetModelType()))
		return
	}

	requestedGPUCount, err := positiveIntEnv("REQUESTED_GPU_COUNT")
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "INVALID_GPU_REQUEST", err.Error())
		return
	}
	if err := validateParallelRequest(run,requestedGPUCount); err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "UNSUPPORTED_GPU_COUNT", err.Error())
		return
	}

	bucket, prefix, err := storage.ParseS3URI(modelVersion.GetArtifactUri())
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "ARTIFACT_URI_INVALID", err.Error())
		return
	}
	expectedBucket := env("MINIO_BUCKET", "model-artifacts")
	if bucket != expectedBucket {
		failTraining(ctx, ops, training, operationID, trainingRunID, "ARTIFACT_BUCKET_MISMATCH", fmt.Sprintf("artifact bucket=%s configured bucket=%s", bucket, expectedBucket))
		return
	}

	store, err := connectStore(ctx, bucket, false)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "MINIO_CONNECT_FAILED", err.Error())
		return
	}

	workspace, err := os.MkdirTemp("", "model-axolotl-train-*")
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "WORKSPACE_CREATE_FAILED", err.Error())
		return
	}
	defer os.RemoveAll(workspace)
 savedDir := ""
 if root:=strings.TrimSpace(os.Getenv("TRAINING_OUTPUT_ROOT"));root!=""{
  token,_:=ops.ExecutionIdentity()
  savedDir,err=trainingoutput.Directory(root,trainingRunID,token)
  if err==nil{err=os.MkdirAll(filepath.Join(savedDir,"output"),0700)}
  if err!=nil{failTraining(ctx,ops,training,operationID,trainingRunID,"OUTPUT_PRESERVATION_FAILED",err.Error());return}
  unlock,lockErr:=trainingoutput.Lock(savedDir)
  if lockErr!=nil{failTraining(ctx,ops,training,operationID,trainingRunID,"OUTPUT_PRESERVATION_FAILED",lockErr.Error());return}
  defer unlock()
  if _,e:=os.Stat(filepath.Join(savedDir,trainingoutput.ReceiptName));e==nil{failTraining(ctx,ops,training,operationID,trainingRunID,"OUTPUT_ALREADY_COMPLETED","refusing to retrain a preserved output");return}
 }
    resumeDirectory,checkpointErr:=checkpointInput(ctx,run,training,operationv1.NewOperationServiceClient(opConn))
    if checkpointErr!=nil{failTraining(ctx,ops,training,operationID,trainingRunID,"CHECKPOINT_INPUT_INVALID",checkpointErr.Error());return}
	modelDir := filepath.Join(workspace, "model")

	_, _ = ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "downloading_model_artifact"})
	artifactFiles, artifactBytes, err := store.DownloadPrefix(ctx, prefix, modelDir)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "MODEL_DOWNLOAD_FAILED", err.Error())
		return
	}
	log.Printf("Axolotl model artifact downloaded model_version=%s files=%d bytes=%d", sourceModelVersionID, artifactFiles, artifactBytes)

	_, _ = ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "resolving_dataset"})
	datasetPath, datasetBytes, err := resolveDataset(ctx, run.GetDataset(), workspace)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "DATASET_RESOLVE_FAILED", err.Error())
		return
	}

	_, _ = ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "rendering_axolotl_config"})
	token,instance:=ops.ExecutionIdentity()
	evaluation,evalErr:=resolveEvaluation(ctx,run,workspace)
    if evalErr!=nil{failTraining(ctx,ops,training,operationID,trainingRunID,"EVALUATION_INPUT_INVALID",evalErr.Error());return}
	adapter, err := renderAxolotlConfig(run, modelDir, datasetPath, workspace, mlflow,token,instance,resumeDirectory,evaluation)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "AXOLOTL_CONFIG_RENDER_FAILED", err.Error())
		return
	}

	_, _ = ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "axolotl_preprocess_validation"})
	preprocessOutput, err := validateAxolotlConfig(adapter.ConfigPath)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "AXOLOTL_PREPROCESS_FAILED", err.Error())
		return
	}

	if mlflow.Enabled {
		_, _ = ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "preparing_mlflow_tracking"})
		tags := map[string]string{
			"model_platform.training_run_id":         trainingRunID,
			"model_platform.operation_id":            operationID,
			"model_platform.source_model_version_id": sourceModelVersionID,
			"model_platform.engine":                  "axolotl",
			"model_platform.training_method":         run.GetTraining().GetMethod(),
            "model_platform.resume_parent": run.GetResume().GetSourceTrainingRunId(),
            "model_platform.resume_step": strconv.Itoa(int(run.GetResume().GetCheckpointStep())),
		}
		if run.GetDataset().GetSourceType() == "version" {
			tags["model_platform.dataset_version_id"] = run.GetDataset().GetDatasetVersionId()
			tags["model_platform.dataset_sha256"] = run.GetDataset().GetSha256()
		}
		mlflow, err = prepareMLflowRun(ctx, mlflow, tags)
		if err != nil {
			failTraining(ctx, ops, training, operationID, trainingRunID, "MLFLOW_PREPARE_FAILED", err.Error())
			return
		}
		if _, err := training.SetTrainingTracking(ctx, &trainingv1.SetTrainingTrackingRequest{
			TrainingRunId: trainingRunID, MlflowExperimentId: mlflow.ExperimentID,
			MlflowRunId: mlflow.RunID, MlflowRunUrl: mlflowRunURL(mlflow),
		}); err != nil {
			_ = finishMLflowRun(ctx, mlflow, "FAILED")
			failTraining(ctx, ops, training, operationID, trainingRunID, "MLFLOW_LINK_FAILED", err.Error())
			return
		}
		if err := os.Setenv("MLFLOW_RUN_ID", mlflow.RunID); err != nil {
			_ = finishMLflowRun(ctx, mlflow, "FAILED")
			failTraining(ctx, ops, training, operationID, trainingRunID, "MLFLOW_ENV_FAILED", err.Error())
			return
		}
		log.Printf("MLflow tracking prepared training_run=%s experiment=%s mlflow_run=%s", trainingRunID, mlflow.ExperimentName, mlflow.RunID)
	}

	mlflowFinished := false
	if mlflow.Enabled {
		defer func() {
			if !mlflowFinished {
				if err := finishMLflowRun(context.Background(), mlflow, "FAILED"); err != nil {
					log.Printf("MLflow fail-safe finalization failed run=%s: %v", mlflow.RunID, err)
				}
			}
		}()
	}

	_, _ = ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "axolotl_training"})
	trainOutput, trainExitCode, err := runAxolotlTrain(adapter.ConfigPath, requestedGPUCount, trainingRunID, run.GetTraining().GetMaxSteps())
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "AXOLOTL_TRAIN_FAILED", fmt.Sprintf("exit_code=%d: %s", trainExitCode, err.Error()))
		return
	}

    if run.GetEvaluation()!=nil {
        adapter.Evaluation,err=readEvaluation(workspace,run)
        if err!=nil{failTraining(ctx,ops,training,operationID,trainingRunID,"EVALUATION_RESULT_INVALID",err.Error());return}
    }
    if run.GetTraining().GetCheckpointSteps()>0 {
        adapter.Checkpoint,err=readCheckpointEvidence(workspace,run)
        if err==nil{err=clearCheckpointStaging(adapter.OutputDir)}
        if err!=nil{failTraining(ctx,ops,training,operationID,trainingRunID,"CHECKPOINT_EVIDENCE_INVALID",err.Error());return}
    }
	batch:=int64(requestedGPUCount)*int64(run.GetTraining().GetMicroBatchSize())*int64(run.GetTraining().GetGradientAccumulationSteps())
    adapter.Parallelism=trainingparallel.Result{Strategy:"single",WorldSize:1,EffectiveBatchSize:batch}
    if requestedGPUCount>1 {
        adapter.Parallelism,err=trainingparallel.Read(filepath.Join(workspace,"ddp-evidence"),trainingRunID,requestedGPUCount,int64(run.GetTraining().GetMaxSteps()),batch)
        if err!=nil {failTraining(ctx,ops,training,operationID,trainingRunID,"DDP_EVIDENCE_INVALID",err.Error());return}
    }

	_, _ = ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "collecting_training_output"})
	outputSummary, err := summarizeTrainingOutput(adapter.OutputDir)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "AXOLOTL_OUTPUT_INVALID", err.Error())
		return
	}
	trainLoss, lossFound := findTrainingLoss(adapter.OutputDir, trainOutput)

	// Reserve a content-addressed output, upload it, and read it back before
	// Management publishes a ready model_version. The workspace remains alive
	// until this finishes; no checkpoint is stored only in the deleted container.
	_, _ = ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "building_training_manifest"})
	manifest, manifestBytes, manifestDigest, err := artifacts.Build(adapter.OutputDir)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "ARTIFACT_MANIFEST_FAILED", err.Error())
		return
	}
	// Build and persist compute evidence before any external publication call.
 if savedDir!=""{
  raw,e:=buildResult(sourceModelVersionID,modelVersion.GetRepoId(),artifactFiles,artifactBytes,datasetBytes,run,adapter,preprocessOutput,trainOutput,trainExitCode,outputSummary,trainLoss,lossFound,mlflow,nil)
  if e!=nil{failTraining(ctx,ops,training,operationID,trainingRunID,"OUTPUT_PRESERVATION_FAILED",e.Error());return}
  token,instance:=ops.ExecutionIdentity()
  receipt:=trainingoutput.Receipt{Version:1,RunID:trainingRunID,OperationID:operationID,DispatchToken:token,InstanceID:instance,Node:os.Getenv("EXECUTION_NODE_NAME"),SourceID:sourceModelVersionID,Manifest:manifestBytes,Digest:manifestDigest,Result:json.RawMessage(raw)}
  if e=trainingoutput.Seal(savedDir,receipt);e!=nil{failTrainingWithResult(ctx,ops,training,operationID,trainingRunID,"OUTPUT_PRESERVATION_FAILED",e.Error(),raw);return}
  log.Printf("completed training output preserved run=%s files=%d bytes=%d manifest=%s",trainingRunID,manifest.FileCount,manifest.TotalBytes,manifestDigest)
  // A scoped test hook applies to exactly one preselected training run.
  if os.Getenv("TRAINING_TEST_FAIL_PUBLICATION_RUN_ID")==trainingRunID{
   failTrainingWithResult(ctx,ops,training,operationID,trainingRunID,"TRAINING_ARTIFACT_PUBLISH_FAILED","scoped publication failure fixture",raw);return
  }
 }
	published, err := publishTrainingOutput(ctx, ops, management, store, operationID, trainingRunID, sourceModelVersionID, adapter.OutputDir, manifest, manifestBytes, manifestDigest)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "TRAINING_ARTIFACT_PUBLISH_FAILED", err.Error())
		return
	}

	resultJSON, err := buildResult(sourceModelVersionID, modelVersion.GetRepoId(), artifactFiles, artifactBytes, datasetBytes, run, adapter, preprocessOutput, trainOutput, trainExitCode, outputSummary, trainLoss, lossFound, mlflow, published)
	if err != nil {
		failTraining(ctx, ops, training, operationID, trainingRunID, "AXOLOTL_RESULT_ENCODE_FAILED", err.Error())
		return
	}
	if mlflow.Enabled {
		if err := finishMLflowRun(ctx, mlflow, "FINISHED"); err != nil {
			failTrainingWithResult(ctx, ops, training, operationID, trainingRunID, "MLFLOW_FINALIZE_FAILED", err.Error(), resultJSON)
			return
		}
		mlflowFinished = true
	}
	if _, err := training.CompleteTraining(ctx, &trainingv1.CompleteTrainingRequest{TrainingRunId: trainingRunID, ResultJson: resultJSON, OutputModelVersionId: published.GetModelVersionId()}); err != nil {
		failTrainingWithResult(ctx, ops, training, operationID, trainingRunID, "TRAINING_COMPLETE_FAILED", err.Error(), resultJSON)
		return
	}
	if _, err := ops.SucceedOperation(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "completed"}); err != nil { log.Printf("domain completed but operation completion report failed: %v", err); return }
	if lossFound {
		log.Printf("Axolotl QLoRA training completed run=%s model_version=%s output_model_version=%s exit_code=%d loss=%g output_files=%d", trainingRunID, sourceModelVersionID, published.GetModelVersionId(), trainExitCode, trainLoss, outputSummary.FileCount)
	} else {
		log.Printf("Axolotl QLoRA training completed run=%s model_version=%s exit_code=%d loss=unavailable output_files=%d", trainingRunID, sourceModelVersionID, trainExitCode, outputSummary.FileCount)
	}
}

func resolveDataset(ctx context.Context, spec *trainingv1.DatasetSpec, workspace string) (string, int64, error) {
	sourceType := "synthetic"
	format := "chat_messages"
	sourceURI := ""
	if spec != nil {
		if v := strings.TrimSpace(spec.GetSourceType()); v != "" {
			sourceType = v
		}
		if v := strings.TrimSpace(spec.GetFormat()); v != "" {
			format = v
		}
		sourceURI = strings.TrimSpace(spec.GetSourceUri())
	}
	if format != "chat_messages" {
		return "", 0, fmt.Errorf("unsupported dataset format %q", format)
	}
	if sourceType == "synthetic" {
		return "", 0, nil
	}
	if sourceType != "s3" && sourceType != "version" {
		return "", 0, fmt.Errorf("unsupported dataset source_type %q", sourceType)
	}
	if sourceType == "version" {
		if id, err := uuid.Parse(spec.GetDatasetVersionId()); err != nil || id == uuid.Nil { return "", 0, fmt.Errorf("dataset version ID is missing or invalid") }
		if err := datasetPin(spec).Validate(); err != nil { return "", 0, err }
	}
	bucket, objectName, err := storage.ParseS3URI(sourceURI)
	if err != nil {
		return "", 0, err
	}
	store, err := connectStore(ctx, bucket, true)
	if err != nil {
		return "", 0, err
	}
	datasetPath := filepath.Join(workspace, "dataset", "train.jsonl")
	if sourceType == "version" {
		reader, err := store.OpenObject(ctx, objectName)
		if err != nil { return "", 0, err }
		defer reader.Close()
		if err := datasets.Materialize(reader, datasetPath, datasetPin(spec)); err != nil { return "", 0, fmt.Errorf("verify registered dataset: %w", err) }
		return datasetPath, spec.GetSizeBytes(), nil
	}
	bytes, err := store.DownloadObject(ctx, objectName, datasetPath)
	if err != nil {
		return "", 0, err
	}
	return datasetPath, bytes, nil
}

func datasetPin(spec *trainingv1.DatasetSpec) datasets.Info {
	return datasets.Info{SHA256: spec.GetSha256(), SizeBytes: spec.GetSizeBytes(), RecordCount: spec.GetRecordCount()}
}

func renderAxolotlConfig(run *trainingv1.TrainingRun, modelDir, datasetPath, workspace string, tracking mlflowTracking,token,instance,resumeDirectory string,evaluation *evaluationInput) (adapterResult, error) {
	dataset := run.GetDataset()
	training := run.GetTraining()
	if dataset == nil || training == nil || training.GetLora() == nil {
		return adapterResult{}, fmt.Errorf("training run is missing normalized dataset/training spec")
	}
	outputDir:=""
 if root:=strings.TrimSpace(os.Getenv("TRAINING_OUTPUT_ROOT"));root!=""{
  dir,e:=trainingoutput.Directory(root,run.GetTrainingRunId(),os.Getenv("DISPATCH_TOKEN"));if e!=nil{return adapterResult{},e};outputDir=filepath.Join(dir,"output")
 }
 input := adapterInput{Evaluation:evaluation,OutputDir:outputDir,Resume:run.GetResume(),ResumeDirectory:resumeDirectory,
        WorldSize: int(run.GetResources().GetGpuCount()),
		ModelDir: modelDir,
		Dataset: adapterDatasetInput{
			SourceType: dataset.GetSourceType(),
			SourceURI:  dataset.GetSourceUri(),
			LocalPath:  datasetPath,
			Format:     dataset.GetFormat(),
		},
		Tracking: adapterTrackingInput{
			Enabled:        tracking.Enabled,
			TrackingURI:    tracking.TrackingURI,
			ExperimentName: tracking.ExperimentName,
			RunName:        tracking.RunName,
		},
		Training: adapterTrainingInput{CheckpointSteps:training.GetCheckpointSteps(),
            Strategy: training.GetStrategy(),
			Method:                    training.GetMethod(),
			SequenceLength:            training.GetSequenceLength(),
			MaxSteps:                  training.GetMaxSteps(),
			MicroBatchSize:            training.GetMicroBatchSize(),
			GradientAccumulationSteps: training.GetGradientAccumulationSteps(),
			LearningRate:              training.GetLearningRate(),
			Lora: adapterLoraInput{
				R:       training.GetLora().GetR(),
				Alpha:   training.GetLora().GetAlpha(),
				Dropout: training.GetLora().GetDropout(),
			},
			SamplePacking: training.GetSamplePacking(),
		},
	}
	if training.GetCheckpointSteps()>0{input.CheckpointMetadata=checkpointMetadata(run,token,instance)}
	encoded, err := json.MarshalIndent(input, "", "  ")
	if err != nil {
		return adapterResult{}, err
	}
	inputPath := filepath.Join(workspace, "platform-training-spec.json")
	if err := os.WriteFile(inputPath, encoded, 0o600); err != nil {
		return adapterResult{}, err
	}

	pythonBinary := env("PYTHON_BIN", "python")
	scriptPath := env("AXOLOTL_ADAPTER_SCRIPT", "/app/config_adapter.py")
    if evaluation!=nil{scriptPath="/app/evaluation_config.py"}
	command := exec.Command(pythonBinary, scriptPath, "--spec", inputPath, "--workspace", workspace)
	output, err := command.CombinedOutput()
	if err != nil {
		return adapterResult{}, fmt.Errorf("config adapter failed: %s", cleanOutput(output, err))
	}
	var result adapterResult
	if err := json.Unmarshal([]byte(strings.TrimSpace(string(output))), &result); err != nil {
		return adapterResult{}, fmt.Errorf("decode config adapter result: %w; output=%s", err, strings.TrimSpace(string(output)))
	}
	if result.ConfigPath == "" || result.DatasetPath == "" || result.OutputDir == "" {
		return adapterResult{}, fmt.Errorf("config adapter returned incomplete paths")
	}
	return result, nil
}

func validateAxolotlConfig(configPath string) (string, error) {
	command := exec.Command("axolotl", "preprocess", configPath, "--debug", "--debug-num-examples", "1")
	command.Env = append(os.Environ(),
		"CUDA_VISIBLE_DEVICES=0",
		"AXOLOTL_DO_NOT_TRACK=1",
		"HF_HUB_OFFLINE=1",
		"HF_DATASETS_OFFLINE=1",
		"TRANSFORMERS_OFFLINE=1",
		"TOKENIZERS_PARALLELISM=false",
	)
	output, err := command.CombinedOutput()
	if err != nil {
		return "", fmt.Errorf("axolotl preprocess failed: %s", cleanOutput(output, err))
	}
	return strings.TrimSpace(string(output)), nil
}

func runAxolotlTrain(configPath string, worldSize int, trainingRunID string, steps int32) (string, int, error) {
    binary,args:=trainingCommand(configPath,worldSize,trainingRunID,steps)
    contextPath:=filepath.Join(filepath.Dir(configPath),"checkpoint-context.json")
    if _,e:=os.Stat(contextPath);e==nil{binary=env("PYTHON_BIN","python");args=[]string{env("AXOLOTL_CHECKPOINT_SCRIPT","/app/checkpoint_train.py"),"--config",configPath,"--context",contextPath}}
    evaluationPath:=filepath.Join(filepath.Dir(configPath),"evaluation-context.json")
    if _,e:=os.Stat(evaluationPath);e==nil{binary=env("PYTHON_BIN","python");args=[]string{"/app/evaluation_train.py","--config",configPath,"--context",evaluationPath}}
	command := exec.Command(binary,args...)
	command.Env = append(os.Environ(),
		"AXOLOTL_DO_NOT_TRACK=1",
		"HF_HUB_OFFLINE=1",
		"HF_DATASETS_OFFLINE=1",
		"TRANSFORMERS_OFFLINE=1",
		"TOKENIZERS_PARALLELISM=false",
	)

	// Stream Axolotl progress to the one-shot container logs while keeping only
	// a bounded tail for result_json. This avoids unbounded in-memory log growth
	// during long training runs and enables `docker logs -f model-op-<id>`.
	output := newTailWriter(256 * 1024)
	stream := io.MultiWriter(os.Stdout, output)
	command.Stdout = stream
	command.Stderr = stream
	err := command.Run()
	exitCode := 0
	if err != nil {
		exitCode = -1
		var exitErr *exec.ExitError
		if errors.As(err, &exitErr) {
			exitCode = exitErr.ExitCode()
		}
		return strings.TrimSpace(output.String()), exitCode, fmt.Errorf("axolotl train failed: %s", cleanOutput([]byte(output.String()), err))
	}
	return strings.TrimSpace(output.String()), exitCode, nil
}

type tailWriter struct {
	mu    sync.Mutex
	limit int
	data  []byte
}

func newTailWriter(limit int) *tailWriter {
	return &tailWriter{limit: limit}
}

func (w *tailWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.limit <= 0 {
		return len(p), nil
	}
	w.data = append(w.data, p...)
	if len(w.data) > w.limit {
		trim := len(w.data) - w.limit
		copy(w.data, w.data[trim:])
		w.data = w.data[:w.limit]
	}
	return len(p), nil
}

func (w *tailWriter) String() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return string(append([]byte(nil), w.data...))
}

func summarizeTrainingOutput(outputDir string) (trainingOutputSummary, error) {
	outputDir = strings.TrimSpace(outputDir)
	if outputDir == "" {
		return trainingOutputSummary{}, fmt.Errorf("Axolotl output_dir is empty")
	}
	info, err := os.Stat(outputDir)
	if err != nil {
		return trainingOutputSummary{}, fmt.Errorf("Axolotl output_dir is unavailable: %w", err)
	}
	if !info.IsDir() {
		return trainingOutputSummary{}, fmt.Errorf("Axolotl output_dir is not a directory: %s", outputDir)
	}

	summary := trainingOutputSummary{}
	err = filepath.WalkDir(outputDir, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		relative, err := filepath.Rel(outputDir, path)
		if err != nil {
			return err
		}
		summary.FileCount++
		summary.Bytes += info.Size()
		summary.Files = append(summary.Files, filepath.ToSlash(relative))
		return nil
	})
	if err != nil {
		return trainingOutputSummary{}, fmt.Errorf("inspect Axolotl output_dir: %w", err)
	}
	if summary.FileCount == 0 {
		return trainingOutputSummary{}, fmt.Errorf("Axolotl output_dir contains no files: %s", outputDir)
	}
	sort.Strings(summary.Files)
	if len(summary.Files) > 200 {
		summary.Files = summary.Files[:200]
	}
	return summary, nil
}

var lossPattern = regexp.MustCompile(`(?i)["'](?:train[/_])?loss["']\s*:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)`)

func findTrainingLoss(outputDir, trainOutput string) (float64, bool) {
	if loss, ok := lossFromTrainerState(outputDir); ok {
		return loss, true
	}
	matches := lossPattern.FindAllStringSubmatch(trainOutput, -1)
	for index := len(matches) - 1; index >= 0; index-- {
		value, err := strconv.ParseFloat(matches[index][1], 64)
		if err == nil && !math.IsNaN(value) && !math.IsInf(value, 0) {
			return value, true
		}
	}
	return 0, false
}

func lossFromTrainerState(outputDir string) (float64, bool) {
	type trainerState struct {
		LogHistory []map[string]any `json:"log_history"`
	}
	var bestLoss float64
	var bestStep float64 = -1
	found := false
	_ = filepath.WalkDir(outputDir, func(path string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() || entry.Name() != "trainer_state.json" {
			return nil
		}
		encoded, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		var state trainerState
		if err := json.Unmarshal(encoded, &state); err != nil {
			return nil
		}
		for _, item := range state.LogHistory {
			loss, ok := numericMetric(item, "train/loss", "train_loss", "loss")
			if !ok {
				continue
			}
			step, hasStep := numericMetric(item, "step", "global_step")
			if !hasStep {
				step = bestStep + 1
			}
			if !found || step >= bestStep {
				bestLoss = loss
				bestStep = step
				found = true
			}
		}
		return nil
	})
	return bestLoss, found
}

func numericMetric(values map[string]any, keys ...string) (float64, bool) {
	for _, key := range keys {
		value, ok := values[key]
		if !ok {
			continue
		}
		switch typed := value.(type) {
		case float64:
			if !math.IsNaN(typed) && !math.IsInf(typed, 0) {
				return typed, true
			}
		case string:
			parsed, err := strconv.ParseFloat(typed, 64)
			if err == nil && !math.IsNaN(parsed) && !math.IsInf(parsed, 0) {
				return parsed, true
			}
		}
	}
	return 0, false
}

func buildResult(sourceModelVersionID, repoID string, artifactFiles int, artifactBytes, datasetBytes int64, run *trainingv1.TrainingRun, adapter adapterResult, preprocessOutput, trainOutput string, trainExitCode int, outputSummary trainingOutputSummary, trainLoss float64, lossFound bool, tracking mlflowTracking, published *managementv1.ModelVersion) (string, error) {
	dataset := run.GetDataset()
	training := run.GetTraining()
	result := map[string]any{
		"smoke_kind":              "axolotl_qlora_train",
		"engine":                  adapter.Engine,
		"engine_version":          axolotlVersion(),
		"method":                  adapter.Method,
		"source_model_version_id": sourceModelVersionID,
		"repo_id":                 repoID,
		"artifact_files":          artifactFiles,
		"artifact_bytes":          artifactBytes,
		"dataset_source_type":     dataset.GetSourceType(),
		"dataset_source_uri":      dataset.GetSourceUri(),
		"dataset_format":          dataset.GetFormat(),
		"dataset_bytes":           datasetBytes,
		"dataset_kind":            adapter.DatasetKind,
		"dataset_version_id":      dataset.GetDatasetVersionId(),
		"dataset_sha256":          dataset.GetSha256(),
		"dataset_record_count":    dataset.GetRecordCount(),
		"chat_template":           adapter.ChatTemplate,
		"training_method":         training.GetMethod(),
		"sequence_length":         training.GetSequenceLength(),
		"max_steps":               training.GetMaxSteps(),
		"micro_batch_size":        training.GetMicroBatchSize(),
		"gradient_accumulation":   training.GetGradientAccumulationSteps(),
		"learning_rate":           training.GetLearningRate(),
		"lora_r":                  training.GetLora().GetR(),
		"lora_alpha":              training.GetLora().GetAlpha(),
		"lora_dropout":            training.GetLora().GetDropout(),
		"sample_packing":          training.GetSamplePacking(),
		"requested_node":          strings.TrimSpace(os.Getenv("REQUESTED_NODE_NAME")),
		"requested_gpu_ids":       strings.TrimSpace(os.Getenv("REQUESTED_GPU_IDS")),
		"requested_gpu_count":     strings.TrimSpace(os.Getenv("REQUESTED_GPU_COUNT")),
		"validation_command":      "axolotl preprocess --debug --debug-num-examples 1",
		"preprocess_output_tail":  tail(preprocessOutput, 6000),
		"training_command":        "axolotl train",
		"train_exit_code":         trainExitCode,
		"train_output_tail":       tail(trainOutput, 12000),
		"output_dir":              adapter.OutputDir,
		"output_file_count":       outputSummary.FileCount,
		"output_bytes":            outputSummary.Bytes,
		"output_files":            outputSummary.Files,
		"train_loss_found":        lossFound,
		"mlflow_enabled":          tracking.Enabled,
		"output_model_version_id": published.GetModelVersionId(),
		"output_artifact_uri":     published.GetArtifactUri(),
		"output_manifest_sha256":  published.GetArtifactManifestSha256(),
		"output_artifact_kind":    published.GetArtifactKind(),
        "parallelism": adapter.Parallelism,
	}
    if run.GetEvaluation()!=nil{result["evaluation"]=adapter.Evaluation}
    if training.GetCheckpointSteps()>0{result["checkpoint"]=adapter.Checkpoint;result["training_command"]="python checkpoint_train.py"}
    if run.GetEvaluation()!=nil{result["training_command"]="python evaluation_train.py"}
    if adapter.Parallelism.WorldSize>1 {result["training_command"]="python -m torch.distributed.run --standalone --nnodes=1 --max-restarts=0 distributed_train.py"}
	if tracking.Enabled {
		result["mlflow_tracking_uri"] = tracking.TrackingURI
		result["mlflow_public_uri"] = tracking.PublicURI
		result["mlflow_experiment_name"] = tracking.ExperimentName
		result["mlflow_experiment_id"] = tracking.ExperimentID
		result["mlflow_run_name"] = tracking.RunName
		result["mlflow_run_id"] = tracking.RunID
		result["mlflow_run_url"] = mlflowRunURL(tracking)
	}
	if lossFound {
		result["train_loss"] = trainLoss
	} else {
		result["train_loss"] = nil
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		return "", err
	}
	return string(encoded), nil
}

func mlflowTrackingFromEnv(trainingRunID string) mlflowTracking {
	trackingURI := strings.TrimRight(strings.TrimSpace(os.Getenv("MLFLOW_TRACKING_URI")), "/")
	publicURI := strings.TrimRight(strings.TrimSpace(os.Getenv("MLFLOW_PUBLIC_URI")), "/")
	if publicURI == "" {
		publicURI = trackingURI
	}
	return mlflowTracking{
		Enabled:        trackingURI != "",
		TrackingURI:    trackingURI,
		PublicURI:      publicURI,
		ExperimentName: env("MLFLOW_EXPERIMENT_NAME", "model-platform-training"),
		RunName:        trainingRunID,
	}
}

type mlflowExperimentResponse struct {
	Experiment struct {
		ExperimentID string `json:"experiment_id"`
	} `json:"experiment"`
}

type mlflowCreateExperimentResponse struct {
	ExperimentID string `json:"experiment_id"`
}

type mlflowRunInfo struct {
	RunID        string `json:"run_id"`
	RunName      string `json:"run_name"`
	ExperimentID string `json:"experiment_id"`
	Status       string `json:"status"`
}

type mlflowRunResponse struct {
	Run struct {
		Info mlflowRunInfo `json:"info"`
	} `json:"run"`
}

type mlflowSearchRunsResponse struct {
	Runs []struct {
		Info mlflowRunInfo `json:"info"`
	} `json:"runs"`
}

func prepareMLflowRun(ctx context.Context, tracking mlflowTracking, tags map[string]string) (mlflowTracking, error) {
	if !tracking.Enabled {
		return tracking, nil
	}
	experimentID, err := ensureMLflowExperiment(ctx, tracking)
	if err != nil {
		return tracking, err
	}
	tracking.ExperimentID = experimentID

	existingRunID, err := findMLflowRun(ctx, tracking, tags["model_platform.training_run_id"])
	if err != nil {
		return tracking, err
	}
	if existingRunID != "" {
		tracking.RunID = existingRunID
		return tracking, nil
	}

	orderedKeys := make([]string, 0, len(tags))
	for key := range tags {
		orderedKeys = append(orderedKeys, key)
	}
	sort.Strings(orderedKeys)
	runTags := make([]map[string]string, 0, len(orderedKeys))
	for _, key := range orderedKeys {
		runTags = append(runTags, map[string]string{"key": key, "value": tags[key]})
	}
	body := map[string]any{
		"experiment_id": experimentID,
		"run_name":      tracking.RunName,
		"start_time":    time.Now().UnixMilli(),
		"tags":          runTags,
	}
	var response mlflowRunResponse
	if err := mlflowJSON(ctx, tracking.TrackingURI, http.MethodPost, "/api/2.0/mlflow/runs/create", nil, body, &response); err != nil {
		return tracking, fmt.Errorf("create MLflow run: %w", err)
	}
	tracking.RunID = strings.TrimSpace(response.Run.Info.RunID)
	if tracking.RunID == "" {
		return tracking, fmt.Errorf("create MLflow run returned empty run_id")
	}
	return tracking, nil
}

func ensureMLflowExperiment(ctx context.Context, tracking mlflowTracking) (string, error) {
	query := url.Values{"experiment_name": []string{tracking.ExperimentName}}
	var existing mlflowExperimentResponse
	err := mlflowJSON(ctx, tracking.TrackingURI, http.MethodGet, "/api/2.0/mlflow/experiments/get-by-name", query, nil, &existing)
	if err == nil {
		if id := strings.TrimSpace(existing.Experiment.ExperimentID); id != "" {
			return id, nil
		}
		return "", fmt.Errorf("MLflow experiment lookup returned empty experiment_id")
	}
	if !errors.Is(err, errMLflowNotFound) {
		return "", fmt.Errorf("lookup MLflow experiment %q: %w", tracking.ExperimentName, err)
	}

	var created mlflowCreateExperimentResponse
	createErr := mlflowJSON(ctx, tracking.TrackingURI, http.MethodPost, "/api/2.0/mlflow/experiments/create", nil, map[string]any{"name": tracking.ExperimentName}, &created)
	if createErr == nil && strings.TrimSpace(created.ExperimentID) != "" {
		return strings.TrimSpace(created.ExperimentID), nil
	}
	// Another concurrent Training Job may have created the same experiment.
	// Re-read by name before treating RESOURCE_ALREADY_EXISTS as fatal.
	var raced mlflowExperimentResponse
	if retryErr := mlflowJSON(ctx, tracking.TrackingURI, http.MethodGet, "/api/2.0/mlflow/experiments/get-by-name", query, nil, &raced); retryErr == nil {
		if id := strings.TrimSpace(raced.Experiment.ExperimentID); id != "" {
			return id, nil
		}
	}
	return "", fmt.Errorf("create MLflow experiment %q: %w", tracking.ExperimentName, createErr)
}

func findMLflowRun(ctx context.Context, tracking mlflowTracking, trainingRunID string) (string, error) {
	if tracking.ExperimentID == "" || trainingRunID == "" {
		return "", nil
	}
	filter := fmt.Sprintf(`tags."model_platform.training_run_id" = '%s'`, strings.ReplaceAll(trainingRunID, "'", "\\'"))
	body := map[string]any{
		"experiment_ids": []string{tracking.ExperimentID},
		"filter":         filter,
		"max_results":    10,
		"order_by":       []string{"attributes.start_time DESC"},
	}
	var response mlflowSearchRunsResponse
	if err := mlflowJSON(ctx, tracking.TrackingURI, http.MethodPost, "/api/2.0/mlflow/runs/search", nil, body, &response); err != nil {
		return "", fmt.Errorf("search MLflow run for training_run_id=%s: %w", trainingRunID, err)
	}
	for _, candidate := range response.Runs {
		if id := strings.TrimSpace(candidate.Info.RunID); id != "" {
			return id, nil
		}
	}
	return "", nil
}

func finishMLflowRun(ctx context.Context, tracking mlflowTracking, status string) error {
	if !tracking.Enabled || strings.TrimSpace(tracking.RunID) == "" {
		return nil
	}
	body := map[string]any{
		"run_id":   tracking.RunID,
		"status":   status,
		"end_time": time.Now().UnixMilli(),
	}
	if err := mlflowJSON(ctx, tracking.TrackingURI, http.MethodPost, "/api/2.0/mlflow/runs/update", nil, body, nil); err != nil {
		return fmt.Errorf("finalize MLflow run %s status=%s: %w", tracking.RunID, status, err)
	}
	return nil
}

var errMLflowNotFound = errors.New("MLflow resource does not exist")

func mlflowJSON(ctx context.Context, trackingURI, method, path string, query url.Values, body any, target any) error {
	endpoint := strings.TrimRight(trackingURI, "/") + path
	if len(query) > 0 {
		endpoint += "?" + query.Encode()
	}
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = strings.NewReader(string(encoded))
	}
	req, err := http.NewRequestWithContext(ctx, method, endpoint, reader)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, endpoint, err)
	}
	defer resp.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		message := strings.TrimSpace(string(payload))
		if resp.StatusCode == http.StatusNotFound || strings.Contains(message, "RESOURCE_DOES_NOT_EXIST") {
			return fmt.Errorf("%w: %s", errMLflowNotFound, message)
		}
		return fmt.Errorf("MLflow HTTP %d: %s", resp.StatusCode, message)
	}
	if target == nil || len(payload) == 0 {
		return nil
	}
	if err := json.Unmarshal(payload, target); err != nil {
		return fmt.Errorf("decode MLflow response: %w", err)
	}
	return nil
}

func mlflowRunURL(tracking mlflowTracking) string {
	if !tracking.Enabled || tracking.PublicURI == "" || tracking.ExperimentID == "" || tracking.RunID == "" {
		return ""
	}
	return fmt.Sprintf("%s/#/experiments/%s/runs/%s", strings.TrimRight(tracking.PublicURI, "/"), url.PathEscape(tracking.ExperimentID), url.PathEscape(tracking.RunID))
}

func axolotlVersion() string {
	command := exec.Command("python", "-c", `import importlib.metadata; print(importlib.metadata.version("axolotl"))`)
	output, err := command.CombinedOutput()
	if err != nil {
		return "unknown"
	}
	return strings.TrimSpace(string(output))
}

func connectStore(ctx context.Context, bucket string, existingOnly bool) (*storage.MinIO, error) {
	useSSL, err := strconv.ParseBool(env("MINIO_USE_SSL", "false"))
	if err != nil {
		return nil, err
	}
	if existingOnly {
		return storage.ConnectExisting(ctx, required("MINIO_ENDPOINT"), required("MINIO_ACCESS_KEY"), required("MINIO_SECRET_KEY"), bucket, useSSL)
	}
	return storage.Connect(ctx, required("MINIO_ENDPOINT"), required("MINIO_ACCESS_KEY"), required("MINIO_SECRET_KEY"), bucket, useSSL)
}

func tail(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	return value[len(value)-limit:]
}

func cleanOutput(output []byte, fallback error) string {
	message := strings.TrimSpace(string(output))
	if message == "" && fallback != nil {
		message = fallback.Error()
	}
	return message
}

func positiveIntEnv(name string) (int, error) {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return 0, fmt.Errorf("%s is required", name)
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return 0, fmt.Errorf("%s must be a positive integer: %q", name, raw)
	}
	return value, nil
}

func failTraining(ctx context.Context, ops operationv1.OperationServiceClient, training trainingv1.TrainingServiceClient, operationID, trainingRunID, code, message string) {
	if training != nil {
		_, _ = training.FailTraining(ctx, &trainingv1.FailTrainingRequest{TrainingRunId: trainingRunID})
	}
	failOperation(ctx, ops, operationID, code, message)
}

func failOperation(ctx context.Context, ops operationv1.OperationServiceClient, id, code, message string) {
	if ops != nil {
		_, _ = ops.FailOperation(ctx, &operationv1.FailOperationRequest{OperationId: id, Phase: "failed", ErrorCode: code, ErrorMessage: message, Retryable: false})
	}
	log.Printf("training failed code=%s message=%s", code, message)
}

func required(name string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	log.Fatalf("%s is required", name)
	return ""
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func failTrainingWithResult(ctx context.Context, ops operationv1.OperationServiceClient, training trainingv1.TrainingServiceClient, operationID, trainingRunID, code, message, resultJSON string) {
	if training != nil {
		if _, err := training.FailTraining(ctx, &trainingv1.FailTrainingRequest{TrainingRunId: trainingRunID, ResultJson: resultJSON}); err != nil {
			log.Printf("persist failed training result: %v", err)
		}
	}
	failOperation(ctx, ops, operationID, code, message)
}

// publishTrainingOutput is idempotent for the same manifest. A different
// manifest for the same training_run_id is rejected by Management.
func publishTrainingOutput(ctx context.Context, ops operationv1.OperationServiceClient, management managementv1.ManagementServiceClient, store *storage.MinIO, operationID, trainingRunID, sourceID, root string, manifest artifacts.Manifest, encoded []byte, digest string) (*managementv1.ModelVersion, error) {
 return trainingoutput.Publish(ctx,management,store,trainingRunID,sourceID,root,manifest,encoded,digest,func(phase string)error{
  _,err:=ops.UpdatePhase(ctx,&operationv1.UpdateOperationRequest{OperationId:operationID,Phase:phase});return err
 })
}
