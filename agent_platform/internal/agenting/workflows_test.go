package agenting

import "testing"

func TestWorkflowLinkURLBoundaries(t *testing.T) {
	base := WorkflowLinkCreate{DisplayName: "질문 Flow", InstanceURL: "http://simsys4090:16200/", WorkflowID: "AbC_123-4", AgentVersionID: "11111111-1111-4111-8111-111111111111"}
	if e := ValidateWorkflowLink(&base); e != nil || base.InstanceURL != "http://simsys4090:16200" {
		t.Fatal(e)
	}
	for _, u := range []string{"javascript:alert(1)", "http://user:password@host", "https://host/x", "https://host/?token=x", "https://host/#fragment", "http://host\n/", "//host", "https://host/%2f"} {
		v := base
		v.InstanceURL = u
		if ValidateWorkflowLink(&v) == nil {
			t.Fatalf("accepted unsafe origin %q", u)
		}
	}
	for _, id := range []string{"../other", "a/b", "a?token=x", "a#x", ""} {
		v := base
		v.WorkflowID = id
		if ValidateWorkflowLink(&v) == nil {
			t.Fatalf("accepted unsafe workflow ID %q", id)
		}
	}
}
