package router

import (
	"context"

	"topicrouter/internal/kernel"
	"topicrouter/internal/store"
	"topicrouter/internal/topic"
)

// RecomputeResult is a deterministic replay: the same topic matched against
// the snapshot of the decision's bound version. Match/equal/mismatch explains
// whether history would be reproduced.
type RecomputeResult struct {
	MessageID    string
	Topic        string
	BoundVersion int64
	Historic     []string
	Recomputed   []string
	Status       string // "match" | "mismatch"
	Stats        kernel.Stats
}

// HistoricDecision returns the stored decision for messageID.
func (r *Router) HistoricDecision(ctx context.Context, messageID string) (store.Decision, error) {
	return r.store.GetDecision(ctx, messageID)
}

// ListHistoric returns a page of the decision log.
func (r *Router) ListHistoric(ctx context.Context, limit int, cursor string) (store.DecisionPage, error) {
	return r.store.ListDecisions(ctx, limit, cursor)
}

// Recompute re-runs the stored topic against the snapshot of the version the
// decision originally bound. Rebuilding is deterministic: the immutable kernel
// plus the subscription history cannot change under it. A mismatch is reported
// as an explicit status, never silently overwritten — deletes do not change
// history.
func (r *Router) Recompute(ctx context.Context, messageID string) (RecomputeResult, error) {
	d, err := r.store.GetDecision(ctx, messageID)
	if err != nil {
		return RecomputeResult{}, err
	}
	snap, err := r.snapshotAt(ctx, d.Version)
	if err != nil {
		return RecomputeResult{}, err
	}
	if err := topic.ValidateTopic(d.Topic); err != nil {
		return RecomputeResult{}, err
	}
	match := snap.Match(topic.SplitSubject(d.Topic))
	status := "match"
	if !sameSet(d.SubIDs, match.Subscribers) {
		status = "mismatch"
	}
	return RecomputeResult{
		MessageID:    d.MessageID,
		Topic:        d.Topic,
		BoundVersion: d.Version,
		Historic:     d.SubIDs,
		Recomputed:   match.Subscribers,
		Status:       status,
		Stats:        match.Stats,
	}, nil
}

// SnapshotVersion exposes the snapshot version bound by the next publish (for
// diagnostics and tests).
func (r *Router) SnapshotVersion() int64 {
	s := r.snapshot()
	if s == nil {
		return 0
	}
	return s.Version
}

// Store exposes the backing store for read-only control-plane queries.
func (r *Router) Store() store.Store { return r.store }

func sameSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	// Both kernel outputs are sorted; historic rows were stored sorted too.
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
