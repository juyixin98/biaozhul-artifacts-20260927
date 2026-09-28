package replaycheck_test

import (
	"os"
	"testing"

	"fwrule/internal/config"
	"fwrule/internal/replay"
)

func loadEngine(t *testing.T, path string) *replay.Engine {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return mustEngine(t, raw)
}

func mustEngine(t *testing.T, raw []byte) *replay.Engine {
	t.Helper()
	pol, err := config.Load(raw, "test")
	if err != nil {
		t.Fatal(err)
	}
	return &replay.Engine{Pol: pol, Version: 1}
}

func u16(v uint16) *uint16 { return &v }

// TestFirstMatchAndTrace checks a concrete packet hits the EARLIEST matching
// rule and that the trace shows the exact positions evaluated.
func TestFirstMatchAndTrace(t *testing.T) {
	eng := loadEngine(t, "../testdata/mini-policy.json")
	d := eng.Evaluate(replay.Request{
		RequestID: "w1", Protocol: "tcp",
		SrcIP: "10.0.0.0", DstIP: "192.168.0.0",
		SrcPort: u16(0), DstPort: u16(0),
	})
	if d.Status != replay.StatusDecided || d.Action != "allow" || d.DecidedBy != "a-broad-allow" {
		t.Fatalf("decision=%+v", d)
	}
	if len(d.Trace) != 10 {
		t.Fatalf("trace length=%d", len(d.Trace))
	}
	if !d.Trace[0].Matched {
		t.Fatal("first step should be a match")
	}
	// b-full-shadow ALSO matches this packet but is shadowed by a; the full
	// trace keeps it visible while the decision stays with a.
	if !d.Trace[1].Matched {
		t.Fatal("shadowed rule b still matches the packet (shown in trace)")
	}
	if d.MatchedRuleIndex != 0 {
		t.Fatalf("winner index=%d, want 0 (first match)", d.MatchedRuleIndex)
	}
	// A genuinely non-matching later rule must show concrete miss reasons.
	if d.Trace[2].Matched || len(d.Trace[2].MissReasons) == 0 {
		t.Fatal("rule c must miss this packet with reasons")
	}
}

// TestDefaultDenyV4 exercises the default-action path and error semantics.
func TestDefaultDenyV4(t *testing.T) {
	eng := loadEngine(t, "../testdata/default-deny-v4.json")

	// Allowed by the single rule.
	d := eng.Evaluate(replay.Request{
		RequestID: "ok", Protocol: "tcp",
		SrcIP: "10.0.0.1", DstIP: "192.168.0.2",
		SrcPort: u16(2), DstPort: u16(80),
	})
	if d.Action != "allow" || d.DecidedBy != "only-allow-80" {
		t.Fatalf("allow case: %+v", d)
	}

	// Unmatched -> default deny.
	d = eng.Evaluate(replay.Request{
		RequestID: "denied", Protocol: "tcp",
		SrcIP: "10.0.0.1", DstIP: "192.168.0.2",
		SrcPort: u16(2), DstPort: u16(22),
	})
	if !d.DefaultApplied || d.Action != "deny" || d.DecidedBy != "<default:deny>" {
		t.Fatalf("default deny case: %+v", d)
	}

	// No v6 default configured -> explicit error, not a guess.
	d = eng.Evaluate(replay.Request{
		RequestID: "v6", Protocol: "tcp",
		SrcIP: "2001:db8::1", DstIP: "2001:db8::2",
		SrcPort: u16(2), DstPort: u16(80),
	})
	if d.Status != replay.StatusError || d.ErrorCode != "DEFAULT_NOT_CONFIGURED" {
		t.Fatalf("v6 case: %+v", d)
	}
}

// TestErrorCodes pins the stable failure codes for malformed input.
func TestErrorCodes(t *testing.T) {
	eng := loadEngine(t, "../testdata/default-deny-v4.json")
	cases := []struct {
		name string
		req  replay.Request
		code string
	}{
		{"unknown protocol name", replay.Request{Protocol: "foo", SrcIP: "10.0.0.1", DstIP: "192.168.0.1"}, "UNKNOWN_PROTOCOL"},
		{"any protocol packet", replay.Request{Protocol: "any", SrcIP: "10.0.0.1", DstIP: "192.168.0.1"}, "INVALID_PROTOCOL"},
		{"bad src", replay.Request{Protocol: "tcp", SrcIP: "nope", DstIP: "192.168.0.1"}, "INVALID_SRC_IP"},
		{"bad dst", replay.Request{Protocol: "tcp", SrcIP: "10.0.0.1", DstIP: "nope"}, "INVALID_DST_IP"},
		{"family mix", replay.Request{Protocol: "tcp", SrcIP: "10.0.0.1", DstIP: "2001:db8::1"}, "FAMILY_MISMATCH"},
		{"missing proto", replay.Request{Protocol: "", SrcIP: "10.0.0.1", DstIP: "192.168.0.1"}, "MISSING_PROTOCOL"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			d := eng.Evaluate(tc.req)
			if d.Status != replay.StatusError || d.ErrorCode != tc.code {
				t.Fatalf("got status=%s code=%s, want code=%s",
					d.Status, d.ErrorCode, tc.code)
			}
		})
	}
}

// TestUnknownProtocolNumberIsUncertain: numeric proto 99 evaluates exactly as
// 99 and is flagged uncertain rather than rejected or guessed.
func TestUnknownProtocolNumberIsUncertain(t *testing.T) {
	eng := loadEngine(t, "../../configs/demo-policy.json")
	d := eng.Evaluate(replay.Request{
		RequestID: "p99", Protocol: "99",
		SrcIP: "10.0.0.1", DstIP: "192.168.1.1",
	})
	if d.Status != replay.StatusDecided || d.Action != "allow" || d.DecidedBy != "r11-unknown-proto" {
		t.Fatalf("proto99: %+v", d)
	}
	if len(d.Uncertainties) != 1 || d.Uncertainties[0].Code != "UNKNOWN_PROTOCOL_NUMBER" {
		t.Fatalf("uncertainty: %+v", d.Uncertainties)
	}
}
