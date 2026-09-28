package model

import "testing"

func TestValidateGroupAddr(t *testing.T) {
	good := []string{"239.1.2.3", "224.0.1.1", "232.10.20.30"}
	for _, g := range good {
		if _, err := ValidateGroupAddr(g); err != nil {
			t.Errorf("group %s rejected: %v", g, err)
		}
	}
	bad := []string{
		"10.0.0.1",  // unicast
		"240.1.1.1", // reserved/non-multicast
		"224.0.0.1", // all-hosts control block
		"224.0.0.252",
		"not-an-ip",
		"::1",
	}
	for _, g := range bad {
		if _, err := ValidateGroupAddr(g); err == nil {
			t.Errorf("group %s should be rejected", g)
		}
	}
}

func TestValidateSourceAddr(t *testing.T) {
	if _, err := ValidateSourceAddr("192.0.2.5"); err != nil {
		t.Errorf("private-range unicast should pass: %v", err)
	}
	for _, s := range []string{"", "0.0.0.0", "239.1.1.1", "255.255.255.255", "x", "fe80::1"} {
		if _, err := ValidateSourceAddr(s); err == nil {
			t.Errorf("source %q should be rejected", s)
		}
	}
}

// TestMaskAddr proves diagnostics redact the host-identifying octet while
// leaving multicast group addresses untouched elsewhere.
func TestMaskAddr(t *testing.T) {
	cases := map[string]string{
		"192.0.2.11":  "192.0.2.x",
		"10.20.30.40": "10.20.30.x",
		"":            "",
		"garbage":     "***",
	}
	for in, want := range cases {
		if got := MaskAddr(in); got != want {
			t.Errorf("MaskAddr(%q)=%q want %q", in, got, want)
		}
	}
}
