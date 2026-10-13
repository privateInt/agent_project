package main

import (
	"context"
	"testing"

	trainingv1 "model-platform/gen/training/v1"
)

func TestRegisteredDatasetRequiresCompletePinBeforeStorage(t *testing.T) {
	for _, spec:=range []*trainingv1.DatasetSpec{
		{SourceType:"version",Format:"chat_messages"},
		{SourceType:"version",Format:"chat_messages",DatasetVersionId:"12345678-1234-4567-89ab-123456789abc"},
		{SourceType:"version",Format:"chat_messages",DatasetVersionId:"00000000-0000-0000-0000-000000000000"},
	} {
		// No MinIO env is configured. Validation must fail before connecting.
		if _,_,err:=resolveDataset(context.Background(),spec,t.TempDir());err==nil {t.Fatal("incomplete version pin accepted")}
	}
	path,size,err:=resolveDataset(context.Background(),nil,t.TempDir())
	if err!=nil || path!="" || size!=0 {t.Fatal("legacy synthetic dataset behavior changed")}
}
