package app_test

import (
	"agent-platform/internal/agenting"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

// Optional local OTLP fixture observes the real Go worker -> Python runtime
// boundary. Server Docker tests can leave this unset and use ops/tracing.py.
func assertAgentTrace(t *testing.T, run agenting.Run) {
	t.Helper()
	url := os.Getenv("AGENT_TEST_TRACE_COLLECTOR")
	if url == "" {
		return
	}
	sum := sha256.Sum256([]byte("agent-platform.o02:agent:" + run.AgentRunID))
	traceID := hex.EncodeToString(sum[:])[:32]
	client := &http.Client{Timeout: 2 * time.Second}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := client.Get(url + "/spans?trace=" + traceID)
		if err != nil {
			t.Fatal("collector unavailable")
		}
		var rows []struct {
			TraceID    string `json:"traceId"`
			ID         string `json:"spanId"`
			Parent     string `json:"parentSpanId"`
			Name       string `json:"name"`
			Attributes []struct {
				Key   string `json:"key"`
				Value struct {
					String string `json:"stringValue"`
				} `json:"value"`
			} `json:"attributes"`
		}
		err = json.NewDecoder(resp.Body).Decode(&rows)
		resp.Body.Close()
		if err != nil {
			t.Fatal("invalid collector response")
		}
		if len(rows) < 10 {
			time.Sleep(50 * time.Millisecond)
			continue
		}
		if len(rows) != 10 {
			t.Fatal("duplicate/unexpected spans", len(rows))
		}
		ids, parents := map[string]string{}, map[string]string{}
		for _, row := range rows {
			if row.TraceID != traceID || ids[row.Name] != "" {
				t.Fatal("trace identity/uniqueness lost")
			}
			ids[row.Name], parents[row.Name] = row.ID, row.Parent
			meta := map[string]string{}
			for _, a := range row.Attributes {
				if a.Key == "langfuse.observation.input" || a.Key == "langfuse.observation.output" || strings.Contains(a.Value.String, "자연어 질문") {
					t.Fatal("private body exported")
				}
				meta[strings.TrimPrefix(a.Key, "langfuse.observation.metadata.")] = a.Value.String
			}
			if meta["agent_run_id"] != run.AgentRunID || meta["request_id"] != run.RequestID || meta["state"] != "succeeded" {
				t.Fatal("durable execution identity/state not linked", row.Name)
			}
			if row.Name == "tool.execute" && meta["tool_call_id"] != *run.ToolCallID {
				t.Fatal("durable Tool ID lost")
			}
		}
		for _, name := range []string{"validate", "search", "context", "generate", "finalize"} {
			if ids["step."+name] == "" || parents["step."+name] != ids["agent.execute"] {
				t.Fatal("step ancestry lost")
			}
		}
		if parents["model.chat"] != ids["step.generate"] || parents["tool.execute"] != ids["step.search"] || parents["model.embedding"] != ids["tool.execute"] || parents["knowledge.search"] != ids["tool.execute"] {
			t.Fatal("distributed Tool/model ancestry lost")
		}
		return
	}
	t.Fatal("trace did not arrive")
}
