package execution

import "time"

type Command struct {
	DispatchToken string    `json:"-"`
	EventID       string    `json:"event_id,omitempty"`
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

// RejectedError is only returned BEFORE invoking Docker creation.
type RejectedError struct{ Reason string }

func (e *RejectedError) Error() string { return e.Reason }

// Supported CPU domain contracts; kind, execution type and resource must agree.
func Supported(kind, resource string) bool { return kind == "chunking.run" && resource == "chunking_run" || kind == "indexing.build" && resource == "index_version" }
