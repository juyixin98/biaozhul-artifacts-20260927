package netmodel

import (
	"fmt"
	"math/big"
)

// MatchBox is a protocol-keyed hyper-rectangle of packet coordinates:
// one address block for the source, one for the destination and closed port
// intervals on each side. Port fields are ignored for non-port-bearing
// protocols (set to FullPorts then).
type MatchBox struct {
	Fam      Family
	Proto    Protocol
	SrcNet   CIDR
	DstNet   CIDR
	SrcPorts PortInterval
	DstPorts PortInterval
}

// Packet is one concrete packet coordinate — the witness type.
type Packet struct {
	Fam      Family
	ProtoNum uint8
	SrcIP    Addr
	DstIP    Addr
	SrcPort  uint16
	DstPort  uint16
}

func (pkt Packet) String() string {
	proto := pkt.ProtoNum
	return fmt.Sprintf("pkt{%s proto=%d %s:%d -> %s:%d}",
		pkt.Fam, proto,
		pkt.SrcIP.String(pkt.Fam), pkt.SrcPort,
		pkt.DstIP.String(pkt.Fam), pkt.DstPort)
}

// NewBox builds a match box and normalizes port fields for protocols that do
// not carry ports.
func NewBox(fam Family, proto Protocol, src, dst CIDR, srcPorts, dstPorts PortInterval) MatchBox {
	b := MatchBox{
		Fam:      fam,
		Proto:    proto,
		SrcNet:   src,
		DstNet:   dst,
		SrcPorts: srcPorts,
		DstPorts: dstPorts,
	}
	if !proto.PortBearing() {
		b.SrcPorts = FullPorts
		b.DstPorts = FullPorts
	}
	return b
}

// Contains reports whether pkt falls inside the box.
func (b MatchBox) Contains(pkt Packet) bool {
	if pkt.Fam != b.Fam {
		return false
	}
	if b.Proto.Kind == ProtoConcrete && pkt.ProtoNum != b.Proto.Number {
		return false
	}
	if !b.SrcNet.Contains(pkt.SrcIP) || !b.DstNet.Contains(pkt.DstIP) {
		return false
	}
	if b.Proto.PortBearing() {
		if !b.SrcPorts.Contains(pkt.SrcPort) || !b.DstPorts.Contains(pkt.DstPort) {
			return false
		}
	}
	return true
}

func (b MatchBox) String() string {
	if b.Proto.PortBearing() {
		return fmt.Sprintf("%s/%s %s:%s -> %s:%s",
			b.Fam, b.Proto,
			b.SrcNet, b.SrcPorts, b.DstNet, b.DstPorts)
	}
	return fmt.Sprintf("%s/%s %s -> %s", b.Fam, b.Proto, b.SrcNet, b.DstNet)
}

// Volume is the number of packet coordinates in the box.
func (b MatchBox) Volume() *big.Int {
	v := new(big.Int).Mul(b.SrcNet.Size(), b.DstNet.Size())
	if b.Proto.PortBearing() {
		v.Mul(v, big.NewInt(int64(b.SrcPorts.Count())))
		v.Mul(v, big.NewInt(int64(b.DstPorts.Count())))
	}
	return v
}

// Witness chooses one concrete packet in the box (lowest coordinates), and the
// protocol number to use. For an "any" box the caller may request a specific
// protocol number; otherwise the box's concrete number is used.
func (b MatchBox) Witness(protoNum uint8) Packet {
	num := protoNum
	if b.Proto.Kind == ProtoConcrete {
		num = b.Proto.Number
	}
	pkt := Packet{
		Fam:      b.Fam,
		ProtoNum: num,
		SrcIP:    b.SrcNet.First,
		DstIP:    b.DstNet.First,
		SrcPort:  b.SrcPorts.Lo,
		DstPort:  b.DstPorts.Lo,
	}
	return pkt
}

// Cell is a unit of the analysis partition: for every packet in the cell, the
// vector of rule-match results is identical.
type Cell struct {
	Fam      Family
	ProtoNum uint8
	SrcNet   CIDR
	DstNet   CIDR
	SrcPorts PortInterval
	DstPorts PortInterval
}

// Box renders the cell as a match box with a concrete protocol.
func (c Cell) Box() MatchBox {
	proto := Protocol{Kind: ProtoConcrete, Number: c.ProtoNum,
		KnownName: numberToName[c.ProtoNum] != "", Name: numberToName[c.ProtoNum]}
	return MatchBox{
		Fam:      c.Fam,
		Proto:    proto,
		SrcNet:   c.SrcNet,
		DstNet:   c.DstNet,
		SrcPorts: c.SrcPorts,
		DstPorts: c.DstPorts,
	}
}

func (c Cell) String() string { return c.Box().String() }

// Volume is the number of packet coordinates in the cell.
func (c Cell) Volume() *big.Int { return c.Box().Volume() }

// FirstPacket returns the lowest-coordinate packet in the cell.
func (c Cell) FirstPacket() Packet {
	return Packet{
		Fam:      c.Fam,
		ProtoNum: c.ProtoNum,
		SrcIP:    c.SrcNet.First,
		DstIP:    c.DstNet.First,
		SrcPort:  c.SrcPorts.Lo,
		DstPort:  c.DstPorts.Lo,
	}
}
