package agenting

import (
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"net/url"
	"regexp"
	"strings"
	"time"
)

type WorkflowLinkCreate struct {
	DisplayName    string `json:"display_name"`
	InstanceURL    string `json:"instance_url"`
	WorkflowID     string `json:"workflow_id"`
	AgentVersionID string `json:"agent_version_id"`
}
type WorkflowLink struct {
	WorkflowLinkID string `json:"workflow_link_id"`
	WorkflowLinkCreate
	EditURL   string    `json:"edit_url"`
	Enabled   bool      `json:"enabled"`
	Revision  int64     `json:"revision"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

var workflowIDPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

func ValidateWorkflowLink(v *WorkflowLinkCreate) error {
	u, e := url.Parse(v.InstanceURL)
	if e != nil || u == nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Path != "" && u.Path != "/") || u.RawPath != "" || strings.ContainsAny(v.InstanceURL, "\r\n\t ") || len(v.InstanceURL) > 512 || !workflowIDPattern.MatchString(v.WorkflowID) || !rpc.ValidID(v.AgentVersionID) || !tooling.ValidLabel(v.DisplayName, 200, true) {
		return tooling.E("WORKFLOW_LINK_INVALID")
	}
	v.InstanceURL = strings.TrimSuffix(v.InstanceURL, "/")
	return nil
}
