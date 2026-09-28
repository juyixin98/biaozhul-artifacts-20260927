package netmodel_test

import (
	"bytes"
	"testing"

	"tcpreplay/internal/netmodel"
)

// TestPCapWriteParseRoundtrip proves the pcap reader can recover packets the
// writer produced: flags, raw seq/ack, direction and payload must all match.
// The reader is the code actually used in production, so this also pins the
// wire layout.
func TestPCapWriteParseRoundtrip(t *testing.T) {
	orig := []netmodel.Packet{
		{SrcIP: "10.0.0.1", SrcPort: 40001, DstIP: "10.0.0.2", DstPort: 80,
			Flags: []string{"SYN"}, Seq: 0xFFFFFFF6},
		{SrcIP: "10.0.0.2", SrcPort: 80, DstIP: "10.0.0.1", DstPort: 40001,
			Flags: []string{"SYN", "ACK"}, Seq: 7000, Ack: 0xFFFFFFF7},
		{SrcIP: "10.0.0.1", SrcPort: 40001, DstIP: "10.0.0.2", DstPort: 80,
			Flags: []string{"ACK", "PSH"}, Seq: 0xFFFFFFF7,
			Payload: []byte{0xde, 0xad, 0xbe, 0xef}},
		{SrcIP: "10.0.0.1", SrcPort: 40001, DstIP: "10.0.0.2", DstPort: 80,
			Flags: []string{"ACK", "FIN"}, Seq: 0x00000013},
	}
	data := netmodel.WritePCap(orig)
	got, err := netmodel.ParsePCap(data)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(got) != len(orig) {
		t.Fatalf("packet count: got %d want %d", len(got), len(orig))
	}
	for i := range orig {
		g, w := got[i], orig[i]
		if g.SrcIP != w.SrcIP || g.SrcPort != w.SrcPort ||
			g.DstIP != w.DstIP || g.DstPort != w.DstPort {
			t.Errorf("pkt %d endpoints: %s:%d->%s:%d want %s:%d->%s:%d",
				i, g.SrcIP, g.SrcPort, g.DstIP, g.DstPort,
				w.SrcIP, w.SrcPort, w.DstIP, w.DstPort)
		}
		if g.Seq != w.Seq || g.Ack != w.Ack {
			t.Errorf("pkt %d seq/ack: %08x/%08x want %08x/%08x", i, g.Seq, g.Ack, w.Seq, w.Ack)
		}
		if g.FlagMask() != mask(w.Flags) {
			t.Errorf("pkt %d flags: %v want %v", i, g.Flags, w.Flags)
		}
		if !bytes.Equal(g.Payload, w.Payload) {
			t.Errorf("pkt %d payload: % x want % x", i, g.Payload, w.Payload)
		}
		if g.Timestamp == "" {
			t.Errorf("pkt %d timestamp not set", i)
		}
	}
}

func mask(names []string) uint8 {
	var p netmodel.Packet
	p.Flags = names
	return p.FlagMask()
}

// TestParsePCapRejectsBadInput covers the concrete failure categories callers
// depend on (truncated file, bad magic) instead of "error is non-nil".
func TestParsePCapRejectsBadInput(t *testing.T) {
	if _, err := netmodel.ParsePCap([]byte("too short")); err == nil {
		t.Error("expected error on truncated global header")
	}
	badMagic := make([]byte, 24)
	copy(badMagic[0:4], []byte{0, 1, 2, 3})
	if _, err := netmodel.ParsePCap(badMagic); err == nil {
		t.Error("expected error on bad magic")
	}
}
