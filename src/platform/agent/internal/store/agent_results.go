package store

import (
	"agent-platform/internal/agenting"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
	"regexp"
	"strconv"
	"strings"
)

var citationPattern = regexp.MustCompile(`\[S([^\]\r\n]*)\]`)
var validCitationNumber = regexp.MustCompile(`^[1-9][0-9]*$`)

func sameJSON(a, b []byte) bool {
	var x, y any
	if tooling.NullableJSON(a, &x, tooling.MaxOutput) != nil || tooling.NullableJSON(b, &y, tooling.MaxOutput) != nil {
		return false
	}
	return tooling.Hash(tooling.JSON(x)) == tooling.Hash(tooling.JSON(y))
}
func (s *Store) validateAgentResult(ctx context.Context, tx pgx.Tx, j *AgentJob, raw []byte) error {
	invalid := tooling.E("AGENT_RESULT_INVALID")
	var r struct {
		Answer    string `json:"answer"`
		Outcome   string `json:"outcome"`
		Citations []struct {
			ID     string          `json:"citation_id"`
			Source json.RawMessage `json:"source"`
		} `json:"citations"`
		Warnings   []string        `json:"warnings"`
		Truncated  bool            `json:"answer_truncated"`
		ToolCallID string          `json:"tool_call_id"`
		Selected   []string        `json:"selected_chunk_ids"`
		Excluded   json.RawMessage `json:"excluded_sources"`
		Identity   struct {
			Template        string          `json:"template_id"`
			TemplateVersion int             `json:"template_version"`
			ToolVersion     string          `json:"tool_version_id"`
			Model           json.RawMessage `json:"model"`
			Knowledge       json.RawMessage `json:"knowledge"`
		} `json:"identity"`
		Usage map[string]int64 `json:"usage"`
	}
	if tooling.NullableJSON(raw, &r, tooling.MaxOutput) != nil || strings.TrimSpace(r.Answer) == "" || len(r.Answer) > 65536 || strings.ContainsRune(r.Answer, 0) || r.Citations == nil || r.Warnings == nil || r.Selected == nil || r.Usage == nil || len(r.Citations) > 50 {
		return invalid
	}
	if r.Identity.Template != agenting.Template || r.Identity.TemplateVersion != 1 || r.Identity.ToolVersion != j.Snapshot.Version.ToolVersionID {
		return invalid
	}
	var callState string
	var callResult []byte
	err := tx.QueryRow(ctx, `SELECT state,result FROM agent.tool_calls WHERE tool_call_id=$1 AND parent_agent_run_id=$2 AND parent_step_id='search' AND tool_version_id=$3`, r.ToolCallID, j.ID, j.Snapshot.Version.ToolVersionID).Scan(&callState, &callResult)
	if err != nil || callState != "succeeded" {
		return invalid
	}
	var search struct {
		Data struct {
			Results []json.RawMessage `json:"results"`
		} `json:"data"`
		Metadata json.RawMessage `json:"metadata"`
	}
	if json.Unmarshal(callResult, &search) != nil || !sameJSON(r.Identity.Knowledge, search.Metadata) {
		return invalid
	}
	var unfinished bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM agent.agent_steps WHERE agent_run_id=$1 AND (state NOT IN ('succeeded','skipped') OR (state='skipped' AND step_id<>'generate')))`, j.ID).Scan(&unfinished); err != nil {
		return err
	}
	if unfinished {
		return invalid
	}
	var contextDetail []byte
	var generateState string
	if err = tx.QueryRow(ctx, `SELECT detail FROM agent.agent_steps WHERE agent_run_id=$1 AND step_id='context'`, j.ID).Scan(&contextDetail); err != nil {
		return err
	}
	var detail struct {
		Selected []string        `json:"selected_chunk_ids"`
		Excluded json.RawMessage `json:"excluded_sources"`
	}
	if json.Unmarshal(contextDetail, &detail) != nil || !sameJSON(tooling.JSON(detail.Selected), tooling.JSON(r.Selected)) || !sameJSON(detail.Excluded, r.Excluded) {
		return invalid
	}
	if err = tx.QueryRow(ctx, `SELECT state FROM agent.agent_steps WHERE agent_run_id=$1 AND step_id='generate'`, j.ID).Scan(&generateState); err != nil {
		return err
	}
	hits := map[string]json.RawMessage{}
	ranks := map[string]int{}
	for i, h := range search.Data.Results {
		var hit struct {
			ID string `json:"chunk_id"`
		}
		if json.Unmarshal(h, &hit) != nil || hit.ID == "" {
			return invalid
		}
		hits[hit.ID] = h
		ranks[hit.ID] = i + 1
	}
	previous := 0
	for _, id := range r.Selected {
		rank := ranks[id]
		if rank <= previous {
			return invalid
		}
		previous = rank
	}
	if len(search.Data.Results) > 0 && len(r.Selected) == 0 {
		return invalid
	}
	if len(r.Selected) == 0 {
		if generateState != "skipped" || !sameJSON(r.Identity.Model, []byte(`{}`)) || len(r.Usage) != 0 || r.Truncated {
			return invalid
		}
	} else {
		if generateState != "succeeded" {
			return invalid
		}
		var m struct {
			DeploymentID    string `json:"deployment_id"`
			ModelVersionID  string `json:"model_version_id"`
			ServedModelName string `json:"served_model_name"`
		}
		if json.Unmarshal(r.Identity.Model, &m) != nil || m.DeploymentID != j.Snapshot.Version.Model.DeploymentID || m.ModelVersionID != j.Snapshot.Version.Model.ModelVersionID || m.ServedModelName != "deployment-"+m.DeploymentID {
			return invalid
		}
		if len(r.Usage) != 3 || r.Usage["prompt_tokens"] < 0 || r.Usage["completion_tokens"] < 0 || r.Usage["completion_tokens"] > int64(j.Snapshot.Version.MaxTokens) || r.Usage["total_tokens"] != r.Usage["prompt_tokens"]+r.Usage["completion_tokens"] {
			return invalid
		}
	}
	refs := citationPattern.FindAllStringSubmatch(r.Answer, -1)
	wanted := map[string]bool{}
	for _, ref := range refs {
		n, e := strconv.Atoi(ref[1])
		if e != nil || !validCitationNumber.MatchString(ref[1]) || n < 1 || n > len(r.Selected) {
			return invalid
		}
		wanted["S"+ref[1]] = true
	}
	if r.Outcome == "no_evidence" {
		if len(r.Citations) != 0 || len(refs) != 0 || r.Truncated || r.Answer != "선택한 자료에서 질문에 답할 충분한 근거를 찾지 못했습니다." {
			return invalid
		}
	} else if r.Outcome == "answered" {
		if len(wanted) == 0 || len(wanted) != len(r.Citations) {
			return invalid
		}
	} else {
		return invalid
	}
	seen := map[string]bool{}
	previous = 0
	for _, c := range r.Citations {
		if !wanted[c.ID] || seen[c.ID] || len(c.ID) < 2 {
			return invalid
		}
		n, e := strconv.Atoi(c.ID[1:])
		if e != nil || n <= previous || n > len(r.Selected) || !sameJSON(c.Source, hits[r.Selected[n-1]]) {
			return invalid
		}
		seen[c.ID] = true
		previous = n
	}
	for _, w := range r.Warnings {
		if w != "CONTEXT_SOURCES_EXCLUDED" && w != "ANSWER_TRUNCATED" && w != "CHECK_SOURCE_QUALITY" {
			return invalid
		}
	}
	return nil
}
