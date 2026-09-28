// Package analyzer performs first-match shadowing analysis over validated
// rule sets.
//
// Semantics: rules are evaluated in configuration order. A packet matches
// the first rule whose region contains it; if no rule matches, the default
// action applies. Rules live in packet spaces per IP family; a rule that
// applies to both families is projected into both. The analysis uses exact
// set-difference on the geometric packet model (see package netmodel), never
// textual comparison.
package analyzer

import (
	"fmt"
	"math/big"
	"sort"

	"netsem/internal/config"
	"netsem/internal/netmodel"
)

// Diagnostic kind constants.
const (
	DiagFullShadow    = "fully_shadowed"     // rule decides no packet at all
	DiagPartialShadow = "partially_shadowed" // part of the rule is decided earlier
	DiagRedundant     = "redundant"          // deleting it changes no decision
)

// Region is one compiled rule in one family.
type Region struct {
	RuleIndex int
	RuleID    string
	Family    netmodel.Family
	Space     netmodel.Space
	Action    string
}

// Witness is a concrete packet proving a diagnosis.
type Witness struct {
	Family             string `json:"family"`
	Protocol           string `json:"protocol"`
	ProtocolNumber     int    `json:"protocol_number"`
	SourceAddress      string `json:"source_address"`
	DestinationAddress string `json:"destination_address"`
	SourcePort         int    `json:"source_port"`
	DestinationPort    int    `json:"destination_port"`
	MatchedRuleID      string `json:"matched_rule_id"`
}

// ProductDesc renders one axis-aligned product in human-readable form.
type ProductDesc struct {
	Protocols        string `json:"protocols"`
	Sources          string `json:"sources"`
	Destinations     string `json:"destinations"`
	SourcePorts      string `json:"source_ports"`
	DestinationPorts string `json:"destination_ports"`
	PacketCount      string `json:"packet_count"`
}

// PartitionPart is one disjoint coverage region in the explanation.
type PartitionPart struct {
	Family      string        `json:"family"`
	Products    []ProductDesc `json:"products"`
	PacketCount string        `json:"packet_count"` // decimal, may be huge (IPv6)
}

// Diagnostic is one finding.
type Diagnostic struct {
	Kind       string          `json:"kind"`
	RuleID     string          `json:"rule_id"`
	Family     string          `json:"family"`
	Detail     string          `json:"detail"`
	Witness    *Witness        `json:"witness,omitempty"`
	Covered    []PartitionPart `json:"covered_partition,omitempty"`
	ShadowedBy []string        `json:"shadowed_by,omitempty"`
}

// Report is the complete analysis result.
type Report struct {
	DefaultAction string       `json:"default_action"`
	Diagnostics   []Diagnostic `json:"diagnostics"`
	Regions       []Region     `json:"-"`
}

// Compile turns parsed rules into per-family regions, preserving order.
// Rules without a CIDR cover both families and are projected into each.
func Compile(rs *config.Ruleset) ([]Region, error) {
	var regions []Region
	families := []netmodel.Family{netmodel.FamilyV4, netmodel.FamilyV6}
	for i, r := range rs.Rules {
		fams := families
		if r.Family != "" {
			fams = []netmodel.Family{r.Family}
		}
		pset, _, _, err := netmodel.ProtocolSet(r.ProtoTok)
		if err != nil {
			return nil, fmt.Errorf("rule %s: %w", r.ID, err)
		}
		for _, fam := range fams {
			src, err := familyAxis(fam, r.Sources)
			if err != nil {
				return nil, fmt.Errorf("rule %s: %w", r.ID, err)
			}
			dst, err := familyAxis(fam, r.Dests)
			if err != nil {
				return nil, fmt.Errorf("rule %s: %w", r.ID, err)
			}
			// "protocol any" with a restricted port spec: port constraints
			// are meaningful only for port-bearing protocols (tcp/udp/sctp).
			// Such a rule therefore matches (a) port-bearing protocols on
			// the given port sets and (b) every non-port protocol with the
			// port axes collapsed to {0}. The config parser emits an
			// uncertainty note whenever this construction occurs.
			var prods []netmodel.Product
			zero := netmodel.Point1D(netmodel.BitsPort, big.NewInt(0))
			if r.ProtoTok == "any" && (!r.SrcPorts.Equals(netmodel.Universe1D(netmodel.BitsPort)) ||
				!r.DstPorts.Equals(netmodel.Universe1D(netmodel.BitsPort))) {
				portProtos := portBearingProtoSet()
				nonPort := netmodel.Universe1D(netmodel.BitsProto).Minus(portProtos)
				prods = append(prods, netmodel.Product{Proto: portProtos, Src: src, Dst: dst, SrcP: r.SrcPorts, DstP: r.DstPorts})
				if !nonPort.IsEmpty() {
					prods = append(prods, netmodel.Product{Proto: nonPort, Src: src, Dst: dst, SrcP: zero, DstP: zero})
				}
			} else {
				prods = []netmodel.Product{{Proto: pset, Src: src, Dst: dst, SrcP: r.SrcPorts, DstP: r.DstPorts}}
			}
			regions = append(regions, Region{
				RuleIndex: i,
				RuleID:    r.ID,
				Family:    fam,
				Space:     netmodel.Space{Family: fam, Products: prods},
				Action:    r.Action,
			})
		}
	}
	return regions, nil
}

func portBearingProtoSet() netmodel.Int1D {
	var segs []netmodel.Seg
	for _, n := range []int64{6, 17, 132} {
		segs = append(segs, netmodel.Seg{Lo: big.NewInt(n), Hi: big.NewInt(n)})
	}
	s, _ := netmodel.FromSegments(netmodel.BitsProto, segs)
	return s
}

// familyAxis turns parsed CIDRs into one address axis for the family; an
// empty list is the wildcard axis.
func familyAxis(fam netmodel.Family, cidrs []netmodel.CIDRInfo) (netmodel.Int1D, error) {
	if len(cidrs) == 0 {
		return netmodel.Universe1D(netmodel.AddressBits(fam)), nil
	}
	as, err := netmodel.AddrSetFromCIDRs(cidrs)
	if err != nil {
		return netmodel.Int1D{}, err
	}
	if as.Family != fam {
		return netmodel.Int1D{}, fmt.Errorf("address family %s cannot be projected into %s", as.Family, fam)
	}
	return as.Vals, nil
}

// Analyze compiles and evaluates the ruleset independently per family.
func Analyze(rs *config.Ruleset) (*Report, error) {
	regions, err := Compile(rs)
	if err != nil {
		return nil, err
	}
	rep := &Report{DefaultAction: rs.DefaultAction, Regions: regions}

	for _, fam := range []netmodel.Family{netmodel.FamilyV4, netmodel.FamilyV6} {
		var fr []Region
		for _, rg := range regions {
			if rg.Family == fam {
				fr = append(fr, rg)
			}
		}
		// Incremental union of all earlier rule regions in this family.
		earlierUnion := netmodel.EmptySpace(fam)
		var ordered []Region // earlier rules in configuration order
		for _, rg := range fr {
			// Effective region: packets no earlier rule reaches. Those are
			// exactly the packets this rule decides under first-match.
			eff := rg.Space.Minus(earlierUnion)
			lost := rg.Space.Minus(eff)

			if eff.IsEmpty() {
				w, _ := firstWitness(rg.Space, ordered)
				rep.Diagnostics = append(rep.Diagnostics, Diagnostic{
					Kind:       DiagFullShadow,
					RuleID:     rg.RuleID,
					Family:     string(fam),
					Detail:     "every packet the rule matches is already decided by an earlier rule; it can never take effect",
					Witness:    w,
					ShadowedBy: intersectingRuleIDs(rg.Space, ordered),
				})
			} else if !lost.IsEmpty() {
				w, _ := firstWitness(lost, ordered)
				rep.Diagnostics = append(rep.Diagnostics, Diagnostic{
					Kind:       DiagPartialShadow,
					RuleID:     rg.RuleID,
					Family:     string(fam),
					Detail:     "part of the rule is decided by earlier rules and never reaches it; the covered partition shows what the rule still effectively governs",
					Witness:    w,
					ShadowedBy: intersectingRuleIDs(lost, ordered),
					Covered:    describePartition(eff),
				})
			}

			// Redundancy: delete the rule and compare decisions over eff.
			// A packet in eff is, after deletion, decided by the first LATER
			// rule that contains it, or by the default action.
			later := laterInFamily(regions, fam, rg.RuleIndex)
			flips := flipRegion(eff, later, rg.Action, rs.DefaultAction)
			if !eff.IsEmpty() && flips.IsEmpty() {
				w, _ := firstWitness(eff, nil)
				rep.Diagnostics = append(rep.Diagnostics, Diagnostic{
					Kind:    DiagRedundant,
					RuleID:  rg.RuleID,
					Family:  string(fam),
					Detail:  redundancyDetail(rg.Action, rs.DefaultAction),
					Witness: w,
				})
			}

			earlierUnion = unionSpace(earlierUnion, rg.Space)
			ordered = append(ordered, rg)
		}
	}

	sort.SliceStable(rep.Diagnostics, func(i, j int) bool {
		a, b := rep.Diagnostics[i], rep.Diagnostics[j]
		if a.Family != b.Family {
			return a.Family < b.Family
		}
		if ruleIndex(rep, a.RuleID) != ruleIndex(rep, b.RuleID) {
			return ruleIndex(rep, a.RuleID) < ruleIndex(rep, b.RuleID)
		}
		return a.Kind < b.Kind
	})
	return rep, nil
}

func redundancyDetail(action, defAction string) string {
	if action == defAction {
		return fmt.Sprintf("deleting the rule changes no decision: every packet it decides is decided identically by a later same-action rule or by the %q default", defAction)
	}
	return "deleting the rule changes no decision: every packet it decides is also matched by a later rule with the same action"
}

func ruleIndex(rep *Report, id string) int {
	for _, r := range rep.Regions {
		if r.RuleID == id {
			return r.RuleIndex
		}
	}
	return 1<<31 - 1
}

func laterInFamily(all []Region, fam netmodel.Family, idx int) []Region {
	var out []Region
	for _, r := range all {
		if r.Family == fam && r.RuleIndex > idx {
			out = append(out, r)
		}
	}
	return out
}

func unionSpace(a, b netmodel.Space) netmodel.Space {
	diff := b.Minus(a)
	return netmodel.Space{Family: a.Family, Products: append(append([]netmodel.Product{}, a.Products...), diff.Products...)}
}

// firstWitness extracts a concrete packet from region and, if earlier rules
// are supplied, annotates which rule matches it first.
func firstWitness(region netmodel.Space, earlier []Region) (*Witness, []string) {
	w, ok := region.Witness()
	if !ok {
		return nil, nil
	}
	var first string
	for _, e := range earlier {
		if e.Space.Contains(w) {
			first = e.RuleID
			break
		}
	}
	return &Witness{
		Family:             string(w.Family),
		ProtocolNumber:     w.Proto,
		Protocol:           protoName(w.Proto),
		SourceAddress:      netmodel.IntToAddr(w.Family, w.SrcAddr).String(),
		DestinationAddress: netmodel.IntToAddr(w.Family, w.DstAddr).String(),
		SourcePort:         w.SrcPort,
		DestinationPort:    w.DstPort,
		MatchedRuleID:      first,
	}, nil
}

func intersectingRuleIDs(region netmodel.Space, rules []Region) []string {
	var ids []string
	for _, r := range rules {
		if !region.Intersect(r.Space).IsEmpty() {
			ids = append(ids, r.RuleID)
		}
	}
	return ids
}

func protoName(n int) string {
	switch n {
	case 6:
		return "tcp"
	case 17:
		return "udp"
	case 132:
		return "sctp"
	case 1:
		return "icmp"
	case 58:
		return "icmpv6"
	}
	return fmt.Sprintf("%d", n)
}

// flipRegion returns the packets in eff whose decision would change if the
// candidate rule were deleted. Walking later rules in order, the first later
// rule containing a packet is its post-deletion decider; packets left over
// fall through to the default.
func flipRegion(eff netmodel.Space, later []Region, action, defAction string) netmodel.Space {
	flips := netmodel.EmptySpace(eff.Family)
	remaining := eff
	for _, l := range later {
		piece := remaining.Intersect(l.Space)
		if !piece.IsEmpty() && l.Action != action {
			flips = unionSpace(flips, piece)
		}
		remaining = remaining.Minus(l.Space)
		if remaining.IsEmpty() {
			break
		}
	}
	// Leftover packets fall through to the default after deletion.
	if !remaining.IsEmpty() && defAction != action {
		flips = unionSpace(flips, remaining)
	}
	return flips
}

func describePartition(s netmodel.Space) []PartitionPart {
	if s.IsEmpty() {
		return nil
	}
	var pp []ProductDesc
	total := new(big.Int)
	for _, p := range s.Products {
		n := productCardinality(p)
		total.Add(total, n)
		pp = append(pp, ProductDesc{
			Protocols:        segList(p.Proto),
			Sources:          addrSegList(s.Family, p.Src),
			Destinations:     addrSegList(s.Family, p.Dst),
			SourcePorts:      segList(p.SrcP),
			DestinationPorts: segList(p.DstP),
			PacketCount:      n.String(),
		})
	}
	return []PartitionPart{{Family: string(s.Family), Products: pp, PacketCount: total.String()}}
}

func productCardinality(p netmodel.Product) *big.Int {
	n := big.NewInt(1)
	n.Mul(n, cardinality(p.Proto))
	n.Mul(n, cardinality(p.Src))
	n.Mul(n, cardinality(p.Dst))
	n.Mul(n, cardinality(p.SrcP))
	n.Mul(n, cardinality(p.DstP))
	return n
}

func cardinality(s netmodel.Int1D) *big.Int {
	n := new(big.Int)
	for _, seg := range s.Segments() {
		n.Add(n, new(big.Int).Add(new(big.Int).Sub(new(big.Int).Set(seg.Hi), seg.Lo), big.NewInt(1)))
	}
	return n
}

func segList(s netmodel.Int1D) string {
	if s.Equals(netmodel.Universe1D(s.Bits())) {
		return "*"
	}
	var b []string
	for _, seg := range s.Segments() {
		if seg.Lo.Cmp(seg.Hi) == 0 {
			b = append(b, seg.Lo.String())
		} else {
			b = append(b, seg.Lo.String()+"-"+seg.Hi.String())
		}
	}
	if len(b) == 0 {
		return "∅"
	}
	return joinComma(b)
}

func addrSegList(fam netmodel.Family, s netmodel.Int1D) string {
	if s.Equals(netmodel.Universe1D(s.Bits())) {
		return "*"
	}
	var b []string
	for _, seg := range s.Segments() {
		lo := netmodel.IntToAddr(fam, seg.Lo)
		hi := netmodel.IntToAddr(fam, seg.Hi)
		if lo == hi {
			b = append(b, lo.String())
		} else {
			b = append(b, lo.String()+".."+hi.String())
		}
	}
	return joinComma(b)
}

func joinComma(b []string) string {
	out := ""
	for i, x := range b {
		if i > 0 {
			out += ", "
		}
		out += x
	}
	return out
}
