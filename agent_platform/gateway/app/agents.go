package app

import "net/http"

func (s *Server) agentRoutes(mux *http.ServeMux) {
	if s.Agents == nil {
		return
	}
	routes := []struct {
		pattern string
		method  toolMethod
		code    int
	}{
		{"POST /api/workflow-links", s.Agents.CreateWorkflowLink, 201}, {"GET /api/workflow-links", s.Agents.ListWorkflowLinks, 200}, {"GET /api/workflow-links/{id}", s.Agents.GetWorkflowLink, 200}, {"PATCH /api/workflow-links/{id}", s.Agents.UpdateWorkflowLink, 200},
		{"POST /api/agents", s.Agents.CreateAgent, 201}, {"GET /api/agents", s.Agents.ListAgents, 200}, {"GET /api/agents/{id}", s.Agents.GetAgent, 200}, {"PATCH /api/agents/{id}", s.Agents.UpdateAgent, 200},
		{"POST /api/agents/{id}/versions", s.Agents.CreateAgentVersion, 201}, {"GET /api/agent-versions/{id}", s.Agents.GetAgentVersion, 200}, {"POST /api/agent-versions/{id}/publish", s.Agents.PublishAgentVersion, 200}, {"POST /api/agent-versions/{id}/retire", s.Agents.RetireAgentVersion, 200},
		{"POST /api/agent-runs", s.Agents.CreateAgentRun, 202}, {"GET /api/agent-runs/{id}", s.Agents.GetAgentRun, 200}, {"GET /api/agent-runs", s.Agents.ListAgentRuns, 200}, {"GET /api/agent-runs/{id}/steps", s.Agents.GetAgentSteps, 200},
		{"POST /api/agent-models/inspect", s.Agents.InspectAgentModel, 200},
	}
	for _, route := range routes {
		mux.HandleFunc(route.pattern, func(w http.ResponseWriter, r *http.Request) { s.toolRequest(w, r, route.method, route.code) })
	}
}
