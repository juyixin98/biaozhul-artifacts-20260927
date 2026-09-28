package analyzer

import (
	"fmt"

	"fwrule/internal/config"
	"fwrule/internal/netmodel"
)

// maxCoverPartitions bounds the cover detail returned per diagnostic. The
// analysis itself is exhaustive; only the rendered witness list is capped.
const maxCoverPartitions = 100

type gkey struct {
	fam   netmodel.Family
	proto uint8 // concrete protocol number carried by the group's cells
}

type cellRec struct {
	key    gkey
	cell   netmodel.Cell
	winner int // first-matching rule index; -1 when no rule matches
	// runner is the decision after the winner is deleted: second-matching
	// rule index, or -1 when no other rule matches (the default decides).
	runner int
	// defaultAction is the policy default for the cell family; "" when the
	// family default is not configured.
	defaultAction config.Action
}

func (a *analyzer) run() *Report {
	a.stats = map[string]int{}
	report := &Report{
		PolicyName:    a.pol.Name,
		PolicyVersion: a.version,
		Default:       map[string]string{},
		RuleCount:     len(a.pol.Rules),
		Stats:         a.stats,
	}
	for fam, act := range a.pol.Default {
		report.Default[fam.String()] = string(act)
	}
	for fam := range map[netmodel.Family]bool{netmodel.FamV4: true, netmodel.FamV6: true} {
		if _, ok := a.pol.Default[fam]; !ok {
			a.uncs = append(a.uncs, Uncertainty{
				Scope: "family",
				Code:  "DEFAULT_NOT_CONFIGURED",
				Detail: fmt.Sprintf(
					"no default action configured for %s; replay of unmatched %s packets returns an error",
					fam, fam),
			})
		}
	}

	n := len(a.pol.Rules)
	nonEmpty := make([]bool, n)
	byFamily := map[netmodel.Family][]*config.CompiledRule{}
	for i, r := range a.pol.Rules {
		nonEmpty[i] = !r.MatchEmpty
		if !r.MatchEmpty {
			byFamily[r.Box.Fam] = append(byFamily[r.Box.Fam], r)
		}
	}

	// ruleState[i] is accumulated from every cell that rule i can match.
	st := make([]ruleState, n)
	for i := range st {
		st[i].init(i)
	}

	// Build groups per family.
	//
	// Exactness argument: a rule's decision only depends on protocol, family,
	// src/dst address and (for tcp/udp) src/dst port. The address/port
	// partitions make every enumerated cell homogeneous for the whole rule
	// vector, so one first-match evaluation per cell decides the entire cell.
	//
	// Protocol dimension is finite (256 numbers + any):
	//  - each concrete protocol named by a rule gets one group enumerated with
	//    ALL family rules (concrete-same-proto and any-rules compete together
	//    in global order);
	//  - any-rules additionally match protocols nobody names concretely; we
	//    enumerate representative such protocols (canonical tcp/udp/icmp when
	//    unused concretely, plus one wholly unused number) to witness any-only
	//    coverage. Removability of an any-rule requires agreement across ALL
	//    these representatives (see fillReport).
	for _, fam := range []netmodel.Family{netmodel.FamV4, netmodel.FamV6} {
		rules := byFamily[fam]
		concreteProtos := map[uint8]bool{}
		hasAny := false
		for _, r := range rules {
			switch r.Box.Proto.Kind {
			case netmodel.ProtoConcrete:
				concreteProtos[r.Box.Proto.Number] = true
			default:
				hasAny = true
			}
		}
		for num := range concreteProtos {
			a.buildGroup(fam, num, rules, st)
		}
		if hasAny {
			anyWitness := map[uint8]bool{}
			for _, canon := range []uint8{6, 17, 1} {
				if !concreteProtos[canon] {
					anyWitness[canon] = true
				}
			}
			if other := smallestOtherProto(concreteProtos); other >= 0 {
				anyWitness[uint8(other)] = true
			}
			for num := range anyWitness {
				a.buildAnyGroup(fam, num, rules, st)
			}
		}
	}

	a.fillReport(report, st)
	report.Uncertainties = a.uncs
	return report
}

// buildGroup enumerates cells for one (family, concrete protocol) using every
// rule of that family, and records first-match outcomes.
func (a *analyzer) buildGroup(fam netmodel.Family, protoNum uint8,
	rules []*config.CompiledRule, st []ruleState) {

	var members []*config.CompiledRule
	var srcs, dsts []netmodel.CIDR
	var srcPorts, dstPorts []netmodel.PortInterval
	portBearing := protoNum == 6 || protoNum == 17
	for _, r := range rules {
		if r.MatchEmpty || r.Box.Fam != fam {
			continue
		}
		pk := r.Box.Proto.Kind == netmodel.ProtoConcrete && r.Box.Proto.Number == protoNum
		pa := r.Box.Proto.Kind == netmodel.ProtoAny
		if !pk && !pa {
			continue
		}
		members = append(members, r)
		srcs = append(srcs, r.Box.SrcNet)
		dsts = append(dsts, r.Box.DstNet)
		if portBearing {
			srcPorts = append(srcPorts, r.Box.SrcPorts)
			dstPorts = append(dstPorts, r.Box.DstPorts)
		}
	}
	if len(members) == 0 {
		return
	}
	a.enumerate(gkey{fam: fam, proto: protoNum}, members, srcs, dsts,
		srcPorts, dstPorts, portBearing, st)
}

// buildAnyGroup enumerates cells for a protocol that NO rule names
// concretely, so only any-rules can compete.
func (a *analyzer) buildAnyGroup(fam netmodel.Family, protoNum uint8,
	allRules []*config.CompiledRule, st []ruleState) {

	portBearing := protoNum == 6 || protoNum == 17
	var members []*config.CompiledRule
	var srcs, dsts []netmodel.CIDR
	var srcPorts, dstPorts []netmodel.PortInterval
	for _, r := range allRules {
		if r.MatchEmpty || r.Box.Fam != fam {
			continue
		}
		if r.Box.Proto.Kind != netmodel.ProtoAny {
			continue
		}
		members = append(members, r)
		srcs = append(srcs, r.Box.SrcNet)
		dsts = append(dsts, r.Box.DstNet)
		if portBearing {
			srcPorts = append(srcPorts, r.Box.SrcPorts)
			dstPorts = append(dstPorts, r.Box.DstPorts)
		}
	}
	if len(members) == 0 {
		return
	}
	a.enumerate(gkey{fam: fam, proto: protoNum}, members, srcs, dsts,
		srcPorts, dstPorts, portBearing, st)
}

func (a *analyzer) enumerate(key gkey, members []*config.CompiledRule,
	srcs, dsts []netmodel.CIDR, srcPorts, dstPorts []netmodel.PortInterval,
	portBearing bool, st []ruleState) {

	srcParts := netmodel.PartitionCIDRs(srcs)
	dstParts := netmodel.PartitionCIDRs(dsts)
	var srcPortParts, dstPortParts []netmodel.PortInterval
	if portBearing {
		srcPortParts = netmodel.PartitionIntervals(srcPorts)
		dstPortParts = netmodel.PartitionIntervals(dstPorts)
	} else {
		srcPortParts = []netmodel.PortInterval{netmodel.FullPorts}
		dstPortParts = []netmodel.PortInterval{netmodel.FullPorts}
	}
	a.stats["groups"]++
	a.stats["cells"] += len(srcParts) * len(dstParts) * len(srcPortParts) * len(dstPortParts)

	for _, sn := range srcParts {
		for _, dn := range dstParts {
			for _, sp := range srcPortParts {
				for _, dp := range dstPortParts {
					cell := netmodel.Cell{
						Fam:      key.fam,
						ProtoNum: key.proto,
						SrcNet:   sn,
						DstNet:   dn,
						SrcPorts: sp,
						DstPorts: dp,
					}
					winner, runner := -1, -1
					var matched []int
					for _, r := range members {
						if ruleMatchesCell(r, cell) {
							matched = append(matched, r.Index)
							if winner < 0 {
								winner = r.Index
							} else if runner < 0 {
								runner = r.Index
							}
						}
					}
					def, _ := a.pol.DefaultFor(key.fam)
					rec := cellRec{
						key:           key,
						cell:          cell,
						winner:        winner,
						runner:        runner,
						defaultAction: def,
					}
					a.record(rec, matched, st)
				}
			}
		}
	}
}

func ruleMatchesCell(r *config.CompiledRule, c netmodel.Cell) bool {
	if r.MatchEmpty || r.Box.Fam != c.Fam {
		return false
	}
	if r.Box.Proto.Kind == netmodel.ProtoConcrete && r.Box.Proto.Number != c.ProtoNum {
		return false
	}
	if !r.Box.SrcNet.Contains(c.SrcNet.First) || !r.Box.DstNet.Contains(c.DstNet.First) {
		return false
	}
	if c.ProtoNum == 6 || c.ProtoNum == 17 {
		if !r.Box.SrcPorts.Contains(c.SrcPorts.Lo) ||
			!r.Box.SrcPorts.Contains(c.SrcPorts.Hi) {
			return false
		}
		if !r.Box.DstPorts.Contains(c.DstPorts.Lo) ||
			!r.Box.DstPorts.Contains(c.DstPorts.Hi) {
			return false
		}
	}
	return true
}

func (a *analyzer) record(rec cellRec, matched []int, st []ruleState) {
	for _, ri := range matched {
		s := &st[ri]
		s.matched = append(s.matched, rec)
		if ri == rec.winner {
			s.wins = append(s.wins, rec)
		}
	}
}

// smallestOtherProto returns a protocol number (0..255) not used concretely
// by any rule, preferring named but-unused protocols; -1 only when all 256
// numbers are taken (impossible for realistic configs).
func smallestOtherProto(used map[uint8]bool) int {
	preference := []uint8{47, 50, 2, 132, 1, 6, 17, 0, 41}
	for _, num := range preference {
		if !used[num] {
			return int(num)
		}
	}
	for i := 0; i < 256; i++ {
		if !used[uint8(i)] {
			return i
		}
	}
	return -1
}
