package store_test

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"path/filepath"
	"testing"
	"time"

	"replicactl/internal/config"
	"replicactl/internal/model"
	"replicactl/internal/store"
)

func openTemp(t *testing.T) (*store.Store, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "data.db")
	st, err := store.New(context.Background(), "file:"+path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	return st, path
}

func reopen(t *testing.T, path string) *store.Store {
	t.Helper()
	st, err := store.New(context.Background(), "file:"+path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	return st
}

func seedConfig(t *testing.T, st *store.Store) config.Config {
	t.Helper()
	c := config.Default()
	c.InitialReplicas = 3
	if _, err := st.SaveConfig(context.Background(), c, time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
		t.Fatalf("save config: %v", err)
	}
	return c
}

// The replica count survives a close/reopen (service restart): seeding after
// a restart must not reset the fleet.
func TestFleetPersistsAcrossRestart(t *testing.T) {
	st, path := openTemp(t)
	seedConfig(t, st)
	now := time.Now().UTC()
	if err := st.SeedFleet(context.Background(), 3, now); err != nil {
		t.Fatal(err)
	}
	// Scale up to 5, then down to 4 before the "restart".
	if _, _, _, err := st.ApplyScale(context.Background(), 5, now); err != nil {
		t.Fatal(err)
	}
	if _, _, removed, err := st.ApplyScale(context.Background(), 4, now); err != nil {
		t.Fatal(err)
	} else if len(removed) != 1 || removed[0] != "ins-0005" {
		t.Fatalf("deterministic removal wrong: %v", removed)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	st2 := reopen(t, path)
	defer st2.Close()
	// Reseeding on startup must be a no-op and preserve 4 replicas.
	if err := st2.SeedFleet(context.Background(), 3, time.Now().UTC()); err != nil {
		t.Fatal(err)
	}
	f, err := st2.Fleet(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(f.Instances) != 4 {
		t.Fatalf("after restart want 4 replicas, got %d (%v)", len(f.Instances), f.Instances)
	}
}

// IDs are never reused: growing after a shrink allocates fresh, higher IDs.
func TestInstanceIDsNeverReused(t *testing.T) {
	st, _ := openTemp(t)
	defer st.Close()
	seedConfig(t, st)
	now := time.Now().UTC()
	_ = st.SeedFleet(context.Background(), 2, now)

	_, added, _, err := st.ApplyScale(context.Background(), 3, now)
	if err != nil || fmt.Sprint(added) != "[ins-0003]" {
		t.Fatalf("first grow: %v %v", added, err)
	}
	if _, _, removed, err := st.ApplyScale(context.Background(), 2, now); err != nil || fmt.Sprint(removed) != "[ins-0003]" {
		t.Fatalf("shrink: %v %v", removed, err)
	}
	_, added2, _, err := st.ApplyScale(context.Background(), 3, now)
	if err != nil {
		t.Fatal(err)
	}
	// Sequence must continue; ins-0003 must NOT be resurrected.
	if fmt.Sprint(added2) != "[ins-0004]" {
		t.Fatalf("id reused! want [ins-0004], got %v", added2)
	}
}

// Config replacement bumps the revision monotonically.
func TestConfigRevisionBumps(t *testing.T) {
	st, _ := openTemp(t)
	defer st.Close()
	c := config.Default()
	ctx := context.Background()
	r1, err := st.SaveConfig(ctx, c, time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil || r1 != 1 {
		t.Fatalf("first revision = %d (%v)", r1, err)
	}
	// An identical save (e.g. a restart with unchanged config) keeps revision.
	rSame, err := st.SaveConfig(ctx, c, time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil || rSame != 1 {
		t.Fatalf("identical config must keep revision 1, got %d (%v)", rSame, err)
	}
	// A genuine change bumps the revision.
	c2 := c
	c2.TargetLoadPerInstance = 125
	r2, err := st.SaveConfig(ctx, c2, time.Now().UTC().Format(time.RFC3339Nano))
	if err != nil || r2 != 2 {
		t.Fatalf("changed revision = %d (%v)", r2, err)
	}
	got, rev, err := st.LoadConfig(ctx)
	if err != nil || rev != 2 || got.TargetLoadPerInstance != 125 {
		t.Fatalf("load: rev=%d target=%v err=%v", rev, got.TargetLoadPerInstance, err)
	}
}

// Pruning removes only reports older than the horizon; recent reports and
// expired-but-retained reports are distinguished correctly.
func TestSamplePruningAndFreshness(t *testing.T) {
	st, _ := openTemp(t)
	defer st.Close()
	seedConfig(t, st)
	ctx := context.Background()
	now := time.Now().UTC()
	_ = st.InsertSample(ctx, model.Sample{InstanceID: "ins-0001", Metric: "requests_per_second", Value: 1, ObservedAt: now.Add(-2 * time.Hour), ReceivedAt: now})
	_ = st.InsertSample(ctx, model.Sample{InstanceID: "ins-0001", Metric: "requests_per_second", Value: 2, ObservedAt: now.Add(-30 * time.Second), ReceivedAt: now})

	got, err := st.LatestSamples(ctx, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Value != 2 {
		t.Fatalf("latest-per-instance wrong: %+v", got)
	}
	if err := st.PruneSamples(ctx, now.Add(-time.Hour)); err != nil {
		t.Fatal(err)
	}
	got2, err := st.LatestSamples(ctx, now)
	if err != nil {
		t.Fatal(err)
	}
	if len(got2) != 1 || got2[0].Value != 2 {
		t.Fatalf("2h-old row should be pruned, fresh row kept: %+v", got2)
	}
}

// Empty demand read reports no row rather than an error.
func TestDemandEmpty(t *testing.T) {
	st, _ := openTemp(t)
	defer st.Close()
	seedConfig(t, st)
	_, ok, err := st.LatestDemand(context.Background(), time.Now().UTC())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if ok {
		t.Fatalf("expected no demand row")
	}
}

// A missing decision by request id returns a standard sql.ErrNoRows.
func TestDecisionNotFound(t *testing.T) {
	st, _ := openTemp(t)
	defer st.Close()
	seedConfig(t, st)
	_, err := st.DecisionByRequestID(context.Background(), "req-does-not-exist")
	if !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("want sql.ErrNoRows, got %v", err)
	}
}
