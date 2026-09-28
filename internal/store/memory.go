package store

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"sync"
	"time"

	"clsnap/internal/errs"
	"clsnap/internal/protocol"
)

// MemoryStore is an in-process Store. It models the same reliability
// semantics as the Postgres store (persist-before-send outbox, journal,
// abort-on-recovery) so protocol tests run without a database. It does NOT
// survive process death; restart semantics are tested with PgStore.
type MemoryStore struct {
	mu        sync.Mutex
	nodeID    string
	runID     string
	capPer    int
	balances  map[string]int64
	clock     int64

	journal   []Event
	sessions  map[string]*SessionRecord
	seen      map[string]struct{}
	lastInSeq  map[string]int64
	lastOutSeq map[string]int64
	outbox     map[string][]OutboxItem
	outboxID  int64
	booted    bool
}

func NewMemoryStore(nodeID, runID string, peers []string, outboxCapPerPeer int) *MemoryStore {
	if outboxCapPerPeer <= 0 {
		outboxCapPerPeer = 1024
	}
	ms := &MemoryStore{
		nodeID: nodeID, runID: runID, capPer: outboxCapPerPeer,
		balances: map[string]int64{},
		sessions: map[string]*SessionRecord{},
		seen:       map[string]struct{}{},
		lastInSeq:  map[string]int64{},
		lastOutSeq: map[string]int64{},
		outbox:     map[string][]OutboxItem{},
	}
	for _, p := range peers {
		ms.outbox[p] = nil
	}
	return ms
}

func (m *MemoryStore) NodeID() string { return m.nodeID }
func (m *MemoryStore) RunID() string  { return m.runID }

// MarkRestarted is used by restart tests: a fresh MemoryStore is a clean boot,
// so "abort unfinished snapshots on restart" only applies to PgStore in real
// runs. (Memory tests assert the abort logic directly via RecoverAborts.)
func (m *MemoryStore) Bootstrap(initial map[string]int64) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.booted {
		return false, nil
	}
	for k, v := range initial {
		if v < 0 {
			return false, errs.New(errs.ClassInputInvalid, errs.CodeBadAmount,
				fmt.Sprintf("negative initial balance for %q", k), nil)
		}
		m.balances[k] = v
	}
	m.booted = true
	return true, nil
}

func (m *MemoryStore) BootstrapTopology(peers []string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, p := range peers {
		if _, ok := m.outbox[p]; !ok {
			m.outbox[p] = nil
		}
		if _, ok := m.lastInSeq[p]; !ok {
			m.lastInSeq[p] = 0
		}
	}
	return nil
}

// RecoverAborts aborts every session still recording. In the memory store the
// data survives a node-object rebuild inside one test process, so calling
// Rebuild models a process restart: partial snapshots are failed here rather
// than stitched to fresh state. On a genuinely empty boot it returns nothing.
func (m *MemoryStore) RecoverAborts(ctx context.Context) ([]string, error) {
	m.mu.Lock()
	var ids []string
	for id, rec := range m.sessions {
		if rec.Status == StatusRecording {
			ids = append(ids, id)
		}
	}
	sort.Strings(ids)
	for _, id := range ids {
		now := time.Now().UTC()
		m.sessions[id].Status = StatusAborted
		m.sessions[id].AbortedAt = &now
		m.sessions[id].AbortReason = "process restarted while session was recording"
		m.sessions[id].PeersPending = nil
	}
	m.mu.Unlock()
	return ids, nil
}

func (m *MemoryStore) Close() error { return nil }

func (m *MemoryStore) Balances() map[string]int64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make(map[string]int64, len(m.balances))
	for k, v := range m.balances {
		out[k] = v
	}
	return out
}

func (m *MemoryStore) AddBalance(account string, delta int64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	v, ok := m.balances[account]
	if !ok {
		return errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer,
			fmt.Sprintf("unknown account %q", account), nil)
	}
	if v+delta < 0 {
		return errs.New(errs.ClassComputeFailure, errs.CodeInsufficientFunds,
			fmt.Sprintf("account %q balance %d would go negative (%+d)", account, v, delta), nil)
	}
	m.balances[account] = v + delta
	return nil
}

func (m *MemoryStore) Clock() (int64, func(int64)) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.clock, func(t int64) {
		m.mu.Lock()
		if t > m.clock {
			m.clock = t
		}
		m.mu.Unlock()
	}
}

func (m *MemoryStore) AppendEvent(ctx context.Context, ev Event) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	ev.RunID = m.runID
	ev.NodeID = m.nodeID
	ev.Seq = int64(len(m.journal)) + 1
	if ev.At.IsZero() {
		ev.At = time.Now().UTC()
	}
	m.journal = append(m.journal, ev)
	return ev.Seq, nil
}

func (m *MemoryStore) Journal(ctx context.Context, sessionID string, limit int) ([]Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]Event, 0, len(m.journal))
	for _, ev := range m.journal {
		if sessionID != "" && ev.SessionID != sessionID {
			continue
		}
		out = append(out, ev)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Seq < out[j].Seq })
	if limit > 0 && len(out) > limit {
		out = out[len(out)-limit:]
	}
	return out, nil
}

func (m *MemoryStore) EnqueueOutbox(ctx context.Context, toPeer string, env protocol.Envelope) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	q, ok := m.outbox[toPeer]
	if !ok {
		return 0, errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer,
			fmt.Sprintf("no channel to peer %q", toPeer), nil)
	}
	if len(q) >= m.capPer {
		return 0, errs.New(errs.ClassResourceExhausted, errs.CodeOutboxFull,
			fmt.Sprintf("outbox to %q full (%d unacked); peer unreachable", toPeer, len(q)), nil)
	}
	m.lastOutSeq[toPeer]++
	seq := m.lastOutSeq[toPeer]
	env.Seq = seq
	m.outboxID++
	m.outbox[toPeer] = append(q, OutboxItem{ID: m.outboxID, ToPeer: toPeer, Envelope: env})
	return seq, nil
}

func (m *MemoryStore) PendingOutbox(peer string) ([]OutboxItem, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	q, ok := m.outbox[peer]
	if !ok {
		return nil, errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer,
			fmt.Sprintf("no channel to peer %q", peer), nil)
	}
	return append([]OutboxItem(nil), q...), nil
}

func (m *MemoryStore) AckOutbox(id int64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for peer, q := range m.outbox {
		for i, it := range q {
			if it.ID == id {
				m.outbox[peer] = append(q[:i], q[i+1:]...)
				return nil
			}
		}
	}
	return nil
}

func (m *MemoryStore) OutboxLen(peer string) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	q, ok := m.outbox[peer]
	if !ok {
		return 0, errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer, "no such peer", nil)
	}
	return len(q), nil
}

func (m *MemoryStore) StartSession(ctx context.Context, rec SessionRecord) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.sessions[rec.SessionID]; ok {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionExists,
			"session "+rec.SessionID+" already exists", nil)
	}
	cp := rec
	cp.Channels = map[string]*ChannelState{}
	for _, p := range m.peerList() {
		cp.Channels[p] = &ChannelState{From: p, To: m.nodeID}
	}
	m.sessions[rec.SessionID] = &cp
	return nil
}

func (m *MemoryStore) GetSession(sessionID string) (*SessionRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec, ok := m.sessions[sessionID]
	if !ok {
		return nil, errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown,
			"unknown session "+sessionID, nil)
	}
	return cloneSession(rec), nil
}

func (m *MemoryStore) ListSessions() ([]*SessionRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	ids := make([]string, 0, len(m.sessions))
	for id := range m.sessions {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]*SessionRecord, 0, len(ids))
	for _, id := range ids {
		out = append(out, cloneSession(m.sessions[id]))
	}
	return out, nil
}

func (m *MemoryStore) SaveLocalState(ctx context.Context, sessionID string, st LocalState) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.sessions[sessionID]; !ok {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown, "unknown session", nil)
	}
	cp := st
	m.sessions[sessionID].Local = &cp
	return nil
}

func (m *MemoryStore) RecordChannelMessage(ctx context.Context, sessionID, fromPeer string, t protocol.Transfer, markerLamport int64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec, ok := m.sessions[sessionID]
	if !ok {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown, "unknown session", nil)
	}
	ch, ok := rec.Channels[fromPeer]
	if !ok {
		return errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer, "channel from "+fromPeer+" not in topology", nil)
	}
	if ch.MarkerSeenAt != (time.Time{}) {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			"channel "+fromPeer+" already closed for session "+sessionID, nil)
	}
	ch.Recorded = append(ch.Recorded, t)
	ch.TotalInFlight += t.Amount
	return nil
}

func (m *MemoryStore) CloseChannel(ctx context.Context, sessionID, fromPeer string, markerLamport int64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec, ok := m.sessions[sessionID]
	if !ok {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown, "unknown session", nil)
	}
	ch, ok := rec.Channels[fromPeer]
	if !ok {
		return errs.New(errs.ClassInputInvalid, errs.CodeUnknownPeer, "channel from "+fromPeer, nil)
	}
	if ch.MarkerSeenAt != (time.Time{}) {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			"channel already closed", nil)
	}
	ch.MarkerSeenAt = time.Now().UTC()
	ch.MarkerLamport = markerLamport
	rec.PeersPending = without(rec.PeersPending, fromPeer)
	return nil
}

func (m *MemoryStore) AbortSession(ctx context.Context, sessionID, reason string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec, ok := m.sessions[sessionID]
	if !ok {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown, "unknown session", nil)
	}
	now := time.Now().UTC()
	rec.Status = StatusAborted
	rec.AbortedAt = &now
	rec.AbortReason = reason
	rec.PeersPending = nil
	return nil
}

func (m *MemoryStore) CompleteSession(ctx context.Context, sessionID string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	rec, ok := m.sessions[sessionID]
	if !ok {
		return errs.New(errs.ClassStateConflict, errs.CodeSessionUnknown, "unknown session", nil)
	}
	if rec.Local == nil {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			"cannot complete without local state", nil)
	}
	for peer, ch := range rec.Channels {
		if ch.MarkerSeenAt == (time.Time{}) {
			return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
				fmt.Sprintf("channel from %s still open", peer), nil)
		}
	}
	now := time.Now().UTC()
	rec.Status = StatusComplete
	rec.CompletedAt = &now
	rec.PeersPending = nil
	return nil
}

func (m *MemoryStore) HasSeenMessage(msgID string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	_, ok := m.seen[msgID]
	return ok, nil
}

func (m *MemoryStore) RememberMessage(msgID string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.seen[msgID] = struct{}{}
	return nil
}

func (m *MemoryStore) LastInSeq(fromPeer string) (int64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.lastInSeq[fromPeer], nil
}

func (m *MemoryStore) SetLastInSeq(fromPeer string, seq int64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if seq <= m.lastInSeq[fromPeer] {
		return errs.New(errs.ClassStateConflict, errs.CodeFIFOViolation,
			fmt.Sprintf("seq %d <= last %d on channel from %s", seq, m.lastInSeq[fromPeer], fromPeer), nil)
	}
	m.lastInSeq[fromPeer] = seq
	return nil
}

func (m *MemoryStore) peerList() []string {
	out := make([]string, 0, len(m.outbox))
	for p := range m.outbox {
		out = append(out, p)
	}
	sort.Strings(out)
	return out
}

func without(xs []string, x string) []string {
	out := xs[:0]
	for _, v := range xs {
		if v != x {
			out = append(out, v)
		}
	}
	return out
}

func cloneSession(r *SessionRecord) *SessionRecord {
	b, _ := json.Marshal(r)
	var cp SessionRecord
	_ = json.Unmarshal(b, &cp)
	return &cp
}
