package router

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"topicrouter/internal/diag"
	"topicrouter/internal/kernel"
	"topicrouter/internal/store"
	"topicrouter/internal/topic"
)

// Router orchestrates the compute kernel and the state store:
//
//   - Updates take the store-registered change, apply it to a copy-on-write
//     kernel builder, and atomically publish one immutable snapshot per
//     routing version.
//   - Publishes match against the snapshot installed at that instant, bind
//     the decision to its version, and append it to the decision log.
//
// Mutex discipline: mu serializes updates (one new snapshot per version, in
// version order). Matches read an atomic snapshot pointer without holding mu,
// so publishes never block on subscription writes.
type Router struct {
	store store.Store
	log   *diag.Logger

	mu      sync.Mutex
	current *kernel.Snapshot
	// versions retains published snapshots incrementally; old versions are
	// shared-trie COW snapshots, cheap to keep. versions[0].Version == 1.
	versions []*kernel.Snapshot
}

// New creates a router and loads the current snapshot from the store.
func New(ctx context.Context, st store.Store, lg *diag.Logger) (*Router, error) {
	r := &Router{store: st, log: lg}
	v, err := st.CurrentVersion(ctx)
	if err != nil {
		return nil, fmt.Errorf("load current version: %w", err)
	}
	data, err := st.SnapshotAt(ctx, v, 0)
	if err != nil {
		return nil, fmt.Errorf("load base snapshot: %w", err)
	}
	snap, err := buildSnapshot(data.Subs, v)
	if err != nil {
		return nil, err
	}
	r.current = snap
	if v > 0 {
		r.versions = []*kernel.Snapshot{snap}
	}
	return r, nil
}

// CurrentVersion reports the live routing version.
func (r *Router) CurrentVersion(ctx context.Context) (int64, error) {
	return r.store.CurrentVersion(ctx)
}

// ApplyResult mirrors store.ApplyResult for callers.
type ApplyResult = store.ApplyResult

// Update validates and applies one subscription upsert/delete and publishes a
// new kernel snapshot. expectedVersion: -1 create-only, -2 unconditional.
func (r *Router) Update(ctx context.Context, id, subscriberID, filter string, delete bool, expectedVersion int64) (ApplyResult, error) {
	change := store.SubscriptionVersion{SubscriptionID: id, SubscriberID: subscriberID, Filter: filter}
	if delete {
		change.Kind = store.KindDelete
	} else {
		change.Kind = store.KindUpsert
		if err := topic.ValidateFilter(filter); err != nil {
			r.recordReject(ctx, "subscription_update", id, err, 0)
			return ApplyResult{}, err
		}
	}
	if id == "" {
		err := &RejectError{Class: "MISSING_SUBSCRIPTION_ID", Detail: "subscription id is required"}
		r.recordReject(ctx, "subscription_update", id, err, 0)
		return ApplyResult{}, err
	}

	// Capture the current filter BEFORE the delete so the kernel can detach
	// the id from the terminal where the previous snapshot had it.
	var oldFilter string
	if delete {
		if cur, gerr := r.store.GetSubscription(ctx, id); gerr == nil {
			oldFilter = cur.Filter
		} else {
			var nf *store.NotFoundError
			if !errors.As(gerr, &nf) {
				r.recordUndecided(ctx, "subscription_update", id, gerr, 0)
				return ApplyResult{}, gerr
			}
		}
	}

	r.mu.Lock()
	defer r.mu.Unlock()

	res, err := r.store.Apply(ctx, change, expectedVersion)
	if err != nil {
		r.recordUndecided(ctx, "subscription_update", id, err, 0)
		return ApplyResult{}, err
	}
	if res.Conflict {
		r.log.Record(ctx, diag.Event{
			Component: "router", Action: "subscription_update", Outcome: diag.Rejected,
			Category: "VERSION_CONFLICT", KeyState: map[string]any{
				"expected_version": expectedVersion,
				"current_version":  res.CurrentVersion,
			},
			Reason: "subscription version conflict",
		})
		return res, nil
	}

	b := kernel.NewBuilder(r.current)
	switch change.Kind {
	case store.KindUpsert:
		b.Insert(topic.SplitSubject(change.Filter), change.SubscriptionID)
	case store.KindDelete:
		if oldFilter != "" {
			b.Delete(topic.SplitSubject(oldFilter), change.SubscriptionID)
		}
	}
	snap := b.Build(res.Version)
	r.current = snap
	r.versions = append(r.versions, snap)

	r.log.Record(ctx, diag.Event{
		Component: "router", Action: "subscription_update", Outcome: diag.Accepted,
		Category: "", Version: res.Version, KeyState: map[string]any{
			"subscription_id": id,
			"change":          string(change.Kind),
		},
		Reason: "snapshot published",
	})
	return res, nil
}

// Publication is the validated input to Publish.
type Publication struct {
	MessageID string
	Topic     string
	Payload   []byte
}

// PublicationResult is what the caller receives; SubIDs is the de-duplicated
// routing set bound to Version.
type PublicationResult struct {
	Version   int64
	SubIDs    []string
	Stats     kernel.Stats
	Inserted  bool // false for an idempotent replay of MessageID
	Decision  store.Decision
}

// Publish routes a message. The decision binds to the snapshot version
// observed at entry; a subscription update arriving concurrently gets a new
// version afterwards and cannot alter this result.
func (r *Router) Publish(ctx context.Context, pub Publication) (PublicationResult, error) {
	if pub.MessageID == "" {
		err := &RejectError{Class: "MISSING_MESSAGE_ID", Detail: "message_id is required"}
		r.recordReject(ctx, "publish", pub.MessageID, err, 0)
		return PublicationResult{}, err
	}
	if err := topic.ValidateTopic(pub.Topic); err != nil {
		r.recordReject(ctx, "publish", pub.MessageID, err, 0)
		return PublicationResult{}, err
	}

	snap := r.snapshot()
	if snap == nil {
		// Should not happen after New, but treat inability to decide honestly.
		err := errors.New("no routing snapshot loaded")
		r.recordUndecided(ctx, "publish", pub.MessageID, err, 0)
		return PublicationResult{}, err
	}
	match := snap.Match(topic.SplitSubject(pub.Topic))

	d := store.Decision{
		MessageID:  pub.MessageID,
		Topic:      pub.Topic,
		Version:    snap.Version,
		SubIDs:     match.Subscribers,
		DecidedAt:  time.Now().UTC(),
		Stats: store.Stats{
			NodeVisits:         match.Stats.NodeVisits,
			EdgeLookups:        match.Stats.EdgeLookups,
			TerminalsCollected: match.Stats.TerminalsCollected,
			DedupHits:          match.Stats.DedupHits,
		},
		PayloadSHA: diag.Fingerprint(pub.Payload),
	}

	existing, inserted, err := r.store.SaveDecision(ctx, d)
	if err != nil {
		r.recordUndecided(ctx, "publish", pub.MessageID, err, snap.Version)
		return PublicationResult{}, err
	}
	if !inserted {
		r.log.Record(ctx, diag.Event{
			Component: "router", Action: "publish", Outcome: diag.Accepted,
			MessageID: pub.MessageID, Version: existing.Version,
			Category: "DUPLICATE_MESSAGE",
			KeyState: map[string]any{
				"payload": diag.Redact(pub.Payload),
			},
			Reason: "message id already decided; returning stored decision",
		})
		return PublicationResult{
			Version: existing.Version, SubIDs: existing.SubIDs,
			Inserted: false, Decision: existing,
		}, nil
	}

	r.log.Record(ctx, diag.Event{
		Component: "router", Action: "publish", Outcome: diag.Accepted,
		MessageID: pub.MessageID, Version: snap.Version,
		KeyState: map[string]any{
			"matched":     len(match.Subscribers),
			"node_visits": match.Stats.NodeVisits,
			"edge_lookups": match.Stats.EdgeLookups,
			"dedup_hits":  match.Stats.DedupHits,
			"payload":     diag.Redact(pub.Payload),
		},
		Reason: "routed and bound to version",
	})
	return PublicationResult{
		Version: snap.Version, SubIDs: match.Subscribers,
		Stats: match.Stats, Inserted: true, Decision: d,
	}, nil
}

// snapshot returns the live snapshot atomically.
func (r *Router) snapshot() *kernel.Snapshot {
	r.mu.Lock()
	s := r.current
	r.mu.Unlock()
	return s
}

// snapshotAt returns the snapshot for routing version v, rebuilding it from
// store history if it is no longer in memory.
func (r *Router) snapshotAt(ctx context.Context, v int64) (*kernel.Snapshot, error) {
	r.mu.Lock()
	for _, s := range r.versions {
		if s.Version == v {
			r.mu.Unlock()
			return s, nil
		}
	}
	r.mu.Unlock()

	data, err := r.store.SnapshotAt(ctx, v, 0)
	if err != nil {
		return nil, err
	}
	return buildSnapshot(data.Subs, v)
}

func (r *Router) recordReject(ctx context.Context, action, key string, err error, version int64) {
	ev := diag.Event{Component: "router", Action: action, Outcome: diag.Rejected, Version: version,
		Reason: err.Error(), KeyState: map[string]any{"key": key}}
	if pe, ok := topic.AsProtocolError(err); ok {
		ev.Category = string(pe.Class)
		ev.KeyState["detail"] = pe.Detail
	}
	var re *RejectError
	if errors.As(err, &re) {
		ev.Category = re.Class
		ev.KeyState["detail"] = re.Detail
	}
	r.log.Record(ctx, ev)
}

func (r *Router) recordUndecided(ctx context.Context, action, key string, err error, version int64) {
	r.log.Record(ctx, diag.Event{
		Component: "router", Action: action, Outcome: diag.Undecided,
		Category: "STORAGE_UNAVAILABLE", Version: version,
		KeyState: map[string]any{"key": key}, Reason: err.Error(),
	})
}

// buildSnapshot constructs an immutable snapshot from a full subscription set.
func buildSnapshot(subs []store.Subscription, version int64) (*kernel.Snapshot, error) {
	b := kernel.NewBuilder(nil)
	for _, s := range subs {
		if err := topic.ValidateFilter(s.Filter); err != nil {
			return nil, fmt.Errorf("stored filter %q invalid: %w", s.ID, err)
		}
		b.Insert(topic.SplitSubject(s.Filter), s.ID)
	}
	return b.Build(version), nil
}
