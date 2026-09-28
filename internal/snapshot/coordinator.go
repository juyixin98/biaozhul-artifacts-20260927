// Package snapshot implements the per-node Chandy-Lamport snapshot state
// machine (the "partition/task protocol" core of the service).
//
// Protocol statement (Chandy & Lamport, 1985), under reliable FIFO channels:
//
//  1. Initiating a snapshot S: the node records its LOCAL STATE, then for each
//     outbound channel enqueues one marker for S AFTER every message already
//     queued there, and starts recording on every inbound channel.
//  2. On receiving a marker for S:
//       - if the node had not recorded local state for S: it records local
//         state now, starts recording the other inbound channels, sends its
//         own markers, and closes the channel on which the marker arrived
//         (its captured state is empty);
//       - if it had already recorded: the channel on which the marker
//         arrived is closed; everything received on it since this node
//         recorded is that channel's state.
//  3. A node's record is complete when local state and all inbound channels
//     are recorded. The global snapshot is the collection of node records.
//
// Restart rule (project requirement): on boot, any round left "recording" is
// aborted — the node refuses to continue it. Callers must restart the whole
// round. The boot epoch stamped on every marker additionally rejects stale
// markers from the pre-restart incarnation, so states from two eras can never
// be stitched together.
//
// Multiple snapshot ids run concurrently; all per-round state lives in
// independent records keyed by snapshot id.
package snapshot

import (
	"context"
	"fmt"
	"sync"
	"time"

	"clsnap/internal/apperr"
	"clsnap/internal/channel"
	"clsnap/internal/clock"
	"clsnap/internal/protocol"
	"clsnap/internal/store"
	"clsnap/internal/transport"
)

// MaxConcurrentRounds bounds parallel snapshots on one node. Beyond it a new
// round fails with resource_exhausted/too_many_snapshots.
const MaxConcurrentRounds = 16

// Logger is the narrow view of the run journal the coordinator needs.
type Logger interface {
	Event(ctx context.Context, ev protocol.Event) protocol.Event
}

// Router resolves an account id to the node that owns it. The account sets
// are disjoint and fixed, so this is a static table built during wiring.
type Router interface {
	OwnerOf(account string) (protocol.NodeID, bool)
}

// LedgerView is the kernel surface the coordinator uses.
type LedgerView interface {
	Debit(t protocol.Transfer) error
	Credit(t protocol.Transfer) error
	Snapshot() (map[string]protocol.Account, uint64, error)
	NodeID() protocol.NodeID
}

// Coordinator runs one node's participation in snapshot rounds.
type Coordinator struct {
	mu       sync.Mutex
	node     protocol.NodeID
	peers    []protocol.NodeID
	ledger   LedgerView
	st       store.Store
	tr       transport.Transport
	clk      *clock.Clock
	log      Logger
	router   Router
	epoch    uint64
	outboxes map[protocol.NodeID]*channel.Outbox // by destination
}

// Deps wires a coordinator.
type Deps struct {
	Node      protocol.NodeID
	Peers     []protocol.NodeID
	Ledger    LedgerView
	Store     store.Store
	Transport transport.Transport
	Clock     *clock.Clock
	Logger    Logger
	Router    Router
	Epoch     uint64
}

// New constructs a coordinator and its per-peer durable outboxes.
func New(d Deps) (*Coordinator, error) {
	if d.Node == "" {
		return nil, apperr.Inputf(apperr.CodeMalformed, "coordinator requires node id")
	}
	if d.Store == nil || d.Transport == nil || d.Ledger == nil || d.Clock == nil || d.Router == nil {
		return nil, apperr.Failure(apperr.CodeKernelInvariant, "snapshot.New",
			"coordinator missing a dependency", nil)
	}
	c := &Coordinator{
		node:     d.Node,
		peers:    append([]protocol.NodeID(nil), d.Peers...),
		ledger:   d.Ledger,
		st:       d.Store,
		tr:       d.Transport,
		clk:      d.Clock,
		log:      d.Logger,
		router:   d.Router,
		epoch:    d.Epoch,
		outboxes: make(map[protocol.NodeID]*channel.Outbox),
	}
	for _, p := range c.peers {
		c.outboxes[p] = channel.NewOutbox(c.node, p, c.st)
	}
	return c, nil
}

// Epoch returns the boot epoch stamped on this node's markers.
func (c *Coordinator) Epoch() uint64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.epoch
}

// RecoverAbortedRounds is called once at boot. Every record left in
// "recording" by a previous process incarnation is flipped to aborted with a
// restart reason. It returns the aborted snapshot ids. The node never resumes
// such a round; its markers now carry a new epoch and the durable record is
// marked, so observers cannot stitch two eras together.
func (c *Coordinator) RecoverAbortedRounds(ctx context.Context) ([]protocol.SnapshotID, error) {
	open, err := c.st.OpenRounds(ctx, c.node)
	if err != nil {
		return nil, err
	}
	for _, s := range open {
		reason := fmt.Sprintf("node %s restarted (epoch now %d) before recording finished; round must be restarted", c.node, c.epoch)
		if err := c.st.Abort(ctx, c.node, s, reason); err != nil {
			return nil, err
		}
		// Drop this aborted round's markers from every outbound queue so they
		// cannot sit at the FIFO head and block the next incarnation. Token
		// transfers in the same queues are preserved.
		for _, p := range c.peers {
			if _, err := c.outboxes[p].PurgeMarkers(s); err != nil {
				return nil, err
			}
		}
		c.emit(ctx, protocol.EvRoundAborted, s, "", map[string]any{"reason": reason, "at_boot": true})
	}
	return open, nil
}

// PurgeAbortedRound removes queued markers for an already-aborted round (for
// example when a node learns it should restart the whole round). Safe to call
// when the round is not present locally.
func (c *Coordinator) PurgeAbortedRound(ctx context.Context, snap protocol.SnapshotID) (int, error) {
	total := 0
	for _, p := range c.peers {
		n, err := c.outboxes[p].PurgeMarkers(snap)
		if err != nil {
			return total, err
		}
		total += n
	}
	return total, nil
}

// StartTransfer performs the sender-side work: validate and route, check the
// idempotency key, debit via the kernel, remember the ref, and enqueue one
// envelope on the destination channel's durable FIFO outbox (or credit
// directly when the destination account is local).
func (c *Coordinator) StartTransfer(ctx context.Context, t protocol.Transfer) error {
	if err := t.Validate(); err != nil {
		c.emit(ctx, protocol.EvTransferRejected, "", "", map[string]any{"ref": t.Ref, "reason": "validation"})
		return err
	}
	dst, err := c.routeTo(t.To)
	if err != nil {
		c.emit(ctx, protocol.EvTransferRejected, "", "", map[string]any{"ref": t.Ref, "reason": "route"})
		return err
	}
	seen, err := c.st.SeenRef(ctx, c.node, t.Ref)
	if err != nil {
		return apperr.Failure(apperr.CodeStoreIO, "StartTransfer", "dedup lookup", err)
	}
	if seen {
		return apperr.Conflict(apperr.CodeSnapshotInProgress,
			"transfer ref "+t.Ref+" already accepted on node "+string(c.node))
	}
	if err := c.ledger.Debit(t); err != nil {
		c.emit(ctx, protocol.EvTransferRejected, "", "", map[string]any{"ref": t.Ref, "error": err.Error()})
		return err
	}
	if err := c.st.RememberRef(ctx, c.node, t.Ref); err != nil {
		return err
	}
	if dst == c.node {
		if err := c.ledger.Credit(t); err != nil {
			return err
		}
		c.emit(ctx, protocol.EvTransferAccepted, "", "", map[string]any{"ref": t.Ref, "local": true, "amount": t.Amount})
		return nil
	}
	env := protocol.Envelope{
		Type:     protocol.MsgTransfer,
		Src:      c.node,
		Dst:      dst,
		Lamport:  c.clk.Tick(),
		Transfer: &t,
	}
	env, err = c.outboxes[dst].Enqueue(env)
	if err != nil {
		return apperr.Failure(apperr.CodeStoreIO, "StartTransfer",
			"ledger debited but outbox append failed; durable ref logged for recovery", err)
	}
	c.emit(ctx, protocol.EvTransferAccepted, "", dst, map[string]any{
		"ref": t.Ref, "amount": t.Amount, "to_account": t.To,
		"lamport": env.Lamport, "seq": env.Seq,
	})
	return nil
}

func (c *Coordinator) routeTo(acc string) (protocol.NodeID, error) {
	owner, ok := c.router.OwnerOf(acc)
	if !ok {
		return "", apperr.Inputf(apperr.CodeUnknownAccount, "account %q is not in the topology", acc)
	}
	return owner, nil
}

// Receive is the transport.Receiver entry point: one envelope off a channel.
// It MUST be invoked in FIFO order per inbound channel; the transports in this
// project guarantee that structurally.
func (c *Coordinator) Receive(ctx context.Context, env protocol.Envelope) error {
	c.clk.Observe(env.Lamport)
	switch env.Type {
	case protocol.MsgTransfer:
		if env.Transfer == nil {
			return apperr.Inputf(apperr.CodeMalformed, "transfer envelope without body")
		}
		return c.onTransfer(ctx, env)
	case protocol.MsgMarker:
		if env.Marker == nil {
			return apperr.Inputf(apperr.CodeMalformed, "marker envelope without body")
		}
		return c.onMarker(ctx, env)
	default:
		return apperr.Inputf(apperr.CodeMalformed, "unknown envelope type %q", env.Type)
	}
}

func (c *Coordinator) onTransfer(ctx context.Context, env protocol.Envelope) error {
	t := *env.Transfer

	c.mu.Lock()
	// For every recording round on this node the message is in flight on the
	// inbound channel from Src unless that channel already saw its marker.
	// Capture happens BEFORE the credit, matching the protocol's definition.
	rounds, err := c.st.OpenRounds(ctx, c.node)
	if err != nil {
		c.mu.Unlock()
		return apperr.Failure(apperr.CodeStoreIO, "onTransfer", "list open rounds", err)
	}
	for _, s := range rounds {
		if err := c.captureLocked(ctx, s, env.Src, t); err != nil {
			c.mu.Unlock()
			return err
		}
	}
	c.mu.Unlock()

	if err := c.ledger.Credit(t); err != nil {
		return err
	}
	c.emit(ctx, protocol.EvTransferDelivered, "", env.Src, map[string]any{
		"ref": t.Ref, "amount": t.Amount, "lamport": env.Lamport,
	})
	return nil
}

func (c *Coordinator) onMarker(ctx context.Context, env protocol.Envelope) error {
	m := env.Marker

	c.mu.Lock()
	defer c.mu.Unlock()

	rec, err := c.st.GetRecord(ctx, c.node, m.Snapshot)
	if err != nil && !isUnknownSnapshot(err) {
		return err
	}
	switch {
	case err == nil && rec.Phase == protocol.PhaseAborted:
		// This node restarted while the round was open and aborted it on
		// boot. A marker carrying the OLD round id can only come from a
		// pre-restart incarnation: refuse it rather than re-joining and
		// stitching states from two eras together. The sender's own boot
		// epoch is logged for diagnostics (epochs are per node, so they are
		// not compared numerically across nodes).
		reason := fmt.Sprintf("marker for round %s aborted on node %s after restart (sender epoch %d, local epoch %d); start a new round",
			m.Snapshot, c.node, m.Epoch, c.epoch)
		c.emit(ctx, protocol.EvRoundAborted, m.Snapshot, env.Src, map[string]any{"reason": reason, "stale_marker": true})
		return apperr.Conflict(apperr.CodeRoundStale, reason)
	case err == nil && rec.Phase == protocol.PhaseComplete:
		c.emit(ctx, protocol.EvMarkerReceived, m.Snapshot, env.Src, map[string]any{"duplicate": true})
		return nil
	case err == nil && rec.Phase == protocol.PhaseRecording:
		if err := c.closeChannelLocked(ctx, m.Snapshot, env.Src); err != nil {
			return err
		}
		c.emit(ctx, protocol.EvMarkerReceived, m.Snapshot, env.Src, map[string]any{"closing": true})
	default:
		// First time this node hears of S.
		if err := c.beginRecordingLocked(ctx, m.Snapshot); err != nil {
			return err
		}
		if err := c.closeChannelLocked(ctx, m.Snapshot, env.Src); err != nil {
			return err
		}
		c.emit(ctx, protocol.EvMarkerReceived, m.Snapshot, env.Src, map[string]any{"first": true})
	}
	return c.maybeCompleteLocked(ctx, m.Snapshot)
}

// Initiate starts a snapshot round at this node (the global initiator case).
func (c *Coordinator) Initiate(ctx context.Context, snap protocol.SnapshotID) error {
	if err := snap.Valid(); err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	if existing, err := c.st.GetRecord(ctx, c.node, snap); err == nil {
		switch existing.Phase {
		case protocol.PhaseRecording:
			return apperr.Conflict(apperr.CodeSnapshotInProgress,
				"snapshot "+string(snap)+" already recording on "+string(c.node))
		case protocol.PhaseComplete:
			return apperr.Conflict(apperr.CodeDuplicateSnapshot,
				"snapshot "+string(snap)+" already complete on "+string(c.node))
		case protocol.PhaseAborted:
			return apperr.Conflict(apperr.CodeSnapshotAborted,
				"snapshot "+string(snap)+" was aborted; start a new round id")
		}
	} else if !isUnknownSnapshot(err) {
		return err
	}
	active, err := c.st.OpenRounds(ctx, c.node)
	if err != nil {
		return err
	}
	if len(active) >= MaxConcurrentRounds {
		return apperr.Exhausted(apperr.CodeTooManySnapshots,
			fmt.Sprintf("node %s already recording %d rounds", c.node, len(active)))
	}

	if err := c.beginRecordingLocked(ctx, snap); err != nil {
		return err
	}
	return c.maybeCompleteLocked(ctx, snap)
}

// beginRecordingLocked performs step 1: snapshot local state, persist a
// recording record with one channel slot per peer, then enqueue markers to
// every outbound channel AFTER existing messages (the same per-peer outbox,
// so FIFO makes every already-accepted transfer precede its marker).
// Caller must hold c.mu.
func (c *Coordinator) beginRecordingLocked(ctx context.Context, snap protocol.SnapshotID) error {
	accounts, total, err := c.ledger.Snapshot()
	if err != nil {
		return err
	}
	ls := &protocol.LocalState{
		Node:       c.node,
		Snapshot:   snap,
		Accounts:   accounts,
		Total:      total,
		Lamport:    c.clk.Peek(),
		RecordedAt: time.Now().UTC().Format(time.RFC3339Nano),
		Epoch:      c.epoch,
	}
	chans := make(map[protocol.NodeID]*protocol.ChannelState, len(c.peers))
	for _, p := range c.peers {
		chans[p] = &protocol.ChannelState{
			From: p, To: c.node, Snapshot: snap, Closed: false,
			Messages: []protocol.Transfer{},
		}
	}
	rec := protocol.NodeRecord{
		Node:      c.node,
		Snapshot:  snap,
		Phase:     protocol.PhaseRecording,
		Local:     ls,
		Channels:  chans,
		UpdatedAt: time.Now().UTC().Format(time.RFC3339Nano),
	}
	if err := c.st.SaveRecord(ctx, rec); err != nil {
		return err
	}
	c.emit(ctx, protocol.EvStateRecorded, snap, "", map[string]any{
		"total": total, "accounts": len(accounts), "lamport": ls.Lamport,
	})

	for _, p := range c.peers {
		env := protocol.Envelope{
			Type:    protocol.MsgMarker,
			Src:     c.node,
			Dst:     p,
			Lamport: c.clk.Tick(),
			Marker:  &protocol.Marker{Snapshot: snap, Epoch: c.epoch},
		}
		if _, err := c.outboxes[p].Enqueue(env); err != nil {
			return err
		}
		c.emit(ctx, protocol.EvMarkerSent, snap, p, nil)
	}
	return nil
}

// captureLocked appends a transfer to one round's inbound channel state when
// that channel is still open. Caller holds c.mu.
func (c *Coordinator) captureLocked(ctx context.Context, snap protocol.SnapshotID, from protocol.NodeID, t protocol.Transfer) error {
	rec, err := c.st.GetRecord(ctx, c.node, snap)
	if err != nil {
		return err
	}
	if rec.Phase != protocol.PhaseRecording {
		return nil
	}
	ch, ok := rec.Channels[from]
	if !ok {
		return apperr.Failure(apperr.CodeKernelInvariant, "captureLocked",
			fmt.Sprintf("no channel slot %s->%s for snapshot %s", from, c.node, snap), nil)
	}
	if ch.Closed {
		return nil
	}
	ch.Messages = append(ch.Messages, t)
	ch.Sum += t.Amount
	rec.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	return c.st.SaveRecord(ctx, rec)
}

// closeChannelLocked marks the inbound channel from peer closed.
func (c *Coordinator) closeChannelLocked(ctx context.Context, snap protocol.SnapshotID, peer protocol.NodeID) error {
	rec, err := c.st.GetRecord(ctx, c.node, snap)
	if err != nil {
		return err
	}
	ch, ok := rec.Channels[peer]
	if !ok {
		return apperr.Failure(apperr.CodeKernelInvariant, "closeChannelLocked",
			fmt.Sprintf("no channel slot %s->%s", peer, c.node), nil)
	}
	if ch.Closed {
		return nil
	}
	ch.Closed = true
	rec.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	if err := c.st.SaveRecord(ctx, rec); err != nil {
		return err
	}
	c.emit(ctx, protocol.EvChannelClosed, snap, peer, map[string]any{
		"captured": len(ch.Messages), "sum": ch.Sum,
	})
	return nil
}

// maybeCompleteLocked transitions to complete once all inbound channels are
// closed. Caller holds c.mu.
func (c *Coordinator) maybeCompleteLocked(ctx context.Context, snap protocol.SnapshotID) error {
	rec, err := c.st.GetRecord(ctx, c.node, snap)
	if err != nil {
		return err
	}
	if rec.Phase != protocol.PhaseRecording || rec.Local == nil {
		return nil
	}
	for _, ch := range rec.Channels {
		if !ch.Closed {
			return nil
		}
	}
	rec.Phase = protocol.PhaseComplete
	rec.UpdatedAt = time.Now().UTC().Format(time.RFC3339Nano)
	if err := c.st.SaveRecord(ctx, rec); err != nil {
		return err
	}
	totalInFlight := uint64(0)
	for _, ch := range rec.Channels {
		totalInFlight += ch.Sum
	}
	c.emit(ctx, protocol.EvRoundComplete, snap, "", map[string]any{
		"local_total": rec.Local.Total, "in_flight_total": totalInFlight,
	})
	return nil
}

// Record returns the durable record of one round on this node.
func (c *Coordinator) Record(ctx context.Context, snap protocol.SnapshotID) (protocol.NodeRecord, error) {
	return c.st.GetRecord(ctx, c.node, snap)
}

// FlushPeer sends queued outbox envelopes for one directed channel through
// the transport, acking each durable entry on success. Used by the direct
// harness ("flush" scenario step): after this call the envelopes are in the
// transport's channel queue, i.e. genuinely in flight.
func (c *Coordinator) FlushPeer(ctx context.Context, peer protocol.NodeID) (int, error) {
	ob, ok := c.outboxes[peer]
	if !ok {
		return 0, apperr.Inputf(apperr.CodeUnknownPeer, "node %s has no peer %s", c.node, peer)
	}
	envs, err := ob.Drain()
	if err != nil {
		return 0, err
	}
	flushed := 0
	for _, env := range envs {
		if err := c.tr.Send(ctx, env); err != nil {
			return flushed, apperr.Failure(apperr.CodeTransport, "FlushPeer",
				"send to "+string(peer)+" failed; envelope remains durable and pending", err)
		}
		if err := ob.Ack(env.Seq); err != nil {
			return flushed, err
		}
		flushed++
	}
	return flushed, nil
}

// FlushOutbox sends all queued outbound envelopes through the transport,
// acking each durable entry after successful send. A failure leaves remaining
// entries pending for the next flush (reliable, at-least-once at this layer;
// idempotency keys make redelivery safe).
func (c *Coordinator) FlushOutbox(ctx context.Context) (int, error) {
	flushed := 0
	for _, p := range c.peers {
		n, err := c.FlushPeer(ctx, p)
		flushed += n
		if err != nil {
			return flushed, err
		}
	}
	return flushed, nil
}

func (c *Coordinator) emit(ctx context.Context, kind protocol.EventKind, snap protocol.SnapshotID, peer protocol.NodeID, detail map[string]any) {
	if c.log == nil {
		return
	}
	c.log.Event(ctx, protocol.Event{
		Kind: kind, Node: c.node, Snapshot: snap, Peer: peer,
		Lamport: c.clk.Peek(), Detail: detail,
	})
}

func isUnknownSnapshot(err error) bool {
	ae, ok := apperr.As(err)
	return ok && ae.Code == apperr.CodeUnknownSnapshot
}
