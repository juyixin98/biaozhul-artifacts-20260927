// Package integration_test runs end-to-end replays of the synthetic trace
// fixtures. Expected verdicts and rejection reasons are hand-authored tables
// below; they are never derived from the engine under test.
package integration_test

import (
	"context"
	"path/filepath"
	"testing"

	"natlab/internal/config"
	"natlab/internal/model"
	"natlab/internal/replay"
	"natlab/internal/storage"
)

func traceDir() string {
	// tests run from their package dir; fixtures live at repo-root/traces.
	return filepath.Join("..", "..", "traces")
}

// expect is one hand-authored row: what the model MUST decide for one seq.
type expect struct {
	verdict model.Verdict
	reason  model.RejectReason // empty for accept*
	postSrc uint16             // expected translated src port (outbound)
	postDst uint16             // expected translated dst port (inbound)
	state   string             // expected state_after when TCP
}

func runTrace(t *testing.T, file string, st storage.Store) (*replay.Report, error) {
	t.Helper()
	cfg := config.Default()
	tr, err := replay.LoadTrace(filepath.Join(traceDir(), file))
	if err != nil {
		t.Fatal(err)
	}
	if st == nil {
		st = storage.NewMemory()
	}
	r := replay.NewRunner(cfg, st)
	return r.Run(context.Background(), tr)
}

func decisionsBySeq(rep *replay.Report) map[int64]model.Decision {
	m := map[int64]model.Decision{}
	for _, d := range rep.Decisions {
		m[d.Seq] = d
	}
	return m
}

func assertRow(t *testing.T, d model.Decision, e expect) {
	t.Helper()
	if d.Verdict != e.verdict {
		t.Fatalf("seq=%d verdict=%s reason=%s, want %s", d.Seq, d.Verdict, d.Reason, e.verdict)
	}
	if e.reason != "" && d.Reason != e.reason {
		t.Fatalf("seq=%d reason=%s, want %s (rationale: %s)", d.Seq, d.Reason, e.reason, d.Rationale)
	}
	if e.reason == "" && model.ClassOf(e.reason) == "" && d.Class != "" {
		t.Fatalf("seq=%d accepted packet carries reject class %s", d.Seq, d.Class)
	}
	if d.Post != nil {
		if e.postSrc != 0 && d.Post.SrcPort != e.postSrc {
			t.Fatalf("seq=%d translated src port=%d, want %d", d.Seq, d.Post.SrcPort, e.postSrc)
		}
		if e.postDst != 0 && d.Post.DstPort != e.postDst {
			t.Fatalf("seq=%d translated dst port=%d, want %d", d.Seq, d.Post.DstPort, e.postDst)
		}
	}
	if e.state != "" && d.StateAfter != e.state {
		t.Fatalf("seq=%d state=%q, want %q", d.Seq, d.StateAfter, e.state)
	}
}

func TestBidirectionalFixture(t *testing.T) {
	rep, err := runTrace(t, "01_bidirectional.json", nil)
	if err != nil {
		t.Fatal(err)
	}
	want := map[int64]expect{
		1: {verdict: model.AcceptTranslate, postSrc: 20000, state: "SYN_SENT"},
		2: {verdict: model.AcceptForward, postDst: 40001, state: "ESTABLISHED"},
		3: {verdict: model.AcceptForward, postSrc: 20000, state: "ESTABLISHED"},
		4: {verdict: model.AcceptForward, postDst: 40001, state: "ESTABLISHED"},
		5: {verdict: model.AcceptTranslate, postSrc: 30000},
		6: {verdict: model.AcceptForward, postDst: 53000},
		7: {verdict: model.Reject, reason: model.ReasonRemoteMismatch},
		8: {verdict: model.Reject, reason: model.ReasonNoMapping},
	}
	bySeq := decisionsBySeq(rep)
	for seq, e := range want {
		d, ok := bySeq[seq]
		if !ok {
			t.Fatalf("missing decision seq=%d", seq)
		}
		assertRow(t, d, e)
	}
	if rep.Stats.MappingsCreated != 2 || rep.Stats.ActiveMappings != 2 {
		t.Fatalf("stats=%+v", rep.Stats)
	}
}

func TestExhaustionFixture(t *testing.T) {
	rep, err := runTrace(t, "02_exhaustion.json", nil)
	if err != nil {
		t.Fatal(err)
	}
	bySeq := decisionsBySeq(rep)
	assertRow(t, bySeq[1], expect{verdict: model.AcceptTranslate, postSrc: 20000})
	assertRow(t, bySeq[2], expect{verdict: model.AcceptTranslate, postSrc: 20001})
	assertRow(t, bySeq[3], expect{verdict: model.Reject, reason: model.ReasonPortExhausted})
	// The SYN retransmit for flow A must still be accepted on its own mapping.
	assertRow(t, bySeq[4], expect{verdict: model.AcceptForward, postSrc: 20000, state: "SYN_SENT"})
	if rep.Stats.RejectedExhaustion != 1 || rep.Stats.MappingsCreated != 2 {
		t.Fatalf("stats=%+v", rep.Stats)
	}
}

func TestTimeoutReuseFixture(t *testing.T) {
	rep, err := runTrace(t, "03_timeout_reuse.json", nil)
	if err != nil {
		t.Fatal(err)
	}
	bySeq := decisionsBySeq(rep)
	assertRow(t, bySeq[1], expect{verdict: model.AcceptTranslate, postSrc: 30000})
	assertRow(t, bySeq[2], expect{verdict: model.AcceptForward, postDst: 50001})
	assertRow(t, bySeq[3], expect{verdict: model.Reject, reason: model.ReasonPortExhausted})
	assertRow(t, bySeq[4], expect{verdict: model.Reject, reason: model.ReasonMappingExpired})
	assertRow(t, bySeq[5], expect{verdict: model.AcceptTranslate, postSrc: 30000})
	assertRow(t, bySeq[6], expect{verdict: model.AcceptForward, postDst: 50004})
	// seq7 is stamped in the past (t=6). At the monotonic clock (t=22) the
	// port is held by flow C with peer .202:123, so the old A packet neither
	// revives A nor matches C: it must be a precise remote_endpoint_mismatch.
	d7 := bySeq[7]
	if d7.Verdict != model.Reject || d7.Reason != model.ReasonRemoteMismatch {
		t.Fatalf("seq7 verdict=%s reason=%s, want reject/remote_endpoint_mismatch",
			d7.Verdict, d7.Reason)
	}
	if rep.Stats.ClockRollbacks != 1 {
		t.Fatalf("rollbacks=%d, want 1", rep.Stats.ClockRollbacks)
	}
	if rep.Stats.MappingsCreated != 2 {
		t.Fatalf("mappings created=%d, want 2", rep.Stats.MappingsCreated)
	}
}

func TestTCPCloseFixture(t *testing.T) {
	rep, err := runTrace(t, "04_tcp_close.json", nil)
	if err != nil {
		t.Fatal(err)
	}
	bySeq := decisionsBySeq(rep)
	assertRow(t, bySeq[1], expect{verdict: model.AcceptTranslate, postSrc: 20000, state: "SYN_SENT"})
	assertRow(t, bySeq[2], expect{verdict: model.AcceptForward, state: "ESTABLISHED"})
	assertRow(t, bySeq[3], expect{verdict: model.AcceptForward, state: "ESTABLISHED"})
	assertRow(t, bySeq[4], expect{verdict: model.AcceptForward, state: "FIN_WAIT_1"})
	assertRow(t, bySeq[5], expect{verdict: model.AcceptForward, state: "TIME_WAIT"})
	assertRow(t, bySeq[6], expect{verdict: model.Reject, reason: model.ReasonStateConflict})
	assertRow(t, bySeq[7], expect{verdict: model.AcceptForward, state: "CLOSED"})
	assertRow(t, bySeq[8], expect{verdict: model.Reject, reason: model.ReasonMappingExpired})
	assertRow(t, bySeq[9], expect{verdict: model.AcceptTranslate, postSrc: 20000})
}

func TestInputRejectsFixture(t *testing.T) {
	rep, err := runTrace(t, "05_input_rejects.json", nil)
	if err != nil {
		t.Fatal(err)
	}
	bySeq := decisionsBySeq(rep)
	want := map[int64]model.RejectReason{
		1: model.ReasonFragmentDropped,
		2: model.ReasonProtocolUnsupport,
		3: model.ReasonInvalidInput,
		4: model.ReasonInvalidInput,
		5: model.ReasonInvalidInput,
		6: model.ReasonInvalidInput,
		7: model.ReasonInvalidInput,
		8: model.ReasonExternalMismatch,
	}
	for seq, reason := range want {
		d := bySeq[seq]
		if d.Verdict != model.Reject || d.Reason != reason || d.Class != model.ClassInput {
			t.Fatalf("seq=%d verdict=%s reason=%s class=%s, want reason=%s class=input_error",
				seq, d.Verdict, d.Reason, d.Class, reason)
		}
	}
	if rep.Stats.MappingsCreated != 0 {
		t.Fatalf("rejected-input trace must create no mappings, got %d", rep.Stats.MappingsCreated)
	}
}
