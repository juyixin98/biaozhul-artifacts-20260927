package flow

import (
	"testing"

	"flexhash/internal/fherr"
)

func TestValidateAndCanonical(t *testing.T) {
	f := &FiveTuple{SrcIP: "10.0.0.1", SrcPort: 1234, DstIP: "10.0.0.2", DstPort: 80, Protocol: "TCP"}
	if err := f.Validate(); err != nil {
		t.Fatalf("validate: %v", err)
	}
	if f.Protocol != "tcp" {
		t.Fatalf("protocol normalized = %q, want tcp", f.Protocol)
	}
	if got, want := f.Canonical(), "10.0.0.1|1234|10.0.0.2|80|tcp"; got != want {
		t.Fatalf("canonical = %q, want %q", got, want)
	}
}

func TestProtocolAliases(t *testing.T) {
	for in, want := range map[string]string{
		"TCP": "tcp", "6": "tcp", "udp": "udp", "17": "udp", "ICMP": "icmp", "1": "icmp",
	} {
		got, err := CanonicalProtocol(in)
		if err != nil || got != want {
			t.Fatalf("CanonicalProtocol(%q) = %q,%v want %q", in, got, err, want)
		}
	}
	for _, bad := range []string{"sctp", "", "99"} {
		if _, err := CanonicalProtocol(bad); fherr.KindOf(err) != fherr.KindInput {
			t.Fatalf("CanonicalProtocol(%q) err kind = %v, want input_error", bad, fherr.KindOf(err))
		}
	}
}

func TestTupleValidationErrors(t *testing.T) {
	bad := []FiveTuple{
		{SrcIP: "not-an-ip", DstIP: "10.0.0.2", Protocol: "tcp"},
		{SrcIP: "10.0.0.1", DstIP: "999.1.1.1", Protocol: "tcp"},
		{SrcIP: "10.0.0.1", DstIP: "10.0.0.2", Protocol: "gre"},
	}
	for i, f := range bad {
		if err := f.Validate(); fherr.KindOf(err) != fherr.KindInput {
			t.Fatalf("case %d: kind=%v err=%v", i, fherr.KindOf(err), err)
		}
	}
}

func TestDecodeRejectsBadJSON(t *testing.T) {
	if _, err := DecodeRequest([]byte("{garbage")); fherr.KindOf(err) != fherr.KindInput {
		t.Fatalf("bad JSON kind = %v", fherr.KindOf(err))
	}
}
