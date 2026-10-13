package app

import (
	operationv1 "knowledge-platform/gen/operation/v1"
	"testing"
)

func TestKnowledgeScopeRequiresMatchingDomainContract(t *testing.T) {
	for _, pair := range [][2]string{{"chunking.run", "chunking_run"}, {"indexing.build", "index_version"}} {
		r := &operationv1.CreateOperationRequest{Kind: pair[0], ExecutionType: pair[0], ResourceType: pair[1], MaxAttempts: 1}
		if e := validateScope(r); e != nil {
			t.Fatal(e)
		}
		r.ResourceType = "model"
		if validateScope(r) == nil {
			t.Fatal("wrong resource accepted")
		}
		r.ResourceType = pair[1]
		r.Kind = "other"
		if validateScope(r) == nil {
			t.Fatal("mismatched kind accepted")
		}
	}
}
