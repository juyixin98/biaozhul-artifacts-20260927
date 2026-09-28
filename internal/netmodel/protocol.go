package netmodel

import (
	"fmt"
	"strconv"
	"strings"
)

// ProtocolKind classifies how a rule's protocol field is modeled.
type ProtocolKind uint8

const (
	// ProtoConcrete: one fixed IP protocol number (tcp/udp/icmp/icmpv6 or a
	// known/unknown numeric value like 41 or 99).
	ProtoConcrete ProtocolKind = iota
	// ProtoAny: the rule matches every protocol. Port fields are forbidden.
	ProtoAny
)

// Protocol is a parsed rule protocol selector.
type Protocol struct {
	Kind   ProtocolKind
	Number uint8 // concrete only
	// KnownName is true when the number resolves to a name in the local IANA
	// registry (tcp, udp, icmp, icmpv6, gre, ...). An unregistered number
	// (e.g. 99) is still a perfectly concrete protocol; analysis proceeds but
	// the rule and every decision touching it are flagged uncertain.
	KnownName bool
	Name      string // canonical name ("tcp", ...) or ""
}

// Well-known protocols from the local registry. Only protocols the policy
// language names are required; everything else can be supplied by number.
var namedProtocols = map[string]uint8{
	"icmp":   1,
	"igmp":   2,
	"tcp":    6,
	"udp":    17,
	"gre":    47,
	"esp":    50,
	"ah":     51,
	"icmpv6": 58,
	"sctp":   132,
}

var numberToName = func() map[uint8]string {
	m := make(map[uint8]string, len(namedProtocols))
	for n, num := range namedProtocols {
		m[num] = n
	}
	return m
}()

// PortBearing reports whether this protocol selector can carry src/dst port
// intervals. Only TCP and UDP do.
func (p Protocol) PortBearing() bool {
	return p.Kind == ProtoConcrete && (p.Number == 6 || p.Number == 17)
}

// ParseProtocol accepts "any", a registry name, or a number 0-255.
//
// Unknown *names* are a hard parse error (UNKNOWN_PROTOCOL) — a typo must not
// silently become a broad rule. Unknown *numbers* are accepted but flagged via
// KnownName=false, so semantics are explicit rather than guessed.
func ParseProtocol(s string) (Protocol, error) {
	q := strings.TrimSpace(strings.ToLower(s))
	if q == "any" || q == "*" || q == "all" {
		return Protocol{Kind: ProtoAny}, nil
	}
	if num, ok := namedProtocols[q]; ok {
		return Protocol{Kind: ProtoConcrete, Number: num, KnownName: true, Name: q}, nil
	}
	if n, err := strconv.ParseUint(q, 10, 8); err == nil {
		return Protocol{
			Kind:      ProtoConcrete,
			Number:    uint8(n),
			KnownName: numberToName[uint8(n)] != "",
			Name:      numberToName[uint8(n)],
		}, nil
	}
	return Protocol{}, fmt.Errorf("unknown protocol name %q (use a registry name, a number 0-255, or \"any\")", s)
}

func (p Protocol) String() string {
	switch p.Kind {
	case ProtoAny:
		return "any"
	default:
		if p.Name != "" {
			return p.Name
		}
		return strconv.Itoa(int(p.Number))
	}
}

// ProtocolNumbers is the concrete numeric protocol (for witness packets).
func (p Protocol) ProtocolNumbers() uint8 { return p.Number }

// ProtoName returns the registry name of a numeric protocol ("" if unknown).
func ProtoName(num uint8) string { return numberToName[num] }

// IsKnownNumber reports whether num resolves in the local registry.
func IsKnownNumber(num uint8) bool { _, ok := numberToName[num]; return ok }
