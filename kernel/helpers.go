package kernel

import (
	"errors"
	"sort"

	"dsnet/proto"
)

func sortStrings(x []string) { sort.Strings(x) }

// cloneState deep-copies the state used as decision scratch. Candidates are
// folded into the clone; only after the DB transaction commits does the
// service fold the same events into its live state.
func cloneState(s *State) *State {
	c := &State{Runs: make(map[string]*Run, len(s.Runs))}
	for id, r := range s.Runs {
		nr := &Run{
			ID: r.ID, Phase: r.Phase, Seq: r.Seq, Announced: r.Announced,
			Duplicates: r.Duplicates, SignalsApplied: r.SignalsApplied,
			OpenCount: r.OpenCount, SettledCount: r.SettledCount,
			UnackedCount: r.UnackedCount, TasksSucceeded: r.TasksSucceeded,
			TasksFailed: r.TasksFailed,
			Nodes: make(map[string]*Node, len(r.Nodes)),
			Edges: make(map[string]*Edge, len(r.Edges)),
		}
		if r.Failure != nil {
			f := *r.Failure
			nr.Failure = &f
		}
		for k, n := range r.Nodes {
			nc := *n
			nr.Nodes[k] = &nc
		}
		for k, e := range r.Edges {
			ec := *e
			nr.Edges[k] = &ec
		}
		nr.Root = nr.Nodes[RootNodeID]
		c.Runs[id] = nr
	}
	return c
}

// Counters is the dependency-count evidence snapshot for one run.
type Counters struct {
	Phase            proto.RunPhase
	Announced        bool
	RootDeficit      int64
	EngagedNodes     int
	OpenEdges        int64
	SettledEdges     int64
	UnackedEdges     int64
	SignalsApplied   int64
	DuplicateSignals int64
	TasksQueued      int64
	TasksRunning     int64
	TasksSucceeded   int64
	TasksFailed      int64
}

// Snapshot computes the invariant-bearing counters. A safe termination
// announcement requires: Announced && RootDeficit==0 && EngagedNodes==0 &&
// OpenEdges==0 && UnackedEdges==0.
func (r *Run) Snapshot() Counters {
	c := Counters{Phase: r.Phase, Announced: r.Announced,
		SignalsApplied: r.SignalsApplied, DuplicateSignals: r.Duplicates,
		OpenEdges: r.OpenCount, SettledEdges: r.SettledCount,
		UnackedEdges: r.UnackedCount,
		TasksSucceeded: r.TasksSucceeded, TasksFailed: r.TasksFailed}
	if r.Root != nil {
		c.RootDeficit = r.Root.Deficit
	}
	for _, n := range r.Nodes {
		if n.Engaged {
			c.EngagedNodes++
		}
	}
	for _, e := range r.Edges {
		if e.State != proto.EdgeOpen {
			continue
		}
		if e.started {
			c.TasksRunning++
		} else if e.claimed {
			c.TasksRunning++ // claimed counts as in-flight on the node
		} else {
			c.TasksQueued++
		}
	}
	return c
}

// EngagedNodeIDs returns the sorted engaged node identities.
func (r *Run) EngagedNodeIDs() []string {
	out := make([]string, 0, len(r.Nodes))
	for id, n := range r.Nodes {
		if n.Engaged {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out
}

// MapError converts a kernel decision error into the stable failure class.
func MapError(err error) (proto.FailureClass, int) {
	switch {
	case errors.Is(err, ErrUnknownRun):
		return proto.FailUnknownRun, 404
	case errors.Is(err, ErrRunClosed):
		return proto.FailRunClosed, 409
	case errors.Is(err, ErrUnknownTransfer):
		return proto.FailUnknownTransfer, 404
	case errors.Is(err, ErrUnknownNode):
		return proto.FailUnknownNode, 404
	case errors.Is(err, ErrDuplicateTransfer):
		return proto.FailDuplicateSignal, 409
	case errors.Is(err, ErrDuplicateSignal):
		return proto.FailDuplicateSignal, 409
	case errors.Is(err, ErrAlreadySettled):
		return proto.FailDuplicateSignal, 409
	case errors.Is(err, ErrNodeActive):
		return proto.FailRunClosed, 409
	case errors.Is(err, ErrNotClaimed):
		return proto.FailInvalidRequest, 409
	default:
		return proto.FailInvalidRequest, 400
	}
}
