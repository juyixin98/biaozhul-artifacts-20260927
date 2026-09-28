package analyzer

import (
	"fmt"
	"sort"

	"fwrule/internal/config"
	"fwrule/internal/netmodel"
)

func (a *analyzer) fillReport(rep *Report, st []ruleState) {
	for _, r := range a.pol.Rules {
		s := &st[r.Index]
		sortCells(s.matched)
		sortCells(s.wins)

		sum := &RuleSummary{
			Index:     r.Index,
			RuleID:    r.ID,
			Action:    string(r.Action),
			Family:    r.Box.Fam.String(),
			Protocol:  r.Box.Proto.String(),
			Reachable: s.reachable(),
		}
		if !r.ProtocolKnown {
			sum.Uncertain = true
			sum.UncertainWhy = fmt.Sprintf(
				"protocol number %d is not in the local IANA registry; "+
					"geometry is exact for that number, but its port semantics are unknown",
				r.Box.Proto.Number)
			a.uncs = append(a.uncs, Uncertainty{
				Scope: "rule", RuleID: r.ID, Code: "UNKNOWN_PROTOCOL_NUMBER",
				Detail: sum.UncertainWhy,
			})
		}

		var diags []Diagnostic

		switch {
		case r.MatchEmpty:
			sum.Kinds = []Category{CatEmptyMatch}
			// An empty-match rule is trivially removable: it never decides a
			// packet, and no packet's runner-up chain includes it either.
			sum.Removable = true
			diags = append(diags, Diagnostic{
				RuleID: r.ID, Index: r.Index, Kind: CatEmptyMatch,
				Detail:    r.EmptyReason,
				Removable: true,
			})
		case s.fullyShadowed():
			sum.Kinds = []Category{CatFullShadow}
			cover, truncated := a.buildCover(s.matched, nil, true)
			sum.Removable = true
			d := Diagnostic{
				RuleID: r.ID, Index: r.Index, Kind: CatFullShadow,
				Detail: fmt.Sprintf(
					"rule never becomes first-match: all %d packet-space region(s) "+
						"it covers are decided by earlier rules", len(s.matched)),
				Removable:           true,
				Partitions:          cover,
				TruncatedPartitions: truncated,
			}
			if len(cover) > 0 {
				w := cover[0].Witness
				d.Witness = &w
			}
			diags = append(diags, d)
		default:
			// Reachable rule.
			if len(s.matched) > len(s.wins) {
				sum.Kinds = append(sum.Kinds, CatPartialShadow)
				stolen := minusCells(s.matched, s.wins)
				cover, truncated := a.buildCover(stolen, s.wins, false)
				detail := fmt.Sprintf(
					"%d of %d covered region(s) are decided by earlier rules; "+
						"%d region(s) still reach this rule",
					len(stolen), len(s.matched), len(s.wins))
				d := Diagnostic{
					RuleID: r.ID, Index: r.Index, Kind: CatPartialShadow,
					Detail: detail, Partitions: cover,
					TruncatedPartitions: truncated,
				}
				if len(cover) > 0 {
					w := cover[0].Witness
					d.Witness = &w
				}
				diags = append(diags, d)
			}
			rem, why := s.removable(a.pol)
			if rem {
				sum.Kinds = append(sum.Kinds, CatRedundant)
				sum.Removable = true
				cover, truncated := a.buildCover(s.wins, nil, false)
				detail := "deleting this rule changes no packet decision: " +
					"every region it wins is decided identically by the next " +
					"matching rule or the default action"
				d := Diagnostic{
					RuleID: r.ID, Index: r.Index, Kind: CatRedundant,
					Detail: detail, Removable: true,
					Partitions: cover, TruncatedPartitions: truncated,
				}
				if len(cover) > 0 {
					w := cover[0].Witness
					d.Witness = &w
				}
				diags = append(diags, d)
			} else if why != "" {
				a.uncs = append(a.uncs, Uncertainty{
					Scope: "rule", RuleID: r.ID, Code: "REDUNDANCY_UNPROVABLE",
					Detail: why,
				})
			}
		}

		rep.Diagnostics = append(rep.Diagnostics, diags...)
		rep.Rules = append(rep.Rules, sum)
	}

	// Stable output order: by rule index within each kind's section, then
	// diagnostics in index order already; enforce explicitly.
	sort.SliceStable(rep.Diagnostics, func(i, j int) bool {
		if rep.Diagnostics[i].Index != rep.Diagnostics[j].Index {
			return rep.Diagnostics[i].Index < rep.Diagnostics[j].Index
		}
		return kindRank(rep.Diagnostics[i].Kind) < kindRank(rep.Diagnostics[j].Kind)
	})
}

func kindRank(k Category) int {
	switch k {
	case CatEmptyMatch:
		return 0
	case CatFullShadow:
		return 1
	case CatPartialShadow:
		return 2
	case CatRedundant:
		return 3
	}
	return 4
}

// minusCells returns cells in a not equal to any cell in b.
func minusCells(a, b []cellRec) []cellRec {
	present := make(map[gkey]map[netmodel.Cell]bool, len(b))
	for _, x := range b {
		m := present[x.key]
		if m == nil {
			m = map[netmodel.Cell]bool{}
			present[x.key] = m
		}
		m[x.cell] = true
	}
	var out []cellRec
	for _, x := range a {
		if m := present[x.key]; m != nil && m[x.cell] {
			continue
		}
		out = append(out, x)
	}
	return out
}

const defaultLabelPrefix = "<default:"

func (a *analyzer) decisionLabel(ruleIdx int, def config.Action) (string, string) {
	if ruleIdx >= 0 {
		return a.pol.Rules[ruleIdx].ID, string(a.pol.Rules[ruleIdx].Action)
	}
	return defaultLabelPrefix + string(def) + ">", string(def)
}

func (a *analyzer) makeWitness(rec cellRec) WitnessJSON {
	pkt := rec.cell.FirstPacket()
	label, act := a.decisionLabel(rec.winner, rec.defaultAction)
	return WitnessJSON{
		Family:    rec.cell.Fam.String(),
		Protocol:  pkt.ProtoNum,
		ProtoName: netmodel.ProtoName(pkt.ProtoNum),
		SrcIP:     pkt.SrcIP.String(rec.cell.Fam),
		SrcPort:   pkt.SrcPort,
		DstIP:     pkt.DstIP.String(rec.cell.Fam),
		DstPort:   pkt.DstPort,
		DecidedBy: label,
		Action:    act,
	}
}

// buildCover renders cell records as exact, human-checkable coverage
// rectangles, merging adjacent cells that share the same winner. Cells from
// `winCells` (when non-nil) describe regions the rule still reaches and are
// tagged for contrast in partial-shadow evidence. The returned slice is capped
// at maxCoverPartitions, largest regions first.
func (a *analyzer) buildCover(records, winCells []cellRec, allStolen bool) ([]CoverPartition, bool) {
	if len(records) == 0 {
		return nil, false
	}
	// Group by winner so merging never crosses decision boundaries.
	byWinner := map[int][]cellRec{}
	var order []int
	for _, rec := range records {
		if _, ok := byWinner[rec.winner]; !ok {
			order = append(order, rec.winner)
		}
		byWinner[rec.winner] = append(byWinner[rec.winner], rec)
	}
	sort.Ints(order)

	var parts []CoverPartition
	for _, w := range order {
		recs := byWinner[w]
		sortCells(recs)
		merged := mergeCells(recs)
		label, act := a.decisionLabel(w, recs[0].defaultAction)
		for _, m := range merged {
			rec := cellRec{
				key: m.key, cell: m.cell, winner: m.winner,
				runner: m.runner, defaultAction: m.defaultAction,
			}
			wit := a.makeWitness(rec)
			p := CoverPartition{
				SrcCIDR:     m.cell.SrcNet.String(),
				DstCIDR:     m.cell.DstNet.String(),
				PacketCount: m.cell.Volume().String(),
				Witness:     wit,
				WinningRule: label,
			}
			if m.cell.ProtoNum == 6 || m.cell.ProtoNum == 17 {
				p.SrcPorts = m.cell.SrcPorts.String()
				p.DstPorts = m.cell.DstPorts.String()
			}
			_ = act
			parts = append(parts, p)
		}
	}

	// Largest regions first for readability; deterministic tiebreak.
	sort.SliceStable(parts, func(i, j int) bool {
		bi, bj := parts[i].PacketCount, parts[j].PacketCount
		if bi != bj {
			return len(bi) > len(bj) || len(bi) == len(bj) && bi > bj
		}
		return coverLess(parts[i], parts[j])
	})
	truncated := false
	if len(parts) > maxCoverPartitions {
		parts = parts[:maxCoverPartitions]
		truncated = true
	}
	return parts, truncated
}

func coverLess(a, b CoverPartition) bool {
	if a.SrcCIDR != b.SrcCIDR {
		return a.SrcCIDR < b.SrcCIDR
	}
	if a.DstCIDR != b.DstCIDR {
		return a.DstCIDR < b.DstCIDR
	}
	if a.DstPorts != b.DstPorts {
		return a.DstPorts < b.DstPorts
	}
	return a.SrcPorts < b.SrcPorts
}

// mergedRec pairs a merged cell with its original first-match metadata.
type mergedRec struct {
	cell          netmodel.Cell
	winner        int
	runner        int
	defaultAction config.Action
	key           gkey
}

// mergeCells combines cells that differ only by adjacent src/dst port
// intervals (keeping address blocks separate to retain CIDR-exact evidence).
// Records share one winner by construction of the caller's grouping.
func mergeCells(recs []cellRec) []mergedRec {
	var out []mergedRec
	flush := func(rec cellRec) {
		out = append(out, mergedRec{
			cell: rec.cell, winner: rec.winner, runner: rec.runner,
			defaultAction: rec.defaultAction, key: rec.key,
		})
	}
	for i, rec := range recs {
		if i == 0 {
			flush(rec)
			continue
		}
		prev := &out[len(out)-1]
		portProto := rec.cell.ProtoNum == 6 || rec.cell.ProtoNum == 17
		sameGeom := rec.cell.Fam == prev.cell.Fam &&
			rec.cell.ProtoNum == prev.cell.ProtoNum &&
			rec.cell.SrcNet == prev.cell.SrcNet &&
			rec.cell.DstNet == prev.cell.DstNet
		if portProto && sameGeom &&
			rec.cell.SrcPorts == prev.cell.SrcPorts &&
			uint32(prev.cell.DstPorts.Hi)+1 == uint32(rec.cell.DstPorts.Lo) {
			prev.cell.DstPorts.Hi = rec.cell.DstPorts.Hi
			continue
		}
		if portProto && sameGeom &&
			rec.cell.DstPorts == prev.cell.DstPorts &&
			uint32(prev.cell.SrcPorts.Hi)+1 == uint32(rec.cell.SrcPorts.Lo) {
			prev.cell.SrcPorts.Hi = rec.cell.SrcPorts.Hi
			continue
		}
		flush(rec)
	}
	return out
}
