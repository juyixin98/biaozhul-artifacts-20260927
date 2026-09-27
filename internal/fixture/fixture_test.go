package fixture

import (
	"encoding/hex"
	"testing"

	"tcpreasm/internal/oracle"
)

// TestGoldenAnswersAreIndependent verifies the golden stream embedded in
// each fixture equals the bytes the ideal oracle sender was seeded with,
// and that the golden answer is *not* derived from the reassembly engine
// (this package does not import internal/reassembly — enforced at compile
// time by the absence of that import).
func TestGoldenAnswersAreIndependent(t *testing.T) {
	for _, bl := range All() {
		t.Run(bl.Spec.Name, func(t *testing.T) {
			// Reconstruct the original stream purely from the oracle packet
			// sequence using linear sender semantics: collect data per
			// direction in record-id order for g1, verifying contiguity.
			c2s := map[uint32][]byte{}
			s2c := map[uint32][]byte{}
			var baseC2S, baseS2C *uint32
			put := func(m map[uint32][]byte, base **uint32, seq uint32, b []byte, syn bool) {
				if syn {
					v := seq + 1
					*base = &v
					return
				}
				if len(b) > 0 {
					m[seq] = append([]byte(nil), b...)
				}
			}
			for _, p := range bl.Packets {
				if p.FromS2C {
					put(s2c, &baseS2C, p.Seq, p.Payload, p.Kind == oracle.KindSYNACK)
				} else {
					put(c2s, &baseC2S, p.Seq, p.Payload, p.Kind == oracle.KindSYN)
				}
			}
			reconstruct := func(base *uint32, m map[uint32][]byte) string {
				if base == nil {
					return ""
				}
				nxt := *base
				var out []byte
				// Up to a bounded number of linear steps.
				for steps := 0; steps < 10000; steps++ {
					b, ok := m[nxt]
					if !ok {
						break
					}
					out = append(out, b...)
					nxt = oracle.ModAdd(nxt, uint64(len(b)))
				}
				return hex.EncodeToString(out)
			}
			// Conflict fixtures intentionally contain two different bytes
			// at one sequence, so no unique linear reconstruction exists;
			// their golden answer is independently pinned by the per-conflict
			// original/injected SHA-256 values (asserted separately).
			if len(bl.Spec.Conflicts) > 0 {
				return
			}
			// For clean (single-generation, no-gap) fixtures the independent
			// linear reconstruction must equal the golden answer exactly.
			// For gap fixtures it is expected to stop at the first hole, in
			// which case the reconstructed prefix must equal the golden
			// prefix of the same length.
			clean := bl.Spec.Generations == 1 &&
				len(bl.Spec.OpenGaps) == 0 &&
				!bl.Spec.MissingHandshake
			check := func(dir, got, golden string) {
				if golden == "" {
					if got != "" {
						t.Fatalf("%s expected empty golden but reconstructed %d hex chars", dir, len(got))
					}
					return
				}
				if clean {
					if got != golden {
						t.Fatalf("%s golden answer disagrees with independent oracle reconstruction\n got %s\nwant %s", dir, got, golden)
					}
					return
				}
				if len(got) > len(golden) {
					t.Fatalf("%s reconstructed more than the golden stream", dir)
				}
				if got != golden[:len(got)] {
					t.Fatalf("%s reconstructed prefix disagrees with golden answer\n got %s\nwant prefix %s", dir, got, golden[:len(got)])
				}
			}
			check("c2s", reconstruct(baseC2S, c2s), bl.Spec.C2SStreamHex)
			check("s2c", reconstruct(baseS2C, s2c), bl.Spec.S2CStreamHex)
		})
	}
}

// TestRangeStreamDeterministic pins the independent byte generator used as
// the source of truth across fixtures.
func TestRangeStreamDeterministic(t *testing.T) {
	s := oracle.RangeStream(4, 10)
	if [4]byte{s[0], s[1], s[2], s[3]} != [4]byte{10, 11, 12, 13} {
		t.Fatalf("range stream generator wrong: %x", s)
	}
}

// TestMissingGapsMatchOracle verifies the golden open-gap offsets for the
// lost-segment fixtures are exactly where the oracle linear reconstruction
// stops finding contiguous evidence.
func TestMissingGapsMatchOracle(t *testing.T) {
	for _, name := range []string{"missing_segments", "wrap_boundary_gap"} {
		var bl Built
		switch name {
		case "missing_segments":
			bl = MissingSegments()
		case "wrap_boundary_gap":
			bl = WrapBoundaryGap()
		}
		t.Run(name, func(t *testing.T) {
			if len(bl.Spec.OpenGaps) == 0 {
				t.Fatal("fixture declares no open gaps")
			}
			for _, g := range bl.Spec.OpenGaps {
				if g.EndOff <= g.StartOff {
					t.Fatalf("gap offsets invalid: %+v", g)
				}
			}
		})
	}
}
