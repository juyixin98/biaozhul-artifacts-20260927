// Package storage defines the persistence boundary of the NAT model and
// provides an in-memory implementation (this file) plus a SQLite-backed one
// (sqlite.go). The core engine depends only on the Store interface.
package storage

import (
	"context"
	"time"

	"natlab/internal/model"
)

// StoredMapping is the serialized form of one active mapping.
type StoredMapping struct {
	RunID        string
	ID           string
	Proto        model.Protocol
	IntSrcIP     string
	IntSrcPort   uint16
	ExtDstIP     string
	ExtDstPort   uint16
	ExternalIP   string
	ExternalPort uint16
	State        string
	CreatedAt    time.Time
	LastSeen     time.Time
	ExpiresAt    time.Time
}

// StoredTombstone remembers a mapping that ended, long enough to distinguish a
// late return packet (mapping_expired) from traffic that never had a mapping
// (no_matching_mapping).
type StoredTombstone struct {
	RunID        string
	Proto        model.Protocol
	ExternalPort uint16
	RemoteIP     string
	RemotePort   uint16
	ClosedAt     time.Time
	RetainUntil  time.Time
}

// RunInfo is the replay-run header row.
type RunInfo struct {
	ID         string
	Name       string
	ConfigJSON string
	CreatedAt  time.Time
	Watermark  time.Time
}

// Store is the full persistence contract. Every method returns an error on
// persistence failure so the engine can label the decision compute_failure;
// policy rejections are never persistence errors.
type Store interface {
	UpsertRun(ctx context.Context, r RunInfo) error
	GetRun(ctx context.Context, id string) (RunInfo, error)

	PutMapping(ctx context.Context, m StoredMapping) error
	UpdateMapping(ctx context.Context, m StoredMapping) error
	DeleteMapping(ctx context.Context, runID, id string, at time.Time) error
	ListMappings(ctx context.Context, runID string) ([]StoredMapping, error)

	AddTombstone(ctx context.Context, t StoredTombstone) error
	FindTombstone(ctx context.Context, runID string, proto model.Protocol, extPort uint16, remoteIP string, remotePort uint16, at time.Time) (bool, error)
	PruneTombstones(ctx context.Context, runID string, at time.Time) error

	AppendDecision(ctx context.Context, d model.Decision) error
	ListDecisions(ctx context.Context, runID string, fromSeq int64, limit int) ([]model.Decision, error)

	SetWatermark(ctx context.Context, runID string, at time.Time) error
	Close() error
}

// ErrNotFound is returned by GetRun/FindTombstone style lookups.
type ErrNotFound struct{ What string }

func (e *ErrNotFound) Error() string { return "storage: not found: " + e.What }

// AsNotFound extracts an ErrNotFound without callers importing the concrete
// type repeatedly.
func AsNotFound(err error) (*ErrNotFound, bool) {
	nf, ok := err.(*ErrNotFound)
	return nf, ok
}
