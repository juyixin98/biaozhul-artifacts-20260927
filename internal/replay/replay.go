// Package replay evaluates concrete packets against compiled rule regions
// using strict first-match order and records every evaluation step, so API
// results are explainable and can be re-derived from the log.
package replay

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"math/big"
	"net/netip"

	"netsem/internal/analyzer"
	"netsem/internal/netmodel"
)

// PacketRequest is one packet to evaluate.
type PacketRequest struct {
	Family             string `json:"family"` // "ipv4" | "ipv6"
	Protocol           string `json:"protocol"`
	SourceAddress      string `json:"source_address"`
	DestinationAddress string `json:"destination_address"`
	SourcePort         *int   `json:"source_port,omitempty"`
	DestinationPort    *int   `json:"destination_port,omitempty"`
}

// Decision is the explainable evaluation result.
type Decision struct {
	RequestID      string        `json:"request_id"`
	Family         string        `json:"family"`
	Packet         PacketRequest `json:"packet"`
	Version        int64         `json:"version"`
	Decision       string        `json:"decision"` // "allow" | "deny"
	DecidedBy      string        `json:"decided_by"`
	MatchedRuleID  string        `json:"matched_rule_id,omitempty"`
	Steps          []Step        `json:"steps"`
	Certain        bool          `json:"certain"`
	Uncertainties  []string      `json:"uncertainties"`
	Errors         []string      `json:"errors"`
	Instance       string        `json:"instance,omitempty"`
	SourceLocation string        `json:"source_location,omitempty"`
}

// Step is one explainable evaluation step.
type Step struct {
	Order       int    `json:"order"`
	RuleID      string `json:"rule_id"`
	Matched     bool   `json:"matched"`
	Action      string `json:"action,omitempty"`
	Explanation string `json:"explanation"`
}

// Evaluator holds the compiled rules for one config version.
type Evaluator struct {
	Version       int64
	DefaultAction string
	Regions       []analyzer.Region
	// Per-rule metadata for explanations.
	Notes map[string][]string
}

// NewEvaluator groups compiled regions by family for ordered replay.
func NewEvaluator(version int64, defaultAction string, regions []analyzer.Region, notes map[string][]string) *Evaluator {
	return &Evaluator{Version: version, DefaultAction: defaultAction, Regions: regions, Notes: notes}
}

// NewRequestID returns a random correlation id.
func NewRequestID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return "req_" + hex.EncodeToString(b[:])
}

// Evaluate runs first-match over one packet. Validation failures are returned
// on the Decision as hard errors (the packet is denied, Certain=false) rather
// than as a Go error, so the HTTP layer can persist and explain them.
func (e *Evaluator) Evaluate(req PacketRequest) Decision {
	id := NewRequestID()
	d := Decision{RequestID: id, Packet: req, Version: e.Version, Certain: true, Steps: nil}

	fam := netmodel.Family(req.Family)
	if fam != netmodel.FamilyV4 && fam != netmodel.FamilyV6 {
		d.Decision = "deny"
		d.DecidedBy = "error"
		d.Certain = false
		d.Errors = append(d.Errors, fmt.Sprintf("family must be %q or %q, got %q", netmodel.FamilyV4, netmodel.FamilyV6, req.Family))
		return d
	}
	d.Family = string(fam)

	pset, proto, unknown, err := netmodel.ProtocolSet(req.Protocol)
	if err != nil {
		d.Decision = "deny"
		d.DecidedBy = "error"
		d.Certain = false
		d.Errors = append(d.Errors, "invalid protocol: "+err.Error())
		return d
	}
	if unknown {
		d.Uncertainties = append(d.Uncertainties, fmt.Sprintf("protocol %q is not in the named catalog (numeric %d); treated as a non-port protocol", req.Protocol, proto.Number))
	}

	addr, err := netip.ParseAddr(req.SourceAddress)
	if err != nil {
		d.Decision = "deny"
		d.DecidedBy = "error"
		d.Certain = false
		d.Errors = append(d.Errors, "invalid source_address: "+err.Error())
		return d
	}
	af := netmodel.FamilyV4
	if addr.Is6() {
		af = netmodel.FamilyV6
	}
	if af != fam {
		d.Decision = "deny"
		d.DecidedBy = "error"
		d.Certain = false
		d.Errors = append(d.Errors, fmt.Sprintf("source_address %s is %s but family is %s", req.SourceAddress, af, fam))
		return d
	}

	// Destination address.
	dstAddr, err := netip.ParseAddr(req.DestinationAddress)
	if err != nil {
		d.Decision = "deny"
		d.DecidedBy = "error"
		d.Certain = false
		d.Errors = append(d.Errors, "invalid destination_address: "+err.Error())
		return d
	}
	daf := netmodel.FamilyV4
	if dstAddr.Is6() {
		daf = netmodel.FamilyV6
	}
	if daf != fam {
		d.Decision = "deny"
		d.DecidedBy = "error"
		d.Certain = false
		d.Errors = append(d.Errors, fmt.Sprintf("destination_address %s is %s but family is %s", req.DestinationAddress, daf, fam))
		return d
	}

	// Port handling mirrors the compile-time semantics in analyzer.Compile.
	sp, dp := 0, 0
	portsApplicable := req.Protocol == "tcp" || req.Protocol == "udp" || req.Protocol == "sctp"
	if portsApplicable || req.Protocol == "any" {
		if req.SourcePort != nil {
			sp = *req.SourcePort
		}
		if req.DestinationPort != nil {
			dp = *req.DestinationPort
		}
		if (req.SourcePort != nil && (sp < 0 || sp > 65535)) || (req.DestinationPort != nil && (dp < 0 || dp > 65535)) {
			d.Decision = "deny"
			d.DecidedBy = "error"
			d.Certain = false
			d.Errors = append(d.Errors, "ports must be in 0..65535")
			return d
		}
	}
	pkt := netmodel.Packet{
		Family:  fam,
		Proto:   firstProto(pset),
		SrcAddr: addrInt(addr),
		DstAddr: addrInt(dstAddr),
		SrcPort: sp,
		DstPort: dp,
	}

	// Walk rules in global configuration order, considering only regions of
	// this family. Rules that project into both families appear once per
	// family but share the same RuleIndex; dedupe by index while preserving
	// order.
	seen := map[int]bool{}
	order := 0
	var famRegions []analyzer.Region
	for _, r := range e.Regions {
		if r.Family == fam && !seen[r.RuleIndex] {
			seen[r.RuleIndex] = true
			famRegions = append(famRegions, r)
		}
	}
	for _, r := range famRegions {
		order++
		matched := r.Space.Contains(pkt)
		st := Step{Order: order, RuleID: r.RuleID, Matched: matched}
		if matched {
			st.Action = r.Action
			st.Explanation = fmt.Sprintf("packet lies within rule region; first match -> %s", r.Action)
			d.Steps = append(d.Steps, st)
			d.Decision = r.Action
			d.DecidedBy = "rule:" + r.RuleID
			d.MatchedRuleID = r.RuleID
			for _, n := range e.Notes[r.RuleID] {
				d.Uncertainties = append(d.Uncertainties, "rule "+r.RuleID+": "+n)
			}
			return d
		}
		st.Explanation = "packet outside rule region; continue"
		d.Steps = append(d.Steps, st)
	}

	d.Steps = append(d.Steps, Step{
		Order:       order + 1,
		RuleID:      "default",
		Matched:     true,
		Action:      e.DefaultAction,
		Explanation: fmt.Sprintf("no rule matched; default action -> %s", e.DefaultAction),
	})
	d.Decision = e.DefaultAction
	d.DecidedBy = "default"
	return d
}

func firstProto(s netmodel.Int1D) int {
	if m := s.Min(); m != nil {
		return int(m.Int64())
	}
	return 0
}

func addrInt(a netip.Addr) *big.Int {
	if a.Is4() {
		b := a.As4()
		return new(big.Int).SetBytes(b[:])
	}
	b := a.As16()
	return new(big.Int).SetBytes(b[:])
}
