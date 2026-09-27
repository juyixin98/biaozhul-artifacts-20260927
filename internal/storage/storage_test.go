package storage_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"natlab/internal/model"
	"natlab/internal/storage"
)

func sampleMapping(runID string, port uint16, state string, deadline time.Time) storage.StoredMapping {
	return storage.StoredMapping{
		RunID: runID, ID: "TCP-" + itoa(port), Proto: model.TCP,
		IntSrcIP: "10.0.0.2", IntSrcPort: 40001,
		ExtDstIP: "203.0.113.10", ExtDstPort: 80,
		ExternalIP: "198.51.100.1", ExternalPort: port, State: state,
		CreatedAt: time.Now(), LastSeen: time.Now(), ExpiresAt: deadline,
	}
}

func itoa(p uint16) string {
	if p == 0 {
		return "0"
	}
	var b [5]byte
	i := len(b)
	for p > 0 {
		i--
		b[i] = byte('0' + p%10)
		p /= 10
	}
	return string(b[i:])
}

// backends runs each store behavior test against both implementations.
func backends(t *testing.T, fn func(*testing.T, storage.Store)) {
	t.Helper()
	t.Run("memory", func(t *testing.T) {
		fn(t, storage.NewMemory())
	})
	t.Run("sqlite", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "natlab.db")
		st, err := storage.OpenSQLite(path)
		if err != nil {
			t.Fatalf("open sqlite: %v", err)
		}
		defer st.Close()
		fn(t, st)
	})
}

func TestRunAndMappingRoundTrip(t *testing.T) {
	backends(t, func(t *testing.T, st storage.Store) {
		ctx := context.Background()
		if err := st.UpsertRun(ctx, storage.RunInfo{ID: "r1", CreatedAt: time.Now()}); err != nil {
			t.Fatal(err)
		}
		if _, err := st.GetRun(ctx, "r1"); err != nil {
			t.Fatalf("get run: %v", err)
		}
		if _, err := st.GetRun(ctx, "missing"); err == nil {
			t.Fatal("expected not-found for missing run")
		}
		m := sampleMapping("r1", 20000, "ESTABLISHED", time.Now().Add(time.Minute))
		if err := st.PutMapping(ctx, m); err != nil {
			t.Fatal(err)
		}
		got, err := st.ListMappings(ctx, "r1")
		if err != nil || len(got) != 1 || got[0].ExternalPort != 20000 ||
			got[0].State != "ESTABLISHED" {
			t.Fatalf("list = %+v err=%v", got, err)
		}
		m.State = "FIN_WAIT_2"
		if err := st.PutMapping(ctx, m); err != nil {
			t.Fatal(err)
		}
		got, _ = st.ListMappings(ctx, "r1")
		if len(got) != 1 || got[0].State != "FIN_WAIT_2" {
			t.Fatalf("update did not apply: %+v", got)
		}
		if err := st.DeleteMapping(ctx, "r1", m.ID, time.Now()); err != nil {
			t.Fatal(err)
		}
		got, _ = st.ListMappings(ctx, "r1")
		if len(got) != 0 {
			t.Fatalf("mapping not deleted: %+v", got)
		}
	})
}

func TestTombstoneWindow(t *testing.T) {
	backends(t, func(t *testing.T, st storage.Store) {
		ctx := context.Background()
		t0 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
		tb := storage.StoredTombstone{
			RunID: "r1", Proto: model.UDP, ExternalPort: 30000,
			RemoteIP: "198.51.100.53", RemotePort: 53,
			ClosedAt: t0, RetainUntil: t0.Add(5 * time.Minute),
		}
		if err := st.AddTombstone(ctx, tb); err != nil {
			t.Fatal(err)
		}
		got, err := st.FindTombstone(ctx, "r1", model.UDP, 30000,
			"198.51.100.53", 53, t0.Add(time.Minute))
		if err != nil || !got {
			t.Fatalf("tombstone within window = %v err=%v", got, err)
		}
		// Different remote endpoint is not the same late packet.
		got, _ = st.FindTombstone(ctx, "r1", model.UDP, 30000,
			"198.51.100.99", 99, t0.Add(time.Minute))
		if got {
			t.Fatal("tombstone must be remote-endpoint specific")
		}
		// Outside retain window.
		got, _ = st.FindTombstone(ctx, "r1", model.UDP, 30000,
			"198.51.100.53", 53, t0.Add(6*time.Minute))
		if got {
			t.Fatal("tombstone must expire with its retain window")
		}
	})
}

func TestDecisionLogOrdering(t *testing.T) {
	backends(t, func(t *testing.T, st storage.Store) {
		ctx := context.Background()
		for _, seq := range []int64{3, 1, 2} {
			if err := st.AppendDecision(ctx, model.Decision{
				RunID: "r1", Seq: seq, Verdict: model.AcceptTranslate,
			}); err != nil {
				t.Fatal(err)
			}
		}
		got, err := st.ListDecisions(ctx, "r1", 0, 0)
		if err != nil {
			t.Fatal(err)
		}
		if len(got) != 3 || got[0].Seq != 1 || got[2].Seq != 3 {
			t.Fatalf("decision order = %+v", got)
		}
		page, _ := st.ListDecisions(ctx, "r1", 2, 1)
		if len(page) != 1 || page[0].Seq != 2 {
			t.Fatalf("pagination = %+v", page)
		}
	})
}

func TestWatermarkMonotonicInStore(t *testing.T) {
	backends(t, func(t *testing.T, st storage.Store) {
		ctx := context.Background()
		_ = st.UpsertRun(ctx, storage.RunInfo{ID: "r1", CreatedAt: time.Now()})
		t1 := time.Date(2026, 1, 1, 0, 0, 10, 0, time.UTC)
		t0 := t1.Add(-5 * time.Second)
		if err := st.SetWatermark(ctx, "r1", t1); err != nil {
			t.Fatal(err)
		}
		if err := st.SetWatermark(ctx, "r1", t0); err != nil {
			t.Fatal(err)
		}
		info, err := st.GetRun(ctx, "r1")
		if err != nil {
			t.Fatal(err)
		}
		if !info.Watermark.Equal(t1) {
			t.Fatalf("store allowed watermark rollback: %v", info.Watermark)
		}
	})
}

func TestInjectedFault(t *testing.T) {
	f := &storage.Faulty{Inner: storage.NewMemory(), FailPutMapping: true}
	ctx := context.Background()
	if err := f.PutMapping(ctx, sampleMapping("r1", 1, "X", time.Now())); err != storage.ErrInjected {
		t.Fatalf("fault error = %v, want ErrInjected", err)
	}
	f.FailPutMapping = false
	if err := f.PutMapping(ctx, sampleMapping("r1", 1, "X", time.Now())); err != nil {
		t.Fatalf("fault disabled but got %v", err)
	}
}
