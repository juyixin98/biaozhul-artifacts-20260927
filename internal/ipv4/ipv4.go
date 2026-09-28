// Package ipv4 models the IPv4 header fields relevant to fragmentation and
// provides parsing (for the PCAP replay path) and serialisation (for the
// independent fixture generator). It performs no reassembly itself.
package ipv4

import (
	"encoding/binary"
	"errors"
	"fmt"
	"net/netip"
)

var (
	ErrNotIPv4   = errors.New("not an IPv4 packet")
	ErrTruncated = errors.New("packet truncated")
	ErrBadHeader = errors.New("invalid IPv4 header")
)

// Packet is the parsed view of one IPv4 datagram (fragment or whole).
type Packet struct {
	Src, Dst netip.Addr
	Protocol uint8
	ID       uint16
	// FragOffsetUnits is the raw 13-bit fragment-offset field, in 8-byte units.
	FragOffsetUnits uint16
	MoreFragments   bool
	DontFragment    bool
	TTL             uint8
	HeaderLen       int // IHL in bytes
	TotalLen        int // total length field in bytes
	Payload         []byte
}

// OffsetBytes converts the wire offset field to a byte offset.
func (p *Packet) OffsetBytes() int { return int(p.FragOffsetUnits) * 8 }

// Fragmented reports whether the packet participates in fragmentation
// (MF set or non-zero offset). Unfragmented packets are passed through by
// the replay layer and never enter the reassembler.
func (p *Packet) Fragmented() bool { return p.MoreFragments || p.FragOffsetUnits != 0 }

// Parse decodes one raw IPv4 packet (starting at the IP header).
func Parse(b []byte) (*Packet, error) {
	if len(b) < 20 {
		return nil, fmt.Errorf("%w: %d bytes", ErrTruncated, len(b))
	}
	if b[0]>>4 != 4 {
		return nil, fmt.Errorf("%w: version nibble %d", ErrNotIPv4, b[0]>>4)
	}
	ihl := int(b[0]&0x0f) * 4
	if ihl < 20 {
		return nil, fmt.Errorf("%w: IHL %d < 20", ErrBadHeader, ihl)
	}
	if len(b) < ihl {
		return nil, fmt.Errorf("%w: header needs %d bytes, have %d", ErrTruncated, ihl, len(b))
	}
	total := int(binary.BigEndian.Uint16(b[2:4]))
	if total < ihl {
		return nil, fmt.Errorf("%w: total length %d < IHL %d", ErrBadHeader, total, ihl)
	}
	if len(b) < total {
		return nil, fmt.Errorf("%w: total length %d, have %d", ErrTruncated, total, len(b))
	}
	flagsOff := binary.BigEndian.Uint16(b[6:8])
	p := &Packet{
		Src:             netip.AddrFrom4([4]byte{b[12], b[13], b[14], b[15]}),
		Dst:             netip.AddrFrom4([4]byte{b[16], b[17], b[18], b[19]}),
		Protocol:        b[9],
		ID:              binary.BigEndian.Uint16(b[4:6]),
		FragOffsetUnits: flagsOff & 0x1fff,
		MoreFragments:   flagsOff&0x2000 != 0,
		DontFragment:    flagsOff&0x4000 != 0,
		TTL:             b[8],
		HeaderLen:       ihl,
		TotalLen:        total,
		Payload:         b[ihl:total],
	}
	return p, nil
}

// MarshalFragment serialises one IPv4 fragment with a correct header
// checksum. It belongs to the fixture generator and to tests; the
// reassembly engine never calls it.
func MarshalFragment(src, dst netip.Addr, proto byte, id uint16, offUnits uint16, more bool, ttl uint8, payload []byte) ([]byte, error) {
	if offUnits > 0x1fff {
		return nil, fmt.Errorf("fragment offset %d exceeds 13-bit field", offUnits)
	}
	total := 20 + len(payload)
	if total > 65535 {
		return nil, fmt.Errorf("total length %d exceeds 65535", total)
	}
	if ttl == 0 {
		ttl = 64
	}
	h := make([]byte, 20, total)
	h[0] = 0x45
	binary.BigEndian.PutUint16(h[2:4], uint16(total))
	binary.BigEndian.PutUint16(h[4:6], id)
	fo := offUnits & 0x1fff
	if more {
		fo |= 0x2000
	}
	binary.BigEndian.PutUint16(h[6:8], fo)
	h[8] = ttl
	h[9] = proto
	s4 := src.As4()
	d4 := dst.As4()
	copy(h[12:16], s4[:])
	copy(h[16:20], d4[:])
	binary.BigEndian.PutUint16(h[10:12], checksum(h))
	return append(h, payload...), nil
}

// checksum computes the RFC 791 header checksum.
func checksum(h []byte) uint16 {
	var sum uint32
	for i := 0; i+1 < len(h); i += 2 {
		sum += uint32(binary.BigEndian.Uint16(h[i : i+2]))
	}
	if len(h)%2 == 1 {
		sum += uint32(h[len(h)-1]) << 8
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return ^uint16(sum)
}
