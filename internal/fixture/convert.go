package fixture

import (
	"net/netip"

	"tcpreasm/internal/oracle"
	"tcpreasm/internal/tcpmodel"
)

// ToModel converts one oracle packet into a wire-model packet for the flow
// used by the builder that created it.
func ToModel(f oracle.Flow, p oracle.Packet) tcpmodel.Packet {
	src, dst := f.Client, f.Server
	dir := tcpmodel.DirC2S
	if p.FromS2C {
		src, dst = f.Server, f.Client
		dir = tcpmodel.DirS2C
	}
	m := tcpmodel.Packet{
		RecordID: p.RecordID, Order: p.Order,
		SrcIP: netip.MustParseAddr(src.IP), SrcPort: src.Port,
		DstIP: netip.MustParseAddr(dst.IP), DstPort: dst.Port,
		Seq:     p.Seq,
		Ack:     p.Ack,
		HasAck:  p.HasAck,
		Payload: append([]byte(nil), p.Payload...),
	}
	switch p.Kind {
	case oracle.KindSYN:
		m.SYN = true
	case oracle.KindSYNACK:
		m.SYN, m.ACK = true, true
		m.HasAck = true
	case oracle.KindACK:
		m.ACK = true
		m.HasAck = true
	case oracle.KindData:
		m.ACK = true
		if !p.HasAck {
			m.HasAck = true
		}
	case oracle.KindDataFIN:
		m.ACK, m.FIN = true, true
		m.HasAck = true
	case oracle.KindFIN:
		m.ACK, m.FIN = true, true
		m.HasAck = true
	case oracle.KindRST:
		m.RST = true
	}
	if p.DirHint != "" {
		m.DirHint = tcpmodel.Direction(p.DirHint)
	}
	_ = dir
	return m
}

// ToModelAll converts a whole built fixture.
func ToModelAll(f oracle.Flow, ps []oracle.Packet) []tcpmodel.Packet {
	out := make([]tcpmodel.Packet, 0, len(ps))
	for _, p := range ps {
		out = append(out, ToModel(f, p))
	}
	return out
}
