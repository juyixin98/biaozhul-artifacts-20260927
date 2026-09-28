package ipparse_test

import (
	"testing"

	"cidrcov/internal/ipparse"
	"cidrcov/internal/netmodel"
)

func TestParseCanonical(t *testing.T) {
	pe, advs, err := ipparse.Parse("10.0.0.0/24")
	if err != nil {
		t.Fatal(err)
	}
	if pe.Kind != ipparse.KindV4 || pe.PrefixLen != 24 {
		t.Fatalf("kind/len = %s/%d", pe.Kind, pe.PrefixLen)
	}
	if pe.CanonicalText != "10.0.0.0/24" {
		t.Fatalf("canonical = %q", pe.CanonicalText)
	}
	if pe.Interval.End.Int64()-pe.Interval.Start.Int64()+1 != 256 {
		t.Fatal("a /24 spans 256 addresses including network and broadcast")
	}
	if len(advs) != 0 {
		t.Fatalf("canonical input should yield no advisories, got %+v", advs)
	}
}

func TestParseHostBitsAdvisory(t *testing.T) {
	pe, advs, err := ipparse.Parse("10.0.0.99/24")
	if err != nil {
		t.Fatal(err)
	}
	if pe.CanonicalText != "10.0.0.0/24" {
		t.Fatalf("masked canonical = %q", pe.CanonicalText)
	}
	if len(advs) != 1 || advs[0].Code != ipparse.AdvHostBitsCanonicalized {
		t.Fatalf("want one host-bits advisory, got %+v", advs)
	}
}

func TestParseBareAddress(t *testing.T) {
	pe, advs, err := ipparse.Parse("192.168.1.1")
	if err != nil {
		t.Fatal(err)
	}
	if pe.PrefixLen != 32 || pe.Kind != ipparse.KindV4 {
		t.Fatalf("bare v4 should be /32, got /%d", pe.PrefixLen)
	}
	if len(advs) != 1 || advs[0].Code != ipparse.AdvBareIPExpanded {
		t.Fatalf("want bare-ip advisory, got %+v", advs)
	}
}

func TestParseV4MappedV6(t *testing.T) {
	pe, advs, err := ipparse.Parse("::ffff:10.20.30.40/120")
	if err != nil {
		t.Fatal(err)
	}
	if pe.Kind != ipparse.KindV4 || pe.PrefixLen != 24 {
		t.Fatalf("mapped prefix rebased to v4 /24, got %s /%d", pe.Kind, pe.PrefixLen)
	}
	if pe.CanonicalText != "10.20.30.0/24" {
		t.Fatalf("canonical = %q", pe.CanonicalText)
	}
	found := false
	for _, a := range advs {
		if a.Code == ipparse.AdvV4MappedInV6 {
			found = true
		}
	}
	if !found {
		t.Fatalf("want mapped advisory, got %+v", advs)
	}

	// A mapped prefix shorter than /96 cannot be expressed in 32 low bits.
	if _, _, err := ipparse.Parse("::ffff:0:0/95"); err == nil {
		t.Fatal("mapped prefix /95 (<96) must be rejected")
	}
}

func TestParseV6RoundTrip(t *testing.T) {
	for _, s := range []string{"2001:db8::/32", "::1/128", "fe80::/10", "::/0"} {
		pe, _, err := ipparse.Parse(s)
		if err != nil {
			t.Fatalf("%s: %v", s, err)
		}
		if pe.Kind != ipparse.KindV6 {
			t.Fatalf("%s parsed as %s", s, pe.Kind)
		}
		if pe.CanonicalText != s {
			t.Fatalf("%s round-trips as %s", s, pe.CanonicalText)
		}
	}
}

func TestPrefixLengthOutOfRangeClass(t *testing.T) {
	for _, s := range []string{"10.0.0.0/33", "::/129"} {
		_, _, err := ipparse.Parse(s)
		pe, ok := err.(*ipparse.ParseError)
		if !ok || pe.Class != ipparse.ClassBadPrefixLen {
			t.Fatalf("%s should be %s, got %v", s, ipparse.ClassBadPrefixLen, err)
		}
	}
}

func TestParseFailures(t *testing.T) {
	bad := []string{"", "not/cidr", "10.0.0.0/", "10.0.0.0/33", "::/129",
		"999.0.0.0/8", "10.0.0.0/24/extra"}
	for _, s := range bad {
		if _, _, err := ipparse.Parse(s); err == nil {
			t.Errorf("expected error for %q", s)
		} else if pe, ok := err.(*ipparse.ParseError); !ok || pe.Class == "" {
			t.Errorf("error for %q must be a classified ParseError, got %T", s, err)
		}
	}
}

func TestFormat(t *testing.T) {
	// Construct via parsing so the big ints are guaranteed well-formed.
	pe, _, _ := ipparse.Parse("2001:db8::/64")
	got := ipparse.Format(pe.Kind, netmodel.Prefix{Base: pe.Network, Len: pe.PrefixLen})
	if got != "2001:db8::/64" {
		t.Fatalf("format = %q", got)
	}
}
