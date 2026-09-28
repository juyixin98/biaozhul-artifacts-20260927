// Package oracle contains an INDEPENDENT re-implementation of the
// NetworkPolicy reachability semantics. It deliberately does not import
// internal/engine: it exists so the golden matrices are checked against two
// independently written algorithms rather than the code under test
// validating itself. It is test-only code (compiled by `go vet ./...` / the
// test packages that import it).
package oracle

import (
	"fmt"
	"os"

	"netpolicy/internal/domain"
	"netpolicy/internal/source"
)

// Verdict is the oracle's own verdict type, kept separate from engine.Verdict
// so the two cannot accidentally agree through a shared constant.
type Verdict string

// Oracle verdicts.
const (
	VAllow       Verdict = "ALLOW"
	VDeny        Verdict = "DENY"
	VUndecidable Verdict = "UNDECIDABLE"
)

// Reason codes are independent strings; tests map them to engine reason
// codes explicitly.
const (
	RAllowBoth          = "allowed_both_sides"
	RAllowIngressUnsel  = "ingress_unselected_default_allow"
	RAllowEgressUnsel   = "egress_unselected_default_allow"
	RAllowBothUnsel     = "ingress_unselected_default_allow+egress_unselected_default_allow"
	RDenyIngress        = "ingress_selected_no_rule_matched"
	RDenyEgress         = "egress_selected_no_rule_matched"
	RDenyBoth           = "both_sides_selected_no_rule_matched"
	RUnknownEndpoint    = "endpoint_unknown"
	RBadProtocol        = "protocol_unsupported"
	RBadPort            = "port_out_of_range"
	RRevisionConflict   = "revision_conflict"
	RAmbiguousNamedPort = "named_port_ambiguous"
)

// Check is one oracle query.
type Check struct {
	Src, Dst string
	Protocol domain.Protocol
	Port     int
	PinRev   int64
}

// Result is the oracle decision.
type Result struct {
	Verdict         Verdict
	Reason          string
	IngressIsolated bool
	EgressIsolated  bool
	IngressPols     []string
	EgressPols      []string
}

// Oracle evaluates against a snapshot using its own indexing and matching
// code paths.
type Oracle struct {
	snap *domain.Snapshot
	ns   map[string]*domain.Namespace
	ep   map[string]*domain.Endpoint
}

// Load parses a fixture exactly as the server does (validation is shared,
// because malformed inputs have no semantics), then evaluates independently.
func Load(path string) (*Oracle, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	snap, err := source.Parse(raw)
	if err != nil {
		return nil, err
	}
	o := &Oracle{snap: snap, ns: map[string]*domain.Namespace{}, ep: map[string]*domain.Endpoint{}}
	for i := range snap.Namespaces {
		o.ns[snap.Namespaces[i].Name] = &snap.Namespaces[i]
	}
	for i := range snap.Endpoints {
		o.ep[snap.Endpoints[i].UID] = &snap.Endpoints[i]
	}
	return o, nil
}

// Revision exposes the pinned version.
func (o *Oracle) Revision() int64 { return o.snap.Revision }

// Evaluate computes a verdict from first principles.
func (o *Oracle) Evaluate(c Check) Result {
	res := Result{}
	if c.Protocol != domain.ProtocolTCP && c.Protocol != domain.ProtocolUDP {
		return Result{Verdict: VUndecidable, Reason: RBadProtocol}
	}
	if c.Port < 1 || c.Port > 65535 {
		return Result{Verdict: VUndecidable, Reason: RBadPort}
	}
	if c.PinRev != 0 && c.PinRev != o.snap.Revision {
		return Result{Verdict: VUndecidable, Reason: RRevisionConflict}
	}
	src, srcOK := o.ep[c.Src]
	dst, dstOK := o.ep[c.Dst]
	if !srcOK || !dstOK {
		return Result{Verdict: VUndecidable, Reason: RUnknownEndpoint}
	}

	if amb := o.ambiguousNamedPorts(src, dst, c.Protocol); len(amb) > 0 {
		return Result{Verdict: VUndecidable, Reason: RAmbiguousNamedPort}
	}

	ingPols := o.selectingPolicies(dst, domain.PolicyTypeIngress)
	egPols := o.selectingPolicies(src, domain.PolicyTypeEgress)
	res.IngressIsolated = len(ingPols) > 0
	res.EgressIsolated = len(egPols) > 0
	res.IngressPols = refs(ingPols)
	res.EgressPols = refs(egPols)

	ingAllow := len(ingPols) == 0 || o.anyRuleAllows(ingPols, true, src, dst, c)
	egAllow := len(egPols) == 0 || o.anyRuleAllows(egPols, false, src, dst, c)

	switch {
	case ingAllow && egAllow:
		res.Verdict = VAllow
		res.Reason = o.allowReason(res.IngressIsolated, res.EgressIsolated)
	case !ingAllow && !egAllow:
		res.Verdict, res.Reason = VDeny, RDenyBoth
	case !ingAllow:
		res.Verdict, res.Reason = VDeny, RDenyIngress
	default:
		res.Verdict, res.Reason = VDeny, RDenyEgress
	}
	return res
}

func (o *Oracle) allowReason(ingIso, egIso bool) string {
	switch {
	case ingIso && egIso:
		return RAllowBoth
	case !ingIso && !egIso:
		return RAllowBothUnsel
	case !ingIso:
		return RAllowIngressUnsel
	default:
		return RAllowEgressUnsel
	}
}

type polRef = *domain.Policy

func (o *Oracle) selectingPolicies(target *domain.Endpoint, dir domain.PolicyType) []polRef {
	var out []polRef
	for i := range o.snap.Policies {
		p := &o.snap.Policies[i]
		if p.Namespace != target.Namespace {
			continue
		}
		if !p.HasPolicyType(dir) {
			continue
		}
		if matchLabels(p.PodSelector.MatchLabels, target.Labels) && matchExprs(p.PodSelector.MatchExprs, target.Labels) {
			out = append(out, p)
		}
	}
	return out
}

func refs(ps []polRef) []string {
	var out []string
	for _, p := range ps {
		out = append(out, p.Namespace+"/"+p.Name)
	}
	return out
}

func (o *Oracle) anyRuleAllows(ps []polRef, ingress bool, src, dst *domain.Endpoint, c Check) bool {
	for _, p := range ps {
		var rules []ruleView
		if ingress {
			for _, r := range p.Ingress {
				rules = append(rules, ruleView{r.Ports, r.From})
			}
		} else {
			for _, r := range p.Egress {
				rules = append(rules, ruleView{r.Ports, r.To})
			}
		}
		for _, r := range rules {
			if o.ruleAllows(r, ingress, p.Namespace, src, dst, c) {
				return true
			}
		}
	}
	return false
}

type ruleView struct {
	ports []domain.RulePort
	peers []domain.Peer
}

func (o *Oracle) ruleAllows(r ruleView, ingress bool, polNS string, src, dst *domain.Endpoint, c Check) bool {
	// Peer side: for ingress the remote is src; for egress the remote is dst.
	remote := dst
	if ingress {
		remote = src
	}
	if !o.peersAllow(r.peers, remote, polNS) {
		return false
	}
	if len(r.ports) == 0 {
		return true
	}
	for _, rp := range r.ports {
		if rp.IsNamed() {
			// Named ports resolve on the DESTINATION endpoint.
			if namedResolves(rp, dst, c.Protocol, c.Port) {
				return true
			}
			continue
		}
		proto := rp.Protocol
		if proto == "" {
			proto = domain.ProtocolTCP
		}
		if proto == c.Protocol && rp.Number == c.Port {
			return true
		}
	}
	return false
}

func namedResolves(rp domain.RulePort, dst *domain.Endpoint, proto domain.Protocol, num int) bool {
	found := false
	for _, pp := range dst.Ports {
		if pp.Name != rp.Name {
			continue
		}
		ppProto := pp.Protocol
		if ppProto == "" {
			ppProto = domain.ProtocolTCP
		}
		rpProto := rp.Protocol
		if rpProto == "" {
			rpProto = domain.ProtocolTCP
		}
		if ppProto == proto && pp.Number == num && rpProto == ppProto {
			found = true
		}
	}
	return found
}

func (o *Oracle) peersAllow(peers []domain.Peer, remote *domain.Endpoint, polNS string) bool {
	if len(peers) == 0 {
		return remote.Namespace == polNS
	}
	for _, peer := range peers {
		if o.peerAllows(peer, remote, polNS) {
			return true
		}
	}
	return false
}

func (o *Oracle) peerAllows(peer domain.Peer, remote *domain.Endpoint, polNS string) bool {
	var nsLabels map[string]string
	if ns := o.ns[remote.Namespace]; ns != nil {
		nsLabels = ns.Labels
	}
	hasNS, hasPod := peer.NamespaceSelector != nil, peer.PodSelector != nil
	switch {
	case !hasNS && !hasPod:
		return remote.Namespace == polNS
	case hasNS && !hasPod:
		return matchLabels(peer.NamespaceSelector.MatchLabels, nsLabels) && matchExprs(peer.NamespaceSelector.MatchExprs, nsLabels)
	case !hasNS && hasPod:
		return remote.Namespace == polNS && matchLabels(peer.PodSelector.MatchLabels, remote.Labels) && matchExprs(peer.PodSelector.MatchExprs, remote.Labels)
	default:
		return matchLabels(peer.NamespaceSelector.MatchLabels, nsLabels) && matchExprs(peer.NamespaceSelector.MatchExprs, nsLabels) &&
			matchLabels(peer.PodSelector.MatchLabels, remote.Labels) && matchExprs(peer.PodSelector.MatchExprs, remote.Labels)
	}
}

func matchLabels(want, got map[string]string) bool {
	for k, v := range want {
		if g, ok := got[k]; !ok || g != v {
			return false
		}
	}
	return true
}

func matchExprs(exprs []domain.Requirement, got map[string]string) bool {
	for _, e := range exprs {
		v, present := got[e.Key]
		switch e.Operator {
		case domain.OpIn:
			if !present || !containsS(e.Values, v) {
				return false
			}
		case domain.OpNotIn:
			if present && containsS(e.Values, v) {
				return false
			}
		case domain.OpExists:
			if !present {
				return false
			}
		case domain.OpDoesNotExist:
			if present {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func containsS(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// ambiguousNamedPorts flags names that are referenced by any relevant policy
// and resolve to more than one (protocol,number) on the destination.
func (o *Oracle) ambiguousNamedPorts(src, dst *domain.Endpoint, proto domain.Protocol) []string {
	ingPols := o.selectingPolicies(dst, domain.PolicyTypeIngress)
	egPols := o.selectingPolicies(src, domain.PolicyTypeEgress)
	used := map[string]bool{}
	collect := func(ps []polRef, ingress bool) {
		for _, p := range ps {
			if ingress {
				for _, r := range p.Ingress {
					for _, rp := range r.Ports {
						if rp.IsNamed() {
							used[rp.Name] = true
						}
					}
				}
				continue
			}
			for _, r := range p.Egress {
				for _, rp := range r.Ports {
					if rp.IsNamed() {
						used[rp.Name] = true
					}
				}
			}
		}
	}
	collect(ingPols, true)
	collect(egPols, false)

	var out []string
	for name := range used {
		sig := map[string]bool{}
		for _, pp := range dst.Ports {
			if pp.Name == name {
				p := pp.Protocol
				if p == "" {
					p = domain.ProtocolTCP
				}
				sig[fmt.Sprintf("%s/%d", p, pp.Number)] = true
			}
		}
		if len(sig) > 1 {
			out = append(out, name)
		}
	}
	return out
}

// MustLoadFixture is a test helper that fatals through panic on read errors.
func MustLoadFixture(path string) *domain.Snapshot {
	raw, err := os.ReadFile(path)
	if err != nil {
		panic(err)
	}
	snap, err := source.Parse(raw)
	if err != nil {
		panic(err)
	}
	return snap
}
