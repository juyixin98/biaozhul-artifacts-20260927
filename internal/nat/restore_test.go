package nat_test

import (
	"context"
	"testing"
	"time"

	"natlab/internal/model"
	"natlab/internal/nat"
	"natlab/internal/storage"
)

// TestRestoreReapsPersistedExpired writes a mapping that is already past its
// deadline relative to the stored watermark, builds a fresh engine, calls
// Restore, and asserts the port is free rather than wrongly held alive.
func TestRestoreReapsPersistedExpired(t *testing.T) {
	cfg := testCfg()
	mem := storage.NewMemory()
	ctx := context.Background()

	t0 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	if err := mem.UpsertRun(ctx, storage.RunInfo{
		ID: "rr", CreatedAt: t0, Watermark: t0.Add(30 * time.Second),
	}); err != nil {
		t.Fatal(err)
	}
	// Active-looking mapping whose deadline (t0+10s) is before the watermark.
	if err := mem.PutMapping(ctx, storage.StoredMapping{
		RunID: "rr", ID: "UDP-30000", Proto: model.UDP,
		IntSrcIP: "10.0.0.2", IntSrcPort: 50001,
		ExtDstIP: "198.51.100.53", ExtDstPort: 53,
		ExternalIP: cfg.ExternalIP, ExternalPort: 30000, State: "UDP_OPEN",
		CreatedAt: t0, LastSeen: t0, ExpiresAt: t0.Add(10 * time.Second),
	}); err != nil {
		t.Fatal(err)
	}

	eng, err := nat.New("rr", cfg, mem)
	if err != nil {
		t.Fatal(err)
	}
	if err := eng.Restore(ctx, mem, "rr"); err != nil {
		t.Fatal(err)
	}
	if got := eng.Stats().MappingsExpired; got != 1 {
		t.Fatalf("restore reaps=%d, want 1", got)
	}
	if len(eng.Snapshots()) != 0 {
		t.Fatalf("expired mapping restored as active: %+v", eng.Snapshots())
	}
	// The reaped port is allocatable immediately.
	d := mustProc(t, eng, model.NewPacket(1, t0.Add(31*time.Second), model.Outbound,
		"10.0.0.9", 50099, "198.51.100.77", 123, model.UDP, model.TCPFlagBits{}))
	if d.Verdict != model.AcceptTranslate || d.AllocatedPort != 30000 {
		t.Fatalf("post-restore alloc verdict=%s port=%d", d.Verdict, d.AllocatedPort)
	}
	// The late packet for the dead mapping is reported as expired (tombstone).
	late := mustProc(t, eng, model.NewPacket(2, t0.Add(32*time.Second), model.Inbound,
		"198.51.100.53", 53, cfg.ExternalIP, 30000, model.UDP, model.TCPFlagBits{}))
	if late.Reason != model.ReasonRemoteMismatch && late.Reason != model.ReasonMappingExpired {
		t.Fatalf("late after restore reason=%s", late.Reason)
	}
}

// TestRestoreKeepsLiveMapping confirms a non-expired mapping is rebuilt and
// continues to translate symmetrically after restore.
func TestRestoreKeepsLiveMapping(t *testing.T) {
	cfg := testCfg()
	mem := storage.NewMemory()
	ctx := context.Background()
	t0 := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	if err := mem.UpsertRun(ctx, storage.RunInfo{ID: "rl", CreatedAt: t0,
		Watermark: t0.Add(2 * time.Second)}); err != nil {
		t.Fatal(err)
	}
	if err := mem.PutMapping(ctx, storage.StoredMapping{
		RunID: "rl", ID: "TCP-20000", Proto: model.TCP,
		IntSrcIP: "10.0.0.2", IntSrcPort: 40001,
		ExtDstIP: "203.0.113.10", ExtDstPort: 80,
		ExternalIP: cfg.ExternalIP, ExternalPort: 20000, State: "ESTABLISHED",
		CreatedAt: t0, LastSeen: t0.Add(2 * time.Second),
		ExpiresAt: t0.Add(2 * time.Second).Add(30 * time.Second),
	}); err != nil {
		t.Fatal(err)
	}
	eng, err := nat.New("rl", cfg, mem)
	if err != nil {
		t.Fatal(err)
	}
	if err := eng.Restore(ctx, mem, "rl"); err != nil {
		t.Fatal(err)
	}
	if len(eng.Snapshots()) != 1 {
		t.Fatalf("live mapping lost on restore: %+v", eng.Snapshots())
	}
	d := mustProc(t, eng, model.NewPacket(1, t0.Add(3*time.Second), model.Inbound,
		"203.0.113.10", 80, cfg.ExternalIP, 20000, model.TCP,
		model.TCPFlagBits{ACK: true}))
	if d.Verdict != model.AcceptForward || d.Post.DstIP != "10.0.0.2" || d.Post.DstPort != 40001 {
		t.Fatalf("post-restore forward: verdict=%s post=%+v", d.Verdict, d.Post)
	}
}
