// Package replay reconstructs routing-table state from the persisted batch
// event stream and verifies stored snapshots against reconstruction.
//
// Replay is deliberately independent of the live mutation path: it starts
// from an empty table and re-applies each recorded batch with persistence
// disabled, so a corrupted head pointer or snapshot table can be detected by
// replaying events rather than trusting a cached state.
package replay

import (
	"context"
	"fmt"
	"sort"

	"github.com/opp221/ribd/internal/netmodel"
	"github.com/opp221/ribd/internal/rib"
	"github.com/opp221/ribd/internal/store"
)

// Result is a replayed table state.
type Result struct {
	Version uint64
	Applied int
	Snap    *rib.Snapshot
}

// Events reconstructs state from an in-memory event list, stopping after the
// batch that produced targetVersion (0 means replay everything).
func Events(events []store.Event, maxDepth int, targetVersion uint64) (Result, error) {
	tbl := rib.NewTable(maxDepth, nil)
	applied := 0
	for _, ev := range events {
		if targetVersion > 0 && ev.Version > targetVersion {
			break
		}
		snap, err := tbl.Apply(ev.Changes)
		if err != nil {
			return Result{}, fmt.Errorf("replay: batch v%d: %w", ev.Version, err)
		}
		if snap.VersionNum() != ev.Version {
			return Result{}, fmt.Errorf("replay: version gap: stream says v%d, reconstruction produced v%d",
				ev.Version, snap.VersionNum())
		}
		applied++
	}
	snap := tbl.Current()
	if targetVersion > 0 && snap.VersionNum() > targetVersion {
		return Result{}, fmt.Errorf("replay: target version %d missing from stream", targetVersion)
	}
	return Result{Version: snap.VersionNum(), Applied: applied, Snap: snap}, nil
}

// FromStore replays the persisted event stream of s.
func FromStore(ctx context.Context, s *store.Store, maxDepth int, targetVersion uint64) (Result, error) {
	events, err := s.Events(ctx, targetVersion)
	if err != nil {
		return Result{}, err
	}
	return Events(events, maxDepth, targetVersion)
}

// Mismatch describes one discrepancy between reconstruction and a stored
// snapshot.
type Mismatch struct {
	Version uint64 `json:"version"`
	RouteID string `json:"route_id"`
	Kind    string `json:"kind"` // missing_in_store, extra_in_store, payload_differs
	Detail  string `json:"detail,omitempty"`
}

// VerifyAt reconstructs state at version and compares it with the routes
// persisted in routes_snapshot. A nil mismatch slice means agreement.
func VerifyAt(ctx context.Context, s *store.Store, maxDepth, version uint64) ([]Mismatch, error) {
	res, err := FromStore(ctx, s, int(maxDepth), version)
	if err != nil {
		return nil, err
	}
	if res.Version != version {
		return nil, fmt.Errorf("replay: stream head %d != requested %d", res.Version, version)
	}
	stored, err := s.RoutesAt(ctx, version)
	if err != nil {
		return nil, err
	}

	have := map[string]netmodel.Route{}
	for _, r := range res.Snap.Routes() {
		have[r.ID] = r
	}
	want := map[string]netmodel.Route{}
	for _, r := range stored {
		want[r.ID] = r
	}

	var mm []Mismatch
	for id, r := range have {
		w, ok := want[id]
		switch {
		case !ok:
			mm = append(mm, Mismatch{Version: version, RouteID: id, Kind: "missing_in_store"})
		case !sameRoute(r, w):
			mm = append(mm, Mismatch{Version: version, RouteID: id, Kind: "payload_differs",
				Detail: fmt.Sprintf("reconstructed %s, stored %s", r.Prefix, w.Prefix)})
		}
	}
	for id := range want {
		if _, ok := have[id]; !ok {
			mm = append(mm, Mismatch{Version: version, RouteID: id, Kind: "extra_in_store"})
		}
	}
	sort.Slice(mm, func(i, j int) bool {
		if mm[i].RouteID != mm[j].RouteID {
			return mm[i].RouteID < mm[j].RouteID
		}
		return mm[i].Kind < mm[j].Kind
	})
	return mm, nil
}

func sameRoute(a, b netmodel.Route) bool {
	return a.ID == b.ID &&
		a.Prefix.String() == b.Prefix.String() &&
		a.AdminDist == b.AdminDist &&
		a.Metric == b.Metric &&
		a.NextHop.Interface == b.NextHop.Interface &&
		a.NextHop.Addr.String() == b.NextHop.Addr.String()
}
