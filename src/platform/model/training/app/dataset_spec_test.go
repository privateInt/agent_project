package app

import (
	"encoding/json"
	"testing"

	trainingv1 "model-platform/gen/training/v1"
)

func TestDatasetLegacyFingerprintShape(t *testing.T) {
	for _, tc := range []struct{req *trainingv1.DatasetSpec; want string}{
		{nil,`{"source_type":"synthetic","format":"chat_messages"}`},
		{&trainingv1.DatasetSpec{SourceType:"s3",SourceUri:"s3://data/train.jsonl"},`{"source_type":"s3","source_uri":"s3://data/train.jsonl","format":"chat_messages"}`},
	} {
		spec,err:=NormalizeDatasetSpec(tc.req);if err!=nil {t.Fatal(err)}
		encoded,err:=json.Marshal(spec);if err!=nil || string(encoded)!=tc.want {t.Fatalf("legacy idempotency input changed: %s %v",encoded,err)}
	}
}

func TestDatasetVersionRequestRejectsClientPins(t *testing.T) {
	const id="12345678-1234-4567-89ab-123456789abc"
	good,err:=NormalizeDatasetSpec(&trainingv1.DatasetSpec{SourceType:" VERSION ",DatasetVersionId:" "+id+" "})
	if err!=nil || good.DatasetVersionID!=id || good.SourceURI!="" || good.SHA256!="" {t.Fatalf("normalize: %+v %v",good,err)}
	for _, req := range []*trainingv1.DatasetSpec{
		{SourceType:"version"}, {SourceType:"version",DatasetVersionId:"00000000-0000-0000-0000-000000000000"},
		{SourceType:"synthetic",DatasetVersionId:id},
		{SourceType:"version",DatasetVersionId:id,SourceUri:"s3://other/train.jsonl"},
		{SourceType:"version",DatasetVersionId:id,Sha256:"fake"},
		{SourceType:"version",DatasetVersionId:id,SizeBytes:1},
		{SourceType:"version",DatasetVersionId:id,RecordCount:2},
	} {if _,err:=NormalizeDatasetSpec(req);err==nil {t.Fatalf("accepted client pin: %+v",req)}}
}
