package execution

import "time"

type Command struct {
    DispatchToken string `json:"-"`
    EventID string `json:"event_id,omitempty"`
	OperationID   string    `json:"operation_id"`
	Kind          string    `json:"kind"`
	ExecutionType string    `json:"execution_type"`
	ResourceType  string    `json:"resource_type"`
	ResourceID    string    `json:"resource_id"`
	Resources     Resources `json:"resources,omitempty"`
	OccurredAt    time.Time `json:"occurred_at"`
}

type Profile struct {
	Image      string
	Command    []string
	ForwardEnv []string
}
