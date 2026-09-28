package reasm_test

import (
	"fmt"
	"math/rand"
	"net/netip"
	"runtime"
	"testing"
	"time"

	"ipreasm/internal/reasm"
)

// Test environment identity: every test uses a run id so SQLite rows and
// logs can be correlated to the exact input scenario.
var t0 = time.Date(2026, 9, 27, 10, 0, 0, 0, time.UTC)

func mustKey(src, dst string, proto byte, id uint16) reasm.Key {
	return reasm.Key{
		Src:   netip.MustParseAddr(src),
		Dst:   netip.MustParseAddr(dst),
		Proto: proto,
		ID:    id,
	}
}

func testCfg() reasm.Config {
	return reasm.Config{
		Timeout:          30 * time.Second,
		MaxDatagramSize:  65535,
		MaxDatagrams:     1024,
		MaxBufferedBytes: 4 << 20,
	}
}

// mkPayload is test-local deterministic data generation; the reassembler
// never sees the full payload before assembly, so it cannot derive the
// expected output from it.
func mkPayload(seed uint32, n int) []byte {
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

// mkFrags fragments payload at the given non-final chunk sizes (each a
// multiple of 8). If the chunks end exactly at payload length, the last
// chunk itself is the final fragment; otherwise the remainder becomes a
// final fragment.
func mkFrags(k reasm.Key, payload []byte, sizes []int) []reasm.Fragment {
	var out []reasm.Fragment
	off := 0
	for _, sz := range sizes {
		if off+sz > len(payload) {
			panic("mkFrags: chunk past payload end")
		}
		out = append(out, reasm.Fragment{Key: k, OffsetBytes: off, More: true, Data: append([]byte(nil), payload[off:off+sz]...)})
		off += sz
	}
	if off < len(payload) {
		out = append(out, reasm.Fragment{Key: k, OffsetBytes: off, More: false, Data: append([]byte(nil), payload[off:]...)})
	} else {
		out[len(out)-1].More = false
	}
	return out
}

func insertAll(t *testing.T, r *reasm.Reassembler, frags []reasm.Fragment, start time.Time) *reasm.Datagram {
	t.Helper()
	var done *reasm.Datagram
	for i, f := range frags {
		res := r.Insert(f, start.Add(time.Duration(i)*time.Millisecond))
		if res.Outcome == reasm.OutcomeRejected {
			t.Fatalf("unexpected rejection at step %d: %s", i, res.Reason)
		}
		if res.Outcome == reasm.OutcomeCompleted {
			if done != nil {
				t.Fatalf("datagram completed twice")
			}
			done = res.Datagram
		}
	}
	return done
}

func permutations(n int) [][]int {
	var res [][]int
	a := make([]int, n)
	for i := range a {
		a[i] = i
	}
	var rec func(int)
	rec = func(k int) {
		if k == 1 {
			res = append(res, append([]int(nil), a...))
			return
		}
		rec(k - 1)
		for i := 0; i < k-1; i++ {
			if k%2 == 0 {
				a[i], a[k-1] = a[k-1], a[i]
			} else {
				a[0], a[k-1] = a[k-1], a[0]
			}
			rec(k - 1)
		}
	}
	rec(n)
	return res
}

// TestPermutationsAllOrders: every arrival order of 6 fragments must
// assemble byte-for-byte identical output, and completion must happen
// exactly on the fragment that closes the coverage.
func TestPermutationsAllOrders(t *testing.T) {
	payload := mkPayload(0xA001, 96) // six 16-byte fragments
	k := mustKey("10.1.0.1", "10.1.0.2", 17, 0x0001)
	frags := mkFrags(k, payload, []int{16, 16, 16, 16, 16, 16})

	for pi, order := range permutations(6) {
		runID := fmt.Sprintf("unit-perm-%03d", pi)
		t.Run(runID, func(t *testing.T) {
			t.Logf("input=%s go=%s order=%v start_offset=0 chunk=16 chunks=6 payload_len=%d; decision rule: contiguous [0,96) => completed",
				runID, runtime.Version(), order, len(payload))
			r := reasm.New(testCfg(), runID, nil)
			shuffled := make([]reasm.Fragment, 6)
			for i, idx := range order {
				shuffled[i] = frags[idx]
			}
			done := insertAll(t, r, shuffled, t0)
			if done == nil {
				t.Fatalf("no datagram emitted for order %v", order)
			}
			if string(done.Data) != string(payload) {
				t.Fatalf("bytes mismatch: got %x..., want %x...", done.Data[:16], payload[:16])
			}
			if done.FragCount != 6 {
				t.Fatalf("frag count=%d want 6", done.FragCount)
			}
			if st := r.Stats(); st.Completed != 1 || st.ActiveGroups != 0 || st.BufferedBytes != 0 {
				t.Fatalf("stats after completion: %+v", st)
			}
		})
	}
}

// TestLastFragmentFirst: knowing the total early must not emit anything
// until coverage is contiguous.
func TestLastFragmentFirst(t *testing.T) {
	const runID = "unit-last-first-001"
	payload := mkPayload(0xA002, 140) // 64+64+12
	k := mustKey("10.2.0.1", "10.2.0.2", 1, 0x0002)
	frags := mkFrags(k, payload, []int{64, 64})
	r := reasm.New(testCfg(), runID, nil)

	res := r.Insert(frags[2], t0) // final fragment at [128,140)
	if res.Outcome != reasm.OutcomeStored {
		t.Fatalf("last-first insert: %s %s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s step=1 last-fragment inserted first; expected=stored (gap), got=%s", runID, res.Outcome)

	res = r.Insert(frags[0], t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeStored {
		t.Fatalf("middle step emitted early: %s", res.Outcome)
	}
	res = r.Insert(frags[1], t0.Add(2*time.Millisecond))
	if res.Outcome != reasm.OutcomeCompleted || res.Datagram == nil {
		t.Fatalf("final step: %s %s", res.Outcome, res.Reason)
	}
	if string(res.Datagram.Data) != string(payload) {
		t.Fatalf("payload mismatch")
	}
}

// TestMissingFragmentsNeverEmits: gap + known total must stay buffered
// until timeout, never output.
func TestMissingFragmentsNeverEmits(t *testing.T) {
	const runID = "unit-missing-001"
	k := mustKey("10.3.0.1", "10.3.0.2", 17, 0x0003)
	r := reasm.New(testCfg(), runID, nil)
	first := reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: mkPayload(0xB1, 64)}
	last := reasm.Fragment{Key: k, OffsetBytes: 128, More: false, Data: mkPayload(0xB2, 32)}
	if res := r.Insert(first, t0); res.Outcome != reasm.OutcomeStored {
		t.Fatalf("first: %s", res.Outcome)
	}
	if res := r.Insert(last, t0.Add(time.Millisecond)); res.Outcome != reasm.OutcomeStored {
		t.Fatalf("gap must not complete, got %s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s known total=160 but [64,128) missing; correctly stayed buffered", runID)
	if st := r.Stats(); st.Completed != 0 || st.ActiveGroups != 1 {
		t.Fatalf("stats: %+v", st)
	}
	expired := r.Sweep(t0.Add(31 * time.Second))
	if len(expired) != 1 || expired[0] != k {
		t.Fatalf("sweep expired=%v want [%s]", expired, k)
	}
	if st := r.Stats(); st.Expired != 1 || st.ActiveGroups != 0 || st.BufferedBytes != 0 {
		t.Fatalf("post-sweep stats: %+v", st)
	}
}

// TestExactDuplicateSeparatelyIdentified: byte-identical retransmission
// is counted as duplicate, not as overlap, and the group still completes
// with the original byte count.
func TestExactDuplicateSeparatelyIdentified(t *testing.T) {
	const runID = "unit-dup-001"
	payload := mkPayload(0xA004, 128)
	k := mustKey("10.4.0.1", "10.4.0.2", 17, 0x0004)
	frags := mkFrags(k, payload, []int{64})
	r := reasm.New(testCfg(), runID, nil)

	if res := r.Insert(frags[0], t0); res.Outcome != reasm.OutcomeStored {
		t.Fatalf("f0: %s", res.Outcome)
	}
	res := r.Insert(frags[0], t0.Add(time.Millisecond)) // identical
	if res.Outcome != reasm.OutcomeDuplicate {
		t.Fatalf("identical retransmission classified=%s/%s want duplicate", res.Outcome, res.Reason)
	}
	t.Logf("input=%s duplicate [0,64) recognised separately: %s", runID, res.Outcome)
	if res := r.Insert(frags[1], t0.Add(2*time.Millisecond)); res.Outcome != reasm.OutcomeCompleted {
		t.Fatalf("complete: %s/%s", res.Outcome, res.Reason)
	}
	if st := r.Stats(); st.Duplicates != 1 || st.Completed != 1 {
		t.Fatalf("stats: %+v", st)
	}
}

// TestDuplicateDifferentMFFlag: same bytes but MF disagreement is a
// conflicting-last rejection, not a benign duplicate.
func TestDuplicateDifferentMFFlag(t *testing.T) {
	const runID = "unit-dup-mf-001"
	k := mustKey("10.5.0.1", "10.5.0.2", 17, 0x0005)
	r := reasm.New(testCfg(), runID, nil)
	f := reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: mkPayload(0xC1, 64)}
	r.Insert(f, t0)
	f.More = false // now claims total length 64 with identical bytes
	res := r.Insert(f, t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonConflictingLast {
		t.Fatalf("MF flip: %s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s same bytes flipped MF -> reason=%s (judged by last-fragment consistency)", runID, res.Reason)
}

// TestOverlapRejectsWholeGroup: any non-identical overlap poisons the
// whole group; late fragments are dropped as poisoned-group.
func TestOverlapRejectsWholeGroup(t *testing.T) {
	const runID = "unit-overlap-001"
	k := mustKey("10.6.0.1", "10.6.0.2", 17, 0x0006)
	r := reasm.New(testCfg(), runID, nil)
	f0 := reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: mkPayload(0xD1, 64)}
	f1 := reasm.Fragment{Key: k, OffsetBytes: 56, More: true, Data: mkPayload(0xD2, 64)} // [56,120) overlaps [56,64)
	r.Insert(f0, t0)
	res := r.Insert(f1, t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonOverlap {
		t.Fatalf("overlap classified=%s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s intersect [0,64) vs [56,120) with different bytes -> whole-group reject reason=%s", runID, res.Reason)

	late := reasm.Fragment{Key: k, OffsetBytes: 120, More: false, Data: mkPayload(0xD3, 8)}
	res = r.Insert(late, t0.Add(2*time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonPoisoned {
		t.Fatalf("late fragment after poison: %s/%s", res.Outcome, res.Reason)
	}
	if st := r.Stats(); st.Completed != 0 || st.Rejected != 2 || st.BufferedBytes != 0 {
		t.Fatalf("stats: %+v", st)
	}
	// a full, correct group arriving later under the same key must stay
	// rejected until the tombstone times out
	good := mkFrags(k, mkPayload(0xD4, 128), []int{64})
	for i, f := range good {
		if res := r.Insert(f, t0.Add(3*time.Millisecond+time.Duration(i))); res.Outcome != reasm.OutcomeRejected {
			t.Fatalf("reuse of poisoned key accepted: %s", res.Outcome)
		}
	}
	// after timeout the key is reusable
	expired := r.Sweep(t0.Add(time.Minute))
	if len(expired) != 1 {
		t.Fatalf("tombstone purge expired=%v", expired)
	}
	for i, f := range good {
		res := r.Insert(f, t0.Add(time.Minute+time.Duration(i+1)))
		want := reasm.OutcomeStored
		if i == len(good)-1 {
			want = reasm.OutcomeCompleted
		}
		if res.Outcome != want {
			t.Fatalf("post-timeout reuse step %d: %s/%s", i, res.Outcome, res.Reason)
		}
	}
}

// TestIdenticalBytesDifferentRange: same content prefix but a different
// length is still an overlap (real retransmits must be range-identical).
func TestIdenticalBytesDifferentRange(t *testing.T) {
	const runID = "unit-overlap-range-001"
	k := mustKey("10.7.0.1", "10.7.0.2", 17, 0x0007)
	r := reasm.New(testCfg(), runID, nil)
	b := mkPayload(0xE1, 128)
	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: b[:64]}, t0)
	res := r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: b[:72]}, t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonOverlap {
		t.Fatalf("range-mismatched retransmit: %s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s same prefix but len 64 vs 72 -> overlap (exact dup requires same range AND bytes)", runID)
}

// TestConflictingLastFragment: two different totals reject the group.
// A gap prevents the first announcement from completing immediately.
func TestConflictingLastFragment(t *testing.T) {
	const runID = "unit-conflict-last-001"
	k := mustKey("10.8.0.1", "10.8.0.2", 6, 0x0008)
	r := reasm.New(testCfg(), runID, nil)
	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: mkPayload(0xF1, 64)}, t0)
	// final frag at [80,128), gap [64,80): total 128 but cannot complete
	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 80, More: false, Data: mkPayload(0xF2, 48)}, t0.Add(time.Millisecond))
	// conflicting final frag at the same offset announcing total 144
	res := r.Insert(reasm.Fragment{Key: k, OffsetBytes: 80, More: false, Data: mkPayload(0xF3, 64)}, t0.Add(2*time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonConflictingLast {
		t.Fatalf("conflicting totals: %s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s totals 128 vs 144 -> reason=%s", runID, res.Reason)
	if r.Stats().Completed != 0 {
		t.Fatalf("conflicting group must never complete")
	}
}

// TestMoreFragmentBeyondKnownTotal: final fragment announces total 128;
// a later MF fragment extending beyond it conflicts.
func TestMoreFragmentBeyondKnownTotal(t *testing.T) {
	const runID = "unit-beyond-total-001"
	k := mustKey("10.9.0.1", "10.9.0.2", 17, 0x0009)
	r := reasm.New(testCfg(), runID, nil)
	last := reasm.Fragment{Key: k, OffsetBytes: 64, More: false, Data: mkPayload(0x1011, 64)}
	r.Insert(last, t0) // total known early: 128
	// MF fragment [0,136) extends beyond the announced total
	res := r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: mkPayload(0x1012, 136)}, t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonConflictingLast {
		t.Fatalf("fragment beyond total: %s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s MF fragment [0,136) beyond known total 128 -> reason=%s", runID, res.Reason)
}

// TestBadFragmentLengths: non-final payloads that are not a multiple of
// 8, and empty non-final fragments, are rejected with bad-length.
func TestBadFragmentLengths(t *testing.T) {
	cases := []struct {
		name string
		off  int
		len  int
		more bool
	}{
		{"non-final-len-60", 0, 60, true},
		{"non-final-len-84-at-120", 15, 84, true},
		{"empty-non-final", 0, 0, true},
	}
	for ci, tc := range cases {
		runID := fmt.Sprintf("unit-badlen-%03d", ci)
		t.Run(tc.name, func(t *testing.T) {
			k := mustKey("11.0.0.1", "11.0.0.2", 17, uint16(0x2000+ci))
			r := reasm.New(testCfg(), runID, nil)
			res := r.Insert(reasm.Fragment{Key: k, OffsetBytes: tc.off * 8, More: tc.more, Data: mkPayload(uint32(ci+1), tc.len)}, t0)
			if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonBadLength {
				t.Fatalf("input=%s off_units=%d len=%d more=%v: classified=%s/%s want rejected/bad-length",
					runID, tc.off, tc.len, tc.more, res.Outcome, res.Reason)
			}
			t.Logf("input=%s judged by rule: MF=1 requires len%%8==0 and len>0; got reason=%s", runID, res.Reason)
		})
	}
}

// TestFinalFragmentNeedNotBeAligned: the final fragment may have an
// arbitrary length (RFC 791) and must complete correctly.
func TestFinalFragmentNeedNotBeAligned(t *testing.T) {
	const runID = "unit-tail-unaligned-001"
	payload := mkPayload(0xA010, 140) // 64+64+12
	k := mustKey("12.0.0.1", "12.0.0.2", 17, 0x0010)
	r := reasm.New(testCfg(), runID, nil)
	done := insertAll(t, r, mkFrags(k, payload, []int{64, 64}), t0)
	if done == nil || string(done.Data) != string(payload) {
		t.Fatalf("unaligned tail failed; done=%v", done != nil)
	}
}

// TestOversizeRejected: offset*8+len beyond the configured cap.
func TestOversizeRejected(t *testing.T) {
	const runID = "unit-oversize-001"
	cfg := testCfg()
	cfg.MaxDatagramSize = 200
	k := mustKey("13.0.0.1", "13.0.0.2", 17, 0x0011)
	r := reasm.New(cfg, runID, nil)
	// offset units 24 -> byte 192, length 16 -> end 208 > 200
	res := r.Insert(reasm.Fragment{Key: k, OffsetBytes: 192, More: true, Data: mkPayload(0x1111, 16)}, t0)
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonOversize {
		t.Fatalf("oversize: %s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s end=208 max=200 -> reason=%s", runID, res.Reason)
	if r.Stats().BufferedBytes != 0 {
		t.Fatalf("oversize fragment must buffer no bytes")
	}
}

// TestGroupCapacityRejected: exceeding MaxDatagrams fails with capacity.
func TestGroupCapacityRejected(t *testing.T) {
	const runID = "unit-cap-groups-001"
	cfg := testCfg()
	cfg.MaxDatagrams = 1
	r := reasm.New(cfg, runID, nil)
	k1 := mustKey("14.0.0.1", "14.0.0.2", 17, 0x0021)
	k2 := mustKey("14.0.0.3", "14.0.0.4", 17, 0x0022)
	if res := r.Insert(reasm.Fragment{Key: k1, OffsetBytes: 0, More: true, Data: mkPayload(0x1212, 64)}, t0); res.Outcome != reasm.OutcomeStored {
		t.Fatalf("first group: %s", res.Outcome)
	}
	res := r.Insert(reasm.Fragment{Key: k2, OffsetBytes: 0, More: true, Data: mkPayload(0x1313, 64)}, t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonCapacity {
		t.Fatalf("capacity: %s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s max_groups=1 -> second key rejected reason=%s", runID, res.Reason)
}

// TestByteBudgetRejected: exceeding MaxBufferedBytes rejects with capacity.
func TestByteBudgetRejected(t *testing.T) {
	const runID = "unit-cap-bytes-001"
	cfg := testCfg()
	cfg.MaxBufferedBytes = 100
	k := mustKey("15.0.0.1", "15.0.0.2", 17, 0x0031)
	r := reasm.New(cfg, runID, nil)
	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: mkPayload(0x1414, 64)}, t0)
	res := r.Insert(reasm.Fragment{Key: k, OffsetBytes: 64, More: true, Data: mkPayload(0x1515, 64)}, t0.Add(time.Millisecond))
	if res.Outcome != reasm.OutcomeRejected || res.Reason != reasm.ReasonCapacity {
		t.Fatalf("byte budget: %s/%s", res.Outcome, res.Reason)
	}
	t.Logf("input=%s 64+64 > 100 -> reason=%s; buffered released=%d", runID, res.Reason, r.Stats().BufferedBytes)
	if r.Stats().BufferedBytes != 0 {
		t.Fatalf("rejected group must release all buffered bytes")
	}
}

// TestTimeoutThenIDReuse: after timeout the same (src,dst,proto,id) must
// start an independent new group and reassemble the NEW payload only.
func TestTimeoutThenIDReuse(t *testing.T) {
	const runID = "unit-timeout-reuse-001"
	k := mustKey("16.0.0.1", "16.0.0.2", 17, 0x2001)
	r := reasm.New(testCfg(), runID, nil)

	oldPayload := mkPayload(0xAAAA, 128)
	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 0, More: true, Data: oldPayload[:64]}, t0)
	r.Insert(reasm.Fragment{Key: k, OffsetBytes: 64, More: true, Data: oldPayload[64:128]}, t0.Add(time.Millisecond))
	if r.Stats().ActiveGroups != 1 || r.Stats().BufferedBytes != 128 {
		t.Fatalf("old group not buffered: %+v", r.Stats())
	}

	expired := r.Sweep(t0.Add(31 * time.Second))
	if len(expired) != 1 || expired[0] != k {
		t.Fatalf("old group expiry: %v", expired)
	}
	t.Logf("input=%s old partial group expired after 30s idle; state reclaimed bytes=%d", runID, r.Stats().BufferedBytes)

	newPayload := mkPayload(0xBBBB, 200)
	frags := mkFrags(k, newPayload, []int{64, 64, 64})
	var done *reasm.Datagram
	for i, f := range frags {
		res := r.Insert(f, t0.Add(31*time.Second+time.Duration(i+1)))
		if i == len(frags)-1 {
			if res.Outcome != reasm.OutcomeCompleted {
				t.Fatalf("reused id completion: %s/%s", res.Outcome, res.Reason)
			}
			done = res.Datagram
		} else if res.Outcome != reasm.OutcomeStored {
			t.Fatalf("reused id step %d: %s/%s", i, res.Outcome, res.Reason)
		}
	}
	if string(done.Data) != string(newPayload) {
		t.Fatalf("reused id assembled OLD or mixed bytes")
	}
	t.Logf("input=%s key reused post-timeout: assembled new payload len=%d sha-checked; expired=%d completed=%d",
		runID, len(done.Data), r.Stats().Expired, r.Stats().Completed)
}

// TestRandomOrderFuzz: randomised arrival orders with rng-seed logged so
// any failure can be reproduced from the test log.
func TestRandomOrderFuzz(t *testing.T) {
	payload := mkPayload(0xF00D, 200) // 64+64+64+8
	k := mustKey("17.0.0.1", "17.0.0.2", 17, 0x0040)
	baseFrags := mkFrags(k, payload, []int{64, 64, 64})
	for iter := 0; iter < 50; iter++ {
		runID := fmt.Sprintf("unit-fuzz-%03d", iter)
		seed := int64(20260927 + iter)
		rng := rand.New(rand.NewSource(seed))
		t.Run(runID, func(t *testing.T) {
			frags := append([]reasm.Fragment(nil), baseFrags...)
			rng.Shuffle(len(frags), func(i, j int) { frags[i], frags[j] = frags[j], frags[i] })
			// sometimes inject an exact duplicate
			if iter%5 == 0 {
				frags = append(frags[:1], append([]reasm.Fragment{frags[0]}, frags[1:]...)...)
			}
			t.Logf("input=%s go=%s rng_seed=%d frags=%d; rule: complete iff contiguous", runID, runtime.Version(), seed, len(frags))
			r := reasm.New(testCfg(), runID, nil)
			done := insertAll(t, r, frags, t0)
			if done == nil || string(done.Data) != string(payload) {
				t.Fatalf("seed=%d produced wrong result", seed)
			}
		})
	}
}
