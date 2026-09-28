package store_test

import (
	"crypto/sha256"
	"encoding/hex"
	"net/netip"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"ipreasm/internal/reasm"
	"ipreasm/internal/store"
)

var t0 = time.Date(2026, 9, 27, 10, 0, 0, 0, time.UTC)

func key(src, dst string, proto byte, id uint16) reasm.Key {
	return reasm.Key{Src: netip.MustParseAddr(src), Dst: netip.MustParseAddr(dst), Proto: proto, ID: id}
}

func cfg() reasm.Config {
	return reasm.Config{Timeout: 30 * time.Second, MaxDatagramSize: 65535, MaxDatagrams: 100, MaxBufferedBytes: 1 << 20}
}

func payload(seed uint32, n int) []byte {
	b := make([]byte, n)
	for i := range b {
		x := seed ^ uint32(i)*2654435761
		x ^= x << 13
		x ^= x >> 17
		x ^= x << 5
		b[i] = byte(x) ^ byte(i)
	}
	return b
}

// TestCompletionReclaimsRows: rows exist while buffered, are deleted on
// completion, and the datagram row carries the exact payload.
func TestCompletionReclaimsRows(t *testing.T) {
	const runID = "store-complete-001"
	st, err := store.Open(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	r := reasm.New(cfg(), runID, st)
	k := key("10.20.0.1", "10.20.0.2", 17, 0x0101)
	p := payload(0x51, 128)

	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: p[:64]}, t0)
	if n, _ := st.FragCount(runID); n != 1 {
		t.Fatalf("after 1 frag rows=%d want 1", n)
	}
	t.Logf("input=%s go=%s buffered rows=1 while incomplete (state visible in sqlite)", runID, runtime.Version())

	res := r.Insert(reasm.Fragment{Key: k, OffsetBytes: 64, More: false, Data: p[64:]}, t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeCompleted {
		t.Fatalf("completion: %s/%s", res.Outcome, res.Reason)
	}
	if n, _ := st.FragCount(runID); n != 0 {
		t.Fatalf("rows after completion=%d want 0 (reclaimed)", n)
	}
	dgs, err := st.Datagrams(runID)
	if err != nil || len(dgs) != 1 {
		t.Fatalf("datagrams=%v err=%v", len(dgs), err)
	}
	sum := sha256.Sum256(p)
	if dgs[0].SHA256 != hex.EncodeToString(sum[:]) || string(dgs[0].Payload) != string(p) {
		t.Fatalf("persisted datagram bytes/digest mismatch")
	}
	if n, _ := st.EventCount(runID, "completed"); n != 1 {
		t.Fatalf("completed events=%d want 1", n)
	}
	t.Logf("input=%s rows reclaimed to 0; datagram persisted sha256=%s", runID, dgs[0].SHA256[:12])
}

// TestRejectionReclaimsRows: an overlap rejection deletes buffered rows
// and writes a rejected audit event.
func TestRejectionReclaimsRows(t *testing.T) {
	const runID = "store-reject-001"
	st, err := store.Open(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	r := reasm.New(cfg(), runID, st)
	k := key("10.21.0.1", "10.21.0.2", 17, 0x0201)

	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: payload(0x61, 64)}, t0)
	res := r.Insert(reasm.Fragment{Key: k, OffsetBytes: 32, More: true, Data: payload(0x62, 64)}, t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonOverlap {
		t.Fatalf("classify=%s/%s", res.Outcome, res.Reason)
	}
	if n, _ := st.FragCount(runID); n != 0 {
		t.Fatalf("rows after rejection=%d want 0", n)
	}
	if n, _ := st.EventCount(runID, "rejected"); n != 1 {
		t.Fatalf("rejected events=%d want 1", n)
	}
	t.Logf("input=%s overlap rejected; sqlite rows=0; audit rejected=1", runID)
}

// TestExpiryReclaimsRows: a partial group expiring frees its rows and
// leaves an expired audit event; a later sweep of an empty table is a
// no-op.
func TestExpiryReclaimsRows(t *testing.T) {
	const runID = "store-expire-001"
	st, err := store.Open(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	r := reasm.New(cfg(), runID, st)
	k := key("10.22.0.1", "10.22.0.2", 17, 0x0301)

	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: payload(0x71, 64)}, t0)
	if n, _ := st.FragCount(runID); n != 1 {
		t.Fatalf("rows=%d want 1", n)
	}
	expired := r.Sweep(t0.Add(31 * time.Second))
	if len(expired) != 1 {
		t.Fatalf("expired=%v", expired)
	}
	if n, _ := st.FragCount(runID); n != 0 {
		t.Fatalf("rows after expiry=%d want 0", n)
	}
	if n, _ := st.EventCount(runID, "expired"); n != 1 {
		t.Fatalf("expired events=%d want 1", n)
	}
	if got := r.Sweep(t0.Add(time.Hour)); len(got) != 0 {
		t.Fatalf("second sweep should be a no-op, got %v", got)
	}
	t.Logf("input=%s expired after 30s idle; rows=0; events expired=1", runID)
}

// TestDuplicateAuditOnly: exact duplicates produce an audit event but no
// additional fragment rows.
func TestDuplicateAuditOnly(t *testing.T) {
	const runID = "store-dup-001"
	st, err := store.Open(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	r := reasm.New(cfg(), runID, st)
	k := key("10.23.0.1", "10.23.0.2", 17, 0x0401)
	f := reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: payload(0x81, 64)}
	r.Insert(f, t0)
	got := r.Insert(f, t0.Add(time.Millisecond))
	if got.Outcome != reasm.OutcomeDuplicate {
		t.Fatalf("dup: %s", got.Outcome)
	}
	if n, _ := st.FragCount(runID); n != 1 {
		t.Fatalf("rows=%d want 1 (duplicate not stored twice)", n)
	}
	if n, _ := st.EventCount(runID, "duplicate"); n != 1 {
		t.Fatalf("duplicate events=%d want 1", n)
	}
	t.Logf("input=%s duplicate identified: rows stay 1, duplicate event=1", runID)
}

// TestEventsCorrelatedByRunID: two runs in the same database stay
// separated by run_id.
func TestEventsCorrelatedByRunID(t *testing.T) {
	st, err := store.Open(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	for _, runID := range []string{"run-A", "run-B"} {
		r := reasm.New(cfg(), runID, st)
		k := key("10.24.0.1", "10.24.0.2", 17, 0x0501)
		p := payload(0x91, 128)
		r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: p[:64]}, t0)
		r.Insert(reasm.Fragment{Key: k, OffsetBytes: 64, More: false, Data: p[64:]}, t0.Add(time.Millisecond))
	}
	a, _ := st.Datagrams("run-A")
	b, _ := st.Datagrams("run-B")
	if len(a) != 1 || len(b) != 1 {
		t.Fatalf("run-scoped datagrams: A=%d B=%d", len(a), len(b))
	}
	t.Logf("run-A and run-B isolated in one sqlite file (1 datagram each)")
}
