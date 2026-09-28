// Package store defines the persistence port of the broker and supplies two
// adapters — an in-memory one (memory.go) and a PostgreSQL one (postgres.go).
//
// Both adapters execute the *same* decisions from internal/kernel inside their
// own atomicity boundary: the memory store holds one mutex, the SQL store
// locks candidate rows in a READ COMMITTED transaction. Therefore the
// extend/ack-vs-timeout race is resolved atomically and identically on both.
package store

import (
	"context"
	"time"

	"workbroker/internal/kernel"
	"workbroker/internal/protocol"
)

// EnqueueInput is an alias for the kernel's enqueue construction input.
type EnqueueInput = kernel.EnqueueInput

// ReceiveInput selects a batch of visible messages.
type ReceiveInput struct {
	Partition   string
	WorkerID    string
	Visibility  time.Duration
	MaxMessages int
}

// Received is one delivered message paired with its fresh receipt.
type Received struct {
	Message *protocol.Message
	Receipt *protocol.Receipt
}

// Store is the broker's persistence port. All methods are safe for concurrent
// use.
type Store interface {
	// Enqueue stores a new message (kernel.EnqueueOne is used to build it).
	Enqueue(ctx context.Context, in *EnqueueInput) (*protocol.Message, protocol.Event, error)

	// Receive reaps expired inflight messages in partition (making them
	// visible/dead) and then hands up to in.MaxMessages visible messages to
	// in.WorkerID, each with a fresh receipt. Returns (nil, nil) when nothing
	// is available — callers implement long polling by retrying.
	Receive(ctx context.Context, in ReceiveInput) ([]Received, error)

	// Extend changes the visibility window of the current delivery.
	Extend(ctx context.Context, receiptID string, extend time.Duration) (*protocol.Message, protocol.Event, error)

	// Ack confirms the current delivery.
	Ack(ctx context.Context, receiptID string) (*protocol.Message, protocol.Event, error)

	// Nack reports explicit worker failure for the current delivery.
	Nack(ctx context.Context, receiptID, reason string) (*protocol.Message, protocol.Event, error)

	// DeadList returns messages parked in the dead-letter area (oldest first),
	// with their full failure histories. Dead messages never appear in
	// Receive.
	DeadList(ctx context.Context, partition string, limit int) ([]DeadRecord, error)

	// Events returns the append-only transition stream (replay interface),
	// ordered by sequence. afterSeq=0 starts from the beginning.
	Events(ctx context.Context, afterSeq int64, limit int) ([]protocol.Event, error)

	// Close releases connections.
	Close() error
}

// DeadRecord pairs a dead message with its terminal reason and history.
type DeadRecord struct {
	Message *protocol.Message
	Reason  *protocol.DeadReason
}
