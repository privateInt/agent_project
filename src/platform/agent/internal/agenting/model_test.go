package agenting

import (
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"strings"
	"testing"
)

func TestAgentDefinitionLimits(t *testing.T) {
	base := VersionCreate{ExpectedRevision: 1, Definition: Definition{ToolVersionID: rpc.NewID(), Model: Model{ConnectionVersionID: rpc.NewID(), DeploymentID: rpc.NewID(), ModelVersionID: rpc.NewID()}}}
	Defaults(&base)
	if err := ValidateVersion(base); err != nil {
		t.Fatal(err)
	}
	for _, change := range []func(*VersionCreate){func(v *VersionCreate) { v.Model.ModelVersionID = "" }, func(v *VersionCreate) { v.TemplateVersion = 2 }, func(v *VersionCreate) { v.ContextMaxChars = 12001 }, func(v *VersionCreate) { v.MaxTokens = 513 }, func(v *VersionCreate) { v.TimeoutMS = 600001 }, func(v *VersionCreate) { v.Prompt = strings.Repeat("한", 1400) }} {
		v := base
		change(&v)
		if ValidateVersion(v) == nil {
			t.Fatal("invalid Agent definition accepted")
		}
	}
	for _, raw := range []string{`{"question":"q","question":"other"}`, `{"question":null}`, `{"question":"q","model_url":"http://example.test"}`} {
		var in Input
		if tooling.StrictJSON([]byte(raw), &in) == nil {
			t.Fatal("invalid public input accepted", raw)
		}
	}
}
