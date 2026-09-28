package engine

import "netpolicy/internal/domain"

// cloneSnapshot returns a value-deep copy sufficient to insulate the engine
// from post-build mutations of labels, ports and policy slices.
func cloneSnapshot(in *domain.Snapshot) *domain.Snapshot {
	if in == nil {
		return nil
	}
	out := &domain.Snapshot{
		Revision:   in.Revision,
		SourceHash: in.SourceHash,
	}
	copyLabels := func(in map[string]string) map[string]string {
		if in == nil {
			return nil
		}
		m := make(map[string]string, len(in))
		for k, v := range in {
			m[k] = v
		}
		return m
	}
	out.Namespaces = make([]domain.Namespace, len(in.Namespaces))
	for i, ns := range in.Namespaces {
		out.Namespaces[i] = domain.Namespace{Name: ns.Name, Labels: copyLabels(ns.Labels)}
	}
	out.Endpoints = make([]domain.Endpoint, len(in.Endpoints))
	for i, ep := range in.Endpoints {
		out.Endpoints[i] = domain.Endpoint{
			UID:       ep.UID,
			Name:      ep.Name,
			Namespace: ep.Namespace,
			Labels:    copyLabels(ep.Labels),
			Ports:     append([]domain.Port(nil), ep.Ports...),
		}
	}
	out.Policies = make([]domain.Policy, len(in.Policies))
	for i, p := range in.Policies {
		np := domain.Policy{
			Name:        p.Name,
			Namespace:   p.Namespace,
			PodSelector: *cloneSelector(&p.PodSelector),
			PolicyTypes: append([]domain.PolicyType(nil), p.PolicyTypes...),
		}
		np.Ingress = make([]domain.IngressRule, len(p.Ingress))
		for j, r := range p.Ingress {
			np.Ingress[j] = domain.IngressRule{
				Ports: append([]domain.RulePort(nil), r.Ports...),
				From:  clonePeers(r.From),
			}
		}
		np.Egress = make([]domain.EgressRule, len(p.Egress))
		for j, r := range p.Egress {
			np.Egress[j] = domain.EgressRule{
				Ports: append([]domain.RulePort(nil), r.Ports...),
				To:    clonePeers(r.To),
			}
		}
		out.Policies[i] = np
	}
	return out
}

func clonePeers(in []domain.Peer) []domain.Peer {
	if in == nil {
		return nil
	}
	out := make([]domain.Peer, len(in))
	for i, p := range in {
		out[i] = domain.Peer{
			PodSelector:       cloneSelector(p.PodSelector),
			NamespaceSelector: cloneSelector(p.NamespaceSelector),
		}
	}
	return out
}

func cloneSelector(s *domain.Selector) *domain.Selector {
	if s == nil {
		return nil
	}
	out := &domain.Selector{MatchExprs: append([]domain.Requirement(nil), s.MatchExprs...)}
	if s.MatchLabels != nil {
		out.MatchLabels = make(map[string]string, len(s.MatchLabels))
		for k, v := range s.MatchLabels {
			out.MatchLabels[k] = v
		}
	}
	return out
}
