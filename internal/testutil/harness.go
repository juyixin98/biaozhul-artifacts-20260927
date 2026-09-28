// Package testutil contains helpers shared by integration tests: they build a
// real store + coordinator + synthetic adapter wired exactly like production,
// but on a temporary database with a controllable clock.
//
// IMPORTANT: the scenario definitions here are authored independently of the
// coordinator's decision logic — they are inputs (group shapes, readiness
// facts, failure facts, request sequences), not expected outputs. Expected
// outputs are asserted concretely in each test.
package testutil

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/local/evictioncoordinator/internal/coordinator"
	"github.com/local/evictioncoordinator/internal/domain"
	"github.com/local/evictioncoordinator/internal/fixture"
	"github.com/local/evictioncoordinator/internal/store"
)

// Harness bundles wired components.
type Harness struct {
	Store   *store.Store
	Coord   *coordinator.Coordinator
	Adapter *fixture.Adapter
	Now     *FakeClock
	Ctx     context.Context
}

// FakeClock is a manually controlled time source.
type FakeClock struct{ T time.Time }

// NewFakeClock starts at a fixed instant.
func NewFakeClock() *FakeClock {
	t, _ := time.Parse(time.RFC3339, "2026-01-01T00:00:00Z")
	return &FakeClock{T: t}
}

func (f *FakeClock) Now() time.Time { return f.T }

// Advance moves the clock.
func (f *FakeClock) Advance(d time.Duration) { f.T = f.T.Add(d) }

// NewHarness creates a file-backed SQLite DB under t.TempDir so WAL mode and
// real locking are exercised (not a substitute in-memory connection).
func NewHarness(t *testing.T, approvalTTL time.Duration) *Harness {
	t.Helper()
	ctx := context.Background()
	dbPath := filepath.Join(t.TempDir(), "test.db")
	st, err := store.Open(ctx, dbPath)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { st.Close() })

	clk := NewFakeClock()
	ad := fixture.NewAdapter(st, clk.Now)
	coord := coordinator.New(st, coordinator.Config{ApprovalTTL: approvalTTL, Clock: clk.Now})
	return &Harness{Store: st, Coord: coord, Adapter: ad, Now: clk, Ctx: ctx}
}

// MemberSpec is declarative input for a test group.
type MemberSpec struct {
	ID         string
	Ready      bool
	Failed     bool
	FailReason string
}

// SetupGroup registers a group with n members and current-epoch readiness
// observations. Returns the stored group (with its real epoch).
func (h *Harness) SetupGroup(t *testing.T, g domain.Group, members []MemberSpec) domain.Group {
	t.Helper()
	g.SelectorLabels = map[string]string{"app": "web"}
	if len(g.SelectorLabels) == 0 {
		g.SelectorLabels = map[string]string{"app": "web"}
	}
	saved, err := h.Store.UpsertGroup(h.Ctx, g, false)
	if err != nil {
		t.Fatalf("upsert group: %v", err)
	}
	for _, m := range members {
		inst := domain.Instance{
			ID: m.ID, Namespace: saved.Namespace, Group: saved.Name,
			Labels: map[string]string{"app": "web"},
		}
		if err := h.Adapter.EmitObservation(h.Ctx, inst, m.Ready, saved.SelectorEpoch); err != nil {
			t.Fatalf("emit observation %s: %v", m.ID, err)
		}
		if m.Failed {
			reason := m.FailReason
			if reason == "" {
				reason = "node lost"
			}
			if err := h.Adapter.EmitFailure(h.Ctx, m.ID, reason, saved.SelectorEpoch); err != nil {
				t.Fatalf("emit failure %s: %v", m.ID, err)
			}
		}
	}
	return saved
}

// MustEvict requests an eviction and fails the test on transport error
// (a rejected budget decision is NOT an error and is returned normally).
func (h *Harness) MustEvict(t *testing.T, ns, group, instance string) domain.Decision {
	t.Helper()
	dec, err := h.Coord.Evict(h.Ctx, coordinator.Request{
		Namespace: ns, Group: group, InstanceID: instance,
	})
	if err != nil {
		t.Fatalf("Evict(%s) returned error: %v", instance, err)
	}
	return dec
}

// LivePendingCount counts pending approvals via a group snapshot (used for
// independent verification through the store, not the coordinator).
func (h *Harness) LivePendingCount(t *testing.T, ns, group string) int {
	t.Helper()
	rows, err := h.Store.DB().QueryContext(h.Ctx,
		`SELECT COUNT(*) FROM approvals WHERE namespace=? AND group_name=? AND state='pending'`,
		ns, group)
	if err != nil {
		t.Fatalf("count pending: %v", err)
	}
	defer rows.Close()
	var n int
	if rows.Next() {
		_ = rows.Scan(&n)
	}
	return n
}
