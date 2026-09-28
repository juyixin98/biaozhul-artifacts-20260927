// Package snapshot is the Chandy-Lamport per-process kernel.
//
// One Manager lives inside every process. It is transport-agnostic: the node
// injects a marker-sender callback so the protocol state machine can be unit
// tested without HTTP.
//
// Invariant implemented here (K. M. Chandy & L. Lamport, 1985, reliable FIFO
// channels assumed):
//
//  1. When a process initiates, or first receives a marker, for a session it
//     does not know: it records its own local state and immediately sends a
//     marker on every outgoing channel.
//  2. After local state is recorded and until the marker for that session
//     arrives on a given incoming channel, every message arriving on that
//     channel is recorded as in-flight channel state.
//  3. When the marker arrives on a channel the channel is closed; messages
//     after it are ordinary post-snapshot traffic and are never recorded.
//  4. The node's slice of the snapshot is complete when all incoming channels
//     have closed.
//
// Multiple session ids run concurrently and are fully isolated by id.
package snapshot

import (
	"context"
	"fmt"
	"sync"
	"time"

	"clsnap/internal/errs"
	"clsnap/internal/protocol"
	"clsnap/internal/store"
	"clsnap/internal/token"
)

// MarkerSender persists+sends one marker. Implemented by the node.
type MarkerSender func(ctx context.Context, toPeer, sessionID string) error

type Manager struct {
	st       store.Store
	clock    *protocol.Clock
	peers    []string // all peer ids (for a 3-node system: the other two)
	sendMark MarkerSender

	mu     sync.Mutex
	live   map[string]*liveSession
}

type liveSession struct {
	id        string
	initiator string
	// pending lists peers whose incoming marker has not arrived yet.
	// Guarded by Manager.mu.
	pending map[string]bool
}

func NewManager(st store.Store, clock *protocol.Clock, peers []string, send MarkerSender) *Manager {
	return &Manager{st: st, clock: clock, peers: peers, sendMark: send, live: map[string]*liveSession{}}
}

func (m *Manager) isPeer(p string) bool {
	for _, x := range m.peers {
		if x == p {
			return true
		}
	}
	return false
}

// Initiate starts a snapshot at this node (step 1 of the algorithm).
func (m *Manager) Initiate(ctx context.Context, sessionID string) (*store.SessionRecord, error) {
	if sessionID == "" {
		return nil, errs.New(errs.ClassInputInvalid, errs.CodeMalformed, "session id required", nil)
	}
	if existing, err := m.st.GetSession(sessionID); err == nil && existing != nil {
		return nil, errs.New(errs.ClassStateConflict, errs.CodeSessionExists,
			fmt.Sprintf("snapshot %q already exists with status %s", sessionID, existing.Status), nil)
	}

	rec := store.SessionRecord{
		SessionID:    sessionID,
		Initiator:    m.st.NodeID(),
		Status:       store.StatusRecording,
		StartedAt:    time.Now().UTC(),
		PeersPending: append([]string(nil), m.peers...),
	}
	if err := m.st.StartSession(ctx, rec); err != nil {
		return nil, err
	}
	if _, err := m.st.AppendEvent(ctx, store.Event{
		RunID: m.st.RunID(), At: time.Now().UTC(), Kind: store.EvSessionStart,
		NodeID: m.st.NodeID(), SessionID: sessionID,
		Detail: map[string]interface{}{"initiator": m.st.NodeID(), "role": "initiator"},
	}); err != nil {
		return nil, err
	}

	if err := m.recordLocalState(ctx, sessionID, m.st.NodeID()); err != nil {
		return nil, err
	}

	m.mu.Lock()
	m.live[sessionID] = &liveSession{
		id: sessionID, initiator: m.st.NodeID(),
		pending: peerSet(m.peers),
	}
	m.mu.Unlock()

	// Markers go out on every channel BEFORE any later application traffic can
	// be enqueued by this call's return.
	for _, p := range m.peers {
		if err := m.sendMark(ctx, p, sessionID); err != nil {
			_ = m.Abort(ctx, sessionID, "failed to send marker: "+err.Error())
			return nil, err
		}
	}
	return m.st.GetSession(sessionID)
}

// recordLocalState freezes the ledger and lamport time at the first marker.
func (m *Manager) recordLocalState(ctx context.Context, sessionID, initiator string) error {
	bal := m.st.Balances()
	lamport := m.clock.Tick()
	lst := store.LocalState{
		NodeID:       m.st.NodeID(),
		Lamport:      lamport,
		RecordedAt:   time.Now().UTC(),
		Balances:     bal,
		TotalBalance: token.Total(bal),
	}
	if err := m.st.SaveLocalState(ctx, sessionID, lst); err != nil {
		return err
	}
	_, err := m.st.AppendEvent(ctx, store.Event{
		RunID: m.st.RunID(), At: time.Now().UTC(), Kind: store.EvStateRecorded,
		NodeID: m.st.NodeID(), SessionID: sessionID, Lamport: lamport,
		Detail: map[string]interface{}{
			"balances": bal, "total": lst.TotalBalance, "initiator": initiator,
		},
	})
	return err
}

// HandleMarker is invoked by the node for a validated, deduplicated marker.
// It returns true when this marker completed the node's slice of the snapshot.
func (m *Manager) HandleMarker(ctx context.Context, env protocol.Envelope) (completed bool, err error) {
	if !m.isPeer(env.From) {
		return false, errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer,
			fmt.Sprintf("marker from unknown peer %q", env.From), nil)
	}

	m.mu.Lock()
	sess := m.live[env.SessionID]
	m.mu.Unlock()

	if sess == nil {
		rec, gerr := m.st.GetSession(env.SessionID)
		if gerr == nil && rec != nil {
			switch rec.Status {
			case store.StatusComplete:
				return false, nil // duplicate marker already accounted; idempotent
			case store.StatusAborted:
				return false, errs.New(errs.ClassSnapshotInterrupted, errs.CodeSessionAborted,
					fmt.Sprintf("marker for aborted snapshot %q", env.SessionID), nil)
			}
			// recording row exists but no live entry -> interrupted process
			return false, errs.New(errs.ClassSnapshotInterrupted, errs.CodeSessionAborted,
				fmt.Sprintf("recording snapshot %q has no live session (process restarted)", env.SessionID), nil)
		}

		// First time this node hears of the session: we are a non-initiator.
		rec = &store.SessionRecord{
			SessionID:    env.SessionID,
			Initiator:    env.From,
			Status:       store.StatusRecording,
			StartedAt:    time.Now().UTC(),
			PeersPending: append([]string(nil), m.peers...),
		}
		if err := m.st.StartSession(ctx, *rec); err != nil {
			return false, err
		}
		if _, err := m.st.AppendEvent(ctx, store.Event{
			RunID: m.st.RunID(), At: time.Now().UTC(), Kind: store.EvSessionStart,
			NodeID: m.st.NodeID(), SessionID: env.SessionID,
			Detail: map[string]interface{}{"initiator": env.From, "role": "follower"},
		}); err != nil {
			return false, err
		}
		if err := m.recordLocalState(ctx, env.SessionID, env.From); err != nil {
			return false, err
		}
		sess = &liveSession{id: env.SessionID, initiator: env.From, pending: peerSet(m.peers)}
		m.mu.Lock()
		m.live[env.SessionID] = sess
		m.mu.Unlock()

		// Rule 1: on first marker, relay a marker on every outgoing channel.
		for _, p := range m.peers {
			if err := m.sendMark(ctx, p, env.SessionID); err != nil {
				_ = m.Abort(ctx, env.SessionID, "failed to relay marker: "+err.Error())
				return false, err
			}
		}
	}

	m.mu.Lock()
	alreadyClosed := !sess.pending[env.From]
	if !alreadyClosed {
		delete(sess.pending, env.From)
	}
	done := len(sess.pending) == 0
	m.mu.Unlock()

	if alreadyClosed {
		// FIFO delivery makes a second marker impossible unless the peer
		// retried an acked send; treat as harmless duplicate.
		return false, nil
	}

	if err := m.st.CloseChannel(ctx, env.SessionID, env.From, env.Lamport); err != nil {
		return false, err
	}
	if _, err := m.st.AppendEvent(ctx, store.Event{
		RunID: m.st.RunID(), At: time.Now().UTC(), Kind: store.EvChannelClosed,
		NodeID: m.st.NodeID(), SessionID: env.SessionID, Lamport: env.Lamport,
		Detail: map[string]interface{}{"from": env.From},
	}); err != nil {
		return false, err
	}

	if done {
		if err := m.st.CompleteSession(ctx, env.SessionID); err != nil {
			return false, err
		}
		m.mu.Lock()
		delete(m.live, env.SessionID)
		m.mu.Unlock()
		if _, err := m.st.AppendEvent(ctx, store.Event{
			RunID: m.st.RunID(), At: time.Now().UTC(), Kind: store.EvSessionDone,
			NodeID: m.st.NodeID(), SessionID: env.SessionID,
		}); err != nil {
			return false, err
		}
		return true, nil
	}
	return false, nil
}

// ObserveTransfer is called AFTER the transfer has been credited to the live
// ledger. It records the message as in-flight state for every open snapshot
// whose cut on the 'from' channel has not happened yet (rule 2).
func (m *Manager) ObserveTransfer(ctx context.Context, env protocol.Envelope) error {
	m.mu.Lock()
	ids := make([]string, 0, len(m.live))
	for id := range m.live {
		ids = append(ids, id)
	}
	m.mu.Unlock()

	for _, id := range ids {
		m.mu.Lock()
		sess := m.live[id]
		m.mu.Unlock()
		if sess == nil {
			continue
		}
	m.mu.Lock()
	open := false
	if sess, ok := m.live[id]; ok {
		open = sess.pending[env.From]
	}
	m.mu.Unlock()
	if !open {
		continue // marker from this peer already seen: post-cut traffic
	}
		if err := m.st.RecordChannelMessage(ctx, id, env.From, *env.Transfer, env.Lamport); err != nil {
			return err
		}
		if _, err := m.st.AppendEvent(ctx, store.Event{
			RunID: m.st.RunID(), At: time.Now().UTC(), Kind: store.EvChannelRecord,
			NodeID: m.st.NodeID(), SessionID: id, Lamport: env.Lamport,
			Detail: map[string]interface{}{
				"from": env.From, "tx_id": env.Transfer.TxID, "amount": env.Transfer.Amount,
			},
		}); err != nil {
			return err
		}
	}
	return nil
}

// Abort marks a session aborted. An aborted session is permanently failed:
// its partial state stays in the journal for replay but can never be
// completed or stitched into another snapshot.
func (m *Manager) Abort(ctx context.Context, sessionID, reason string) error {
	m.mu.Lock()
	delete(m.live, sessionID)
	m.mu.Unlock()
	if err := m.st.AbortSession(ctx, sessionID, reason); err != nil {
		return err
	}
	_, err := m.st.AppendEvent(ctx, store.Event{
		RunID: m.st.RunID(), At: time.Now().UTC(), Kind: store.EvSessionAborted,
		NodeID: m.st.NodeID(), SessionID: sessionID,
		Detail: map[string]interface{}{"reason": reason},
	})
	return err
}

func peerSet(peers []string) map[string]bool {
	s := make(map[string]bool, len(peers))
	for _, p := range peers {
		s[p] = true
	}
	return s
}
