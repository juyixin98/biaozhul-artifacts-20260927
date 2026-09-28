// Package store defines the durable state contract: subscriptions and their
// version history, the monotonic routing version, and the append-only routing
// decision log.
//
// Two implementations exist:
//
//   - MemoryStore is a faithful in-process implementation used for unit tests
//     and local runs without a database.
//   - pgStore persists to PostgreSQL; the two share an interface so tests
//     (including replay and concurrency) run identically against both.
//
// Versioning model
//
// Every Put/Delete advances a monotonic routing version inside the same
// transaction that records the subscription-version row. The kernel snapshot
// published for that version is the subscription *snapshot*; a Publish later
// binds its routing decision to exactly that version, so a concurrent update
// cannot retroactively change which subscribers were selected. Historical
// decisions are never deleted, and nothing mutates them after the fact.
package store

import (
	"context"
	"errors"
	"time"
)

// Kind is what a subscription-version row describes.
type Kind string

const (
	KindUpsert Kind = "upsert"
	KindDelete Kind = "delete"
)

// Subscription is the current state of one subscription row. A deleted
// subscription is absent from current state but fully retained in
// SubscriptionVersion history.
type Subscription struct {
	ID           string
	SubscriberID string
	Filter       string
	UpdatedAt    time.Time
	Version      int64 // routing version at which this row was last set
}

// SubscriptionVersion is one immutable change in a subscription's history.
type SubscriptionVersion struct {
	SubscriptionID string
	Version        int64
	Kind           Kind
	SubscriberID   string
	Filter         string // empty for KindDelete
	CreatedAt      time.Time
}

// RoutingVersion records one advance of the routing table.
type RoutingVersion struct {
	Version   int64
	CreatedAt time.Time
	// SubscriptionID and Change identify what produced this version.
	SubscriptionID string
	Change         Kind
}

// Stats mirrors kernel.Stats across the store boundary without importing the
// kernel package (kernel is lower-level than store in the dependency graph).
type Stats struct {
	NodeVisits         int
	EdgeLookups        int
	TerminalsCollected int
	DedupHits          int
}

// Decision is one persisted publish-time routing result. Rows are append-only:
// no method in this package updates or deletes them.
type Decision struct {
	MessageID  string
	Topic      string
	Version    int64 // routing version bound at publish time
	SubIDs     []string
	Stats      Stats
	DecidedAt  time.Time
	PayloadSHA string // optional, populated by the router for diagnostics
}

// DecisionPage is one page of the decision log, newest first.
type DecisionPage struct {
	Decisions   []Decision
	NextCursor  string // empty when there are no further rows
	HasMore     bool
}

// ApplyResult is the outcome of one subscription mutation.
type ApplyResult struct {
	Version        int64
	SubscriptionID string
	Change         Kind
	Conflict       bool // true when ExpectedVersion did not match
	CurrentVersion int64 // actual version, meaningful when Conflict is true
}

// SnapshotData is the materialized subscription set at a routing version, plus
// the version rows needed to rebuild any later version incrementally.
type SnapshotData struct {
	Version int64
	Subs    []Subscription
	// Changes is the strictly increasing slice of version rows with
	// fromVersion < row.Version <= Version.
	Changes []SubscriptionVersion
}

// Store is the full state contract. All methods are safe for concurrent use.
type Store interface {
	// Apply upserts or deletes a subscription and atomically advances the
	// routing version. When ExpectedVersion >= 0 it is an optimistic
	// concurrency check against the subscription's current version (-1 =
	// create-only, -2 = unconditional).
	Apply(ctx context.Context, change SubscriptionVersion, expectedVersion int64) (ApplyResult, error)

	CurrentVersion(ctx context.Context) (int64, error)

	GetSubscription(ctx context.Context, id string) (Subscription, error) // ErrNotFound when absent
	ListSubscriptions(ctx context.Context) ([]Subscription, error)

	// SnapshotAt returns current subscriptions as of routing version v and the
	// version changes strictly after fromVersion up to and including v.
	// fromVersion <= 0 returns the full history.
	SnapshotAt(ctx context.Context, v int64, fromVersion int64) (SnapshotData, error)

	// SaveDecision appends one routing decision. A duplicate MessageID returns
	// the existing row and Inserted=false (idempotent republish).
	SaveDecision(ctx context.Context, d Decision) (existing Decision, inserted bool, err error)
	GetDecision(ctx context.Context, messageID string) (Decision, error)
	ListDecisions(ctx context.Context, limit int, cursor string) (DecisionPage, error)
}

// ErrNotFound is returned by single-row lookups.
type NotFoundError struct{ Resource, Key string }

func (e *NotFoundError) Error() string { return e.Resource + " not found: " + e.Key }

// AsNotFound extracts a *NotFoundError.
func AsNotFound(err error) (*NotFoundError, bool) {
	var nf *NotFoundError
	if errors.As(err, &nf) {
		return nf, true
	}
	return nil, false
}
