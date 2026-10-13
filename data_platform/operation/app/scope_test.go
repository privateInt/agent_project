package app

import (
	operationv1 "data-platform/gen/operation/v1"
	"testing"
)

func TestDataScopeRequiresMatchingDomainContract(t *testing.T) {
	for _, pair := range [][2]string{{"collection.crawl", "crawl_run"}, {"collection.browser", "crawl_run"}, {"refinement.parse", "refinement_run"}} {
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
