package store

import (
	"context"
	"sort"
	"sync"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
)

// Memory is an in-process Store. It is safe for concurrent use and models all
// persistence semantics durably within one process lifetime. It deliberately
// does not survive process exit; restart behaviour is exercised against the
// PostgreSQL implementation in tests/postgres_test.go.
type Memory struct {
	mu sync.Mutex

	accounts map[protocol.NodeID][]protocol.Account
	records  map[recordKey]*protocol.NodeRecord
	outbox   map[channelKey][]outboxEntry
	events   []protocol.Event
	seqs     map[string]uint64 // run event seq counters
	refs     map[protocol.NodeID]map[string]bool
	epoch    map[protocol.NodeID]uint64
	runList  map[string]bool

	maxAccounts int // capacity guard, 0 = unlimited
}

type recordKey struct {
	node protocol.NodeID
	snap protocol.SnapshotID
}

type channelKey struct {
	src, dst protocol.NodeID
}

type outboxEntry struct {
	seq uint64
	env protocol.Envelope
}

// NewMemory builds an empty memory store.
func NewMemory() *Memory {
	return &Memory{
		accounts: make(map[protocol.NodeID][]protocol.Account),
		records:  make(map[recordKey]*protocol.NodeRecord),
		outbox:   make(map[channelKey][]outboxEntry),
		seqs:     make(map[string]uint64),
		refs:     make(map[protocol.NodeID]map[string]bool),
		epoch:    make(map[protocol.NodeID]uint64),
		runList:  make(map[string]bool),
	}
}

// SetAccountCap bounds the number of stored accounts; beyond it writes fail
// with resource_exhausted/store_capacity_exceeded. Used by a unit test.
func (m *Memory) SetAccountCap(n int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.maxAccounts = n
}

func (m *Memory) Bootstrap(_ context.Context, node protocol.NodeID, accounts []protocol.Account) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, ok := m.accounts[node]; ok {
		return nil // idempotent
	}
	cp := make([]protocol.Account, len(accounts))
	copy(cp, accounts)
	m.accounts[node] = cp
	if m.refs[node] == nil {
		m.refs[node] = make(map[string]bool)
	}
	return nil
}

func (m *Memory) LoadAccounts(_ context.Context, node protocol.NodeID) ([]protocol.Account, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	a, ok := m.accounts[node]
	if !ok {
		return nil, apperr.Inputf(apperr.CodeUnknownAccount, "node %q not bootstrapped", node)
	}
	out := make([]protocol.Account, len(a))
	copy(out, a)
	return out, nil
}

func (m *Memory) SaveAccounts(_ context.Context, node protocol.NodeID, accounts []protocol.Account) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.maxAccounts > 0 && len(accounts) > m.maxAccounts {
		return apperr.Exhausted(apperr.CodeStoreFull, "account cap exceeded")
	}
	cp := make([]protocol.Account, len(accounts))
	copy(cp, accounts)
	m.accounts[node] = cp
	return nil
}

func (m *Memory) BumpEpoch(_ context.Context, node protocol.NodeID) (uint64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.epoch[node]++
	return m.epoch[node], nil
}

func (m *Memory) CurrentEpoch(_ context.Context, node protocol.NodeID) (uint64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.epoch[node], nil
}

func (m *Memory) OpenRounds(_ context.Context, node protocol.NodeID) ([]protocol.SnapshotID, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []protocol.SnapshotID
	for k, r := range m.records {
		if k.node == node && r.Phase == protocol.PhaseRecording {
			out = append(out, k.snap)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

func (m *Memory) SaveRecord(_ context.Context, rec protocol.NodeRecord) error {
	if rec.Snapshot == "" || rec.Node == "" {
		return apperr.Inputf(apperr.CodeMalformed, "record requires node and snapshot")
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	cp := cloneRecord(rec)
	m.records[recordKey{rec.Node, rec.Snapshot}] = &cp
	return nil
}

func (m *Memory) GetRecord(_ context.Context, node protocol.NodeID, snap protocol.SnapshotID) (protocol.NodeRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	r, ok := m.records[recordKey{node, snap}]
	if !ok {
		return protocol.NodeRecord{}, apperr.Inputf(apperr.CodeUnknownSnapshot,
			"node %s has no record for snapshot %q", node, snap)
	}
	return cloneRecord(*r), nil
}

func (m *Memory) ListRecords(_ context.Context, snap protocol.SnapshotID) ([]protocol.NodeRecord, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []protocol.NodeRecord
	for k, r := range m.records {
		if k.snap == snap {
			out = append(out, cloneRecord(*r))
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Node < out[j].Node })
	return out, nil
}

func (m *Memory) ListSnapshots(_ context.Context) ([]protocol.SnapshotID, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	seen := make(map[protocol.SnapshotID]bool)
	for k := range m.records {
		seen[k.snap] = true
	}
	out := make([]protocol.SnapshotID, 0, len(seen))
	for s := range seen {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out, nil
}

func (m *Memory) Abort(_ context.Context, node protocol.NodeID, snap protocol.SnapshotID, reason string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	r, ok := m.records[recordKey{node, snap}]
	if !ok {
		return apperr.Inputf(apperr.CodeUnknownSnapshot, "cannot abort unknown round")
	}
	r.Phase = protocol.PhaseAborted
	r.Reason = reason
	return nil
}

// --- channel.PendingStore ---

func (m *Memory) AppendOutbox(env protocol.Envelope) (uint64, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	k := channelKey{env.Src, env.Dst}
	seq := uint64(len(m.outbox[k]) + 1)
	env.Seq = seq
	m.outbox[k] = append(m.outbox[k], outboxEntry{seq: seq, env: env})
	return seq, nil
}

func (m *Memory) ListOutbox(src, dst protocol.NodeID) ([]protocol.Envelope, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	entries := m.outbox[channelKey{src, dst}]
	out := make([]protocol.Envelope, len(entries))
	for i, e := range entries {
		e.env.Seq = e.seq
		out[i] = e.env
	}
	return out, nil
}

func (m *Memory) AckOutbox(src, dst protocol.NodeID, seq uint64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	k := channelKey{src, dst}
	entries := m.outbox[k]
	idx := -1
	for i, e := range entries {
		if e.seq == seq {
			idx = i
			break
		}
	}
	if idx == -1 {
		return apperr.Inputf(apperr.CodeMalformed, "ack: seq %d not pending on %s->%s", seq, src, dst)
	}
	m.outbox[k] = append(entries[:idx], entries[idx+1:]...)
	return nil
}

func (m *Memory) PurgeMarkers(src, dst protocol.NodeID, snap protocol.SnapshotID) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	k := channelKey{src, dst}
	entries := m.outbox[k]
	kept := entries[:0]
	removed := 0
	for _, e := range entries {
		if e.env.Type == protocol.MsgMarker && e.env.Marker != nil && e.env.Marker.Snapshot == snap {
			removed++
			continue
		}
		kept = append(kept, e)
	}
	m.outbox[k] = kept
	return removed, nil
}

// --- events ---

func (m *Memory) AppendEvent(_ context.Context, ev protocol.Event) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.seqs[ev.RunID]++
	ev.Seq = m.seqs[ev.RunID]
	m.events = append(m.events, ev)
	m.runList[ev.RunID] = true
	return nil
}

func (m *Memory) ListEvents(_ context.Context, runID string) ([]protocol.Event, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []protocol.Event
	for _, ev := range m.events {
		if ev.RunID == runID {
			out = append(out, ev)
		}
	}
	if out == nil {
		return nil, apperr.Inputf(apperr.CodeUnknownRun, "run %q not found", runID)
	}
	return out, nil
}

func (m *Memory) ListRuns(_ context.Context) ([]string, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]string, 0, len(m.runList))
	for r := range m.runList {
		out = append(out, r)
	}
	sort.Strings(out)
	return out, nil
}

// --- dedup ---

func (m *Memory) SeenRef(_ context.Context, node protocol.NodeID, ref string) (bool, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.refs[node][ref], nil
}

func (m *Memory) RememberRef(_ context.Context, node protocol.NodeID, ref string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.refs[node] == nil {
		m.refs[node] = make(map[string]bool)
	}
	if m.refs[node][ref] {
		return apperr.Conflict(apperr.CodeSnapshotInProgress, "ref "+ref+" already known")
	}
	m.refs[node][ref] = true
	return nil
}

func (m *Memory) Close() error { return nil }

func cloneRecord(r protocol.NodeRecord) protocol.NodeRecord {
	out := r
	if r.Local != nil {
		l := *r.Local
		l.Accounts = make(map[string]protocol.Account, len(r.Local.Accounts))
		for k, v := range r.Local.Accounts {
			l.Accounts[k] = v
		}
		out.Local = &l
	}
	if r.Channels != nil {
		out.Channels = make(map[protocol.NodeID]*protocol.ChannelState, len(r.Channels))
		for k, v := range r.Channels {
			cv := *v
			if v.Messages != nil {
				cv.Messages = append([]protocol.Transfer(nil), v.Messages...)
			}
			out.Channels[k] = &cv
		}
	}
	return out
}
