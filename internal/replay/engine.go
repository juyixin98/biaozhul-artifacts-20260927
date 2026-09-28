// Package replay evaluates concrete packets against a compiled policy using
// strict first-match semantics and produces an explainable trace of every
// decision. It is the runtime counterpart to the offline analyzer and shares
// the exact same config/netmodel packages, so analyzer witnesses can be
// replayed verbatim.
package replay

import (
	"fmt"
	"strings"

	"fwrule/internal/config"
	"fwrule/internal/netmodel"
)

// Request is one packet to evaluate.
type Request struct {
	RequestID string  `json:"request_id,omitempty"`
	Protocol  string  `json:"protocol"` // name or number; "any" is invalid for a packet
	SrcIP     string  `json:"src_ip"`
	DstIP     string  `json:"dst_ip"`
	SrcPort   *uint16 `json:"src_port,omitempty"`
	DstPort   *uint16 `json:"dst_port,omitempty"`
}

// Step records the evaluation of one rule position.
type Step struct {
	Index   int    `json:"index"`
	RuleID  string `json:"rule_id"`
	Action  string `json:"action"`
	Matched bool   `json:"matched"`
	// MissReasons lists every packet dimension the rule failed (empty on a
	// match). A match that is not first is still recorded with matched=true.
	MissReasons []string `json:"miss_reasons,omitempty"`
}

// Status values for a decision.
const (
	StatusDecided = "decided"
	StatusError   = "error"
)

// Decision is the full, self-explaining result.
type Decision struct {
	RequestID  string `json:"request_id"`
	Version    int64  `json:"version"`
	PolicyName string `json:"policy_name"`

	Status      string `json:"status"`
	ErrorCode   string `json:"error_code,omitempty"`
	ErrorDetail string `json:"error_detail,omitempty"`

	Action string `json:"action,omitempty"`
	// DecidedBy is the winning rule id, or "<default:allow|deny>".
	DecidedBy        string `json:"decided_by,omitempty"`
	MatchedRuleIndex int    `json:"matched_rule_index"` // -1 when default decided
	DefaultApplied   bool   `json:"default_applied"`

	Family   string `json:"family,omitempty"`
	Protocol string `json:"protocol,omitempty"`
	ProtoNum *uint8 `json:"protocol_num,omitempty"`

	Trace []Step `json:"trace"`
	// Uncertainties are non-fatal caveats (unknown protocol number, ...).
	Uncertainties []Uncertainty `json:"uncertainties,omitempty"`
}

// Uncertainty mirrors analyzer.Uncertainty for runtime decisions.
type Uncertainty struct {
	Code   string `json:"code"`
	Detail string `json:"detail"`
}

// Engine evaluates packets against one compiled policy version.
type Engine struct {
	Pol     *config.Policy
	Version int64
}

// NewEngine compiles the stored policy source for a given version.
func NewEngine(source string, version int64) (*Engine, error) {
	pol, err := config.Load([]byte(source), fmt.Sprintf("policy@%d", version))
	if err != nil {
		return nil, err
	}
	return &Engine{Pol: pol, Version: version}, nil
}

// Evaluate runs strict first-match evaluation. The returned Decision is always
// non-nil; malformed requests come back with Status="error" and a stable code.
func (e *Engine) Evaluate(req Request) *Decision {
	d := &Decision{
		RequestID:        req.RequestID,
		Version:          e.Version,
		PolicyName:       e.Pol.Name,
		MatchedRuleIndex: -1,
	}

	// A concrete packet must name exactly one protocol.
	if strings.TrimSpace(req.Protocol) == "" {
		return e.fail(d, "MISSING_PROTOCOL", "request must name a protocol (name or number)")
	}
	proto, err := netmodel.ParseProtocol(req.Protocol)
	if err != nil {
		return e.fail(d, "UNKNOWN_PROTOCOL", err.Error())
	}
	if proto.Kind == netmodel.ProtoAny {
		return e.fail(d, "INVALID_PROTOCOL",
			`a packet cannot have protocol "any"; name one concrete protocol`)
	}
	pn := proto.Number
	d.ProtoNum = &pn
	d.Protocol = proto.String()
	if !proto.KnownName {
		d.Uncertainties = append(d.Uncertainties, Uncertainty{
			Code: "UNKNOWN_PROTOCOL_NUMBER",
			Detail: fmt.Sprintf(
				"protocol number %d is not in the local IANA registry; "+
					"evaluated as exactly that number, ports ignored", pn),
		})
	}

	srcAddr, srcFam, err := netmodel.ParseAddr(req.SrcIP)
	if err != nil {
		return e.fail(d, "INVALID_SRC_IP", fmt.Sprintf("bad src_ip %q: %v", req.SrcIP, err))
	}
	dstAddr, dstFam, err := netmodel.ParseAddr(req.DstIP)
	if err != nil {
		return e.fail(d, "INVALID_DST_IP", fmt.Sprintf("bad dst_ip %q: %v", req.DstIP, err))
	}
	if srcFam != dstFam {
		return e.fail(d, "FAMILY_MISMATCH", fmt.Sprintf(
			"src is %s but dst is %s; no packet can have mismatched address families",
			srcFam, dstFam))
	}
	fam := srcFam
	d.Family = fam.String()

	pkt := netmodel.Packet{
		Fam: fam, ProtoNum: pn, SrcIP: srcAddr, DstIP: dstAddr,
	}
	portBearing := proto.PortBearing()
	if portBearing {
		if req.SrcPort != nil {
			pkt.SrcPort = *req.SrcPort
		}
		if req.DstPort != nil {
			pkt.DstPort = *req.DstPort
		}
	}

	// Full walk: every rule position is traced, including rules AFTER the
	// winner, so the trace shows rules that also match but are shadowed. The
	// decision itself is strict first-match.
	for _, r := range e.Pol.Rules {
		step := Step{Index: r.Index, RuleID: r.ID, Action: string(r.Action)}
		if r.MatchEmpty {
			step.MissReasons = append(step.MissReasons,
				"rule has empty match set ("+r.EmptyReason+")")
		} else {
			step.MissReasons = explainMiss(r, pkt)
		}
		step.Matched = len(step.MissReasons) == 0
		d.Trace = append(d.Trace, step)
		if step.Matched && d.MatchedRuleIndex < 0 {
			d.Status = StatusDecided
			d.Action = string(r.Action)
			d.DecidedBy = r.ID
			d.MatchedRuleIndex = r.Index
		}
	}
	if d.MatchedRuleIndex >= 0 {
		return d
	}

	// No rule matched: apply the family default.
	def, ok := e.Pol.DefaultFor(fam)
	if !ok {
		return e.fail(d, "DEFAULT_NOT_CONFIGURED", fmt.Sprintf(
			"no rule matched and no default action is configured for %s", fam))
	}
	d.Status = StatusDecided
	d.Action = string(def)
	d.DecidedBy = "<default:" + string(def) + ">"
	d.DefaultApplied = true
	return d
}

func (e *Engine) fail(d *Decision, code, detail string) *Decision {
	d.Status = StatusError
	d.ErrorCode = code
	d.ErrorDetail = detail
	return d
}

// explainMiss returns the list of dimensions on which the rule fails the
// packet; empty means a match.
func explainMiss(r *config.CompiledRule, pkt netmodel.Packet) []string {
	var reasons []string
	if r.Box.Fam != pkt.Fam {
		reasons = append(reasons, fmt.Sprintf("family rule=%s packet=%s", r.Box.Fam, pkt.Fam))
	}
	if r.Box.Proto.Kind == netmodel.ProtoConcrete && r.Box.Proto.Number != pkt.ProtoNum {
		reasons = append(reasons, fmt.Sprintf("protocol rule=%s packet=%d",
			r.Box.Proto, pkt.ProtoNum))
	}
	if !r.Box.SrcNet.Contains(pkt.SrcIP) {
		reasons = append(reasons, fmt.Sprintf("src %s outside %s",
			pkt.SrcIP.String(pkt.Fam), r.Box.SrcNet))
	}
	if !r.Box.DstNet.Contains(pkt.DstIP) {
		reasons = append(reasons, fmt.Sprintf("dst %s outside %s",
			pkt.DstIP.String(pkt.Fam), r.Box.DstNet))
	}
	if r.Box.Proto.PortBearing() && (pkt.ProtoNum == 6 || pkt.ProtoNum == 17) {
		if !r.Box.SrcPorts.Contains(pkt.SrcPort) {
			reasons = append(reasons, fmt.Sprintf("src_port %d outside %s",
				pkt.SrcPort, r.Box.SrcPorts))
		}
		if !r.Box.DstPorts.Contains(pkt.DstPort) {
			reasons = append(reasons, fmt.Sprintf("dst_port %d outside %s",
				pkt.DstPort, r.Box.DstPorts))
		}
	}
	return reasons
}
