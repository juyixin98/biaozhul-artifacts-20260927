// Package kernel: Fold is the ONLY state-transition function. Apply validates
// commands against a folded state and produces candidate facts without
// mutating anything; the caller persists the facts transactionally and folds
// them in. The replay service folds the same event log through the same
// function, so its projection cannot disagree with the live path by
// construction.
package kernel

import (
	"time"

	"dsnet/proto"
)

// stamp returns the event time. Time is injected in Fold for tests; commands
// default to wall clock but the persisted value is what Fold records.
var nowFunc = time.Now

func stamp() time.Time { return nowFunc().UTC() }

// Fold applies one event to the state. Unknown event kinds are ignored only
// for forward-compatibility of the log; all current kinds are handled.
func Fold(s *State, ev proto.Event) *State {
	if s == nil {
		s = NewState()
	}
	r, exists := s.Runs[ev.RunID]
	if !exists {
		if ev.Kind != proto.EvRunStarted {
			// Orphan event before start: create a shell so folding never panics;
			// the projection marks this as a divergence in the replay layer.
			r = newRunShell(ev.RunID)
			s.Runs[ev.RunID] = r
		} else {
			r = newRunShell(ev.RunID)
			s.Runs[ev.RunID] = r
		}
	}
	r.Seq = ev.Seq

	switch ev.Kind {
	case proto.EvRunStarted:
		r.Phase = proto.PhaseRunning
	case proto.EvTransferOpen:
		foldOpen(r, ev)
	case proto.EvNodeEngaged:
		foldEngage(r, ev, false)
	case proto.EvNodeReengaged:
		foldEngage(r, ev, true)
	case proto.EvSignalSent:
		foldSignal(r, ev)
	case proto.EvEdgeSettled:
		foldEdgeSettled(r, ev)
	case proto.EvTaskClaimed:
		if e := r.Edges[ev.TransferID]; e != nil {
			e.claimed = true
		}
	case proto.EvTaskStarted:
		if e := r.Edges[ev.TransferID]; e != nil {
			e.started = true
		}
	case proto.EvTaskSucceeded:
		r.TasksSucceeded++
		if n := r.Nodes[ev.NodeID]; n != nil && n.ActiveTasks > 0 {
			n.ActiveTasks--
		}
	case proto.EvTaskFailed:
		r.TasksFailed++
		if n := r.Nodes[ev.NodeID]; n != nil && n.ActiveTasks > 0 {
			n.ActiveTasks--
		}
	case proto.EvNodeIdle:
		// informational: passivity is derived from ActiveTasks, which the
		// claim/succeed/fail events already maintain.
	case proto.EvRunFailed:
		r.Phase = proto.PhaseFailed
	case proto.EvBudgetExpired:
		r.Phase = proto.PhaseUnacknowledged
	case proto.EvDSAnnounce:
		r.Phase = proto.PhaseAnnounced
		r.Announced = true
	}
	return s
}

func newRunShell(id string) *Run {
	root := &Node{ID: RootNodeID, Engaged: false}
	return &Run{ID: id, Phase: proto.PhaseRunning,
		Nodes: map[string]*Node{RootNodeID: root},
		Edges: map[string]*Edge{}, Root: root}
}

func foldOpen(r *Run, ev proto.Event) {
	if _, dup := r.Edges[ev.TransferID]; dup {
		return
	}
	r.Edges[ev.TransferID] = &Edge{
		ID: ev.TransferID, From: ev.FromNode, To: ev.ToNode,
		TaskID: ev.TaskID, State: proto.EdgeOpen, SeqOpened: ev.Seq}
	// Sender deficit was incremented at send and decremented by the immediate
	// receipt event; both facts are represented explicitly in the log.
	if n := r.Nodes[ev.FromNode]; n != nil {
		n.Deficit++
	}
	if n := r.Nodes[ev.ToNode]; n != nil {
		n.ActiveTasks++
	}
	r.OpenCount++
}

func foldEngage(r *Run, ev proto.Event, re bool) {
	n := r.Nodes[ev.NodeID]
	if n == nil {
		n = &Node{ID: ev.NodeID}
		r.Nodes[ev.NodeID] = n
	}
	n.Engaged = true
	n.Parent = ev.FromNode
	n.EngagingEdge = ev.TransferID
	if e := r.Edges[ev.TransferID]; e != nil {
		e.Engaging = true
	}
}

func foldSignal(r *Run, ev proto.Event) {
	e := r.Edges[ev.TransferID]
	r.SignalsApplied++
	switch ev.Signal {
	case proto.SigReceipt:
		if e == nil || e.Receipted {
			r.Duplicates++
			return
		}
		e.Receipted = true
		if n := r.Nodes[ev.ToNode]; n != nil { // receipt goes to the sender
			n.Deficit--
		}
	case proto.SigDisengage:
		if e == nil || e.Disengaged {
			r.Duplicates++
			return
		}
		e.Disengaged = true
		if n := r.Nodes[ev.ToNode]; n != nil { // parent
			n.Deficit--
		}
		if child := r.Nodes[ev.FromNode]; child != nil {
			child.Engaged = false
			child.Parent = ""
			child.EngagingEdge = ""
		}
	}
}

func foldEdgeSettled(r *Run, ev proto.Event) {
	e := r.Edges[ev.TransferID]
	if e == nil {
		return
	}
	switch ev.Reason {
	case "budget:unacknowledged":
		if e.State == proto.EdgeOpen {
			e.State = proto.EdgeUnacked
			r.OpenCount--
			r.UnackedCount++
		}
	default:
		if e.State == proto.EdgeOpen {
			e.State = proto.EdgeSettled
			r.OpenCount--
			r.SettledCount++
		}
	}
}

// FoldAll folds an ordered slice, returning the resulting state.
func FoldAll(events []proto.Event) *State {
	s := NewState()
	for _, ev := range events {
		Fold(s, ev)
	}
	return s
}
