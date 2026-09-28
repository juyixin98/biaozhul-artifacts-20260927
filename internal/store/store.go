// Package store defines the persistence boundary of the broker.
//
// Two implementations satisfy it: storemem (in-memory, for hermetic tests and
// zero-dependency local runs) and storepg (PostgreSQL, for production-shaped
// runs and concurrency tests). Both run the SAME pure rules from
// internal/kernel inside their own atomic transactions; this interface only
// describes the I/O boundary.
package store

import (
	"context"
	"errors"
	"time"

	"localbroker/internal/kernel"
	"localbroker/internal/protocol"
)

// ErrClosed is returned by operations on a closed store.
var ErrClosed = errors.New("store closed")

// ClaimResult is one granted delivery.
type ClaimResult struct {
	MessageID string
	Body      []byte
	Attempts  int
	kernel.Claimed
}

// Store is the full persistence contract.
type Store interface {
	// CreateQueue registers a queue config.
	CreateQueue(ctx context.Context, cfg protocol.QueueConfig) error
	// QueueConfig returns the config or kernel.ErrQueueNotFound.
	QueueConfig(ctx context.Context, name string) (protocol.QueueConfig, error)
	// Queues lists configured queues (used by the background sweeper).
	Queues(ctx context.Context) ([]protocol.QueueConfig, error)

	// Publish stores one new message and appends its enqueued event.
	Publish(ctx context.Context, queue, id string, body []byte, now time.Time, runID string) error

	// Claim atomically grants one available delivery: it locks one available
	// message, applies kernel.DecideClaim (dead-lettering instead if the
	// delivery budget is exhausted), writes the claimed event and returns a
	// fresh receipt. Returns kernel.ErrNoMessage when nothing can be granted.
	Claim(ctx context.Context, queue string, now time.Time, receipt, runID string) (ClaimResult, error)

	// Extend atomically validates the receipt against its lease and timeout
	// (kernel.CheckReceipt under a row lock) and pushes the deadline to
	// now+extra.
	Extend(ctx context.Context, queue, receipt string, extra time.Duration, now time.Time, runID string) (newDeadline time.Time, err error)

	// Ack confirms a live receipt; terminal thereafter.
	Ack(ctx context.Context, queue, receipt string, now time.Time, runID string) error

	// Nack negatively acknowledges a live receipt with a reason; it may
	// redeliver or dead-letter according to the attempt budget.
	Nack(ctx context.Context, queue, receipt, reason string, now time.Time, runID string) error

	// ExpireDue atomically reclaims every lease in the queue whose deadline is
	// <= now (each becoming available again or dead). It returns the number of
	// leases handled. Sweeper and tests both drive it.
	ExpireDue(ctx context.Context, queue string, now time.Time, runID string) (int, error)

	// Message returns live state by id.
	Message(ctx context.Context, queue, id string) (protocol.Message, error)

	// ListDead returns the dead-letter contents of a queue (oldest first);
	// dead messages never appear in ordinary Claim.
	ListDead(ctx context.Context, queue string) ([]protocol.Message, error)

	// ListMessages returns all live rows (diagnostics/admin/replay diff).
	ListMessages(ctx context.Context, queue string) ([]protocol.Message, error)

	// Events returns the ordered event log for a queue (all messages).
	Events(ctx context.Context, queue string) ([]kernel.Event, error)

	// Close releases resources.
	Close() error
}
