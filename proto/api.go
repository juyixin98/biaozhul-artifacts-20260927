package proto

import "time"

// SubmitRequest creates one run: a root task plus its descendant policy.
// Child specs are produced by the compute kernel when op=spawn/chain/parsum
// executes; the submit body only seeds the root.
type SubmitRequest struct {
	RunID      string    `json:"run_id"`
	Root       TaskSpec  `json:"root"`
	BudgetMs   int       `json:"budget_ms"` // hard wall-clock budget from acceptance; 0 = unbounded
	ClientRef  string    `json:"client_ref,omitempty"`
}

// FailureDetail is one item of the "failures / uncertain conclusions" list.
type FailureDetail struct {
	Class      FailureClass `json:"class"`
	Message    string       `json:"message"`
	NodeID     string       `json:"node_id,omitempty"`
	TransferID string       `json:"transfer_id,omitempty"`
	TaskID     string       `json:"task_id,omitempty"`
	SinceSeq   int64        `json:"since_seq,omitempty"`
}

// RunStatus is the explainable status document. Counts are dependency
// evidence: a root announcement is only credible with open=0,
// unacknowledged=0, engaged_nodes=0, root_deficit=0.
type RunStatus struct {
	SchemaVersion string       `json:"schema_version"`
	RunID         string       `json:"run_id"`
	ClientRef     string       `json:"client_ref,omitempty"`
	Phase         RunPhase     `json:"phase"`
	Announced     bool         `json:"announced"`
	SubmittedAt   time.Time    `json:"submitted_at"`
	Deadline      *time.Time   `json:"deadline,omitempty"`

	// Dependency-count evidence (the Dijkstra-Scholten bookkeeping).
	RootNode       string `json:"root_node"`
	RootDeficit    int64  `json:"root_deficit"`
	EngagedNodes   int    `json:"engaged_nodes"`
	OpenEdges      int64  `json:"open_edges"`
	SettledEdges   int64  `json:"settled_edges"`
	UnackedEdges   int64  `json:"unacknowledged_edges"`
	SignalsApplied int64  `json:"signals_applied"`
	DuplicateSignals int64 `json:"duplicate_signals"`

	// Workload counters.
	TasksQueued    int64 `json:"tasks_queued"`
	TasksRunning   int64 `json:"tasks_running"`
	TasksSucceeded int64 `json:"tasks_succeeded"`
	TasksFailed    int64 `json:"tasks_failed"`

	// Explainability.
	LastEventSeq int64           `json:"last_event_seq"`
	Trail        []TrailEntry    `json:"trail,omitempty"`
	Failures     []FailureDetail `json:"failures,omitempty"`
}

// TrailEntry is one human/machine-readable key step. Handler is the processing
// location (HTTP route or "sweeper"/"kernel"); Ver references the semantics.
type TrailEntry struct {
	Seq       int64     `json:"seq"`
	At        time.Time `json:"at"`
	Kind      EventKind `json:"kind"`
	Handler   string    `json:"handler"`
	NodeID    string    `json:"node_id,omitempty"`
	TransferID string   `json:"transfer_id,omitempty"`
	TaskID    string    `json:"task_id,omitempty"`
	Detail    string    `json:"detail,omitempty"`
}

// ClaimRequest is a worker polling for work on one of its partitions.
type ClaimRequest struct {
	WorkerID   string `json:"worker_id"`
	Partition  string `json:"partition"`
}

// ClaimResponse hands a task to a worker. Empty TaskID means no work (but an
// empty queue is NOT termination; DS counters are in the status document).
type ClaimResponse struct {
	RunID      string `json:"run_id,omitempty"`
	TaskID     string `json:"task_id,omitempty"`
	TransferID string `json:"transfer_id,omitempty"`
	Op         TaskOp `json:"op,omitempty"`
	SleepMs    int    `json:"sleep_ms,omitempty"`
	Count      int    `json:"count,omitempty"`
	ChildOp    TaskOp `json:"child_op,omitempty"`
	ChildPartition string `json:"child_partition,omitempty"`
	Reason     string `json:"reason,omitempty"`
	ParentTask string `json:"parent_task,omitempty"`
}

// StartRequest marks a claimed task as executing (used to distinguish queued
// from genuinely in-flight work).
type StartRequest struct {
	WorkerID   string `json:"worker_id"`
	RunID      string `json:"run_id"`
	TransferID string `json:"transfer_id"`
}

// CompleteRequest reports success. Children describe causal edges the task
// opened BEFORE completing: each becomes a DS transfer whose deficit is
// charged to this node, so they cannot be forgotten.
type CompleteRequest struct {
	WorkerID   string         `json:"worker_id"`
	RunID      string         `json:"run_id"`
	TransferID string         `json:"transfer_id"`
	Children   []TaskSpec     `json:"children,omitempty"`
}

// FailRequest reports a deterministic task failure.
type FailRequest struct {
	WorkerID   string `json:"worker_id"`
	RunID      string `json:"run_id"`
	TransferID string `json:"transfer_id"`
	Reason     string `json:"reason"`
}

// IdleRequest tells the server a worker went passive on a partition (no
// queued task, nothing running). The kernel only sends the disengage signal
// when the node deficit is 0; otherwise the node stays engaged.
type IdleRequest struct {
	WorkerID  string `json:"worker_id"`
	RunID     string `json:"run_id"`
	Partition string `json:"partition"`
}

// AckRequest delivers the raw DS receipt signal for an edge. Exposed so tests
// can drive duplicate/out-of-order confirmation explicitly. The production
// path emits this automatically at claim.
type AckRequest struct {
	RunID      string     `json:"run_id"`
	TransferID string     `json:"transfer_id"`
	Signal     SignalKind `json:"signal"`
}

// ReplayProjection is the independently materialized view produced by folding
// the event log outside the server's request path (the replay service).
type ReplayProjection struct {
	SchemaVersion  string           `json:"schema_version"`
	ReplayVer      string           `json:"replay_engine"`
	RunID          string           `json:"run_id"`
	EventsReplayed int64            `json:"events_replayed"`
	Phase          RunPhase         `json:"phase"`
	RootDeficit    int64            `json:"root_deficit"`
	EngagedNodes   int              `json:"engaged_nodes"`
	OpenEdges      int64            `json:"open_edges"`
	SettledEdges   int64            `json:"settled_edges"`
	UnackedEdges   int64            `json:"unacknowledged_edges"`
	PerNode        []NodeCounters   `json:"per_node"`
	Divergences    []string         `json:"divergences,omitempty"`
}

// NodeCounters are the replayed per-node DS bookkeeping.
type NodeCounters struct {
	NodeID       string `json:"node_id"`
	Engaged      bool   `json:"engaged"`
	Deficit      int64  `json:"deficit"`
	Parent       string `json:"parent,omitempty"`
	ActiveTasks  int    `json:"active_tasks"`
}

// Envelope is the common response wrapper. RequestID correlates every HTTP
// response with its structured log line.
type Envelope struct {
	SchemaVersion string      `json:"schema_version"`
	RequestID     string      `json:"request_id"`
	Handler       string      `json:"handler"`
	OK            bool        `json:"ok"`
	Data          interface{} `json:"data,omitempty"`
	Error         *APIError   `json:"error,omitempty"`
}

// APIError separates failure reasons from normal data.
type APIError struct {
	Class  FailureClass `json:"class"`
	Code   int          `json:"code"`
	Msg    string       `json:"msg"`
	RunID  string       `json:"run_id,omitempty"`
}
