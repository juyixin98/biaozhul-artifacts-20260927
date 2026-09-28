package testsupport

import (
	"bytes"
	"fmt"

	"tcpreplay/internal/reassembly"
)

// ExpectedDirection is the hand-authored expected answer for one direction.
// Every value comes from the scenario's known original stream or from
// arithmetic the test writer performed against the authored ISN — never from
// an engine output.
type ExpectedDirection struct {
	HandshakeKnown  bool
	Stream          []byte
	Gaps            []reassembly.Gap
	HeldRunsData    [][]byte // held out-of-order runs, ordered by start
	FINSeen         bool
	FINPos          int64 // ignored unless FINSeen
	LengthProved    int64 // ignored unless FINSeen
	QuarantineBytes int
}

// Gap is shorthand for reassembly.Gap construction in tables.
func Gap(start, end int64) reassembly.Gap { return reassembly.Gap{Start: start, End: end} }

// CheckDirection compares a DirectionView against the hand-authored
// expectation and returns human-readable mismatches (empty = pass). It is
// deliberately independent of the engine: equality of byte sequences, exact
// gap intervals and exact held-run payloads are checked.
func CheckDirection(v reassembly.DirectionView, want ExpectedDirection) []string {
	var msgs []string
	if v.HandshakeKnown != want.HandshakeKnown {
		msgs = append(msgs, fmt.Sprintf("handshake_known: got %v want %v", v.HandshakeKnown, want.HandshakeKnown))
	}
	if !bytes.Equal(v.Stream, want.Stream) {
		msgs = append(msgs, fmt.Sprintf("stream: got %q (len=%d) want %q (len=%d)",
			v.Stream, len(v.Stream), want.Stream, len(want.Stream)))
	}
	if ms := checkGaps(v.Gaps, want.Gaps); ms != "" {
		msgs = append(msgs, ms)
	}
	if len(v.HeldRuns) != len(want.HeldRunsData) {
		msgs = append(msgs, fmt.Sprintf("held run count: got %d want %d", len(v.HeldRuns), len(want.HeldRunsData)))
	} else {
		for i, run := range v.HeldRuns {
			if !bytes.Equal(run.Data, want.HeldRunsData[i]) {
				msgs = append(msgs, fmt.Sprintf("held run %d: got %q want %q", i, run.Data, want.HeldRunsData[i]))
			}
		}
	}
	if v.FINSeen != want.FINSeen {
		msgs = append(msgs, fmt.Sprintf("fin_seen: got %v want %v", v.FINSeen, want.FINSeen))
	}
	if want.FINSeen && v.FINPos != want.FINPos {
		msgs = append(msgs, fmt.Sprintf("fin_pos: got %d want %d", v.FINPos, want.FINPos))
	}
	if want.FINSeen && v.LengthProved != want.LengthProved {
		msgs = append(msgs, fmt.Sprintf("length_proved: got %d want %d", v.LengthProved, want.LengthProved))
	}
	if len(v.HeldQuarantine) != want.QuarantineBytes {
		msgs = append(msgs, fmt.Sprintf("quarantined byte count: got %d want %d",
			len(v.HeldQuarantine), want.QuarantineBytes))
	}
	return msgs
}

func checkGaps(got, want []reassembly.Gap) string {
	if len(got) != len(want) {
		return fmt.Sprintf("gap count: got %v want %v", got, want)
	}
	for i := range got {
		if got[i] != want[i] {
			return fmt.Sprintf("gap %d: got [%d,%d) want [%d,%d)",
				i, got[i].Start, got[i].End, want[i].Start, want[i].End)
		}
	}
	return ""
}

// ExpectConflict describes one expected byte-level conflict, with the exact
// coordinate and both bytes, authored from the scenario.
type ExpectConflict struct {
	Generation  int
	Direction   string // "a_to_b" | "b_to_a"
	ByteOffset  int64
	Accepted    byte
	Offered     byte
	Disposition string // reassembly.Disp*
}

// CheckConflicts compares the engine-reported conflicts against authored
// expectations. Order is normalized (the test may sort engine conflicts) but
// coordinates and values must match exactly.
func CheckConflicts(got []reassembly.Conflict, want []ExpectConflict) []string {
	var msgs []string
	if len(got) != len(want) {
		msgs = append(msgs, fmt.Sprintf("conflict count: got %d want %d", len(got), len(want)))
	}
	matched := make([]bool, len(got))
	for wi, w := range want {
		found := -1
		for gi, g := range got {
			if matched[gi] {
				continue
			}
			if g.Generation == w.Generation && g.Direction == w.Direction &&
				g.ByteOffset == w.ByteOffset && g.Accepted == w.Accepted &&
				g.Offered == w.Offered && g.Disposition == w.Disposition {
				found = gi
				break
			}
		}
		if found < 0 {
			msgs = append(msgs, fmt.Sprintf("expected conflict #%d not found: gen=%d dir=%s off=%d accepted=0x%02x offered=0x%02x disp=%s",
				wi, w.Generation, w.Direction, w.ByteOffset, w.Accepted, w.Offered, w.Disposition))
		} else {
			matched[found] = true
		}
	}
	return msgs
}

// FindEvent reports whether an event with the given code exists at all and,
// when level is non-empty, matches it.
func FindEvent(events []reassembly.Event, code reassembly.EventCode, level reassembly.EventLevel) bool {
	for _, e := range events {
		if e.Code == code && (level == "" || e.Level == level) {
			return true
		}
	}
	return false
}

// MustContain fails when want is not a contiguous subsequence of got.
func MustContain(got, want []byte) bool { return bytes.Contains(got, want) }
