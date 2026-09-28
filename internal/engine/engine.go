// Package engine evaluates offline reachability decisions against one
// immutable domain.Snapshot. It implements the two-direction composition:
// a connection is allowed only when the destination ingress side AND the
// source egress side both permit it.
package engine

import (
	"fmt"

	"netpolicy/internal/domain"
)

// Direction identifies which side of a connection a policy match came from.
type Direction string

// Direction tags.
const (
	DirectionIngress Direction = "ingress"
	DirectionEgress  Direction = "egress"
)

// Verdict is the top-level result category of a check.
type Verdict string

// Verdict categories.
const (
	VerdictAllow       Verdict = "ALLOW"
	VerdictDeny        Verdict = "DENY"
	VerdictUndecidable Verdict = "UNDECIDABLE"
)

// Stable reason codes used in diagnostics and asserted on by independent
// tests.
const (
	ReasonAllowed             = "allowed_both_sides"
	ReasonIngressUnselected   = "ingress_unselected_default_allow"
	ReasonEgressUnselected    = "egress_unselected_default_allow"
	ReasonIngressDefaultDeny  = "ingress_selected_no_rule_matched"
	ReasonEgressDefaultDeny   = "egress_selected_no_rule_matched"
	ReasonBothDefaultDeny     = "both_sides_selected_no_rule_matched"
	ReasonEndpointUnknown     = "endpoint_unknown"
	ReasonProtocolUnsupported = "protocol_unsupported"
	ReasonPortOutOfRange      = "port_out_of_range"
	ReasonRevisionConflict    = "revision_conflict"
	ReasonNamedPortAmbiguous  = "named_port_ambiguous"
)

// Match is one policy rule that permitted the traffic on one side.
type Match struct {
	PolicyNamespace string            `json:"policyNamespace"`
	PolicyName      string            `json:"policyName"`
	Direction       Direction         `json:"direction"`
	RuleIndex       int               `json:"ruleIndex"`
	MatchedPorts    []domain.RulePort `json:"matchedPorts,omitempty"`
	MatchedPeer     *domain.Peer      `json:"matchedPeer,omitempty"`
}

func (m Match) policyRef() string { return m.PolicyNamespace + "/" + m.PolicyName }

// Side holds the evaluation trace for one direction.
type Side struct {
	// Isolated reports whether any policy selects the endpoint for this
	// direction. This is the explicit default-behavior boundary:
	// unselected workloads are allowed by default.
	Isolated bool `json:"isolated"`
	Allowed  bool `json:"allowed"`
	// SelectedPolicies lists every policy isolating this side, even when no
	// rule inside it matched (shows why a DENY was reached).
	SelectedPolicies []string `json:"selectedPolicies,omitempty"`
	Matches          []Match  `json:"matches,omitempty"`
	// Hints explain near-misses, e.g. a peer matched but the named port did
	// not resolve on the destination.
	Hints []string `json:"hints,omitempty"`
	// Ambiguities make the side undecidable rather than allowed or denied.
	Ambiguities []string `json:"ambiguities,omitempty"`
}

// Input is one concrete connectivity query against a numeric destination
// port. Named ports only exist inside policies and are resolved against the
// destination endpoint; real packets are always numeric.
type Input struct {
	SourceUID string
	DestUID   string
	Protocol  domain.Protocol
	Port      int
	// PinRevision, when non-zero, requests evaluation against an exact
	// policy version; a mismatch yields an undecidable decision.
	PinRevision int64
}

// Decision is the full result of a check, including both side traces so a
// caller can see exactly why an ALLOW or DENY was reached.
type Decision struct {
	Verdict   Verdict `json:"verdict"`
	Allowed   bool    `json:"allowed"`
	Reason    string  `json:"reason"`
	Revision  int64   `json:"revision"`
	SourceUID string  `json:"sourceUid"`
	DestUID   string  `json:"destUid"`
	Protocol  string  `json:"protocol"`
	Port      int     `json:"port"`
	Ingress   Side    `json:"ingress"`
	Egress    Side    `json:"egress"`
}

// Engine indexes a single snapshot for evaluation.
type Engine struct {
	snap            *domain.Snapshot
	nsByName        map[string]*domain.Namespace
	epByUID         map[string]*domain.Endpoint
	ingressSelected map[string][]*domain.Policy
	egressSelected  map[string][]*domain.Policy
}

// New builds an engine over an already-validated snapshot. The snapshot is
// deep-copied, so later mutation of the caller's snapshot cannot reach
// decisions: an engine always evaluates the label snapshot and policy
// version it was built from.
func New(snap *domain.Snapshot) *Engine {
	snap = cloneSnapshot(snap)
	e := &Engine{
		snap:            snap,
		nsByName:        make(map[string]*domain.Namespace, len(snap.Namespaces)),
		epByUID:         make(map[string]*domain.Endpoint, len(snap.Endpoints)),
		ingressSelected: map[string][]*domain.Policy{},
		egressSelected:  map[string][]*domain.Policy{},
	}
	for i := range snap.Namespaces {
		e.nsByName[snap.Namespaces[i].Name] = &snap.Namespaces[i]
	}
	for i := range snap.Endpoints {
		e.epByUID[snap.Endpoints[i].UID] = &snap.Endpoints[i]
	}
	for i := range snap.Policies {
		p := &snap.Policies[i]
		ingress, egress := p.HasPolicyType(domain.PolicyTypeIngress), p.HasPolicyType(domain.PolicyTypeEgress)
		for _, ep := range snap.Endpoints {
			if ep.Namespace != p.Namespace {
				continue
			}
			if !p.PodSelector.Matches(ep.Labels) {
				continue
			}
			uid := ep.UID
			if ingress {
				e.ingressSelected[uid] = append(e.ingressSelected[uid], p)
			}
			if egress {
				e.egressSelected[uid] = append(e.egressSelected[uid], p)
			}
		}
	}
	return e
}

// Revision returns the policy version this engine evaluates.
func (e *Engine) Revision() int64 { return e.snap.Revision }

// Snapshot returns the underlying snapshot (used by persistence/history).
func (e *Engine) Snapshot() *domain.Snapshot { return e.snap }

// Check evaluates one connection. It returns a Decision in every structurally
// callable case (unknown endpoint, ambiguity, revision pin mismatch); the
// only returned errors are programmer mistakes such as an unset engine.
func (e *Engine) Check(in Input) (*Decision, error) {
	if e == nil || e.snap == nil {
		return nil, fmt.Errorf("engine not initialized")
	}
	d := &Decision{
		Revision:  e.snap.Revision,
		SourceUID: in.SourceUID,
		DestUID:   in.DestUID,
		Protocol:  string(in.Protocol),
		Port:      in.Port,
	}
	if in.Protocol != domain.ProtocolTCP && in.Protocol != domain.ProtocolUDP {
		d.Verdict, d.Reason = VerdictUndecidable, ReasonProtocolUnsupported
		return d, nil
	}
	if in.Port < 1 || in.Port > 65535 {
		d.Verdict, d.Reason = VerdictUndecidable, ReasonPortOutOfRange
		return d, nil
	}
	if in.PinRevision != 0 && in.PinRevision != e.snap.Revision {
		d.Verdict = VerdictUndecidable
		d.Reason = ReasonRevisionConflict
		return d, nil
	}
	src, srcOK := e.epByUID[in.SourceUID]
	dst, dstOK := e.epByUID[in.DestUID]
	if !srcOK || !dstOK {
		d.Verdict = VerdictUndecidable
		d.Reason = ReasonEndpointUnknown
		if !srcOK {
			d.Egress.Hints = append(d.Egress.Hints, "source endpoint "+in.SourceUID+" not in revision snapshot")
		}
		if !dstOK {
			d.Ingress.Hints = append(d.Ingress.Hints, "destination endpoint "+in.DestUID+" not in revision snapshot")
		}
		return d, nil
	}

	d.Ingress = e.evalIngress(src, dst, in)
	d.Egress = e.evalEgress(src, dst, in)

	if len(d.Ingress.Ambiguities) > 0 || len(d.Egress.Ambiguities) > 0 {
		d.Verdict, d.Reason = VerdictUndecidable, ReasonNamedPortAmbiguous
		return d, nil
	}

	switch {
	case d.Ingress.Allowed && d.Egress.Allowed:
		d.Verdict, d.Allowed, d.Reason = VerdictAllow, true, composeAllowReason(d.Ingress, d.Egress)
	case !d.Ingress.Allowed && !d.Egress.Allowed:
		d.Verdict, d.Reason = VerdictDeny, ReasonBothDefaultDeny
	case !d.Ingress.Allowed:
		d.Verdict, d.Reason = VerdictDeny, ReasonIngressDefaultDeny
	default:
		d.Verdict, d.Reason = VerdictDeny, ReasonEgressDefaultDeny
	}
	return d, nil
}

func composeAllowReason(in, eg Side) string {
	if in.Isolated && eg.Isolated {
		return ReasonAllowed
	}
	switch {
	case !in.Isolated && !eg.Isolated:
		return ReasonIngressUnselected + "+" + ReasonEgressUnselected
	case !in.Isolated:
		return ReasonIngressUnselected
	default:
		return ReasonEgressUnselected
	}
}

func (e *Engine) evalIngress(src, dst *domain.Endpoint, in Input) Side {
	pols := e.ingressSelected[dst.UID]
	// Explicit default boundary: an endpoint selected by no policy in this
	// direction is allowed by default. Isolation flips the default to deny;
	// a matching rule flips it back to allow.
	side := Side{Isolated: len(pols) > 0, Allowed: len(pols) == 0}
	for _, p := range pols {
		side.SelectedPolicies = append(side.SelectedPolicies, p.Namespace+"/"+p.Name)
		for i, rule := range p.Ingress {
			matched, detail := e.evalRule(rule.Ports, rule.From, src, dst, p, in, DirectionIngress)
			if matched {
				side.Allowed = true
				side.Matches = append(side.Matches, matchOf(p, i, rule.Ports, rule.From, DirectionIngress))
				continue
			}
			if detail != "" {
				side.Hints = append(side.Hints, fmt.Sprintf("policy %s ingress[%d]: %s", p.Name, i, detail))
			}
		}
	}
	side.Ambiguities = append(side.Ambiguities, e.collectAmbiguities(pols, dst, DirectionIngress)...)
	return side
}

func (e *Engine) evalEgress(src, dst *domain.Endpoint, in Input) Side {
	pols := e.egressSelected[src.UID]
	side := Side{Isolated: len(pols) > 0, Allowed: len(pols) == 0}
	for _, p := range pols {
		side.SelectedPolicies = append(side.SelectedPolicies, p.Namespace+"/"+p.Name)
		for i, rule := range p.Egress {
			// Named ports always resolve against the DESTINATION endpoint
			// (dst), even for an egress rule selected by the source.
			matched, detail := e.evalRule(rule.Ports, rule.To, dst, dst, p, in, DirectionEgress)
			if matched {
				side.Allowed = true
				side.Matches = append(side.Matches, matchOf(p, i, rule.Ports, rule.To, DirectionEgress))
				continue
			}
			if detail != "" {
				side.Hints = append(side.Hints, fmt.Sprintf("policy %s egress[%d]: %s", p.Name, i, detail))
			}
		}
	}
	side.Ambiguities = append(side.Ambiguities, e.collectAmbiguities(pols, dst, DirectionEgress)...)
	return side
}

func matchOf(p *domain.Policy, idx int, ports []domain.RulePort, peers []domain.Peer, dir Direction) Match {
	m := Match{
		PolicyNamespace: p.Namespace,
		PolicyName:      p.Name,
		Direction:       dir,
		RuleIndex:       idx,
		MatchedPorts:    append([]domain.RulePort(nil), ports...),
	}
	if len(peers) > 0 {
		peer := peers[0]
		m.MatchedPeer = &peer
	}
	return m
}
