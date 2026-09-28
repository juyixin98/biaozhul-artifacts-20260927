// Package node assembles one Chandy-Lamport process: the token kernel, the
// snapshot state machine, the reliable-FIFO outbox pump and the HTTP surface.
package node

import (
	"context"
	"fmt"
	"sync"
	"time"

	"clsnap/internal/errs"
	"clsnap/internal/protocol"
	"clsnap/internal/snapshot"
	"clsnap/internal/store"
)

// Peer is one outgoing channel destination.
type Peer struct {
	ID      string `json:"id"`
	BaseURL string `json:"base_url"`
}

// Config constructs a Node.
type Config struct {
	NodeID          string
	RunID           string
	InitialBalances map[string]int64
	Peers           []Peer
	OutboxCap       int
	Store           store.Store
	Transport       Transport
	PumpInterval    time.Duration
}

type Node struct {
	cfg  Config
	st   store.Store
	clk  *protocol.Clock
	mgr  *snapshot.Manager
	tr   Transport

	procMu sync.Mutex // serializes all state-changing protocol steps

	pinMu     sync.Mutex
	pinned    map[string]bool // whole-channel delivery gate (test scheduling)

	abortedAtBoot []string

	wg      sync.WaitGroup
	cancel  context.CancelFunc
}

// New wires a node and performs recovery: any snapshot left recording by a
// crashed process is immediately aborted (no stitching across time points).
func New(ctx context.Context, cfg Config) (*Node, error) {
	if cfg.NodeID == "" {
		return nil, errs.New(errs.ClassInputInvalid, errs.CodeMalformed, "node id required", nil)
	}
	if cfg.Store == nil {
		return nil, errs.New(errs.ClassInputInvalid, errs.CodeMalformed, "store required", nil)
	}
	if cfg.Transport == nil {
		cfg.Transport = NewHTTPTransport()
	}
	if cfg.PumpInterval == 0 {
		cfg.PumpInterval = 20 * time.Millisecond
	}
	if cfg.OutboxCap == 0 {
		cfg.OutboxCap = 4096
	}

	n := &Node{
		cfg:    cfg,
		st:     cfg.Store,
		clk:    protocol.NewClock(),
		tr:     cfg.Transport,
		pinned: map[string]bool{},
	}
	peerIDs := make([]string, 0, len(cfg.Peers))
	for _, p := range cfg.Peers {
		peerIDs = append(peerIDs, p.ID)
	}
	if err := cfg.Store.BootstrapTopology(peerIDs); err != nil {
		return nil, err
	}
	n.mgr = snapshot.NewManager(cfg.Store, n.clk, peerIDs, n.sendMarker)

	if _, err := n.st.Bootstrap(cfg.InitialBalances); err != nil {
		return nil, err
	}
	// Restore the persisted lamport clock so logical time is monotonic across
	// restarts.
	if t, _ := n.st.Clock(); t > 0 {
		for n.clk.Get() < t {
			n.clk.Tick()
		}
	}
	aborted, err := n.st.RecoverAborts(ctx)
	if err != nil {
		return nil, err
	}
	for _, sid := range aborted {
		// Journal the recovery decision so a replay shows WHY the session is
		// aborted rather than simply missing.
		_, _ = n.st.AppendEvent(ctx, store.Event{
			RunID: n.st.RunID(), At: time.Now().UTC(), Kind: store.EvSessionAborted,
			NodeID: n.cfg.NodeID, SessionID: sid,
			Detail: map[string]interface{}{"reason": "process restarted while session was recording"},
		})
	}
	n.abortedAtBoot = aborted
	return n, nil
}

// Run starts the outbox pump. Returns when ctx is cancelled.
func (n *Node) Run(ctx context.Context) {
	ctx, n.cancel = context.WithCancel(ctx)
	n.wg.Add(1)
	go n.pumpLoop(ctx)
}

func (n *Node) Shutdown(ctx context.Context) error {
	if n.cancel != nil {
		n.cancel()
	}
	done := make(chan struct{})
	go func() { n.wg.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
	}
	return n.st.Close()
}

func (n *Node) NodeID() string  { return n.cfg.NodeID }
func (n *Node) RunID() string   { return n.st.RunID() }
func (n *Node) Peers() []Peer   { return append([]Peer(nil), n.cfg.Peers...) }
func (n *Node) Store() store.Store { return n.st }
func (n *Node) ClockNow() int64    { return n.clk.Get() }

// peerURL looks up a configured peer.
func (n *Node) peerURL(id string) (string, error) {
	for _, p := range n.cfg.Peers {
		if p.ID == id {
			return p.BaseURL, nil
		}
	}
	return "", errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer,
		fmt.Sprintf("unknown peer %q", id), nil)
}

// SubmitTransfer is the local client API: debit here, persist to the reliable
// outbox, then the pump delivers it. The debit happens at the logical send
// instant; the snapshot algorithm accounts for it correctly whether or not
// delivery has happened.
func (n *Node) SubmitTransfer(ctx context.Context, toPeer string, t protocol.Transfer) (protocol.Envelope, error) {
	if _, err := n.peerURL(toPeer); err != nil {
		return protocol.Envelope{}, err
	}
	if t.TxID == "" || t.Amount <= 0 {
		return protocol.Envelope{}, errs.New(errs.ClassInputInvalid, errs.CodeBadAmount,
			"tx_id and positive amount required", nil)
	}

	n.procMu.Lock()
	defer n.procMu.Unlock()

	if err := n.st.AddBalance(n.cfg.NodeID, -t.Amount); err != nil {
		return protocol.Envelope{}, err
	}
	lamport := n.clk.Tick()
	n.persistClock(lamport)
	env := protocol.Envelope{
		Kind:     protocol.KindTransfer,
		MsgID:    newMsgID(),
		From:     n.cfg.NodeID,
		To:       toPeer,
		Lamport:  lamport,
		Transfer: &t,
		SentAt:   time.Now().UTC(),
	}
	seq, err := n.st.EnqueueOutbox(ctx, toPeer, env)
	if err != nil {
		_ = n.st.AddBalance(n.cfg.NodeID, t.Amount) // compensate
		return protocol.Envelope{}, err
	}
	env.Seq = seq
	_, _ = n.st.AppendEvent(ctx, store.Event{
		RunID: n.st.RunID(), At: time.Now().UTC(), Kind: store.EvTransferSent,
		NodeID: n.cfg.NodeID, Lamport: env.Lamport,
		Detail: map[string]interface{}{
			"to": toPeer, "tx_id": t.TxID, "amount": t.Amount, "seq": seq,
			"msg_id": env.MsgID,
		},
	})
	return env, nil
}

// sendMarker is the snapshot.Manager callback: persist marker to outbox.
func (n *Node) sendMarker(ctx context.Context, toPeer, sessionID string) error {
	if _, err := n.peerURL(toPeer); err != nil {
		return err
	}
	lamport := n.clk.Get()
	env := protocol.Envelope{
		Kind:      protocol.KindMarker,
		MsgID:     newMsgID(),
		From:      n.cfg.NodeID,
		To:        toPeer,
		Lamport:   lamport,
		SessionID: sessionID,
		SentAt:    time.Now().UTC(),
	}
	seq, err := n.st.EnqueueOutbox(ctx, toPeer, env)
	if err != nil {
		return err
	}
	env.Seq = seq
	_, _ = n.st.AppendEvent(ctx, store.Event{
		RunID: n.st.RunID(), At: time.Now().UTC(), Kind: store.EvMarkerSent,
		NodeID: n.cfg.NodeID, SessionID: sessionID, Lamport: lamport,
		Detail: map[string]interface{}{"to": toPeer, "seq": seq, "msg_id": env.MsgID},
	})
	return nil
}

// persistClock pushes the local lamport value into durable storage.
func (n *Node) persistClock(t int64) {
	_, set := n.st.Clock()
	set(t)
}

// InitiateSnapshot starts a global snapshot from this process.
func (n *Node) InitiateSnapshot(ctx context.Context, sessionID string) (*store.SessionRecord, error) {
	n.procMu.Lock()
	defer n.procMu.Unlock()
	return n.mgr.Initiate(ctx, sessionID)
}

// HandleIncoming is the /message entry point: validate, defend FIFO, then apply
// the payload under procMu so local-state recording and message application can
// never interleave.
func (n *Node) HandleIncoming(ctx context.Context, env protocol.Envelope) error {
	if err := env.Validate(n.cfg.NodeID); err != nil {
		return errs.New(errs.ClassInputInvalid, errs.CodeMalformed, err.Error(), err)
	}
	if _, err := n.peerURL(env.From); err != nil {
		return err
	}

	seen, err := n.st.HasSeenMessage(env.MsgID)
	if err != nil {
		return err
	}
	if seen {
		return nil // at-least-once redelivery: idempotent ack
	}
	last, err := n.st.LastInSeq(env.From)
	if err != nil {
		return err
	}
	if env.Seq != last+1 {
		// The documented assumption is reliable FIFO. A gap/reorder is a
		// protocol violation, never silently reordered.
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			fmt.Sprintf("channel %s->%s: expected seq %d, got %d", env.From, n.cfg.NodeID, last+1, env.Seq), nil)
	}

	n.procMu.Lock()
	defer n.procMu.Unlock()

	switch env.Kind {
	case protocol.KindTransfer:
		t := *env.Transfer
		if err := n.st.AddBalance(n.cfg.NodeID, t.Amount); err != nil {
			return err
		}
		observed := n.clk.Observe(env.Lamport)
		n.persistClock(observed)
		if err := n.st.SetLastInSeq(env.From, env.Seq); err != nil {
			return err
		}
		if err := n.st.RememberMessage(env.MsgID); err != nil {
			return err
		}
		_, _ = n.st.AppendEvent(ctx, store.Event{
			RunID: n.st.RunID(), At: time.Now().UTC(), Kind: store.EvTransferIn,
			NodeID: n.cfg.NodeID, Lamport: n.clk.Get(),
			Detail: map[string]interface{}{
				"from": env.From, "tx_id": t.TxID, "amount": t.Amount, "seq": env.Seq,
			},
		})
		return n.mgr.ObserveTransfer(ctx, env)

	case protocol.KindMarker:
		rec, gerr := n.st.GetSession(env.SessionID)
		if gerr == nil && rec != nil && rec.Status == store.StatusAborted {
			return errs.New(errs.ClassSnapshotInterrupted, errs.CodeSessionAborted,
				"session "+env.SessionID+" is aborted on this node", nil)
		}
		observed := n.clk.Observe(env.Lamport)
		n.persistClock(observed)
		if err := n.st.SetLastInSeq(env.From, env.Seq); err != nil {
			return err
		}
		if err := n.st.RememberMessage(env.MsgID); err != nil {
			return err
		}
		_, _ = n.st.AppendEvent(ctx, store.Event{
			RunID: n.st.RunID(), At: time.Now().UTC(), Kind: store.EvMarkerIn,
			NodeID: n.cfg.NodeID, SessionID: env.SessionID, Lamport: n.clk.Get(),
			Detail: map[string]interface{}{"from": env.From, "seq": env.Seq},
		})
		_, err := n.mgr.HandleMarker(ctx, env)
		return err
	}
	return errs.New(errs.ClassInputInvalid, errs.CodeMalformed, "unhandled kind", nil)
}

// SetPinned gates ALL delivery on one outgoing channel. This only holds
// messages in the local outbox; every process keeps running and every other
// channel flows, so it never approximates a global pause.
func (n *Node) SetPinned(peer string, pinned bool) error {
	if _, err := n.peerURL(peer); err != nil {
		return err
	}
	n.pinMu.Lock()
	n.pinned[peer] = pinned
	n.pinMu.Unlock()
	ctx := context.Background()
	kind := store.EvPin
	if !pinned {
		kind = store.EvRelease
	}
	_, _ = n.st.AppendEvent(ctx, store.Event{
		RunID: n.st.RunID(), At: time.Now().UTC(), Kind: kind,
		NodeID: n.cfg.NodeID,
		Detail: map[string]interface{}{"peer": peer, "pinned": pinned},
	})
	return nil
}

// AbortSnapshot fails a session deliberately (also used by restart recovery).
func (n *Node) AbortSnapshot(ctx context.Context, sessionID, reason string) error {
	n.procMu.Lock()
	defer n.procMu.Unlock()
	if _, err := n.st.GetSession(sessionID); err != nil {
		return err
	}
	return n.mgr.Abort(ctx, sessionID, reason)
}

func (n *Node) isPinned(peer string) bool {
	n.pinMu.Lock()
	defer n.pinMu.Unlock()
	return n.pinned[peer]
}

// handleFatalChannelFailure aborts the snapshot carried by the message (if
// any) and journals the delivery failure. This never mutates another snapshot.
func (n *Node) handleFatalChannelFailure(ctx context.Context, it store.OutboxItem, reason string) {
	if env := it.Envelope; env.Kind == protocol.KindMarker && env.SessionID != "" {
		if _, err := n.st.GetSession(env.SessionID); err == nil {
			_ = n.AbortSnapshot(ctx, env.SessionID, reason)
		}
	}
	_, _ = n.st.AppendEvent(context.Background(), store.Event{
		RunID: n.st.RunID(), At: time.Now().UTC(), Kind: store.EvDeliveryFail,
		NodeID: n.cfg.NodeID, Detail: map[string]interface{}{
			"peer": it.ToPeer, "seq": it.Envelope.Seq, "reason": reason,
		},
	})
}

// pumpLoop delivers outbox heads. One head per peer at a time -> per-channel
// FIFO. Errors leave the item in place for the next tick (reliable delivery).
func (n *Node) pumpLoop(ctx context.Context) {
	defer n.wg.Done()
	t := time.NewTicker(n.cfg.PumpInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			n.pumpOnce(ctx)
		}
	}
}

func (n *Node) pumpOnce(ctx context.Context) {
	for _, p := range n.cfg.Peers {
		if n.isPinned(p.ID) {
			continue
		}
		items, err := n.st.PendingOutbox(p.ID)
		if err != nil || len(items) == 0 {
			continue
		}
		it := items[0] // head only
		dctx, cancel := context.WithTimeout(ctx, 2*time.Second)
		derr := n.tr.Deliver(dctx, p.BaseURL, it.Envelope)
		cancel()
		if derr != nil {
			if ce, ok := errs.As(derr); ok && ce.Class == errs.ClassStateConflict && ce.Code == errs.CodeFIFOViolation {
				n.handleFatalChannelFailure(ctx, it, "fifo_order_violation: "+ce.Message)
				continue
			}
			if ce, ok := errs.As(derr); ok && ce.Class == errs.ClassSnapshotInterrupted {
				// The peer's round for this snapshot is dead (it restarted
				// mid-recording). Stitching is forbidden, so this node aborts
				// the same round and drops the undeliverable marker.
				n.handleFatalChannelFailure(ctx, it, "peer aborted session: "+ce.Message)
				if it.Envelope.Kind == protocol.KindMarker {
					_ = n.st.AckOutbox(it.ID)
				}
				continue
			}
			_, _ = n.st.AppendEvent(context.Background(), store.Event{
				RunID: n.st.RunID(), At: time.Now().UTC(), Kind: store.EvDeliveryRetry,
				NodeID: n.cfg.NodeID, Detail: map[string]interface{}{
					"peer": p.ID, "seq": it.Envelope.Seq,
				},
			})
			continue
		}
		_ = n.st.AckOutbox(it.ID)
	}
}
