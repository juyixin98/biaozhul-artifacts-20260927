// Package flow defines the network model: a transport-layer five-tuple and the
// canonical, hashable flow identity. Parsing normalizes addresses so the same
// logical flow always produces the same key regardless of textual spelling
// (IPv6 compression, leading zeros, ...).
package flow

import (
	"fmt"
	"net/netip"
	"strconv"
	"strings"

	"flowrouter/internal/apperr"
	"flowrouter/internal/hashx"
)

// Protocols supported with port-bearing semantics. Numeric IP protocol numbers
// mirror IANA assignments; unknown protocols are rejected with INVALID_INPUT.
const (
	ProtoTCP  = 6
	ProtoUDP  = 17
	ProtoSCTP = 132
)

// FiveTuple is the routing identity of a packet flow.
type FiveTuple struct {
	SrcIP   netip.Addr `json:"-"`
	DstIP   netip.Addr `json:"-"`
	Proto   uint8      `json:"proto"`
	SrcPort uint16     `json:"src_port"`
	DstPort uint16     `json:"dst_port"`
}

// protoName maps a protocol number to its canonical lowercase name used in
// canonical keys. Unknown numbers fall back to "p<n>".
func protoName(proto uint8) string {
	switch proto {
	case ProtoTCP:
		return "tcp"
	case ProtoUDP:
		return "udp"
	case ProtoSCTP:
		return "sctp"
	default:
		return "p" + strconv.Itoa(int(proto))
	}
}

// portBearing reports whether the protocol carries source/destination ports.
// Non-port protocols must supply port 0, which keeps tuples unambiguous.
func portBearing(proto uint8) bool {
	return proto == ProtoTCP || proto == ProtoUDP || proto == ProtoSCTP
}

// Parse builds a FiveTuple from textual parts, normalizing both addresses with
// netip. Addresses must be plain IP literals; hostnames are rejected because a
// name that resolves to two addresses would silently alias two flows.
func Parse(srcIP, dstIP string, proto uint8, srcPort, dstPort uint16) (FiveTuple, error) {
	var ft FiveTuple
	s, err := netip.ParseAddr(srcIP)
	if err != nil {
		return ft, apperr.Invalid("BAD_SRC_IP",
			fmt.Sprintf("invalid source IP %q: %v", srcIP, err)).WithCause(err)
	}
	d, err := netip.ParseAddr(dstIP)
	if err != nil {
		return ft, apperr.Invalid("BAD_DST_IP",
			fmt.Sprintf("invalid destination IP %q: %v", dstIP, err)).WithCause(err)
	}
	if s.Is4() != d.Is4() {
		return ft, apperr.Invalid("IP_FAMILY_MISMATCH",
			"source and destination IP must belong to the same address family")
	}
	if !portBearing(proto) {
		if srcPort != 0 || dstPort != 0 {
			return ft, apperr.Invalid("PORTS_FOR_PROTO",
				fmt.Sprintf("protocol %d carries no ports; set both ports to 0", proto))
		}
	}
	ft.SrcIP = s
	ft.DstIP = d
	ft.Proto = proto
	ft.SrcPort = srcPort
	ft.DstPort = dstPort
	return ft, nil
}

// CanonicalKey is the single canonical text identity of the flow. It is what
// gets persisted, logged and hashed:
//
//	tcp|10.0.0.1:12345|10.0.0.2:80
//
// netip's String() is itself canonical (compressed IPv6, zone retained), which
// makes the key stable across equivalent textual inputs.
func (f FiveTuple) CanonicalKey() string {
	var b strings.Builder
	b.Grow(64)
	b.WriteString(protoName(f.Proto))
	b.WriteByte('|')
	b.WriteString(f.SrcIP.String())
	b.WriteByte(':')
	b.WriteString(strconv.Itoa(int(f.SrcPort)))
	b.WriteByte('|')
	b.WriteString(f.DstIP.String())
	b.WriteByte(':')
	b.WriteString(strconv.Itoa(int(f.DstPort)))
	return b.String()
}

// String is the display form; identical to CanonicalKey by design so logs and
// API payloads cannot disagree about a flow's identity.
func (f FiveTuple) String() string { return f.CanonicalKey() }

// Hash returns the routing hash ordinal of the flow.
func (f FiveTuple) Hash() uint64 { return hashx.FlowHash(f.CanonicalKey()) }

// MarshalJSON emits addresses as strings rather than dropping them.
func (f FiveTuple) MarshalJSON() ([]byte, error) {
	return []byte(fmt.Sprintf(`{"src_ip":%q,"dst_ip":%q,"proto":%d,"src_port":%d,"dst_port":%d,"key":%q}`,
		f.SrcIP.String(), f.DstIP.String(), f.Proto, f.SrcPort, f.DstPort, f.CanonicalKey())), nil
}
