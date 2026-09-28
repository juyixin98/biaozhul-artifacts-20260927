// Package store is the persistence boundary of the causal layer.
//
// The core never touches database drivers directly; it depends on the Store
// interface. Two implementations are provided:
//   - MemoryStore: zero-dependency, used by default and by most tests.
//   - PostgresStore: durable storage with transactional delivery commits.
//
// Delivery commit is transactional: either the whole causal cascade becomes
// delivered (status flip + delivery order + advanced delivered clock) or none
// of it does, so a crash can never record an advanced clock without the
// predecessor messages it implies.
package store

import (
	"context"

	"cbcast/internal/protocol"
)

// Status of a stored envelope.
type Status string

const (
	StatusPending   Status = "pending"
	StatusDelivered Status = "delivered"
)

// PutOutcome classifies an attempt to store an envelope.
type PutOutcome int

const (
	// PutInserted: a new pending row was written.
	PutInserted PutOutcome = iota
	// PutDuplicate: same MessageID and same content hash already stored.
	PutDuplicate
	// PutConflict: same MessageID but different content hash already stored.
	PutConflict
)

// State is a snapshot of the store's causal state.
type State struct {
	DeliveredClock protocol.VC
	// DeliverSeq is the last assigned delivery sequence (0 on empty log).
	DeliverSeq uint64
}

// DeliveryTxn is the unit of crash-atomic delivery advancement.
type DeliveryTxn interface {
	// CurrentClock returns the delivered clock as locked at BeginDelivery.
	CurrentClock() protocol.VC
	// CommitDelivery marks the ordered envelopes delivered in the given local
	// delivery order, assigns contiguous sequences starting past the current
	// high-water mark and advances the delivered clock to newClock.
	CommitDelivery(ordered []*protocol.Envelope, newClock protocol.VC) error
	// Discard abandons the transaction.
	Discard()
}

// Store is the full persistence surface used by the core.
type Store interface {
	// BeginDelivery opens a delivery transaction and locks the clock row(s)
	// so concurrent deliveries serialize.
	BeginDelivery(ctx context.Context) (DeliveryTxn, error)

	// Get returns the stored envelope (nil when absent) and its status
	// (empty when absent).
	Get(ctx context.Context, messageID string) (*protocol.Envelope, Status, error)

	// Put stores a NEW envelope as pending. Duplicate/conflict is reported
	// without mutation.
	Put(ctx context.Context, env *protocol.Envelope) (PutOutcome, error)

	// ListPending returns all currently buffered, not-yet-delivered envelopes.
	ListPending(ctx context.Context) ([]*protocol.Envelope, error)

	// ListDeliveredSince returns delivered envelopes with delivery sequence >
	// sinceSeq, in delivery order. Used by the replay interface.
	ListDeliveredSince(ctx context.Context, sinceSeq uint64) ([]*protocol.Envelope, error)

	// State returns the durable delivered clock and delivery high-water mark.
	State(ctx context.Context) (State, error)

	// Close releases resources.
	Close() error
}
