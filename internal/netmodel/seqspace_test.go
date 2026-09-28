package netmodel

import "testing"

func TestFwdDistAndOrder(t *testing.T) {
	cases := []struct {
		seq, ref uint32
		want     int64
	}{
		{10, 5, 5},
		{5, 10, -5},
		{0x00000001, 0xFFFFFFFF, 2},           // 1 is two steps after FFFFFFFF
		{0xFFFFFFFF, 0x00000001, -2},          // and vice versa
		{0x80000000, 0x00000000, -2147483648}, // exactly half window: behind
		{0x7FFFFFFF, 0x00000000, 2147483647},  // one inside half window: ahead
		{0, 0, 0},
	}
	for _, c := range cases {
		if got := FwdDist(c.seq, c.ref); got != c.want {
			t.Errorf("FwdDist(%08x,%08x)=%d want %d", c.seq, c.ref, got, c.want)
		}
	}
	if !SeqBefore(0xFFFFFFFF, 0x00000001) {
		t.Error("FFFFFFFF should be before 00000001")
	}
	if !SeqAfter(0x00000001, 0xFFFFFFFF) {
		t.Error("00000001 should be after FFFFFFFF")
	}
}

func TestSeqMapperWrapProjection(t *testing.T) {
	// SYN at raw 0xFFFFFFF6 anchored to coordinate 0. First data byte at
	// coordinate 1; offset 18 wraps to raw 0x00000009.
	var m SeqMapper
	m.AddAnchor(0xFFFFFFF6, 0)
	cases := []struct {
		raw uint32
		abs int64
	}{
		{0xFFFFFFF6, 0}, // SYN
		{0xFFFFFFF7, 1}, // first data byte
		{0xFFFFFFFF, 9},
		{0x00000000, 10}, // wrapped
		{0x00000009, 19},
		{0x00000013, 29}, // FIN position for a 28-byte stream
	}
	for _, c := range cases {
		if got := m.Abs(c.raw); got != c.abs {
			t.Errorf("Abs(%08x)=%d want %d", c.raw, got, c.abs)
		}
	}
}

func TestSeqMapperLoadExportRoundtrip(t *testing.T) {
	var m SeqMapper
	m.AddAnchor(100, 0)
	m.AddAnchor(200, 100)
	raw, abs := m.Export()
	var n SeqMapper
	n.Load(raw, abs)
	if got := n.Abs(150); got != int64(50) {
		t.Errorf("reloaded mapper Abs(150)=%d want 50", got)
	}
}

func TestSeqMapperAnchorlessFirstSeen(t *testing.T) {
	var m SeqMapper
	abs, anchored := m.AbsWith(5000) // first observed data byte -> relative 0
	if anchored || abs != 0 {
		t.Fatalf("first AbsWith = (%d,%v), want (0,false)", abs, anchored)
	}
	abs, anchored = m.AbsWith(5005)
	if !anchored || abs != 5 {
		t.Fatalf("second AbsWith = (%d,%v), want (5,true)", abs, anchored)
	}
}
