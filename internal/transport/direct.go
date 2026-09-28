// Package transport defines the peer-to-peer transport contract and its two
// implementations:
//
//   - Transport (interface): Send one envelope to a peer. Reliability/FIFO
//     semantics per directed channel are the responsibility of the
//     implementation and MUST be documented (see docs/ASSUMPTIONS.md).
//   - DirectTransport: in-process, deterministic, gateable transport used by
//     the director and all scenario tests. Envelopes are handed to per-channel
//     inboxes; message movement is driven explicitly by scenario steps so
//     interleavings are reproducible without sleeps.
//
// The HTTP transport lives in internal/server (transportHTTP), since it is
// the node process's own client/server pair.
package transport

import (
	"context"
	"sync"

	"clsnap/internal/apperr"
	"clsnap/internal/channel"
	"clsnap/internal/protocol"
)

// Transport sends envelopes between nodes.
type Transport interface {
	// Send delivers (or parks) an envelope. Implementations must not reorder
	// within one directed channel. A failed Send is a computation_failure /
	// transport_failure; callers keep the message in their durable outbox.
	Send(ctx context.Context, env protocol.Envelope) error
}

// Receiver is the node-side sink for envelopes coming off a transport.
type Receiver interface {
	Receive(ctx context.Context, env protocol.Envelope) error
}

// Handler is the function the node registers to process one envelope.
type Handler func(ctx context.Context, env protocol.Envelope) error

// DirectTransport is the deterministic in-process transport.
//
// Send appends an envelope to the destination channel's inbox; it is then
// genuinely "in flight" until a scenario step pumps it to the receiver.
// Message movement is driven explicitly (Pump / PumpAll), so marker
// interleavings are reproducible without sleeps and without pausing any
// process.
type DirectTransport struct {
	mu        sync.Mutex
	inboxes   map[inboxKey]*channel.Inbox
	receivers map[protocol.NodeID]Receiver
}

type inboxKey struct{ src, dst protocol.NodeID }

// NewDirect builds a transport for the given complete topology (every ordered
// pair of distinct nodes gets one directed channel).
func NewDirect(nodes []protocol.NodeID) *DirectTransport {
	d := &DirectTransport{
		inboxes:   make(map[inboxKey]*channel.Inbox),
		receivers: make(map[protocol.NodeID]Receiver),
	}
	for _, s := range nodes {
		for _, r := range nodes {
			if s != r {
				d.inboxes[inboxKey{s, r}] = channel.NewInbox(s, r)
			}
		}
	}
	return d
}

// RegisterReceiver attaches a node's receive handler.
func (d *DirectTransport) RegisterReceiver(n protocol.NodeID, r Receiver) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.receivers[n] = r
}

// Send enqueues the envelope at the tail of the destination's inbox.
// Delivery only happens on an explicit Pump: the system is closed under
// step scheduling.
func (d *DirectTransport) Send(_ context.Context, env protocol.Envelope) error {
	if env.Src == env.Dst {
		return apperr.Inputf(apperr.CodeMalformed, "refusing self-directed envelope")
	}
	in, ok := d.inboxes[inboxKey{env.Src, env.Dst}]
	if !ok {
		return apperr.Inputf(apperr.CodeUnknownPeer,
			"no channel %s->%s in topology", env.Src, env.Dst)
	}
	return in.Push(env)
}

// Pending reports how many envelopes are queued on a directed channel.
func (d *DirectTransport) Pending(src, dst protocol.NodeID) int {
	d.mu.Lock()
	in := d.inboxes[inboxKey{src, dst}]
	d.mu.Unlock()
	if in == nil {
		return -1
	}
	return in.Len()
}

// Pump delivers up to n queued envelopes from src->dst to dst's receiver in
// FIFO order. It returns the number delivered. When n <= 0 it pumps one.
func (d *DirectTransport) Pump(ctx context.Context, src, dst protocol.NodeID, n int) (int, error) {
	d.mu.Lock()
	in := d.inboxes[inboxKey{src, dst}]
	rcv := d.receivers[dst]
	d.mu.Unlock()
	if in == nil {
		return 0, apperr.Inputf(apperr.CodeUnknownPeer, "no channel %s->%s", src, dst)
	}
	if rcv == nil {
		return 0, apperr.Failure(apperr.CodeTransport, "DirectTransport.Pump",
			"no receiver registered for "+string(dst), nil)
	}
	if n <= 0 {
		n = 1
	}
	delivered := 0
	for i := 0; i < n; i++ {
		env, ok := in.Peek()
		if !ok {
			break
		}
		if err := rcv.Receive(ctx, env); err != nil {
			// The head envelope stays queued: FIFO + reliability mean a
			// failed delivery neither drops nor skips it.
			return delivered, err
		}
		in.Pop()
		delivered++
	}
	return delivered, nil
}

// PumpAll delivers every queued envelope on one channel, in FIFO order.
func (d *DirectTransport) PumpAll(ctx context.Context, src, dst protocol.NodeID) (int, error) {
	return d.Pump(ctx, src, dst, d.Pending(src, dst))
}

// DropMarkersFor simulates the network-control side of a whole-round
// restart: every in-flight marker envelope for one snapshot id is removed
// from every channel queue. Token transfers are preserved. It returns the
// number removed. Restarted receivers would reject these markers anyway;
// dropping them models the round being torn down so a fresh round is not
// blocked behind stale control messages at the FIFO head.
func (d *DirectTransport) DropMarkersFor(snap protocol.SnapshotID) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	removed := 0
	for k, in := range d.inboxes {
		_ = k
		// Inbox does not expose arbitrary removal; rebuild via Pop/Push,
		// keeping everything but matching markers. Because Pop is FIFO this
		// preserves the relative order of surviving envelopes.
		var kept []protocol.Envelope
		for {
			env, ok := in.Pop()
			if !ok {
				break
			}
			if env.Type == protocol.MsgMarker && env.Marker != nil && env.Marker.Snapshot == snap {
				removed++
				continue
			}
			kept = append(kept, env)
		}
		for _, env := range kept {
			_ = in.Push(env)
		}
	}
	return removed
}
