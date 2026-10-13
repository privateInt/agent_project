package app

import (
	"fmt"
	"math"
	"strings"

	trainingv1 "model-platform/gen/training/v1"
)

type DatasetSpec struct {
	SourceType string `json:"source_type"`
	SourceURI  string `json:"source_uri,omitempty"`
	Format     string `json:"format"`
	DatasetVersionID string `json:"dataset_version_id,omitempty"`
	SHA256 string `json:"sha256,omitempty"`
	SizeBytes int64 `json:"size_bytes,omitempty"`
	RecordCount int64 `json:"record_count,omitempty"`
}

type LoraSpec struct {
	R       int32   `json:"r"`
	Alpha   int32   `json:"alpha"`
	Dropout float64 `json:"dropout"`
}

type TrainingSpec struct {
 CheckpointSteps int32 `json:"checkpoint_steps,omitempty"`
	Method                    string   `json:"method"`
	SequenceLength            int32    `json:"sequence_length"`
	MaxSteps                  int32    `json:"max_steps"`
	MicroBatchSize            int32    `json:"micro_batch_size"`
	GradientAccumulationSteps int32    `json:"gradient_accumulation_steps"`
	LearningRate              float64  `json:"learning_rate"`
	Lora                      LoraSpec `json:"lora"`
	SamplePacking             bool     `json:"sample_packing"`
    Strategy string `json:"strategy,omitempty"`
}

func NormalizeDatasetSpec(req *trainingv1.DatasetSpec) (DatasetSpec, error) {
	out := DatasetSpec{SourceType: "synthetic", Format: "chat_messages"}
	if req != nil {
		if value := strings.ToLower(strings.TrimSpace(req.GetSourceType())); value != "" {
			out.SourceType = value
		}
		out.SourceURI = strings.TrimSpace(req.GetSourceUri())
		out.DatasetVersionID = strings.TrimSpace(req.GetDatasetVersionId())
		if req.GetSha256() != "" || req.GetSizeBytes() != 0 || req.GetRecordCount() != 0 {
			return DatasetSpec{}, fmt.Errorf("dataset hash, size and record count are server-owned")
		}
		if value := strings.ToLower(strings.TrimSpace(req.GetFormat())); value != "" {
			out.Format = value
		}
	}
	if out.Format != "chat_messages" {
		return DatasetSpec{}, fmt.Errorf("dataset.format must be chat_messages, got %q", out.Format)
	}
	if out.SourceType != "version" && out.DatasetVersionID != "" {
		return DatasetSpec{}, fmt.Errorf("dataset_version_id requires source_type=version")
	}
	switch out.SourceType {
	case "version":
		if out.SourceURI != "" { return DatasetSpec{}, fmt.Errorf("dataset.source_uri is server-owned when source_type=version") }
		id, err := datasetUUID(out.DatasetVersionID, "dataset_version_id")
		if err != nil { return DatasetSpec{}, err }
		out.DatasetVersionID = id
	case "synthetic":
		if out.SourceURI != "" {
			return DatasetSpec{}, fmt.Errorf("dataset.source_uri must be empty when source_type=synthetic")
		}
	case "s3":
		if !strings.HasPrefix(out.SourceURI, "s3://") {
			return DatasetSpec{}, fmt.Errorf("dataset.source_uri must be an s3:// URI when source_type=s3")
		}
		if !strings.HasSuffix(strings.ToLower(out.SourceURI), ".jsonl") {
			return DatasetSpec{}, fmt.Errorf("current s3 dataset milestone requires one .jsonl object")
		}
	default:
		return DatasetSpec{}, fmt.Errorf("dataset.source_type must be synthetic, s3 or version, got %q", out.SourceType)
	}
	return out, nil
}

func NormalizeTrainingSpec(req *trainingv1.TrainingSpec) (TrainingSpec, error) {
	out := TrainingSpec{
		Method:                    "qlora",
		SequenceLength:            256,
		MaxSteps:                  1,
		MicroBatchSize:            1,
		GradientAccumulationSteps: 1,
		LearningRate:              0.0002,
		Lora: LoraSpec{
			R:       8,
			Alpha:   16,
			Dropout: 0,
		},
		SamplePacking: false,
	}
	if req != nil {
		if value := strings.ToLower(strings.TrimSpace(req.GetMethod())); value != "" {
			out.Method = value
		}
		if req.GetSequenceLength() != 0 {
			out.SequenceLength = req.GetSequenceLength()
		}
		if req.GetMaxSteps() != 0 {
			out.MaxSteps = req.GetMaxSteps()
		}
		if req.GetMicroBatchSize() != 0 {
			out.MicroBatchSize = req.GetMicroBatchSize()
		}
		if req.GetGradientAccumulationSteps() != 0 {
			out.GradientAccumulationSteps = req.GetGradientAccumulationSteps()
		}
		if req.GetLearningRate() != 0 {
			out.LearningRate = req.GetLearningRate()
		}
		if lora := req.GetLora(); lora != nil {
			if lora.GetR() != 0 {
				out.Lora.R = lora.GetR()
			}
			if lora.GetAlpha() != 0 {
				out.Lora.Alpha = lora.GetAlpha()
			}
			out.Lora.Dropout = lora.GetDropout()
		}
		out.SamplePacking = req.GetSamplePacking()
        out.CheckpointSteps = req.GetCheckpointSteps()
        out.Strategy = strings.ToLower(strings.TrimSpace(req.GetStrategy()))
	}
    if out.CheckpointSteps<0||out.CheckpointSteps>out.MaxSteps{return TrainingSpec{},fmt.Errorf("checkpoint_steps must be zero or between 1 and max_steps")}
    if out.CheckpointSteps>0&&(out.SamplePacking||out.Strategy=="ddp"){return TrainingSpec{},fmt.Errorf("checkpoint training requires single GPU without sample_packing")}
    if out.Strategy=="single" {out.Strategy=""} // Preserve legacy fingerprints.
    if out.Strategy!="" && out.Strategy!="ddp" {return TrainingSpec{},fmt.Errorf("training.strategy must be single or ddp")}
	if out.Method != "qlora" {
		return TrainingSpec{}, fmt.Errorf("current Axolotl milestone supports training.method=qlora, got %q", out.Method)
	}
	if out.SequenceLength <= 0 || out.SequenceLength > 131072 {
		return TrainingSpec{}, fmt.Errorf("training.sequence_length must be between 1 and 131072")
	}
	if out.MaxSteps <= 0 {
		return TrainingSpec{}, fmt.Errorf("training.max_steps must be > 0")
	}
	if out.MicroBatchSize <= 0 {
		return TrainingSpec{}, fmt.Errorf("training.micro_batch_size must be > 0")
	}
	if out.GradientAccumulationSteps <= 0 {
		return TrainingSpec{}, fmt.Errorf("training.gradient_accumulation_steps must be > 0")
	}
	if out.LearningRate <= 0 || math.IsNaN(out.LearningRate) || math.IsInf(out.LearningRate, 0) {
		return TrainingSpec{}, fmt.Errorf("training.learning_rate must be a finite value > 0")
	}
	if out.Lora.R <= 0 || out.Lora.Alpha <= 0 {
		return TrainingSpec{}, fmt.Errorf("training.lora.r and training.lora.alpha must be > 0")
	}
	if out.Lora.Dropout < 0 || out.Lora.Dropout >= 1 || math.IsNaN(out.Lora.Dropout) || math.IsInf(out.Lora.Dropout, 0) {
		return TrainingSpec{}, fmt.Errorf("training.lora.dropout must be >= 0 and < 1")
	}
	return out, nil
}

func (s DatasetSpec) Proto() *trainingv1.DatasetSpec {
	return &trainingv1.DatasetSpec{SourceType: s.SourceType, SourceUri: s.SourceURI, Format: s.Format, DatasetVersionId: s.DatasetVersionID, Sha256: s.SHA256, SizeBytes: s.SizeBytes, RecordCount: s.RecordCount}
}

func (s TrainingSpec) Proto() *trainingv1.TrainingSpec {
	return &trainingv1.TrainingSpec{CheckpointSteps:s.CheckpointSteps,
		Method:                    s.Method,
		SequenceLength:            s.SequenceLength,
		MaxSteps:                  s.MaxSteps,
		MicroBatchSize:            s.MicroBatchSize,
		GradientAccumulationSteps: s.GradientAccumulationSteps,
		LearningRate:              s.LearningRate,
		Lora: &trainingv1.LoraSpec{
			R:       s.Lora.R,
			Alpha:   s.Lora.Alpha,
			Dropout: s.Lora.Dropout,
		},
		SamplePacking: s.SamplePacking,
        Strategy: s.Strategy,
	}
}
