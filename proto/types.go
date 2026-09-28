// Package proto defines the wire and domain protocol for the Dijkstra-Scholten
// termination-detection task network. It carries no behaviour beyond validation:
// the computation kernel (dsnet/kernel), storage (dsnet/store) and HTTP layer
// (dsnet/app) all depend on these shared types and never re-declare them.
package proto

import (
	"errors"
	"fmt"
	"time"
)

// Version is the protocol/projection version. It is surfaced in every status,
// replay response and structured log line so an observer can tell which
// processing semantics produced a result.
const Version = "dsnet-proto/1.0.0"

// FailureClass is a stable, machine-readable category for failed or uncertain
// conclusions. Independent tests assert on these exact strings.
type FailureClass string

const (
	// FailBudgetExceeded: the run wall-clock or task budget expired while
	// causal edges were still open. The conclusion is UNCONFIRMED.
	FailBudgetExceeded FailureClass = "budget_exceeded"
	// FailTaskFailed: a task op failed deterministically. All DS edges still
	// settle; the run is failed (not unconfirmed).
	FailTaskFailed FailureClass = "task_failed"
	// FailInvalidRequest: malformed submission / body.
	FailInvalidRequest FailureClass = "invalid_request"
	// FailUnknownRun: referenced run does not exist.
	FailUnknownRun FailureClass = "unknown_run"
	// FailUnknownTransfer: ack/claim references an unknown transfer.
	FailUnknownTransfer FailureClass = "unknown_transfer"
	// FailUnknownNode: a command references a node that never engaged.
	FailUnknownNode FailureClass = "unknown_node"
	// FailDuplicateSignal: a signal for an already-settled edge was received.
	// It is accepted idempotently and does NOT decrement any counter twice.
	FailDuplicateSignal FailureClass = "duplicate_signal"
	// FailRunClosed: a mutating call arrived after the run reached a terminal
	// phase (announced / failed / unacknowledged).
	FailRunClosed FailureClass = "run_closed"
	// FailNoWorker: no live worker owns the target partition.
	FailNoWorker FailureClass = "no_worker"
)

// TaskOp enumerates the tiny, fully synthetic compute vocabulary. Every op is
// deterministic and local; there is no external business data.
type TaskOp string

const (
	OpNop     TaskOp = "nop"     // succeed immediately, no children
	OpSleep   TaskOp = "sleep"   // succeed after SleepMs wall-clock
	OpFail    TaskOp = "fail"    // deterministically fail with Reason
	OpSpawn   TaskOp = "spawn"   // succeed after opening Count children to ChildOp
	OpChain   TaskOp = "chain"   // open exactly one child, then succeed
	OpParSum  TaskOp = "parsum"  // open Count children (nop), then succeed
)

// TaskSpec is one unit of work. Tasks are assigned to a Partition; workers
// subscribe to partitions, which is how the causal graph is spread over nodes.
type TaskSpec struct {
	ID        string `json:"id"`
	Op        TaskOp `json:"op"`
	Partition string `json:"partition"`
	// SleepMs delays completion (op=sleep); used to create genuine in-flight
	// edges while the queue is already empty.
	SleepMs int `json:"sleep_ms,omitempty"`
	// Count children for op=spawn/parsum.
	Count int `json:"count,omitempty"`
	// ChildOp is the op assigned to spawned children (defaults to nop).
	ChildOp TaskOp `json:"child_op,omitempty"`
	// ChildPartition overrides the partition of spawned children (defaults to
	// the parent's partition; set explicitly to force hierarchy across nodes).
	ChildPartition string `json:"child_partition,omitempty"`
	// Reason is attached to op=fail outcomes.
	Reason string `json:"reason,omitempty"`
}

// Validate enforces the protocol invariants on a single task spec.
func (t TaskSpec) Validate() error {
	if t.ID == "" {
		return errors.New("task.id is required")
	}
	switch t.Op {
	case OpNop, OpSleep, OpFail, OpSpawn, OpChain, OpParSum:
	default:
		return fmt.Errorf("task %q: unknown op %q", t.ID, t.Op)
	}
	if t.Partition == "" {
		return fmt.Errorf("task %q: partition is required", t.ID)
	}
	if t.Op == OpSleep && t.SleepMs < 0 {
		return fmt.Errorf("task %q: sleep_ms must be >= 0", t.ID)
	}
	if (t.Op == OpSpawn || t.Op == OpParSum) && t.Count < 0 {
		return fmt.Errorf("task %q: count must be >= 0", t.ID)
	}
	if t.Op == OpFail && t.Reason == "" {
		return fmt.Errorf("task %q: fail op requires a reason", t.ID)
	}
	return nil
}

// RunPhase is the high-level lifecycle state of one run.
type RunPhase string

const (
	PhaseRunning       RunPhase = "running"
	PhaseAnnounced     RunPhase = "announced"      // DS termination, all tasks succeeded
	PhaseFailed        RunPhase = "failed"         // terminal: a task failed (edges all settled)
	PhaseUnacknowledged RunPhase = "unacknowledged" // terminal: budget exhausted with open edges
)

// Terminal reports whether no further mutation is possible.
func (p RunPhase) Terminal() bool {
	return p == PhaseAnnounced || p == PhaseFailed || p == PhaseUnacknowledged
}

// TransferState classifies one causal edge (one task hand-off).
type TransferState string

const (
	EdgeOpen      TransferState = "open"      // handed off, not yet settled toward parent
	EdgeSettled   TransferState = "settled"   // signal reached the parent exactly once
	EdgeUnacked   TransferState = "unacknowledged" // budget expired while open
)

// EventKind enumerates the append-only event-log vocabulary. The event stream
// is the single source of truth; both the kernel fold and the independent SQL
// projection in the test oracle read it.
type EventKind string

const (
	EvRunStarted   EventKind = "run.started"
	EvTransferOpen EventKind = "transfer.opened"
	EvTaskClaimed  EventKind = "task.claimed"
	EvTaskStarted  EventKind = "task.started"
	EvTaskSucceeded EventKind = "task.succeeded"
	EvTaskFailed   EventKind = "task.failed"
	EvNodeEngaged  EventKind = "node.engaged"
	EvNodeIdle     EventKind = "node.idle"
	EvNodeReengaged EventKind = "node.reengaged"
	EvSignalSent   EventKind = "signal.sent"
	EvEdgeSettled  EventKind = "edge.settled"
	EvRunFailed    EventKind = "run.failed"
	EvBudgetExpired EventKind = "run.budget_expired"
	EvDSAnnounce   EventKind = "ds.announced"
)

// SignalKind distinguishes the two DS signal directions.
type SignalKind string

const (
	// SigReceipt is the immediate acknowledgement of a basic message by an
	// engaged receiver. It decrements the SENDER deficit.
	SigReceipt SignalKind = "receipt"
	// SigDisengage is sent by a node that became passive with deficit 0 to
	// its DS tree parent. It decrements the PARENT deficit and settles the
	// edge that engaged the node.
	SigDisengage SignalKind = "disengage"
)

// Event is one persisted fact. JSON tag names are the stable external contract;
// the replay endpoint renders these verbatim.
type Event struct {
	Seq        int64          `json:"seq"`
	RunID      string         `json:"run_id"`
	Kind       EventKind      `json:"kind"`
	NodeID     string         `json:"node_id,omitempty"`
	FromNode   string         `json:"from_node,omitempty"`
	ToNode     string         `json:"to_node,omitempty"`
	TransferID string         `json:"transfer_id,omitempty"`
	TaskID     string         `json:"task_id,omitempty"`
	ParentTask string         `json:"parent_task,omitempty"`
	Signal     SignalKind     `json:"signal,omitempty"`
	Partition  string         `json:"partition,omitempty"`
	Reason     string         `json:"reason,omitempty"`
	At         time.Time      `json:"at"`
}
