package netmodel

import (
	"encoding/json"
	"testing"
)

func TestPrefixNormalization(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"10.0.0.1/8", "10.0.0.0/8"},                 // host bits masked
		{"192.168.1.77/24", "192.168.1.0/24"},        // host bits masked
		{"2001:0DB8:0000::0001/32", "2001:db8::/32"}, // uppercase + host bits
		{"2001:db8::1/128", "2001:db8::1/128"},       // host route keeps address
		{"0.0.0.0/0", "0.0.0.0/0"},
		{"::/0", "::/0"},
	}
	for _, c := range cases {
		p, err := ParsePrefix(c.in)
		if err != nil {
			t.Fatalf("ParsePrefix(%q): %v", c.in, err)
		}
		if got := p.String(); got != c.want {
			t.Errorf("ParsePrefix(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestPrefixRejectsMalformed(t *testing.T) {
	bad := []string{
		"198.51.100.000/24", // leading zeros in IPv4 octet
		"10.0.0.0/33",       // length out of range
		"2001:db8::/129",
		"::ffff:10.0.0.1/120", // IPv4-mapped IPv6
		"10.0.0.0",            // missing length
		"fe80::1%eth0/64",     // zones not allowed
		"300.1.1.1/24",
	}
	for _, b := range bad {
		if _, err := ParsePrefix(b); err == nil {
			t.Errorf("ParsePrefix(%q) unexpectedly accepted", b)
		}
	}
}

func TestFamilyIsolationOfModel(t *testing.T) {
	v4 := MustPrefix("10.0.0.0/8")
	v6 := MustPrefix("2001:db8::/32")
	if v4.Family() != FamilyV4 || v6.Family() != FamilyV6 {
		t.Fatalf("families: %v %v", v4.Family(), v6.Family())
	}
	a4, _ := ParseAddr("10.1.2.3")
	a6, _ := ParseAddr("2001:db8::5")
	if !v4.Contains(a4) {
		t.Error("v4 prefix must contain v4 address")
	}
	if v4.Contains(a6) || v6.Contains(a4) {
		t.Error("cross-family containment must be false")
	}
}

func TestAddrRejects4In6(t *testing.T) {
	if _, err := ParseAddr("::ffff:192.0.2.1"); err == nil {
		t.Error("IPv4-mapped IPv6 address must be rejected to keep families isolated")
	}
}

func TestRouteValidation(t *testing.T) {
	good := Route{
		ID: "r1", Prefix: MustPrefix("10.0.0.0/8"), AdminDist: 10,
		NextHop: NextHop{Interface: "eth0"},
	}
	if err := good.Validate(); err != nil {
		t.Fatalf("valid route rejected: %v", err)
	}

	bad := []Route{
		{ID: "", Prefix: MustPrefix("10.0.0.0/8"), NextHop: NextHop{Interface: "eth0"}},
		{ID: "r2", Prefix: MustPrefix("10.0.0.0/8"), AdminDist: 256, NextHop: NextHop{Interface: "eth0"}},
		{ID: "r3", Prefix: MustPrefix("10.0.0.0/8"), AdminDist: -1, NextHop: NextHop{Interface: "eth0"}},
		{ID: "r4", Prefix: MustPrefix("10.0.0.0/8"), NextHop: NextHop{}},
		{ID: "r5", Prefix: MustPrefix("10.0.0.0/8"), NextHop: NextHop{Interface: "eth0"}},                // ok actually
		{ID: "r6", Prefix: MustPrefix("10.0.0.0/8"), NextHop: NextHop{Addr: mustAddr(t, "2001:db8::1")}}, // cross-family
		{ID: "r7", Prefix: MustPrefix("10.0.0.0/8"), NextHop: NextHop{Addr: mustAddr(t, "10.0.0.1"), Interface: "eth0"}},
	}
	for i, r := range bad[:4] {
		if err := r.Validate(); err == nil {
			t.Errorf("bad route %d accepted", i)
		}
	}
	if err := bad[4].Validate(); err != nil {
		t.Errorf("interface-only route rejected: %v", err)
	}
	if err := bad[5].Validate(); err == nil {
		t.Error("cross-family recursive next hop must be rejected")
	}
	if err := bad[6].Validate(); err == nil {
		t.Error("next hop with both addr and interface must be rejected")
	}
}

func TestPrefixJSONRoundTrip(t *testing.T) {
	p := MustPrefix("2001:0DB8::1/32")
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != `"2001:db8::/32"` {
		t.Fatalf("marshal = %s", raw)
	}
	var back Prefix
	if err := json.Unmarshal(raw, &back); err != nil {
		t.Fatal(err)
	}
	if back != p {
		t.Fatalf("round trip: %v != %v", back, p)
	}
}

func mustAddr(t *testing.T, s string) Addr {
	t.Helper()
	a, err := ParseAddr(s)
	if err != nil {
		t.Fatal(err)
	}
	return a
}
