// Package director runs deterministic snapshot scenarios in-process.
//
// A scenario is a hand-authored sequence of steps over three nodes connected
// by FIFO channels. Sending and delivery are deliberately separate:
//
//   - transfer: the source node debits and the envelope lands in its DURABLE
//     outbox (not yet on the wire);
//   - flush:    queued outbox envelopes for one src->dst channel are handed to
//     the transport, which parks them in the channel inbox — they are now
//     genuinely IN FLIGHT: sender debited, receiver not yet credited;
//   - pump:     delivers queued in-flight envelopes to the receiver in FIFO
//     order (n=-1 delivers all);
//   - snapshot: a node initiates a Chandy-Lamport round (its markers also sit
//     in its outbox until flush, exactly as transfers do).
//
// There are no sleeps and no global pauses: nodes keep accepting transfers
// while rounds are open; precise marker interleavings come from the explicit
// flush/pump order, which is recorded step-by-step and is fully replayable.
package director

import (
	"context"
	"fmt"
	"sync"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
	"clsnap/internal/transport"
)

// Scenario is the fixture format (testdata/scenarios/*.json).
type Scenario struct {
	Name        string             `json:"name"`
	Description string             `json:"description"`
	Nodes       []protocol.NodeID  `json:"nodes"`
	Accounts    []protocol.Account `json:"accounts"`
	Steps       []Step             `json:"steps"`
	Expect      Expect             `json:"expect"`
}

// Step is one deterministic action. Exactly one action field is set.
type Step struct {
	Kind string `json:"kind"` // see the Step* constants

	Node protocol.NodeID  `json:"node,omitempty"`     // acting node
	Src  protocol.NodeID  `json:"src,omitempty"`      // channel source (flush/pump)
	Dst  protocol.NodeID  `json:"dst,omitempty"`      // channel destination

	Transfer *protocol.Transfer  `json:"transfer,omitempty"` // kind=transfer
	Snapshot protocol.SnapshotID `json:"snapshot,omitempty"`  // kind=snapshot
	N        int                 `json:"n,omitempty"`         // pump/flush count (0=1, -1=all)

	// ExpectError optionally asserts this step fails with the given kind.
	ExpectError string `json:"expect_error,omitempty"`
	Note        string `json:"note,omitempty"`
}

// Expect is the hand-computed expected outcome of the scenario.
type Expect struct {
	// InitialTotal is the sum of all seeded balances. Token conservation
	// means every completed global snapshot must equal it.
	InitialTotal uint64 `json:"initial_total"`
	// FinalLiveTotal is the sum of the live ledgers after all steps.
	FinalLiveTotal uint64 `json:"final_live_total"`
	// Snapshots maps each snapshot id to its detailed expectations.
	Snapshots map[string]ExpectSnapshot `json:"snapshots"`
}

// ExpectSnapshot states the concrete numbers a correct run must produce.
type ExpectSnapshot struct {
	Complete bool `json:"complete"`
	// GlobalTotal is the independent global snapshot sum
	// (sum local totals - sum captured in-flight amounts).
	GlobalTotal uint64 `json:"global_total"`
	// LocalTotals states the recorded local total per node.
	LocalTotals map[string]uint64 `json:"local_totals"`
	// InFlight sums captured on directed channels keyed "src->dst".
	InFlight map[string]uint64 `json:"in_flight"`
	// Aborted lists nodes expected to have this round aborted.
	Aborted []string `json:"aborted,omitempty"`
}

const (
	StepTransfer = "transfer" // node accepts a transfer (lands in durable outbox)
	StepFlush    = "flush"    // move src->dst outbox envelopes onto the wire (in flight)
	StepPump     = "pump"     // deliver n queued in-flight envelopes src->dst
	StepSnapshot = "snapshot" // node initiates a round
)

// Cluster is the wired in-process test cluster.
type Cluster struct {
	mu sync.Mutex

	Nodes    []protocol.NodeID
	Accounts map[protocol.NodeID][]protocol.Account
	Dir      *transport.DirectTransport

	// Node operations are injected by the harness after it builds
	// coordinators, avoiding an import cycle.
	Transfer func(ctx context.Context, node protocol.NodeID, t protocol.Transfer) error
	Snapshot func(ctx context.Context, node protocol.NodeID, s protocol.SnapshotID) error
	Flush    func(ctx context.Context, src, dst protocol.NodeID) (int, error)
}

// NewCluster builds the direct transport matrix and account seed map.
func NewCluster(sc *Scenario) *Cluster {
	seed := map[protocol.NodeID][]protocol.Account{}
	for _, a := range sc.Accounts {
		seed[a.Owner] = append(seed[a.Owner], a)
	}
	return &Cluster{
		Nodes:    append([]protocol.NodeID(nil), sc.Nodes...),
		Accounts: seed,
		Dir:      transport.NewDirect(sc.Nodes),
	}
}

// Pending reports queued in-flight envelope count.
func (c *Cluster) Pending(src, dst protocol.NodeID) int { return c.Dir.Pending(src, dst) }

// OutboxPending is implemented via the injected Flush machinery; harnesses
// expose counts through OutboxCount for diagnostics.
var _ = fmt.Sprintf

// Run executes every step in order and checks ExpectError annotations.
func (c *Cluster) Run(ctx context.Context, sc *Scenario) error {
	for i := range sc.Steps {
		st := sc.Steps[i]
		err := c.runStep(ctx, st)
		if st.ExpectError != "" {
			if err == nil {
				return apperr.Inputf(apperr.CodeMalformed,
					"step %d expected error kind %s but succeeded (%s)", i, st.ExpectError, st.Note)
			}
			ae, ok := apperr.As(err)
			if !ok {
				return apperr.Failure(apperr.CodeFailure, "director.Run",
					fmt.Sprintf("step %d produced non-structured error: %v", i, err), nil)
			}
			if string(ae.Kind) != st.ExpectError {
				return apperr.Failure(apperr.CodeFailure, "director.Run",
					fmt.Sprintf("step %d expected kind %s got %s/%s: %s",
						i, st.ExpectError, ae.Kind, ae.Code, ae.Msg), nil)
			}
			continue
		}
		if err != nil {
			return fmt.Errorf("step %d (%s) %s: %w", i, st.Kind, st.Note, err)
		}
	}
	return nil
}

func (c *Cluster) runStep(ctx context.Context, st Step) error {
	switch st.Kind {
	case StepTransfer:
		if c.Transfer == nil || st.Transfer == nil {
			return apperr.Inputf(apperr.CodeMalformed, "transfer step misconfigured")
		}
		return c.Transfer(ctx, st.Node, *st.Transfer)
	case StepSnapshot:
		if c.Snapshot == nil {
			return apperr.Inputf(apperr.CodeMalformed, "snapshot step misconfigured")
		}
		return c.Snapshot(ctx, st.Node, st.Snapshot)
	case StepFlush:
		if c.Flush == nil {
			return apperr.Inputf(apperr.CodeMalformed, "flush step misconfigured")
		}
		_, err := c.Flush(ctx, st.Src, st.Dst)
		return err
	case StepPump:
		if st.N < 0 {
			_, err := c.Dir.PumpAll(ctx, st.Src, st.Dst)
			return err
		}
		_, err := c.Dir.Pump(ctx, st.Src, st.Dst, st.N)
		return err
	default:
		return apperr.Inputf(apperr.CodeUnknownScenario, "unknown step kind %q", st.Kind)
	}
}

// Pump is exported for custom harness sequences.
func (c *Cluster) Pump(ctx context.Context, src, dst protocol.NodeID, n int) (int, error) {
	if n < 0 {
		return c.Dir.PumpAll(ctx, src, dst)
	}
	return c.Dir.Pump(ctx, src, dst, n)
}
