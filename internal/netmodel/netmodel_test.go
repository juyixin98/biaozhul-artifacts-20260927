package netmodel_test

import (
	"math/big"
	"testing"

	"fwrule/internal/netmodel"
)

func TestParseCIDR(t *testing.T) {
	c, err := netmodel.ParseCIDR("10.0.0.0/23")
	if err != nil {
		t.Fatal(err)
	}
	if c.String() != "10.0.0.0/23" {
		t.Fatalf("round trip: %s", c)
	}
	if c.First.String(netmodel.FamV4)+"" != "10.0.0.0" {
		t.Fatalf("first=%s", c.First.String(netmodel.FamV4))
	}
	if c.Last.String(netmodel.FamV4) != "10.0.1.255" {
		t.Fatalf("last=%s", c.Last.String(netmodel.FamV4))
	}
	if c.Size().Cmp(big.NewInt(512)) != 0 {
		t.Fatalf("size=%s", c.Size())
	}
}

func TestParseCIDRv6(t *testing.T) {
	c, err := netmodel.ParseCIDR("2001:db8::/32")
	if err != nil {
		t.Fatal(err)
	}
	if c.First.String(netmodel.FamV6) != "2001:db8::" {
		t.Fatalf("first=%s", c.First.String(netmodel.FamV4))
	}
	if c.Last.String(netmodel.FamV6) != "2001:db8:ffff:ffff:ffff:ffff:ffff:ffff" {
		t.Fatalf("last=%s", c.Last.String(netmodel.FamV4))
	}
}

func mustCIDR(t *testing.T, s string) netmodel.CIDR {
	t.Helper()
	c, err := netmodel.ParseCIDR(s)
	if err != nil {
		t.Fatalf("parse %s: %v", s, err)
	}
	return c
}

// TestPartitionHomogeneity is the core geometric invariant: every output
// block must be fully inside or fully outside EVERY input block.
func TestPartitionHomogeneity(t *testing.T) {
	cases := [][]string{
		{"10.0.0.0/8", "10.0.0.0/16"},
		{"10.0.0.0/23", "10.0.1.0/24"},   // non-text-prefix containment
		{"10.0.0.0/24", "10.0.0.128/25"}, // child boundary inside
		{"10.0.0.0/25", "10.0.0.64/26", "10.0.0.128/26"},
	}
	for _, tc := range cases {
		blocks := make([]netmodel.CIDR, len(tc))
		for i, s := range tc {
			blocks[i] = mustCIDR(t, s)
		}
		parts := netmodel.PartitionCIDRs(blocks)
		if len(parts) == 0 {
			t.Fatalf("%v: empty partition", tc)
		}
		for _, p := range parts {
			for _, b := range blocks {
				in := b.Contains(p.First)
				if !in {
					continue
				}
				if !b.Contains(p.Last) {
					t.Fatalf("%v: block %s straddles boundary of input %s",
						tc, p, b)
				}
			}
		}
	}
}

// TestPartitionExhaustive enumerates the entire 10.0.0.0/29 space and checks
// the partition's union equals the input union, with no overlaps.
func TestPartitionExhaustive(t *testing.T) {
	base := uint32(0x0A000000)
	inputs := []string{"10.0.0.0/30", "10.0.0.4/31"}
	blocks := []netmodel.CIDR{mustCIDR(t, inputs[0]), mustCIDR(t, inputs[1])}
	parts := netmodel.PartitionCIDRs(blocks)
	for v := uint32(0); v < 8; v++ {
		addr := netmodel.Addr{L: uint64(base + v)}
		want := blocks[0].Contains(addr) || blocks[1].Contains(addr)
		got := false
		for _, p := range parts {
			if p.Contains(addr) {
				if got {
					t.Fatalf("address %v in two partition blocks", addr)
				}
				got = true
			}
		}
		if got != want {
			t.Fatalf("addr %d: union mismatch got=%v want=%v", v, got, want)
		}
	}
}

func TestPortIntervals(t *testing.T) {
	parts := netmodel.PartitionIntervals([]netmodel.PortInterval{
		{Lo: 80, Hi: 100}, {Lo: 90, Hi: 120}, {Lo: 200, Hi: 300},
	})
	want := []netmodel.PortInterval{
		{80, 89}, {90, 100}, {101, 120}, {200, 300},
	}
	if len(parts) != len(want) {
		t.Fatalf("parts=%v want=%v", parts, want)
	}
	for i := range want {
		if parts[i] != want[i] {
			t.Fatalf("[%d] got=%v want=%v", i, parts[i], want[i])
		}
	}
}

// TestPortPartitionAtomicity: each resulting interval is wholly inside or
// outside every input interval (checked at both endpoints).
func TestPortPartitionAtomicity(t *testing.T) {
	in := []netmodel.PortInterval{{10, 30}, {20, 50}, {40, 45}}
	parts := netmodel.PartitionIntervals(in)
	for _, p := range parts {
		for _, iv := range in {
			loIn := iv.Contains(p.Lo)
			hiIn := iv.Contains(p.Hi)
			if loIn != hiIn {
				t.Fatalf("interval %v straddles input %v", p, iv)
			}
		}
	}
}

func TestParseProtocol(t *testing.T) {
	p, err := netmodel.ParseProtocol("TCP")
	if err != nil || p.Number != 6 || !p.PortBearing() {
		t.Fatalf("tcp parse: %+v err=%v", p, err)
	}
	if _, err := netmodel.ParseProtocol("sctpx"); err == nil {
		t.Fatal("unknown protocol name must error")
	}
	p2, err := netmodel.ParseProtocol("99")
	if err != nil || p2.KnownName {
		t.Fatalf("unknown number accepted but flagged: %+v err=%v", p2, err)
	}
	p3, err := netmodel.ParseProtocol("any")
	if err != nil || p3.Kind != netmodel.ProtoAny {
		t.Fatalf("any: %+v err=%v", p3, err)
	}
}
