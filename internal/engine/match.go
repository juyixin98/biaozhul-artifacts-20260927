package engine

import (
	"fmt"
	"sort"

	"netpolicy/internal/domain"
)

// evalRule evaluates one rule against a packet. `remote` is the endpoint on
// the other side of the connection from the policy-selected endpoint; for
// ingress that is the source, for egress the destination. `target` is always
// the endpoint whose ports named ports resolve against — which is the
// destination of the traffic in both directions.
//
// It returns (matched, nearMissHint). The hint is populated when the peer
// matched but the port did not, so callers can explain a default-deny.
func (e *Engine) evalRule(ports []domain.RulePort, peers []domain.Peer, remote, target *domain.Endpoint, p *domain.Policy, in Input, dir Direction) (bool, string) {
	peerMatched := len(peers) == 0
	for _, peer := range peers {
		if e.peerMatches(peer, remote, p.Namespace) {
			peerMatched = true
			break
		}
	}
	if !peerMatched {
		return false, ""
	}
	// No ports means the rule applies to all ports and protocols.
	if len(ports) == 0 {
		return true, ""
	}
	var portNearMiss string
	for _, rp := range ports {
		switch {
		case rp.IsNamed():
			resolved, ok := resolveNamedPort(rp, target, in.Protocol, in.Port)
			switch {
			case resolved:
				return true, ""
			case !ok:
				// Name is not served by the destination at this protocol:
				// explicit near-miss, never a silent global numeric match.
				portNearMiss = fmt.Sprintf("peer matched but named port %q not served by destination %s", rp.Name, target.UID)
			default:
				portNearMiss = fmt.Sprintf("peer matched but named port %q resolves to a different number/protocol than %s/%d on destination %s",
					rp.Name, in.Protocol, in.Port, target.UID)
			}
		case portMatches(rp, in):
			return true, ""
		}
	}
	return false, portNearMiss
}

// resolveNamedPort resolves a named rule port against the DESTINATION
// endpoint. Returns (resolved, exists):
//   - (true, true)   the name resolves and equals the queried port/protocol;
//   - (false, true)  the name resolves but to a different port/protocol;
//   - (false, false) the destination does not serve that name at all.
func resolveNamedPort(rp domain.RulePort, dst *domain.Endpoint, proto domain.Protocol, num int) (resolved bool, exists bool) {
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
		exists = true
		if ppProto == proto && pp.Number == num && rpProto == ppProto {
			resolved = true
		}
	}
	return resolved, exists
}

func portMatches(rp domain.RulePort, in Input) bool {
	proto := rp.Protocol
	if proto == "" {
		proto = domain.ProtocolTCP // k8s default for numeric rule ports
	}
	return proto == in.Protocol && rp.Number == in.Port
}

// peerMatches implements the four independent peer scope knobs:
//   - both selectors nil: match all peers in the policy's own namespace;
//   - namespaceSelector only ({} ok): match by namespace labels;
//   - podSelector only ({} ok): same namespace, match by pod labels;
//   - both: match by namespace labels AND pod labels.
func (e *Engine) peerMatches(peer domain.Peer, remote *domain.Endpoint, policyNamespace string) bool {
	ns := e.nsByName[remote.Namespace]
	nsLabels := map[string]string(nil)
	if ns != nil {
		nsLabels = ns.Labels
	}
	switch {
	case peer.NamespaceSelector == nil && peer.PodSelector == nil:
		return remote.Namespace == policyNamespace
	case peer.NamespaceSelector != nil && peer.PodSelector == nil:
		return peer.NamespaceSelector.Matches(nsLabels)
	case peer.NamespaceSelector == nil && peer.PodSelector != nil:
		return remote.Namespace == policyNamespace && peer.PodSelector.Matches(remote.Labels)
	default:
		return peer.NamespaceSelector.Matches(nsLabels) && peer.PodSelector.Matches(remote.Labels)
	}
}

// collectAmbiguities flags destination-side named ports that collide (same
// name on two container ports with different number/protocol). A rule
// referencing such a name cannot be resolved to one numeric port, so the
// decision is undecidable rather than guessed. Only names actually used by
// the side's policies are reported.
func (e *Engine) collectAmbiguities(pols []*domain.Policy, dst *domain.Endpoint, dir Direction) []string {
	used := map[string]bool{}
	for _, p := range pols {
		var rules []domain.RulePort
		switch dir {
		case DirectionIngress:
			for _, r := range p.Ingress {
				rules = append(rules, r.Ports...)
			}
		case DirectionEgress:
			for _, r := range p.Egress {
				rules = append(rules, r.Ports...)
			}
		}
		for _, rp := range rules {
			if rp.IsNamed() {
				used[rp.Name] = true
			}
		}
	}
	if len(used) == 0 {
		return nil
	}
	var names []string
	for n := range used {
		names = append(names, n)
	}
	sort.Strings(names)
	var out []string
	for _, n := range names {
		signatures := map[string]bool{}
		for _, pp := range dst.Ports {
			if pp.Name == n {
				proto := pp.Protocol
				if proto == "" {
					proto = domain.ProtocolTCP
				}
				signatures[fmt.Sprintf("%s/%d", proto, pp.Number)] = true
			}
		}
		if len(signatures) > 1 {
			out = append(out, fmt.Sprintf("named port %q is ambiguous on destination %s: resolves to multiple ports %v",
				n, dst.UID, sortedKeys(signatures)))
		}
	}
	return out
}

func sortedKeys(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
