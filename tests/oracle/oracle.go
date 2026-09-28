// Package oracle is an INDEPENDENT reference implementation of firewall
// first-match semantics. It deliberately shares nothing with the analyzer's
// partition machinery: instead of geometry it enumerates every concrete
// packet of a small address/port space and scans rules linearly. Test
// suites compare its brute-force verdicts against the analyzer's verdicts
// and against the analyzer's removability claims.
//
// Because it is written from the spec (not from the analyzer code) and only
// uses config + netmodel parsing, it is the cross-check that "the core does
// not grade its own homework".
package oracle

import (
	"fmt"

	"fwrule/internal/config"
	"fwrule/internal/netmodel"
)

// Packet is a concrete coordinate inside an enumerated mini space.
type Packet struct {
	ProtoNum uint8
	SrcIP    uint32
	DstIP    uint32
	SrcPort  uint16
	DstPort  uint16
}

func (p Packet) String() string {
	return fmt.Sprintf("proto=%d %d:%d -> %d:%d",
		p.ProtoNum, p.SrcIP, p.SrcPort, p.DstIP, p.DstPort)
}

// Outcome is the first-match result for one packet.
type Outcome struct {
	Action  config.Action // "allow"/"deny"/"" when no default for family
	Winner  int           // winning rule index, -1 for default/error
	Default bool
}

// Space describes the enumerated packet universe.
type Space struct {
	SrcIPs []uint32
	DstIPs []uint32
	Ports  []uint16
	Protos []uint8 // 6 and 17 get port coordinates; others do not
	// ExtraIPs are source/destination addresses OUTSIDE every rule's blocks,
	// included to exercise default-action fallback (they occupy both roles).
	OutsideIPs []uint32
}

// Decide evaluates one packet by linear rule scan — the textbook definition
// of first-match, independently of any partition code. Winner is a RULE
// SLICE POSITION (identical to rule index for a full policy; relative after
// deletion via WithoutPolicy).
func Decide(pol *config.Policy, pkt Packet) Outcome {
	m := Matching(pol, pkt)
	if len(m) > 0 {
		return Outcome{Action: pol.Rules[m[0]].Action, Winner: m[0]}
	}
	def, ok := pol.DefaultFor(netmodel.FamV4)
	if !ok {
		return Outcome{Winner: -1}
	}
	return Outcome{Action: def, Winner: -1, Default: true}
}

// Matching returns the slice positions of ALL rules that match pkt, in policy
// order. The first entry is the first-match winner; the rest are
// shadowed-but-matching.
func Matching(pol *config.Policy, pkt Packet) []int {
	var matches []int
	for pos, r := range pol.Rules {
		if r.MatchEmpty {
			continue
		}
		b := r.Box
		if b.Fam != netmodel.FamV4 {
			continue
		}
		if b.Proto.Kind == netmodel.ProtoConcrete && b.Proto.Number != pkt.ProtoNum {
			continue
		}
		src := netmodel.Addr{L: uint64(pkt.SrcIP)}
		dst := netmodel.Addr{L: uint64(pkt.DstIP)}
		if !b.SrcNet.Contains(src) || !b.DstNet.Contains(dst) {
			continue
		}
		if pkt.ProtoNum == 6 || pkt.ProtoNum == 17 {
			if !b.SrcPorts.Contains(pkt.SrcPort) || !b.DstPorts.Contains(pkt.DstPort) {
				continue
			}
		}
		matches = append(matches, pos)
	}
	return matches
}

// Enumerate returns every packet of the space (Cartesian product).
func Enumerate(sp Space) []Packet {
	srcs := append(append([]uint32{}, sp.SrcIPs...), sp.OutsideIPs...)
	dsts := append(append([]uint32{}, sp.DstIPs...), sp.OutsideIPs...)
	var out []Packet
	for _, proto := range sp.Protos {
		for _, s := range srcs {
			for _, d := range dsts {
				if proto == 6 || proto == 17 {
					for _, spPort := range sp.Ports {
						for _, dpPort := range sp.Ports {
							out = append(out, Packet{
								ProtoNum: proto, SrcIP: s, DstIP: d,
								SrcPort: spPort, DstPort: dpPort,
							})
						}
					}
				} else {
					out = append(out, Packet{
						ProtoNum: proto, SrcIP: s, DstIP: d,
					})
				}
			}
		}
	}
	return out
}

// DecisionMap is packet -> outcome.
type DecisionMap struct {
	Verdict map[Packet]Outcome
}

// EvalAll evaluates every packet of the space.
func EvalAll(pol *config.Policy, sp Space) (*DecisionMap, []Packet) {
	pkts := Enumerate(sp)
	m := &DecisionMap{Verdict: make(map[Packet]Outcome, len(pkts))}
	for _, p := range pkts {
		m.Verdict[p] = Decide(pol, p)
	}
	return m, pkts
}

// WithoutPolicy returns a copy with the given rule indices deleted.
func WithoutPolicy(pol *config.Policy, remove map[int]bool) *config.Policy {
	out := &config.Policy{
		Name:    pol.Name,
		Default: pol.Default,
	}
	for _, r := range pol.Rules {
		if remove[r.Index] {
			continue
		}
		cp := *r
		// Original Index is preserved so winner identity stays comparable.
		out.Rules = append(out.Rules, &cp)
	}
	return out
}

// Equivalent reports whether two decision maps agree for every packet.
func Equivalent(a, b *DecisionMap) (bool, string, Packet) {
	for pkt, va := range a.Verdict {
		vb, ok := b.Verdict[pkt]
		if !ok {
			return false, "packet missing after removal", pkt
		}
		if va.Action != vb.Action {
			return false, fmt.Sprintf("action changed %q -> %q", va.Action, vb.Action), pkt
		}
	}
	return true, "", Packet{}
}
