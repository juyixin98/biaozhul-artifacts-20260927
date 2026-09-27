package reassembly_test

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"testing"

	"tcpreasm/internal/config"
	"tcpreasm/internal/diag"
	"tcpreasm/internal/fixture"
	"tcpreasm/internal/reassembly"
	"tcpreasm/internal/store"
	"tcpreasm/internal/tcpmodel"
)

// expectedFile mirrors a generated .expected.json envelope.
type expectedFile struct {
	Flow struct {
		Client string `json:"client"`
		Server string `json:"server"`
		ISNC2S uint32 `json:"isn_c2s"`
		ISNS2C uint32 `json:"isn_s2c"`
	} `json:"flow"`
	fixture.FixtureSpec
}

// harness is one engine run against a fresh in-memory store.
type harness struct {
	t       *testing.T
	cfg     config.Config
	st      *store.Store
	eng     *reassembly.Engine
	flowKey string
}

func newHarness(t *testing.T, policy config.OverlapPolicy) *harness {
	t.Helper()
	cfg := config.Default()
	cfg.Storage.DSN = ":memory:"
	if policy != "" {
		cfg.Reassembly.OverlapPolicy = policy
	}
	st, err := store.Open(context.Background(), ":memory:")
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = st.Close() })
	sink := diag.NewSink(io.Discard, st, false)
	eng := reassembly.NewEngine(reassembly.NewOptions(cfg, st, sink))
	return &harness{t: t, cfg: cfg, st: st, eng: eng}
}

// loadCapture reads a generated testdata capture relative to repo root.
func loadCapture(t *testing.T, name string) []tcpmodel.Packet {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", name+".jsonl"))
	if err != nil {
		t.Fatalf("read capture %s: %v", name, err)
	}
	pkts, err := tcpmodel.ParseCapture(raw)
	if err != nil {
		t.Fatalf("parse capture %s: %v", name, err)
	}
	return pkts
}

func loadExpected(t *testing.T, name string) expectedFile {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "testdata", name+".expected.json"))
	if err != nil {
		t.Fatalf("read expected %s: %v", name, err)
	}
	var ef expectedFile
	if err := json.Unmarshal(raw, &ef); err != nil {
		t.Fatalf("parse expected %s: %v", name, err)
	}
	return ef
}

// run ingests all packets of a fixture and returns per-packet results.
func (h *harness) run(t *testing.T, pkts []tcpmodel.Packet, requestID string) []reassembly.ProcessResult {
	t.Helper()
	out := make([]reassembly.ProcessResult, 0, len(pkts))
	ctx := context.Background()
	for _, p := range pkts {
		res, err := h.eng.Process(ctx, p, requestID)
		if err != nil {
			t.Fatalf("process %s: %v", p.RecordID, err)
		}
		out = append(out, res)
		if h.flowKey == "" && res.FlowKey != "" {
			h.flowKey = res.FlowKey
		}
	}
	return out
}

func (h *harness) runFixture(t *testing.T, name string) ([]reassembly.ProcessResult, expectedFile) {
	ef := loadExpected(t, name)
	pkts := loadCapture(t, name)
	return h.run(t, pkts, "test-"+name), ef
}

// streamBytes reads the delivered contiguous bytes for a direction.
func (h *harness) streamBytes(t *testing.T, gen int, dir string, start, end uint64) []byte {
	t.Helper()
	b, contig, total, err := h.st.StreamByteRange(context.Background(), h.flowKey, gen, dir, start, end, -1)
	if err != nil {
		t.Fatalf("stream bytes gen=%d dir=%s: %v", gen, dir, err)
	}
	_ = contig
	_ = total
	return b
}

// streamState returns the delivered prefix, its length and total delivered.
func (h *harness) streamState(t *testing.T, gen int, dir string, length uint64) ([]byte, bool, uint64) {
	t.Helper()
	b, contig, total, err := h.st.StreamByteRange(context.Background(), h.flowKey, gen, dir, 0, length, -1)
	if err != nil {
		t.Fatalf("stream state: %v", err)
	}
	return b, contig, total
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad golden hex: %v", err)
	}
	return b
}

// assertStreamEqual compares a delivered prefix to a golden byte slice.
func (h *harness) assertStreamEqual(t *testing.T, gen int, dir string, golden []byte) {
	t.Helper()
	got, contig, total := h.streamState(t, gen, dir, uint64(len(golden)))
	if !contig {
		t.Errorf("%s gen%d: expected contiguous %d bytes, got gap", dir, gen, len(golden))
	}
	if total != uint64(len(golden)) {
		t.Errorf("%s gen%d: total delivered %d, want %d", dir, gen, total, len(golden))
	}
	if len(got) != len(golden) {
		t.Fatalf("%s gen%d: got %d bytes want %d", dir, gen, len(got), len(golden))
	}
	for i := range got {
		if got[i] != golden[i] {
			t.Fatalf("%s gen%d byte %d = %#x want %#x", dir, gen, i, got[i], golden[i])
		}
	}
}

// assertDeliveredPrefix verifies exactly `n` contiguous bytes are delivered
// and that they equal golden[:n].
func (h *harness) assertDeliveredPrefix(t *testing.T, gen int, dir string, golden []byte, n uint64) {
	t.Helper()
	got, contig, total := h.streamState(t, gen, dir, n)
	if !contig {
		t.Errorf("%s gen%d: prefix not contiguous", dir, gen)
	}
	if total != n {
		t.Errorf("%s gen%d: total delivered %d want %d (evidence after a gap must not be emitted)",
			dir, gen, total, n)
	}
	if uint64(len(got)) != n {
		t.Fatalf("%s gen%d: got %d prefix bytes want %d", dir, gen, len(got), n)
	}
	for i := range got {
		if got[i] != golden[i] {
			t.Fatalf("%s gen%d prefix byte %d = %#x want %#x", dir, gen, i, got[i], golden[i])
		}
	}
}

// openGaps returns gap rows with status open.
func (h *harness) openGaps(t *testing.T, gen int) []store.GapRow {
	t.Helper()
	gaps, err := h.st.ListGaps(context.Background(), h.flowKey, gen, "", "open")
	if err != nil {
		t.Fatal(err)
	}
	return gaps
}

// allGaps returns every gap row (open and filled).
func (h *harness) allGaps(t *testing.T, gen int) []store.GapRow {
	t.Helper()
	gaps, err := h.st.ListGaps(context.Background(), h.flowKey, gen, "", "")
	if err != nil {
		t.Fatal(err)
	}
	return gaps
}

func (h *harness) conflicts(t *testing.T, gen int) []store.ConflictOut {
	t.Helper()
	cs, err := h.st.ListConflicts(context.Background(), h.flowKey, gen, "")
	if err != nil {
		t.Fatal(err)
	}
	return cs
}

func (h *harness) genCount(t *testing.T) int {
	conns, err := h.st.ListConnections(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for _, c := range conns {
		if c.FlowKey == h.flowKey {
			return len(c.Gens)
		}
	}
	t.Fatalf("flow %s not found", h.flowKey)
	return 0
}

// categories returns the multiset of diag categories for a request id.
func (h *harness) categories(t *testing.T, requestID string) map[diag.Category]int {
	t.Helper()
	recs, err := h.st.ListDiagnostics(context.Background(), requestID, "", -1, "", 5000)
	if err != nil {
		t.Fatal(err)
	}
	m := map[diag.Category]int{}
	for _, r := range recs {
		m[diag.Category(r.Category)]++
	}
	return m
}

func (h *harness) findCategory(t *testing.T, requestID string, cat diag.Category) []store.DiagOut {
	recs, err := h.st.ListDiagnostics(context.Background(), requestID, "", -1, string(cat), 100)
	if err != nil {
		t.Fatal(err)
	}
	return recs
}

// nilctx returns a background context for tests.
func nilctx() context.Context { return context.Background() }
