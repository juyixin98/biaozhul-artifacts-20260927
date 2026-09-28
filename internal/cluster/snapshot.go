// Package cluster assembles the per-node slices of a Chandy-Lamport snapshot
// into the global snapshot and checks conservation. It contains NO node
// process code: it only reads what nodes persisted.
package cluster

import (
	"context"
	"fmt"
	"sort"

	"clsnap/internal/protocol"
	"clsnap/internal/store"
)

// NodeView is how a node appears to the assembler (either over HTTP or from an
// in-process test harness).
type NodeView interface {
	ID() string
	GetSession(ctx context.Context, sessionID string) (*store.SessionRecord, error)
}

// GlobalSnapshot is the assembled cut: local states + all in-flight messages.
type GlobalSnapshot struct {
	SessionID    string                          `json:"session_id"`
	Nodes        map[string]*store.SessionRecord `json:"nodes"`
	StatusByNode map[string]store.Status         `json:"status_by_node"`
	AbortedOn    []string                        `json:"aborted_on"`
	Locals       map[string]map[string]int64     `json:"locals"`
	LocalTotals  map[string]int64                `json:"local_totals"`
	InFlight     []InFlightMessage               `json:"in_flight"`
	InFlightSum  int64                           `json:"in_flight_sum"`
	GlobalTotal  int64                           `json:"global_total"`
	Complete     bool                            `json:"complete"`
	AnyAborted   bool                            `json:"any_aborted"`
	MissingNodes []string                        `json:"missing_nodes,omitempty"`
}

type InFlightMessage struct {
	From    string            `json:"from"`
	To      string            `json:"to"`
	TxID    string            `json:"tx_id"`
	Amount  int64             `json:"amount"`
	Transfer protocol.Transfer `json:"transfer"`
}

// Collect gathers each node's slice and computes the global cut.
func Collect(ctx context.Context, sessionID string, nodes []NodeView) (*GlobalSnapshot, error) {
	g := &GlobalSnapshot{
		SessionID:    sessionID,
		Nodes:        map[string]*store.SessionRecord{},
		StatusByNode: map[string]store.Status{},
		Locals:       map[string]map[string]int64{},
		LocalTotals:  map[string]int64{},
	}
	complete := true
	for _, n := range nodes {
		rec, err := n.GetSession(ctx, sessionID)
		if err != nil {
			g.MissingNodes = append(g.MissingNodes, n.ID())
			complete = false
			continue
		}
		g.Nodes[n.ID()] = rec
		g.StatusByNode[n.ID()] = rec.Status
		if rec.Status == store.StatusAborted {
			g.AnyAborted = true
			g.AbortedOn = append(g.AbortedOn, n.ID())
		}
		if rec.Status != store.StatusComplete || rec.Local == nil {
			complete = false
			continue
		}
		g.Locals[n.ID()] = rec.Local.Balances
		g.LocalTotals[n.ID()] = rec.Local.TotalBalance
		g.GlobalTotal += rec.Local.TotalBalance
		for fromID, ch := range rec.Channels {
			for _, t := range ch.Recorded {
				g.InFlight = append(g.InFlight, InFlightMessage{
					From: fromID, To: n.ID(), TxID: t.TxID, Amount: t.Amount, Transfer: t,
				})
				g.InFlightSum += t.Amount
			}
		}
	}
	sort.Strings(g.AbortedOn)
	sort.Slice(g.InFlight, func(i, j int) bool {
		if g.InFlight[i].From != g.InFlight[j].From {
			return g.InFlight[i].From < g.InFlight[j].From
		}
		if g.InFlight[i].To != g.InFlight[j].To {
			return g.InFlight[i].To < g.InFlight[j].To
		}
		return g.InFlight[i].TxID < g.InFlight[j].TxID
	})
	g.Complete = complete && len(g.MissingNodes) == 0
	return g, nil
}

// AbortedError marks a snapshot that cannot be assembled because a node
// aborted it (e.g. restarted mid-recording).
type AbortedError struct {
	Node   string
	Reason string
}

func (e *AbortedError) Error() string {
	return fmt.Sprintf("snapshot aborted on node %s: %s", e.Node, e.Reason)
}
