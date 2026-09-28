// Package storage owns durable state: the per-group state blob and the
// append-only event journal. Two implementations are provided: an
// in-process memory store (default, zero dependencies) and a PostgreSQL
// store with SERIALIZABLE transactions.
package storage

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/lib/pq"

	"example.com/cgcoord/protocol"
)

// Store is a transactional storage backend.
type Store interface {
	// Begin opens a transaction. Transactions are SERIALIZABLE: the
	// coordinator's read-modify-write of a group is atomic even under
	// concurrent requests.
	Begin(ctx context.Context) (Tx, error)
	// ListGroups returns all known group names.
	ListGroups(ctx context.Context) ([]string, error)
	// Close releases backend resources.
	Close() error
}

// Tx is one transaction. Every coordinator request is exactly one tx:
// events are appended and the state blob is updated together, so the journal
// and the served state can never disagree.
type Tx interface {
	// CreateGroup inserts a new group row at seq -1 (next event gets seq 0).
	CreateGroup(name string) error
	// SaveGroupState upserts the state blob.
	SaveGroupState(state *protocol.GroupState) error
	// AppendEvent assigns the next per-group seq, stores the event and
	// returns the assigned seq.
	AppendEvent(e protocol.Event) (protocol.Seq, error)
	// LoadGroup returns the state blob, or (nil,nil) when absent.
	LoadGroup(name string) (*protocol.GroupState, error)
	// ReadEvents returns up to limit events with seq >= from.
	ReadEvents(group string, from protocol.Seq, limit int) ([]protocol.Event, error)
	// Commit commits the transaction.
	Commit() error
	// Rollback aborts it (safe to call after Commit).
	Rollback()
}

// ErrNotFound is returned by CreateGroup conflicts / lookups.
var ErrNotFound = errors.New("storage: not found")

// ErrGroupExists is returned when a group row already exists.
var ErrGroupExists = errors.New("storage: group exists")

// WithTx runs fn in a SERIALIZABLE transaction, retrying only on
// serialization failures/deadlocks. Application errors are returned as-is
// after rollback and are never retried.
func WithTx(ctx context.Context, st Store, fn func(Tx) error) error {
	var lastErr error
	for attempt := 0; attempt < 20; attempt++ {
		tx, err := st.Begin(ctx)
		if err != nil {
			if IsRetryable(err) {
				lastErr = err
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(time.Duration(attempt+1) * time.Millisecond):
				}
				continue
			}
			return fmt.Errorf("storage: begin: %w", err)
		}
		if err := fn(tx); err != nil {
			tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			tx.Rollback()
			if IsRetryable(err) {
				lastErr = err
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(time.Duration(attempt+1) * time.Millisecond):
				}
				continue
			}
			return fmt.Errorf("storage: commit: %w", err)
		}
		return nil
	}
	return fmt.Errorf("storage: transaction did not converge: %w", lastErr)
}

// IsRetryable reports whether the database error is a serialization anomaly
// or deadlock that justifies a retry.
func IsRetryable(err error) bool {
	var pqErr *pq.Error
	if errors.As(err, &pqErr) {
		switch pqErr.Code {
		case "40001", "40P01": // serialization_failure, deadlock_detected
			return true
		}
	}
	return false
}
