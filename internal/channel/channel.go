// Package channel models the reliable FIFO channels the Chandy-Lamport
// protocol assumes (and that the service explicitly requires — see
// docs/ASSUMPTIONS.md).
//
// Two primitives live here:
//
//   - Outbox: per directed channel (src->dst) send queue. Every envelope is
//     persisted before it is handed to a transport pump, so a crash after an
//     accepted transfer cannot lose it ("reliable"). Envelopes are released
//     in insertion order per channel ("FIFO").
//   - Inbox: per directed channel receive queue used by the deterministic
//     director transport. A message that was sent (and removed from the
//     sender outbox) but not yet pumped to the receiver is genuinely "in
//     flight": the receiver has not applied it, and it sits visibly in the
//     channel queue. Scenario steps pump the queue explicitly, in FIFO
//     order — no wall-clock sleep, fully replayable interleavings.
package channel

import (
	"strconv"
	"sync"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
)

// MaxQueued bounds one directed channel's queue; exceeding it is reported as
// resource_exhausted/channel_queue_full rather than silently dropping.
const MaxQueued = 10_000

// PendingStore is the persistence contract for queued envelopes.
// Implementations must preserve insertion order within one (src,dst) channel.
type PendingStore interface {
	// AppendOutbox persists one envelope and returns its channel-local seq.
	AppendOutbox(env protocol.Envelope) (uint64, error)
	// ListOutbox returns queued envelopes of one channel in FIFO order.
	ListOutbox(src, dst protocol.NodeID) ([]protocol.Envelope, error)
	// AckOutbox removes one envelope identified by its channel-local seq.
	AckOutbox(src, dst protocol.NodeID, seq uint64) error
	// PurgeMarkers removes queued marker envelopes for one snapshot on one
	// channel, returning how many were removed. Transfer envelopes are never
	// removed (they carry tokens and must stay durable). This is used at
	// restart to drop control messages of an aborted round that would
	// otherwise sit ahead of the new round's markers in the FIFO queue and
	// stall the channel.
	PurgeMarkers(src, dst protocol.NodeID, snap protocol.SnapshotID) (int, error)
}

// Outbox is a durable, per-channel FIFO send queue. Durability is provided by
// the injected PendingStore; the Outbox supplies ordering and concurrency.
type Outbox struct {
	mu      sync.Mutex
	src     protocol.NodeID
	dst     protocol.NodeID
	pending PendingStore
}

// NewOutbox wires a channel queue to its persistence.
func NewOutbox(src, dst protocol.NodeID, pending PendingStore) *Outbox {
	return &Outbox{src: src, dst: dst, pending: pending}
}

// Enqueue persists an envelope at the tail, stamping its channel-local Seq.
// Capacity is enforced against the durable queue length, so a blocked
// receiver eventually surfaces exhaustion instead of unbounded growth.
func (o *Outbox) Enqueue(env protocol.Envelope) (protocol.Envelope, error) {
	if env.Src != o.src || env.Dst != o.dst {
		return env, apperr.Inputf(apperr.CodeMalformed,
			"outbox %s->%s received envelope %s->%s", o.src, o.dst, env.Src, env.Dst)
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	queued, err := o.pending.ListOutbox(o.src, o.dst)
	if err != nil {
		return env, apperr.Failure(apperr.CodeStoreIO, "Outbox.Enqueue", "queue length lookup failed", err)
	}
	if len(queued) >= MaxQueued {
		return env, apperr.Exhausted(apperr.CodeChannelFull,
			"channel "+string(o.src)+"->"+string(o.dst)+" is full ("+strconv.Itoa(len(queued))+" queued)")
	}
	seq, err := o.pending.AppendOutbox(env)
	if err != nil {
		if ae, ok := apperr.As(err); ok && ae.Kind == apperr.KindExhausted {
			return env, err
		}
		return env, apperr.Failure(apperr.CodeStoreIO, "Outbox.Enqueue", "append failed", err)
	}
	env.Seq = seq
	return env, nil
}

// Drain returns queued envelopes in FIFO order without removing them.
func (o *Outbox) Drain() ([]protocol.Envelope, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	envs, err := o.pending.ListOutbox(o.src, o.dst)
	if err != nil {
		return nil, apperr.Failure(apperr.CodeStoreIO, "Outbox.Drain", "list failed", err)
	}
	return envs, nil
}

// Ack removes an envelope after confirmed delivery.
func (o *Outbox) Ack(seq uint64) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	if err := o.pending.AckOutbox(o.src, o.dst, seq); err != nil {
		return apperr.Failure(apperr.CodeStoreIO, "Outbox.Ack", "ack failed", err)
	}
	return nil
}

// PurgeMarkers drops queued marker envelopes for one aborted round on this
// channel. Transfer envelopes are untouched. It is the restart recovery hook
// that stops stale control messages blocking the FIFO head.
func (o *Outbox) PurgeMarkers(snap protocol.SnapshotID) (int, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	n, err := o.pending.PurgeMarkers(o.src, o.dst, snap)
	if err != nil {
		return 0, apperr.Failure(apperr.CodeStoreIO, "Outbox.PurgeMarkers", "purge failed", err)
	}
	return n, nil
}

// Inbox is the receive-side FIFO used by the deterministic director
// transport. There are no blocking receivers: delivery is scheduler driven
// so every interleaving is reproducible from the scenario log.
type Inbox struct {
	mu    sync.Mutex
	src   protocol.NodeID
	dst   protocol.NodeID
	queue []protocol.Envelope
}

// NewInbox creates a receive queue for channel src->dst.
func NewInbox(src, dst protocol.NodeID) *Inbox {
	return &Inbox{src: src, dst: dst}
}

// Push injects one envelope at the tail, preserving per-channel FIFO order.
func (in *Inbox) Push(env protocol.Envelope) error {
	if env.Src != in.src || env.Dst != in.dst {
		return apperr.Inputf(apperr.CodeMalformed,
			"inbox %s->%s received envelope %s->%s", in.src, in.dst, env.Src, env.Dst)
	}
	in.mu.Lock()
	defer in.mu.Unlock()
	if len(in.queue) >= MaxQueued {
		return apperr.Exhausted(apperr.CodeChannelFull,
			"inbox "+string(in.src)+"->"+string(in.dst)+" is full")
	}
	in.queue = append(in.queue, env)
	return nil
}

// Pop removes and returns the head envelope, or false if empty.
func (in *Inbox) Pop() (protocol.Envelope, bool) {
	in.mu.Lock()
	defer in.mu.Unlock()
	if len(in.queue) == 0 {
		return protocol.Envelope{}, false
	}
	env := in.queue[0]
	in.queue = in.queue[1:]
	return env, true
}

// Peek returns the head envelope without removing it.
func (in *Inbox) Peek() (protocol.Envelope, bool) {
	in.mu.Lock()
	defer in.mu.Unlock()
	if len(in.queue) == 0 {
		return protocol.Envelope{}, false
	}
	return in.queue[0], true
}

// Len reports the queued length.
func (in *Inbox) Len() int {
	in.mu.Lock()
	defer in.mu.Unlock()
	return len(in.queue)
}
