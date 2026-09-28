// Package replay is the independent read-side projection. It re-derives the
// Dijkstra-Scholten bookkeeping from the persisted event log with its OWN
// accounting (not kernel.Fold), then optionally compares that against a
// kernel fold. A disagreement is surfaced as a divergence: the live write
// path and this read path share only proto.Event, never code.
package replay

import (
	"fmt"
	"sort"

	"dsnet/kernel"
	"dsnet/proto"
)

// EngineVer identifies this projection semantics in every response.
const EngineVer = "dsnet-replay/1.0.0 (independent event fold)"

type nodeAcct struct {
	engaged     bool
	parent      string
	engagingEdge string
	deficit     int64
	active      int
}

type edgeAcct struct {
	from, to, task string
	state          proto.TransferState
	receipted      bool
	disengaged     bool
}

// Projector folds events using local-only state. It deliberately does not
// import any kernel helper; the only shared symbol is the proto event shape.
type Projector struct {
	phase        proto.RunPhase
	announced    bool
	nodes        map[string]*nodeAcct
	edges        map[string]*edgeAcct
	signals      int64
	duplicates   int64
	open, settled, unacked int64
	succeeded, failed      int64
	lastSeq      int64
	orderIssues  []string
}

func New() *Projector {
	return &Projector{
		phase: proto.PhaseRunning,
		nodes: map[string]*nodeAcct{kernel.RootNodeID: {engaged: true}},
		edges: map[string]*edgeAcct{},
	}
}

// Step applies one event. Validity checks that an independent reader can make
// from the log alone are recorded as orderIssues (never silently "fixed").
func (p *Projector) Step(ev proto.Event) {
	if ev.Seq <= p.lastSeq && ev.Kind != proto.EvRunStarted {
		p.orderIssues = append(p.orderIssues,
			fmt.Sprintf("seq regression: got %d after %d", ev.Seq, p.lastSeq))
	}
	p.lastSeq = ev.Seq

	switch ev.Kind {
	case proto.EvRunStarted:
		p.phase = proto.PhaseRunning
		p.nodes[kernel.RootNodeID].engaged = true

	case proto.EvTransferOpen:
		if _, dup := p.edges[ev.TransferID]; dup {
			p.orderIssues = append(p.orderIssues, "duplicate transfer.open: "+ev.TransferID)
			return
		}
		p.edges[ev.TransferID] = &edgeAcct{
			from: ev.FromNode, to: ev.ToNode, task: ev.TaskID, state: proto.EdgeOpen}
		if s := p.nodes[ev.FromNode]; s != nil {
			s.deficit++
		} else {
			p.orderIssues = append(p.orderIssues, "open from unknown node "+ev.FromNode)
		}
		if r := p.nodes[ev.ToNode]; r != nil {
			r.active++
		}
		p.open++

	case proto.EvNodeEngaged, proto.EvNodeReengaged:
		n := p.nodes[ev.NodeID]
		if n == nil {
			n = &nodeAcct{}
			p.nodes[ev.NodeID] = n
		}
		n.engaged = true
		n.parent = ev.FromNode
		n.engagingEdge = ev.TransferID

	case proto.EvSignalSent:
		p.signals++
		e := p.edges[ev.TransferID]
		switch ev.Signal {
		case proto.SigReceipt:
			if e == nil {
				p.orderIssues = append(p.orderIssues, "receipt for unknown edge "+ev.TransferID)
				return
			}
			if e.receipted {
				// A repeated receipt is observed but changes no counter.
				p.duplicates++
				return
			}
			e.receipted = true
			if s := p.nodes[ev.ToNode]; s != nil { // receipt targets the sender
				s.deficit--
			}
		case proto.SigDisengage:
			if e == nil {
				p.orderIssues = append(p.orderIssues, "disengage for unknown edge "+ev.TransferID)
				return
			}
			if e.disengaged {
				p.duplicates++
				return
			}
			e.disengaged = true
			if par := p.nodes[ev.ToNode]; par != nil {
				par.deficit--
			}
			if ch := p.nodes[ev.FromNode]; ch != nil {
				ch.engaged = false
				ch.parent = ""
				ch.engagingEdge = ""
			}
		}

	case proto.EvEdgeSettled:
		e := p.edges[ev.TransferID]
		if e == nil {
			p.orderIssues = append(p.orderIssues, "settle for unknown edge "+ev.TransferID)
			return
		}
		if e.state != proto.EdgeOpen {
			p.orderIssues = append(p.orderIssues, "settle of non-open edge "+ev.TransferID)
			return
		}
		if ev.Reason == "budget:unacknowledged" {
			e.state = proto.EdgeUnacked
			p.open--
			p.unacked++
		} else {
			e.state = proto.EdgeSettled
			p.open--
			p.settled++
		}

	case proto.EvTaskClaimed:
		// queued->running does not move the active counter (both keep the
		// DS node active); nothing to record beyond existence.

	case proto.EvTaskSucceeded:
		p.succeeded++
		if n := p.nodes[ev.NodeID]; n != nil && n.active > 0 {
			n.active--
		}
	case proto.EvTaskFailed:
		p.failed++
		if n := p.nodes[ev.NodeID]; n != nil && n.active > 0 {
			n.active--
		}

	case proto.EvRunFailed:
		p.phase = proto.PhaseFailed
	case proto.EvBudgetExpired:
		p.phase = proto.PhaseUnacknowledged
	case proto.EvDSAnnounce:
		p.phase = proto.PhaseAnnounced
		p.announced = true
	}
}

// Result is the independent projection.
type Result struct {
	Phase          proto.RunPhase
	Announced      bool
	RootDeficit    int64
	Engaged        int
	Open, Settled, Unacked int64
	Signals        int64
	Duplicates     int64
	Succeeded      int64
	Failed         int64
	PerNode        []proto.NodeCounters
	OrderIssues    []string
}

func (p *Projector) Result() Result {
	r := Result{Phase: p.phase, Announced: p.announced,
		Open: p.open, Settled: p.settled, Unacked: p.unacked,
		Signals: p.signals, Duplicates: p.duplicates,
		Succeeded: p.succeeded, Failed: p.failed, OrderIssues: p.orderIssues}
	ids := make([]string, 0, len(p.nodes))
	for id := range p.nodes {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		n := p.nodes[id]
		if n.engaged {
			r.Engaged++
		}
		if id == kernel.RootNodeID {
			r.RootDeficit = n.deficit
		}
		r.PerNode = append(r.PerNode, proto.NodeCounters{
			NodeID: id, Engaged: n.engaged, Deficit: n.deficit,
			Parent: n.parent, ActiveTasks: n.active})
	}
	return r
}

// Fold folds a whole stream.
func Fold(events []proto.Event) Result {
	p := New()
	for _, ev := range events {
		p.Step(ev)
	}
	return p.Result()
}

// CrossCheck runs BOTH projections over the same events and reports
// divergences. The kernel fold is the system implementation; this package's
// fold is the independent one. They must agree field by field.
func CrossCheck(events []proto.Event) (proto.ReplayProjection, error) {
	ind := Fold(events)

	ks := kernel.FoldAll(events)
	runID := ""
	if len(events) > 0 {
		runID = events[0].RunID
	}
	var kr *kernel.Run
	if runID != "" {
		kr = ks.Runs[runID]
	}

	proj := proto.ReplayProjection{
		SchemaVersion: proto.Version,
		ReplayVer:      EngineVer,
		RunID:          runID,
		EventsReplayed: int64(len(events)),
		Phase:          ind.Phase,
		RootDeficit:    ind.RootDeficit,
		EngagedNodes:   ind.Engaged,
		OpenEdges:      ind.Open,
		SettledEdges:   ind.Settled,
		UnackedEdges:   ind.Unacked,
		PerNode:        ind.PerNode,
	}
	for _, msg := range ind.OrderIssues {
		proj.Divergences = append(proj.Divergences, "replay: "+msg)
	}
	if kr != nil {
		snap := kr.Snapshot()
		if snap.RootDeficit != ind.RootDeficit {
			proj.Divergences = append(proj.Divergences,
				fmt.Sprintf("root_deficit kernel=%d replay=%d", snap.RootDeficit, ind.RootDeficit))
		}
		if snap.EngagedNodes != ind.Engaged {
			proj.Divergences = append(proj.Divergences,
				fmt.Sprintf("engaged kernel=%d replay=%d", snap.EngagedNodes, ind.Engaged))
		}
		if snap.OpenEdges != ind.Open || snap.SettledEdges != ind.Settled ||
			snap.UnackedEdges != ind.Unacked {
			proj.Divergences = append(proj.Divergences,
				fmt.Sprintf("edges kernel=(%d/%d/%d) replay=(%d/%d/%d)",
					snap.OpenEdges, snap.SettledEdges, snap.UnackedEdges,
					ind.Open, ind.Settled, ind.Unacked))
		}
		if kr.Phase != ind.Phase {
			proj.Divergences = append(proj.Divergences,
				fmt.Sprintf("phase kernel=%s replay=%s", kr.Phase, ind.Phase))
		}
	}
	return proj, nil
}
