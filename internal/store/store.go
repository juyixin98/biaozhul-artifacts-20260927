// Package store defines the persistence boundary and implements it twice:
//
//   - memory.go: in-process implementation used by unit tests and by the
//     default zero-config demo. It is NOT durable across process restart by
//     design; restart semantics are therefore tested against postgres.go.
//   - postgres.go: PostgreSQL implementation used in production-shaped runs
//     and in the integration restart test. Snapshot records, channel state,
//     outbox entries, dedup keys and the append-only event log are all
//     transactional there.
//
// The interface is split into small interfaces so the snapshot coordinator
// depends only on what it uses.
package store

import (
	"context"

	"clsnap/internal/channel"
	"clsnap/internal/protocol"
)

// channelPendingStore is the channel.PendingStore view; kept unexported here
// because callers depend on channel.PendingStore directly.
type channelPendingStore = channel.PendingStore

// Ensure both implementations satisfy the union contract.
var _ channel.PendingStore = (Store)(nil)

// SnapshotStore persists per-node snapshot records.
type SnapshotStore interface {
	// SaveRecord upserts a node's full record for one round.
	SaveRecord(ctx context.Context, rec protocol.NodeRecord) error
	// GetRecord fetches one record; ErrNotFound (apperr input/unknown_snapshot)
	// when missing.
	GetRecord(ctx context.Context, node protocol.NodeID, snap protocol.SnapshotID) (protocol.NodeRecord, error)
	// ListRecords returns every node record for one round.
	ListRecords(ctx context.Context, snap protocol.SnapshotID) ([]protocol.NodeRecord, error)
	// ListSnapshots returns distinct snapshot ids known to this store.
	ListSnapshots(ctx context.Context) ([]protocol.SnapshotID, error)
	// Abort marks one node's record aborted with a reason; no-op style upsert.
	Abort(ctx context.Context, node protocol.NodeID, snap protocol.SnapshotID, reason string) error
}

// EventStore is the append-only replay log.
type EventStore interface {
	AppendEvent(ctx context.Context, ev protocol.Event) error
	ListEvents(ctx context.Context, runID string) ([]protocol.Event, error)
	ListRuns(ctx context.Context) ([]string, error)
}

// Store is the union used by node wiring.
type Store interface {
	channelPendingStore
	SnapshotStore
	EventStore
	DedupStore
	// Bootstrap creates schema/tables and seeds accounts. It is idempotent.
	Bootstrap(ctx context.Context, node protocol.NodeID, accounts []protocol.Account) error
	// LoadAccounts returns the durable accounts of a node.
	LoadAccounts(ctx context.Context, node protocol.NodeID) ([]protocol.Account, error)
	// SaveAccounts overwrites account state (used when reloading after crash
	// recovery or replay restoration).
	SaveAccounts(ctx context.Context, node protocol.NodeID, accounts []protocol.Account) error
	// BumpEpoch returns a new boot epoch for the node; epoch 1 on first boot.
	BumpEpoch(ctx context.Context, node protocol.NodeID) (uint64, error)
	// CurrentEpoch reads without incrementing.
	CurrentEpoch(ctx context.Context, node protocol.NodeID) (uint64, error)
	// OpenRounds returns snapshot ids on this node that were left recording.
	OpenRounds(ctx context.Context, node protocol.NodeID) ([]protocol.SnapshotID, error)
	// Close releases resources.
	Close() error
}

// DedupStore persists accepted transfer idempotency keys.
type DedupStore interface {
	// SeenRef reports whether a ref was already accepted on a node.
	SeenRef(ctx context.Context, node protocol.NodeID, ref string) (bool, error)
	// RememberRef records an accepted ref; a duplicate is an input/conflict.
	RememberRef(ctx context.Context, node protocol.NodeID, ref string) error
}
