package tooling

import (
	"agent-platform/internal/rpc"
	"strings"
	"testing"
)

func knowledgeFixture() ToolVersion {
	return ToolVersion{Kind: "knowledge_search_v1", Binding: Binding{ConnectionVersionID: rpc.NewID(), Knowledge: &KnowledgeBinding{ModelConnectionVersionID: rpc.NewID(), IndexID: rpc.NewID(), IndexVersionID: rpc.NewID(), EmbeddingSpaceID: "es_" + strings.Repeat("a", 64), EmbeddingDeploymentID: rpc.NewID(), TopK: 5, CandidateK: 5}}}
}
func TestKnowledgeValidation(t *testing.T) {
	if err := ValidateKnowledgeInspect(KnowledgeInspect{Action: "indexes", ConnectionVersionID: rpc.NewID(), ModelConnectionVersionID: rpc.NewID(), Cursor: "eyJ2IjoxLCJzY29wZSI6ImluZGV4ZXMifQ"}); err != nil {
		t.Fatal("opaque cursor rejected", err)
	}
	v := knowledgeFixture()
	create := VersionCreate{ExpectedRevision: 1, Kind: v.Kind, Binding: v.Binding, TimeoutMS: 180000}
	if err := ValidateVersion(create); err != nil {
		t.Fatal(err)
	}
	if err := ValidateArguments(v, []byte(`{"query":"  자연어 질문\n","top_k":8,"filters":{"kinds":["text","table"]}}`)); err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{`{}`, `{"query":" "}`, `{"query":"x","query":"y"}`, `{"query":null}`, `{"query":"x","top_k":0}`, `{"query":"x","top_k":true}`, `{"query":"x","candidate_k":4}`, `{"query":"x","filters":{"kinds":[]}}`, `{"query":"x","filters":{"kinds":["text","text"]}}`, `{"query":"x","filters":{"canonical_ids":["latest"]}}`, `{"query":"x","connection_version_id":"override"}`} {
		if ValidateArguments(v, []byte(raw)) == nil {
			t.Fatalf("accepted invalid input: %s", raw)
		}
	}
	if ValidateArguments(v, JSON(map[string]any{"query": strings.Repeat("가", 3000)})) == nil {
		t.Fatal("byte limit not enforced")
	}
	for _, modify := range []func(*VersionCreate){
		func(v *VersionCreate) { v.Binding.Effect = "read" }, func(v *VersionCreate) { v.Binding.ImportID = rpc.NewID() },
		func(v *VersionCreate) { v.TimeoutMS = 240001 }, func(v *VersionCreate) { v.Binding.ConnectionVersionID = "latest" },
	} {
		bad := create
		modify(&bad)
		if ValidateVersion(bad) == nil {
			t.Fatal("accepted mixed/invalid binding")
		}
	}
	create.Kind = "diagnostic_connection_v1"
	if ValidateVersion(create) == nil {
		t.Fatal("knowledge fields escaped adapter")
	}
	if ValidateKnowledgeInspect(KnowledgeInspect{Action: "resolve", ConnectionVersionID: rpc.NewID(), ModelConnectionVersionID: rpc.NewID(), IndexID: "../../private"}) == nil {
		t.Fatal("path injection")
	}
}
