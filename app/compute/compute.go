// Package compute is the synthetic computation kernel: a deterministic
// interpreter for the tiny task vocabulary. It contains no network or storage
// logic — given a TaskSpec it decides whether the task succeeds/fails and
// which child tasks it causally opens. The DS state machine treats those
// children as in-flight causal edges.
package compute

import (
	"fmt"
	"sync/atomic"

	"dsnet/proto"
)

// childSeq guarantees unique child task identities within a process run.
var childSeq uint64

// Outcome is the interpreted result of executing one task spec.
type Outcome struct {
	Failure  string
	Children []proto.TaskSpec
}

// Execute interprets spec. sleep_ms is NOT honoured here (the worker sleeps);
// the server only needs the deterministic success/failure/children decision.
func Execute(spec proto.TaskSpec) Outcome {
	switch spec.Op {
	case proto.OpNop, proto.OpSleep:
		return Outcome{}

	case proto.OpFail:
		return Outcome{Failure: spec.Reason}

	case proto.OpChain:
		return Outcome{Children: []proto.TaskSpec{
			childSpec(spec, 1, proto.OpNop),
		}}

	case proto.OpSpawn:
		out := make([]proto.TaskSpec, 0, spec.Count)
		for i := 1; i <= spec.Count; i++ {
			op := spec.ChildOp
			if op == "" {
				op = proto.OpNop
			}
			out = append(out, childSpec(spec, i, op))
		}
		return Outcome{Children: out}

	case proto.OpParSum:
		out := make([]proto.TaskSpec, 0, spec.Count)
		for i := 1; i <= spec.Count; i++ {
			out = append(out, childSpec(spec, i, proto.OpNop))
		}
		return Outcome{Children: out}

	default:
		return Outcome{Failure: "unknown op: " + string(spec.Op)}
	}
}

func childSpec(parent proto.TaskSpec, idx, total int, op proto.TaskOp) proto.TaskSpec {
	n := atomic.AddUint64(&childSeq, 1)
	part := parent.ChildPartition
	if part == "" {
		part = parent.Partition
	}
	return proto.TaskSpec{
		ID:        fmt.Sprintf("%s.c%d-%d", parent.ID, idx, n),
		Op:        op,
		Partition: part,
	}
}

// ResetChildSeq restarts identity allocation (tests only).
func ResetChildSeq() { atomic.StoreUint64(&childSeq, 0) }
