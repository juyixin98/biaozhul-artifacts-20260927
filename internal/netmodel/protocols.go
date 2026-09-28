package netmodel

import (
	"fmt"
	"math/big"
)

// Family identifies an IP address family. They are modeled and analyzed
// independently.
type Family string

const (
	FamilyV4 Family = "ipv4"
	FamilyV6 Family = "ipv6"
)

// Protocol descriptor. CarriesPorts tells whether source/destination ports
// participate in matching; FixedFamily restricts the rule to one IP family.
type Protocol struct {
	Name         string
	Number       int
	CarriesPorts bool
	Family       Family // "" means usable in both families
}

func (p Protocol) String() string { return p.Name }

// Known protocols. The catalog deliberately stays small; "any" is handled
// specially (it matches every IP protocol number, including unknown ones).
var (
	ProtoTCP    = Protocol{"tcp", 6, true, ""}
	ProtoUDP    = Protocol{"udp", 17, true, ""}
	ProtoSCTP   = Protocol{"sctp", 132, true, ""}
	ProtoICMP   = Protocol{"icmp", 1, false, FamilyV4}
	ProtoICMPv6 = Protocol{"icmpv6", 58, false, FamilyV6}
)

// namedProtocols is the lookup table for textual protocol identifiers.
var namedProtocols = map[string]Protocol{
	"tcp":    ProtoTCP,
	"udp":    ProtoUDP,
	"sctp":   ProtoSCTP,
	"icmp":   ProtoICMP,
	"icmpv6": ProtoICMPv6,
}

// ParseProtocolSpec parses one protocol token from a rule.
//
//   - "any" is the wildcard: present in every rule; its protocol set is the
//     whole 0..255 domain so genuinely unknown numeric protocols are covered.
//   - known names ("tcp", "udp", "sctp", "icmp", "icmpv6") resolve to their
//     IANA numbers.
//   - decimal numbers 0..255 are accepted directly. Numbers with no known
//     name are valid (packets on the wire can carry them) but flagged as
//     uncertain, since their semantics (ports etc.) are not modeled.
//
// The returned Protocol has Family set when a named protocol is family-bound;
// numeric protocols and "any" carry Family "". unknown=false only for names
// found in the catalog; reserved/unknown numbers return unknown=true so the
// caller can surface an uncertainty note instead of guessing.
func ParseProtocolSpec(tok string) (p Protocol, unknown bool, err error) {
	if tok == "any" {
		return Protocol{Name: "any", Number: -1, CarriesPorts: false, Family: ""}, false, nil
	}
	if pr, ok := namedProtocols[tok]; ok {
		return pr, false, nil
	}
	n, perr := parseDecimalUint(tok, BitsProto)
	if perr != nil {
		return Protocol{}, false, fmt.Errorf("unknown protocol %q: not a known name (tcp, udp, sctp, icmp, icmpv6, any) nor a number 0..255", tok)
	}
	for _, pr := range namedProtocols {
		if pr.Number == n {
			// e.g. user wrote "6"; return canonical descriptor
			return pr, false, nil
		}
	}
	return Protocol{Name: fmt.Sprintf("%d", n), Number: n, CarriesPorts: false, Family: ""}, true, nil
}

func parseDecimalUint(s string, bits int) (int, error) {
	if s == "" {
		return -1, fmt.Errorf("empty number")
	}
	v, ok := new(big.Int).SetString(s, 10)
	if !ok || v.Sign() < 0 {
		return -1, fmt.Errorf("not a non-negative integer: %q", s)
	}
	if v.BitLen() > bits {
		return -1, fmt.Errorf("%s does not fit in %d bits", s, bits)
	}
	return int(v.Int64()), nil
}

// ProtocolSet converts the protocol token resolution into a 1-D set over the
// 8-bit protocol domain. "any" yields the universe.
func ProtocolSet(tok string) (Int1D, Protocol, bool, error) {
	p, unknown, err := ParseProtocolSpec(tok)
	if err != nil {
		return Int1D{}, p, false, err
	}
	if tok == "any" {
		return Universe1D(BitsProto), p, false, nil
	}
	return MustRange1D(BitsProto, big.NewInt(int64(p.Number)), big.NewInt(int64(p.Number))), p, unknown, nil
}
