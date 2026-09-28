// Package kernel: the decision half.
//
// Apply validates a command against the caller's folded state and returns the
// ordered facts the command produces. It mutates NOTHING the caller can see:
// candidate events are folded into an internal scratch state so cascading
// effects (fan-out opens followed by disengage signals, possibly reaching the
// root announcement) are all decided in one atomic decision. The service then
// appends exactly those facts in one DB transaction and folds them into the
// real state; replay folds the same facts through the same Fold.
package kernel

import (
	"errors"
	"fmt"

	"dsnet/proto"
)

// RootNodeID is the fixed identity of the DS tree root for every run.
const RootNodeID = "root"

// Sentinel errors. The HTTP layer maps these to proto.FailureClass.
var (
	ErrUnknownRun        = errors.New("unknown run")
	ErrRunClosed         = errors.New("run already terminal")
	ErrUnknownTransfer   = errors.New("unknown transfer")
	ErrUnknownNode       = errors.New("unknown node")
	ErrDuplicateTransfer = errors.New("duplicate transfer id")
	ErrDuplicateSignal   = errors.New("duplicate signal for edge")
	ErrAlreadySettled    = errors.New("edge already settled")
	ErrNodeActive        = errors.New("node still active or non-zero deficit")
	ErrBadSignal         = errors.New("signal cannot be applied in this state")
	ErrNotClaimed        = errors.New("task not claimed")
)

// Edge is one causal (task transfer) edge with its DS accounting.
type Edge struct {
	ID         string
	From       string
	To         string
	TaskID     string
	State      proto.TransferState
	Receipted  bool
	Disengaged bool
	// Engaging marks the edge that engaged (or re-engaged) the receiver into
	// the DS tree. Such an edge has NO immediate receipt; its sole balancing
	// signal is the child's disengage. Raw receipt signals on it are rejected.
	Engaging   bool
	claimed    bool
	started    bool
	SeqOpened  int64
}

// Claimed/Started expose task execution flags for status reporting.
func (e *Edge) Claimed() bool { return e.claimed }
func (e *Edge) Started() bool { return e.started }

// Node is per-node DS bookkeeping plus the workload activity flag.
type Node struct {
	ID           string
	Engaged      bool
	Parent       string
	Deficit      int64
	ActiveTasks  int
	EngagingEdge string
}

// Run is the full folded state of one run.
type Run struct {
	ID             string
	Phase          proto.RunPhase
	Seq            int64
	Announced      bool
	Nodes          map[string]*Node
	Edges          map[string]*Edge
	Root           *Node
	Failure        *proto.FailureDetail
	Duplicates     int64
	SignalsApplied int64
	OpenCount      int64
	SettledCount   int64
	UnackedCount   int64
	TasksSucceeded int64
	TasksFailed    int64
}

// State is the kernel state, potentially holding several runs.
type State struct {
	Runs map[string]*Run
}

func NewState() *State { return &State{Runs: map[string]*Run{}} }

func (s *State) Run(id string) (*Run, error) {
	r, ok := s.Runs[id]
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrUnknownRun, id)
	}
	return r, nil
}

// Command is the sum type of all state transitions; exactly one pointer set.
type Command struct {
	Start     *StartRun
	Open      *OpenTransfer
	Claim     *ClaimTask
	StartExec *StartExec
	Succeed   *SucceedTask
	FailTask  *FailTask
	GoIdle    *GoIdle
	Signal    *ApplySignal
	Budget    *ExpireBudget
}

type StartRun struct{ RunID string }

type OpenTransfer struct {
	RunID        string
	TransferID   string
	Sender       string // "" => root
	ReceiverNode string
	TaskID       string
	Partition    string
}

type ClaimTask struct {
	RunID, TransferID, NodeID string
}

type StartExec struct{ RunID, TransferID string }

type SucceedTask struct {
	RunID      string
	TransferID string
	Children   []OpenTransfer
}

type FailTask struct {
	RunID, TransferID, Reason string
}

type GoIdle struct{ RunID, NodeID string }

type ApplySignal struct {
	RunID, TransferID string
	Kind              proto.SignalKind
}

type ExpireBudget struct{ RunID, Reason string }

// Apply returns the decided fact batch for cmd. The input state is untouched.
func Apply(s *State, cmd Command) ([]proto.Event, error) {
	w := &work{scratch: cloneState(s)}
	switch {
	case cmd.Start != nil:
		return w.start(cmd.Start)
	case cmd.Open != nil:
		return w.open(cmd.Open, true)
	case cmd.Claim != nil:
		return w.claim(cmd.Claim)
	case cmd.StartExec != nil:
		return w.startExec(cmd.StartExec)
	case cmd.Succeed != nil:
		return w.succeed(cmd.Succeed)
	case cmd.FailTask != nil:
		return w.fail(cmd.FailTask)
	case cmd.GoIdle != nil:
		return w.idle(cmd.GoIdle)
	case cmd.Signal != nil:
		return w.signal(cmd.Signal)
	case cmd.Budget != nil:
		return w.budget(cmd.Budget)
	default:
		return nil, errors.New("empty command")
	}
}

type work struct {
	scratch *State
	curRun  string
	out     []proto.Event
}

// append assigns the sequence, folds the candidate event into scratch and
// records it. The caller's state never sees these candidates.
func (w *work) append(ev proto.Event) proto.Event {
	r := w.scratch.Runs[w.curRun]
	r.Seq++
	ev.Seq = r.Seq
	ev.RunID = w.curRun
	if ev.At.IsZero() {
		ev.At = stamp()
	}
	w.out = append(w.out, ev)
	Fold(w.scratch, ev)
	return ev
}

func (w *work) startRun(id string) *Run {
	w.curRun = id
	return w.scratch.Runs[id]
}

func (w *work) mustRun(id string) *Run {
	w.curRun = id
	r, ok := w.scratch.Runs[id]
	if !ok {
		panic(badErr{fmt.Errorf("%w: %s", ErrUnknownRun, id)})
	}
	return r
}

type badErr struct{ err error }

func (e badErr) Error() string { return e.err.Error() }

func (w *work) start(c *StartRun) ([]proto.Event, error) {
	if _, ok := w.scratch.Runs[c.RunID]; ok {
		return nil, fmt.Errorf("run %q already exists", c.RunID)
	}
	root := &Node{ID: RootNodeID, Engaged: true}
	w.scratch.Runs[c.RunID] = &Run{
		ID: c.RunID, Phase: proto.PhaseRunning,
		Nodes: map[string]*Node{RootNodeID: root},
		Edges: map[string]*Edge{}, Root: root}
	w.curRun = c.RunID
	w.append(proto.Event{Kind: proto.EvRunStarted, NodeID: RootNodeID})
	return w.take(), nil
}

// open decides one basic message + engagement + immediate receipt.
func (w *work) open(c *OpenTransfer, checkOpen bool) ([]proto.Event, error) {
	r := w.mustRun(c.RunID)
	if r.Phase.Terminal() {
		return nil, fmt.Errorf("%w: %s", ErrRunClosed, r.Phase)
	}
	if _, dup := r.Edges[c.TransferID]; dup {
		return nil, fmt.Errorf("%w: %s", ErrDuplicateTransfer, c.TransferID)
	}
	from := c.Sender
	if from == "" {
		from = RootNodeID
	}
	sender := r.Nodes[from]
	if sender == nil || !sender.Engaged {
		return nil, fmt.Errorf("%w: sender %s not engaged", ErrUnknownNode, from)
	}
	if c.ReceiverNode == "" {
		return nil, errors.New("receiver node required")
	}
	to := c.ReceiverNode

	w.append(proto.Event{Kind: proto.EvTransferOpen, FromNode: from, ToNode: to,
		TransferID: c.TransferID, TaskID: c.TaskID, Partition: c.Partition})

	// DS tree rule: the message that ENGAGES (or re-engages) the receiver is
	// acknowledged exactly once — by that node's eventual disengage signal.
	// A message reaching an ALREADY engaged node is receipt-acknowledged
	// immediately and is not a tree edge.
	child := r.Nodes[to]
	alreadyEngaged := child != nil && child.Engaged
	if !alreadyEngaged {
		kind := proto.EvNodeEngaged
		if child != nil {
			kind = proto.EvNodeReengaged
		}
		w.append(proto.Event{Kind: kind, NodeID: to, FromNode: from,
			TransferID: c.TransferID})
	} else {
		w.append(proto.Event{Kind: proto.EvSignalSent, FromNode: to, ToNode: from,
			TransferID: c.TransferID, Signal: proto.SigReceipt})
	}
	return w.take(), nil
}

func (w *work) claim(c *ClaimTask) ([]proto.Event, error) {
	r := w.mustRun(c.RunID)
	if r.Phase.Terminal() {
		return nil, fmt.Errorf("%w: %s", ErrRunClosed, r.Phase)
	}
	e := r.Edges[c.TransferID]
	if e == nil {
		return nil, fmt.Errorf("%w: %s", ErrUnknownTransfer, c.TransferID)
	}
	if e.State != proto.EdgeOpen {
		return nil, fmt.Errorf("%w: edge %s is %s", ErrAlreadySettled, c.TransferID, e.State)
	}
	if e.To != c.NodeID {
		return nil, fmt.Errorf("transfer %s belongs to %s, not %s", c.TransferID, e.To, c.NodeID)
	}
	if e.claimed {
		return nil, fmt.Errorf("%w: %s already claimed", ErrDuplicateTransfer, c.TransferID)
	}
	w.append(proto.Event{Kind: proto.EvTaskClaimed, NodeID: c.NodeID,
		TransferID: e.ID, TaskID: e.TaskID})
	return w.take(), nil
}

func (w *work) startExec(c *StartExec) ([]proto.Event, error) {
	r := w.mustRun(c.RunID)
	if r.Phase.Terminal() {
		return nil, fmt.Errorf("%w: %s", ErrRunClosed, r.Phase)
	}
	e := r.Edges[c.TransferID]
	if e == nil {
		return nil, fmt.Errorf("%w: %s", ErrUnknownTransfer, c.TransferID)
	}
	if !e.claimed {
		return nil, fmt.Errorf("%w: %s", ErrNotClaimed, c.TransferID)
	}
	if e.started {
		return nil, nil // idempotent: already started, no new facts
	}
	w.append(proto.Event{Kind: proto.EvTaskStarted, NodeID: e.To,
		TransferID: e.ID, TaskID: e.TaskID})
	return w.take(), nil
}

func (w *work) succeed(c *SucceedTask) ([]proto.Event, error) {
	r := w.mustRun(c.RunID)
	if r.Phase.Terminal() {
		return nil, fmt.Errorf("%w: %s", ErrRunClosed, r.Phase)
	}
	e := r.Edges[c.TransferID]
	if e == nil {
		return nil, fmt.Errorf("%w: %s", ErrUnknownTransfer, c.TransferID)
	}
	if e.State != proto.EdgeOpen {
		return nil, fmt.Errorf("%w: edge %s is %s", ErrAlreadySettled, c.TransferID, e.State)
	}

	// Children first: charge all descendant deficit to this node while it is
	// still active, so a fan-out can never transiently look terminated.
	batch := []OpenTransfer{}
	batch = append(batch, c.Children...)
	seen := map[string]bool{}
	for _, ch := range batch {
		if seen[ch.TransferID] {
			return nil, fmt.Errorf("%w: %s in same batch", ErrDuplicateTransfer, ch.TransferID)
		}
		seen[ch.TransferID] = true
		if _, err := w.open(&ch, false); err != nil {
			return nil, err
		}
	}
	node := r.Nodes[e.To]
	w.append(proto.Event{Kind: proto.EvTaskSucceeded, NodeID: e.To,
		TransferID: e.ID, TaskID: e.TaskID})
	w.settleIfNonTree(r, e, node)
	w.cascade(node, false)
	return w.take(), nil
}

func (w *work) fail(c *FailTask) ([]proto.Event, error) {
	r := w.mustRun(c.RunID)
	if r.Phase.Terminal() {
		return nil, fmt.Errorf("%w: %s", ErrRunClosed, r.Phase)
	}
	e := r.Edges[c.TransferID]
	if e == nil {
		return nil, fmt.Errorf("%w: %s", ErrUnknownTransfer, c.TransferID)
	}
	if e.State != proto.EdgeOpen {
		return nil, fmt.Errorf("%w: edge %s is %s", ErrAlreadySettled, c.TransferID, e.State)
	}
	node := r.Nodes[e.To]
	w.append(proto.Event{Kind: proto.EvTaskFailed, NodeID: e.To,
		TransferID: e.ID, TaskID: e.TaskID, Reason: c.Reason})
	w.settleIfNonTree(r, e, node)
	w.cascade(node, true)
	// Failure is the run's last word even if the tree fully settled above.
	w.append(proto.Event{Kind: proto.EvRunFailed, NodeID: e.To,
		TaskID: e.TaskID, TransferID: e.ID, Reason: c.Reason})
	return w.take(), nil
}

// settleIfNonTree settles a completed-task edge immediately when it is NOT the
// edge that engaged the node: its receipt already returned the sender deficit,
// so settlement here only closes the edge (no deficit movement).
func (w *work) settleIfNonTree(r *Run, e *Edge, node *Node) {
	if e.Receipted && node.EngagingEdge != e.ID {
		w.append(proto.Event{Kind: proto.EvEdgeSettled, FromNode: e.To,
			ToNode: e.From, TransferID: e.ID, TaskID: e.TaskID})
	}
}

func (w *work) idle(c *GoIdle) ([]proto.Event, error) {
	r := w.mustRun(c.RunID)
	if r.Phase.Terminal() {
		return nil, fmt.Errorf("%w: %s", ErrRunClosed, r.Phase)
	}
	n := r.Nodes[c.NodeID]
	if n == nil || !n.Engaged {
		return nil, fmt.Errorf("%w: %s", ErrUnknownNode, c.NodeID)
	}
	w.append(proto.Event{Kind: proto.EvNodeIdle, NodeID: n.ID})
	w.cascade(n, false)
	return w.take(), nil
}

func (w *work) signal(c *ApplySignal) ([]proto.Event, error) {
	r := w.mustRun(c.RunID)
	if r.Phase.Terminal() {
		return nil, fmt.Errorf("%w: %s", ErrRunClosed, r.Phase)
	}
	e := r.Edges[c.TransferID]
	if e == nil {
		return nil, fmt.Errorf("%w: %s", ErrUnknownTransfer, c.TransferID)
	}
	switch c.Kind {
	case proto.SigReceipt:
		if e.Receipted {
			r.Duplicates++ // observed in scratch only; caller maps 409
			return nil, fmt.Errorf("%w: receipt %s", ErrDuplicateSignal, e.ID)
		}
		if e.Engaging {
			// The engaging edge is balanced by the disengage signal alone;
			// applying a receipt here would double-count and corrupt the tree.
			return nil, fmt.Errorf("%w: engaging edge %s settled by disengage only",
				ErrBadSignal, e.ID)
		}
		w.append(proto.Event{Kind: proto.EvSignalSent, FromNode: e.To,
			ToNode: e.From, TransferID: e.ID, Signal: proto.SigReceipt})
	case proto.SigDisengage:
		if e.Disengaged || e.State == proto.EdgeSettled {
			r.Duplicates++
			return nil, fmt.Errorf("%w: disengage %s", ErrDuplicateSignal, e.ID)
		}
		n := r.Nodes[e.To]
		if n.ActiveTasks != 0 || n.Deficit != 0 {
			return nil, ErrNodeActive
		}
		w.append(proto.Event{Kind: proto.EvSignalSent, FromNode: e.To,
			ToNode: e.From, TransferID: e.ID, Signal: proto.SigDisengage})
		w.append(proto.Event{Kind: proto.EvEdgeSettled, FromNode: e.To,
			ToNode: e.From, TransferID: e.ID, TaskID: e.TaskID})
		w.cascade(r.Nodes[e.From], false)
	default:
		return nil, fmt.Errorf("%w: %v", ErrBadSignal, c.Kind)
	}
	return w.take(), nil
}

func (w *work) budget(c *ExpireBudget) ([]proto.Event, error) {
	r := w.mustRun(c.RunID)
	if r.Phase.Terminal() {
		return nil, fmt.Errorf("%w: %s", ErrRunClosed, r.Phase)
	}
	w.append(proto.Event{Kind: proto.EvBudgetExpired, NodeID: RootNodeID,
		Reason: c.Reason})
	// Deterministic order for the unacked edges.
	ids := make([]string, 0, len(r.Edges))
	for id, e := range r.Edges {
		if e.State == proto.EdgeOpen {
			ids = append(ids, id)
		}
	}
	sortStrings(ids)
	for _, id := range ids {
		e := r.Edges[id]
		w.append(proto.Event{Kind: proto.EvEdgeSettled, FromNode: e.From,
			ToNode: e.To, TransferID: id, TaskID: e.TaskID,
			Reason: "budget:unacknowledged"})
	}
	return w.take(), nil
}

// cascade walks the disengage chain after a node went passive: emit the
// disengage signal + settled edge for this node, its parent, and so on while
// each is passive with zero deficit; if that reaches the root, announce.
// suppressAnnounce is set on the failure path so run.failed is the last word.
func (w *work) cascade(start *Node, suppressAnnounce bool) {
	r := w.scratch.Runs[w.curRun]
	n := start
	for n != nil && n.ID != RootNodeID && n.Engaged && n.ActiveTasks == 0 && n.Deficit == 0 {
		edgeID := n.EngagingEdge
		e := r.Edges[edgeID]
		w.append(proto.Event{Kind: proto.EvSignalSent, FromNode: n.ID,
			ToNode: n.Parent, TransferID: edgeID, Signal: proto.SigDisengage})
		w.append(proto.Event{Kind: proto.EvEdgeSettled, FromNode: n.ID,
			ToNode: n.Parent, TransferID: edgeID, TaskID: e.TaskID})
		n = r.Nodes[n.Parent]
	}
	if !suppressAnnounce && n != nil && n.ID == RootNodeID &&
		r.Phase == proto.PhaseRunning && n.ActiveTasks == 0 && n.Deficit == 0 {
		w.append(proto.Event{Kind: proto.EvDSAnnounce, NodeID: RootNodeID})
	}
}

func (w *work) take() []proto.Event {
	out := w.out
	w.out = nil
	return out
}
