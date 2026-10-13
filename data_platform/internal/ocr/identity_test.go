package ocr

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestIdentityRejectsArbitraryEndpointAndChangedModelAlias(t *testing.T) {
	id := "11111111-1111-4111-8111-111111111111"
	good := Identity{DeploymentID: id, ModelVersionID: "22222222-2222-4222-8222-222222222222", RuntimeImageID: "sha256:" + strings.Repeat("a", 64), RuntimeContainerID: strings.Repeat("b", 64), Endpoint: "http://model-serving-" + id + ":8000/v1", Model: "deployment-" + id, Profile: Profile, ManifestSHA256: strings.Repeat("c", 64)}
	if good.Validate() != nil {
		t.Fatal("valid identity rejected")
	}
	raw, _ := json.Marshal(good)
	parsed, e := Decode(string(raw))
	if e != nil || *parsed != good {
		t.Fatal(e)
	}
	for _, change := range []func(*Identity){func(i *Identity) { i.Endpoint = "http://example.invalid/v1" }, func(i *Identity) { i.Model = "other" }, func(i *Identity) { i.RuntimeContainerID = "short" }, func(i *Identity) { i.DeploymentID = "../bad" }, func(i *Identity) { i.Profile = "unknown" }} {
		bad := good
		change(&bad)
		if bad.Validate() == nil {
			t.Fatal("invalid identity accepted")
		}
	}
}
