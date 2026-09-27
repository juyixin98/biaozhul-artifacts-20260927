package reassembly_test

import (
	"context"
	"net/netip"
	"testing"

	"tcpreasm/internal/config"
	"tcpreasm/internal/diag"
	"tcpreasm/internal/tcpmodel"
)

// TestInferredGenerationBothDirections verifies a handshake-less capture with
// traffic in both directions lands in one inferred generation and each
// direction is delivered independently (no cross-contamination).
func TestInferredGenerationBothDirections(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	ctx := context.Background()
	mk := func(id string, fromClient bool, seq uint32, payload string, fin bool) tcpmodel.Packet {
		srcIP, srcPort, dstIP, dstPort := netip.MustParseAddr("10.1.0.1"), uint16(5001),
			netip.MustParseAddr("10.1.0.2"), uint16(9000)
		if !fromClient {
			srcIP, srcPort, dstIP, dstPort = dstIP, dstPort, srcIP, srcPort
		}
		return tcpmodel.Packet{
			RecordID: id, Order: int64(0),
			SrcIP: srcIP, SrcPort: srcPort, DstIP: dstIP, DstPort: dstPort,
			Seq: seq, ACK: true, HasAck: true, FIN: fin, Payload: []byte(payload),
		}
	}
	pkts := []tcpmodel.Packet{
		mk("c1", true, 1000, "client-req", false),
		mk("s1", false, 5000, "server-answer-body", false),
		mk("c2", true, 1010, "-tail", true),
		mk("s2", false, 5018, "", true), // FIN-only after 18 s2c data bytes
	}
	var reqID = "test-inferred-bidir"
	for _, p := range pkts {
		res, err := h.eng.Process(ctx, p, reqID)
		if err != nil {
			t.Fatal(err)
		}
		if h.flowKey == "" && res.FlowKey != "" {
			h.flowKey = res.FlowKey
		}
	}
	if n := h.genCount(t); n != 1 {
		t.Fatalf("want one inferred generation, got %d", n)
	}
	c2s := h.streamBytes(t, 1, "c2s", 0, 15)
	s2c := h.streamBytes(t, 1, "s2c", 0, 18)
	if string(c2s) != "client-req-tail" {
		t.Fatalf("c2s inferred stream wrong: %q", c2s)
	}
	if string(s2c) != "server-answer-body" {
		t.Fatalf("s2c inferred stream wrong: %q", s2c)
	}
}

// TestStrayPacketUnknownFlow rejects a bare ACK on a 4-tuple for which no
// handshake/flow exists, with an explicit category rather than a guess.
func TestStrayPacketUnknownFlow(t *testing.T) {
	h := newHarness(t, config.PolicyFirstWins)
	ctx := context.Background()
	res, err := h.eng.Process(ctx, tcpmodel.Packet{
		RecordID: "stray", Order: 2,
		SrcIP: netip.MustParseAddr("10.9.9.9"), SrcPort: 33333,
		DstIP: netip.MustParseAddr("10.2.0.2"), DstPort: 9001,
		Seq: 8, ACK: true, HasAck: true,
	}, "r")
	if err != nil {
		t.Fatal(err)
	}
	if res.Decision != diag.Rejected || res.Category != diag.CatUnknownFlow {
		t.Fatalf("want REJECTED/UNKNOWN_FLOW, got %s/%s", res.Decision, res.Category)
	}
}
