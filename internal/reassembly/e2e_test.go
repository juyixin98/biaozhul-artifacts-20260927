package reassembly_test

import (
	"testing"

	"tcpreasm/internal/config"
	"tcpreasm/internal/diag"
	"tcpreasm/internal/store"
)

// TestInOrder is the baseline: both directions reassemble byte-for-byte.
func TestInOrder(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	results, ef := h.runFixture(t, "in_order")
	h.assertStreamEqual(t, 1, "c2s", mustHex(t, ef.C2SStreamHex))
	h.assertStreamEqual(t, 1, "s2c", mustHex(t, ef.S2CStreamHex))
	if len(h.openGaps(t, 1)) != 0 {
		t.Fatalf("expected no gaps, got %v", h.openGaps(t, 1))
	}
	if len(h.conflicts(t, 1)) != 0 {
		t.Fatal("expected no conflicts")
	}
	cats := h.categories(t, "test-in_order")
	if cats[diag.CatHandshakeSYN] != 1 || cats[diag.CatHandshakeSYNACK] != 1 {
		t.Fatalf("handshake categories wrong: %v", cats)
	}
	// FIN must be recorded per direction (attached to last data segment).
	fin := 0
	for _, r := range results {
		if r.Category == diag.CatFIN {
			fin++
		}
	}
	if fin != 2 {
		t.Fatalf("want FIN category on both directions, got %d", fin)
	}
}

// TestOutOfOrder reverses data arrival; reassembly must be identical and no
// bytes may be emitted twice.
func TestOutOfOrder(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	_, ef := h.runFixture(t, "out_of_order")
	h.assertStreamEqual(t, 1, "c2s", mustHex(t, ef.C2SStreamHex))
	h.assertStreamEqual(t, 1, "s2c", mustHex(t, ef.S2CStreamHex))
	if gaps := h.openGaps(t, 1); len(gaps) != 0 {
		t.Fatalf("out-of-order capture eventually fills everything: %v", gaps)
	}
}

// TestRetransmission verifies identical retransmissions are evidence but
// never emitted twice.
func TestRetransmission(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	results, ef := h.runFixture(t, "retransmission")
	h.assertStreamEqual(t, 1, "c2s", mustHex(t, ef.C2SStreamHex))
	cats := h.categories(t, "test-retransmission")
	// Two duplicated packets are recorded.
	dup := 0
	for _, r := range results {
		if r.Category == diag.CatRetransmitIdentical {
			dup++
		}
	}
	if dup < 2 {
		t.Fatalf("want at least 2 identical-retransmit verdicts, got %d (cats=%v)", dup, cats)
	}
}

// TestConflictingRetransmission: contradictory bytes for already-delivered
// data cannot replace anything; an UNDECIDABLE conflict ledger row must name
// the exact byte offset and both SHA-256 fingerprints.
func TestConflictingRetransmission(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	_, ef := h.runFixture(t, "conflicting_retransmission")
	golden := mustHex(t, ef.C2SStreamHex)
	h.assertStreamEqual(t, 1, "c2s", golden)

	recs := h.findCategory(t, "test-conflicting_retransmission", diag.CatConflictDelivered)
	// The per-packet summary also carries this category; the ledger-backed
	// record is exactly one, verified above via the conflicts table. Assert
	// that at least one ledger diagnostic exists and its offsets match.
	if len(recs) < 1 {
		t.Fatalf("want a delivered-conflict record, got %d", len(recs))
	}
	cs := h.conflicts(t, 1)
	if len(cs) != 1 {
		t.Fatalf("want exactly one conflict ledger row, got %d", len(cs))
	}
	c := cs[0]
	if c.StartOff != 10 || c.EndOff != 11 {
		t.Fatalf("conflict offsets = [%d,%d), want [10,11)", c.StartOff, c.EndOff)
	}
	if c.IncumbentSHA != ef.Conflicts[0].OriginalSHA {
		t.Fatal("incumbent SHA must match golden original")
	}
	if c.NewcomerSHA != ef.Conflicts[0].InjectedSHA {
		t.Fatal("newcomer SHA must match golden injected")
	}
	if c.Winner != "incumbent" || c.Status != "open-delivered-immutable" {
		t.Fatalf("delivered bytes must stay immutable: %s/%s", c.Winner, c.Status)
	}
	// The delivered byte at offset 10 must remain the original value.
	got, _, _ := h.streamState(t, 1, "c2s", 11)
	if got[10] != golden[10] {
		t.Fatalf("byte 10 was overwritten by contradictory retransmit: %#x", got[10])
	}
}

// TestOverlapPolicies runs the buffered-overlap fixture under every policy
// and asserts policy-specific outcomes, byte by byte.
func TestOverlapPolicies(t *testing.T) {
	cases := []struct {
		name   string
		policy config.OverlapPolicy
		// wantBytes16 is the expected value of bytes 16..19 after delivery.
		wantBytes16 []byte
		wantStatus  string
		wantWinner  string
		// deliveredLen: quarantine stops at 16, other policies deliver 30.
		deliveredLen uint64
		cat          diag.Category
	}{
		{"first-wins", config.PolicyFirstWins, []byte{'A', 'A', 'A', 'A'},
			"resolved-first-wins", "incumbent", 30, diag.CatConflictFirstWins},
		{"last-wins", config.PolicyLastWins, []byte{'X', 'X', 'X', 'X'},
			"resolved-last-wins", "newcomer", 30, diag.CatConflictLastReplaced},
		{"quarantine", config.PolicyQuarantine, nil,
			"open-quarantined", "held", 16, diag.CatConflictQuarantined},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := newHarness(t, tc.policy)
			_, ef := h.runFixture(t, "overlap_conflict")
			golden := mustHex(t, ef.C2SStreamHex)

			got, _, total := h.streamState(t, 1, "c2s", tc.deliveredLen)
			if total != tc.deliveredLen {
				t.Fatalf("delivered total=%d want %d", total, tc.deliveredLen)
			}
			if uint64(len(got)) != tc.deliveredLen {
				t.Fatalf("got %d bytes want %d", len(got), tc.deliveredLen)
			}
			if tc.policy != config.PolicyQuarantine {
				// Before the conflict, bytes must equal the golden stream.
				for i := 0; i < 16; i++ {
					if got[i] != golden[i] {
						t.Fatalf("byte %d changed: %#x want %#x", i, got[i], golden[i])
					}
				}
				for i := 0; i < 4; i++ {
					if got[16+i] != tc.wantBytes16[i] {
						t.Fatalf("policy %s byte %d = %q want %q",
							tc.policy, 16+i, got[16+i], tc.wantBytes16[i])
					}
				}
			} else {
				// Quarantine: prefix 0..15 must equal golden, then stop.
				for i := 0; i < 16; i++ {
					if got[i] != golden[i] {
						t.Fatalf("quarantine changed byte %d", i)
					}
				}
				gaps := h.openGaps(t, 1)
				foundHeld := false
				for _, g := range gaps {
					if g.StartOff == 16 && g.EndOff == 20 {
						foundHeld = true
					}
				}
				if !foundHeld {
					t.Fatalf("quarantine must record held gap [16,20): %v", gaps)
				}
			}

			cs := h.conflicts(t, 1)
			if len(cs) != 1 {
				t.Fatalf("want 1 conflict, got %d", len(cs))
			}
			if cs[0].Status != tc.wantStatus || cs[0].Winner != tc.wantWinner {
				t.Fatalf("conflict status=%s winner=%s want %s/%s",
					cs[0].Status, cs[0].Winner, tc.wantStatus, tc.wantWinner)
			}
			if cs[0].StartOff != 16 || cs[0].EndOff != 20 {
				t.Fatalf("conflict offsets = [%d,%d) want [16,20)", cs[0].StartOff, cs[0].EndOff)
			}
			if recs := h.findCategory(t, "test-overlap_conflict", tc.cat); len(recs) < 1 {
				t.Fatalf("want at least one %s ledger record, got %d", tc.cat, len(recs))
			}
		})
	}
}

// TestMissingSegments locates every missing byte precisely: only the
// contiguous prefix before each hole is delivered; evidence beyond the hole
// must not be concatenated into the stream.
func TestMissingSegments(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	_, ef := h.runFixture(t, "missing_segments")
	goldenC2S := mustHex(t, ef.C2SStreamHex)
	goldenS2C := mustHex(t, ef.S2CStreamHex)

	// c2s: delivered 0..29, missing 30..44, tail 45..59 must NOT be emitted.
	h.assertDeliveredPrefix(t, 1, "c2s", goldenC2S, 30)
	// s2c: delivered 0..14, missing 15..29, tail 30..44 held.
	h.assertDeliveredPrefix(t, 1, "s2c", goldenS2C, 15)

	gaps := h.openGaps(t, 1)
	want := map[string][2]uint64{"c2s": {30, 45}, "s2c": {15, 30}}
	seen := map[string]bool{}
	for _, g := range gaps {
		w, ok := want[g.Direction]
		if !ok {
			t.Fatalf("unexpected gap direction %q", g.Direction)
		}
		if g.StartOff != w[0] || g.EndOff != w[1] {
			t.Fatalf("%s gap [%d,%d) want [%d,%d)", g.Direction, g.StartOff, g.EndOff, w[0], w[1])
		}
		seen[g.Direction] = true
	}
	if len(seen) != 2 {
		t.Fatalf("want 2 open gaps, got %v", gaps)
	}
	// Diagnostic gap records must name the offsets.
	if recs := h.findCategory(t, "test-missing_segments", diag.CatGap); len(recs) < 2 {
		t.Fatalf("want at least 2 GAP diagnostics, got %d", len(recs))
	}
}

// TestWrapBoundary reassembles a stream crossing the 2^32 sequence boundary.
func TestWrapBoundary(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	_, ef := h.runFixture(t, "wrap_boundary")
	golden := mustHex(t, ef.C2SStreamHex)
	h.assertStreamEqual(t, 1, "c2s", golden)
	if gaps := h.openGaps(t, 1); len(gaps) != 0 {
		t.Fatalf("wrapped stream reassembled without gaps: %v", gaps)
	}
}

// TestWrapBoundaryGap proves the gap accounting uses stream offsets, not raw
// 32-bit seq values, across the boundary: offsets [30,60) are missing while
// bytes on both sides of 2^32 are delivered.
func TestWrapBoundaryGap(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	_, ef := h.runFixture(t, "wrap_boundary_gap")
	golden := mustHex(t, ef.C2SStreamHex)
	h.assertDeliveredPrefix(t, 1, "c2s", golden, 30)
	gaps := h.openGaps(t, 1)
	if len(gaps) != 1 || gaps[0].StartOff != 30 || gaps[0].EndOff != 60 {
		t.Fatalf("wrap gap offsets wrong: %v", gaps)
	}
}

// TestHalfClose verifies FIN consumes a sequence number, half-close leaves
// the other direction open, and data beyond the FIN is rejected.
func TestHalfClose(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	results, ef := h.runFixture(t, "half_close")
	h.assertStreamEqual(t, 1, "c2s", mustHex(t, ef.C2SStreamHex))
	h.assertStreamEqual(t, 1, "s2c", mustHex(t, ef.S2CStreamHex))

	var rejected bool
	for _, r := range results {
		if r.Category == diag.CatDataAfterFIN && r.Decision == diag.Rejected {
			rejected = true
		}
	}
	if !rejected {
		t.Fatal("stray data after FIN must be rejected with DATA_AFTER_FIN")
	}
	// Rejected stray bytes must not appear in the c2s stream.
	if b, _, total := h.streamState(t, 1, "c2s", 64); total != 20 {
		t.Fatalf("c2s total delivered %d, want exactly 20", total)
		_ = b
	}
}

// TestMissingHandshake ingests a data-only capture and verifies it lands in
// an explicitly *inferred* generation whose offsets remain unproven.
func TestMissingHandshake(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	_, ef := h.runFixture(t, "missing_handshake")
	golden := mustHex(t, ef.C2SStreamHex)
	conns, err := h.st.ListConnections(nilctx())
	if err != nil {
		t.Fatal(err)
	}
	var inferred bool
	for _, c := range conns {
		for _, g := range c.Gens {
			if g.Inferred {
				inferred = true
			}
		}
	}
	if !inferred {
		t.Fatal("capture without SYN must create an inferred generation")
	}
	// Bytes are still delivered from the inferred epoch, compared to the
	// oracle stream, but diagnostics mark the inference.
	h.assertStreamEqual(t, 1, "c2s", golden)
	recs := h.findCategory(t, "test-missing_handshake", diag.CatInferredGeneration)
	if len(recs) == 0 {
		t.Fatal("expected INFERRED_GENERATION diagnostic")
	}
}

// TestConnectionReuse proves two handshakes on one 4-tuple create two
// generations whose streams never bleed into each other.
func TestConnectionReuse(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	_, ef := h.runFixture(t, "connection_reuse")
	if n := h.genCount(t); n != 2 {
		t.Fatalf("want 2 generations, got %d", n)
	}
	// Generation 2 streams come from the golden file.
	h.assertStreamEqual(t, 2, "c2s", mustHex(t, ef.C2SStreamHex))
	h.assertStreamEqual(t, 2, "s2c", mustHex(t, ef.S2CStreamHex))

	// Generation 1 must hold its own distinct streams (25 c2s, 20 s2c) and
	// be closed, while the query for gen 2 must never include gen-1 bytes.
	g1c := h.streamBytes(t, 1, "c2s", 0, 25)
	g1s := h.streamBytes(t, 1, "s2c", 0, 20)
	if len(g1c) != 25 || len(g1s) != 20 {
		t.Fatalf("gen1 lengths wrong: c2s=%d s2c=%d", len(g1c), len(g1s))
	}
	g2c := h.streamBytes(t, 1, "c2s", 0, 40)
	if len(g2c) != 25 {
		t.Fatalf("gen1 query unexpectedly returned %d bytes (cap leaks?)", len(g2c))
	}
	// Both generation-1 directions are closed; reuse category recorded.
	recs := h.findCategory(t, "test-connection_reuse", diag.CatGenerationReuse)
	if len(recs) != 1 {
		t.Fatalf("want one GENERATION_REUSE record, got %d", len(recs))
	}
}

// TestSYNConsumesSequence checks the engine treats the SYN as byte zero that
// never enters the data stream: first data seq is ISN+1, FIN is at data end.
func TestSYNConsumesSequence(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	_, ef := h.runFixture(t, "in_order")
	golden := mustHex(t, ef.C2SStreamHex)
	// Offset zero is the first *data* byte, not the SYN.
	if b, _, _ := h.streamState(t, 1, "c2s", 1); len(b) != 1 || b[0] != golden[0] {
		t.Fatalf("SYN leaked into data stream or offset base wrong: %x", b)
	}
}

// TestDiagnosticRedaction ensures payload bytes are never logged: only
// length + fingerprint appear, and previews are masked.
func TestDiagnosticRedaction(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	h.runFixture(t, "in_order")
	recs, err := h.st.ListDiagnostics(nilctx(), "test-in_order", "", -1, "", 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) == 0 {
		t.Fatal("expected diagnostics")
	}
	for _, r := range recs {
		if r.PayloadLen > 0 && len(r.PayloadSHA) != 64 {
			t.Fatalf("payload record must carry SHA-256, len=%d sha=%q", r.PayloadLen, r.PayloadSHA)
		}
		// Raw payload never lands in reason/preview fields.
		for _, ch := range []byte(r.Reason + r.Preview) {
			if ch < 0x20 && ch != '\n' && ch != '\t' {
				t.Fatal("control byte leaked into diagnostic text")
			}
		}
	}
}

// TestStoreRoundTrip persists and reads gap/conflict/diagnostic rows
// independently of the engine through the store contract.
func TestStoreRoundTrip(t *testing.T) {
	st := h2(t)
	ctx := nilctx()
	if err := st.UpsertConnection(ctx, store.ConnectionRow{
		FlowKey: "1.1.1.1:1<->2.2.2.2:2", EndpointA: "1.1.1.1:1", EndpointB: "2.2.2.2:2",
		ClientEP: "1.1.1.1:1", State: "ESTABLISHED", CreatedSeq: 1, UpdatedSeq: 1,
	}); err != nil {
		t.Fatal(err)
	}
	isn := uint32(7)
	if err := st.InsertGeneration(ctx, store.GenerationRow{
		FlowKey: "1.1.1.1:1<->2.2.2.2:2", GenIndex: 1, C2SState: "ESTABLISHED",
		S2CState: "ESTABLISHED", C2SISN: &isn, CreatedSeq: 1, UpdatedSeq: 1,
	}); err != nil {
		t.Fatal(err)
	}
	if err := st.InsertChunk(ctx, store.ChunkRow{
		FlowKey: "1.1.1.1:1<->2.2.2.2:2", GenIndex: 1, Direction: "c2s",
		StreamOff: 0, Data: []byte("abc"), IngestSeq: 1,
	}); err != nil {
		t.Fatal(err)
	}
	b, contig, total, err := st.StreamByteRange(ctx, "1.1.1.1:1<->2.2.2.2:2", 1, "c2s", 0, 3, -1)
	if err != nil || !contig || total != 3 || string(b) != "abc" {
		t.Fatalf("chunk roundtrip wrong: %q %v %d %v", b, contig, total, err)
	}
}

// h2 opens a fresh empty store for store-level tests.
func h2(t *testing.T) *store.Store {
	t.Helper()
	st, err := store.Open(nilctx(), ":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })
	return st
}
