package domain

import (
	"fmt"
	"sort"
)

// Namespace is a cluster namespace with its own label set; namespace
// selectors in peer blocks match against these labels.
type Namespace struct {
	Name   string            `json:"name"`
	Labels map[string]string `json:"labels,omitempty"`
}

// Endpoint is a single workload network endpoint (one pod-like entity with
// one IP is enough for the offline model; multi-IP pods are out of scope).
type Endpoint struct {
	UID       string            `json:"uid"`
	Name      string            `json:"name"`
	Namespace string            `json:"namespace"`
	Labels    map[string]string `json:"labels,omitempty"`
	Ports     []Port            `json:"ports,omitempty"`
}

// Snapshot is an immutable, revisioned view of all input state. Label
// snapshots and policy versions are only ever read together under one
// revision, which is what keeps label snapshots consistent with policy
// versions during evaluation.
type Snapshot struct {
	// Revision is the monotonically increasing policy version. Every
	// reconciliation that changes state appends a new revision; unchanged
	// reconciliations keep the current one.
	Revision int64 `json:"revision"`
	// SourceHash identifies the fixture content the revision was built from.
	SourceHash string      `json:"sourceHash"`
	Namespaces []Namespace `json:"namespaces"`
	Endpoints  []Endpoint  `json:"endpoints"`
	Policies   []Policy    `json:"policies"`
}

// Validate checks one complete desired set for structural problems. It is
// the single place where the fixture adapter and the reconcile loop validate
// input, so failure categories are stable.
func (s *Snapshot) Validate() error {
	seenNS := map[string]bool{}
	for _, ns := range s.Namespaces {
		if ns.Name == "" {
			return ValidationError{Kind: ErrNamespaceNameless}
		}
		if seenNS[ns.Name] {
			return ValidationError{Kind: ErrNamespaceDuplicate, Name: ns.Name}
		}
		seenNS[ns.Name] = true
		if err := validateLabels(ns.Labels); err != nil {
			return ValidationError{Kind: ErrLabelInvalid, Detail: err.Error()}
		}
	}

	seenEP := map[string]bool{}
	for _, ep := range s.Endpoints {
		if ep.UID == "" {
			return ValidationError{Kind: ErrEndpointNoUID, Name: ep.Name}
		}
		if seenEP[ep.UID] {
			return ValidationError{Kind: ErrEndpointDuplicateUID, Name: ep.UID}
		}
		seenEP[ep.UID] = true
		if ep.Namespace == "" {
			return ValidationError{Kind: ErrEndpointNoNamespace, Name: ep.UID}
		}
		if !seenNS[ep.Namespace] {
			return ValidationError{Kind: ErrEndpointUnknownNamespace, Name: ep.UID, Detail: ep.Namespace}
		}
		if err := validateLabels(ep.Labels); err != nil {
			return ValidationError{Kind: ErrLabelInvalid, Name: ep.UID, Detail: err.Error()}
		}
		seenPortSig := map[string]bool{}
		for _, p := range ep.Ports {
			if p.Number < 1 || p.Number > 65535 {
				return ValidationError{Kind: ErrPortNumberInvalid, Name: ep.UID, Detail: fmt.Sprintf("port %d", p.Number)}
			}
			if p.Protocol != "" && p.Protocol != ProtocolTCP && p.Protocol != ProtocolUDP {
				return ValidationError{Kind: ErrPortProtocolInvalid, Name: ep.UID, Detail: string(p.Protocol)}
			}
			// An exact duplicate (same name, number and protocol) is a
			// modeling error. The same name reused for a DIFFERENT
			// number/protocol is legal input: it makes that named port
			// ambiguous for policy references, which the engine reports as
			// UNDECIDABLE rather than guessing.
			proto := p.Protocol
			if proto == "" {
				proto = ProtocolTCP
			}
			sig := fmt.Sprintf("%s/%d/%s", p.Name, p.Number, proto)
			if seenPortSig[sig] {
				return ValidationError{Kind: ErrPortNameDuplicate, Name: ep.UID, Detail: "duplicate port " + sig}
			}
			seenPortSig[sig] = true
		}
	}

	seenPol := map[string]bool{}
	for i := range s.Policies {
		p := &s.Policies[i]
		if p.Name == "" {
			return ValidationError{Kind: ErrPolicyNameless}
		}
		key := p.Namespace + "/" + p.Name
		if seenPol[key] {
			return ValidationError{Kind: ErrPolicyDuplicate, Name: key}
		}
		seenPol[key] = true
		if p.Namespace == "" {
			return ValidationError{Kind: ErrPolicyNoNamespace, Name: p.Name}
		}
		if !seenNS[p.Namespace] {
			return ValidationError{Kind: ErrPolicyUnknownNamespace, Name: key}
		}
		if err := p.PodSelector.Validate(); err != nil {
			return ValidationError{Kind: ErrSelectorInvalid, Name: key, Detail: err.Error()}
		}
		for _, pt := range p.PolicyTypes {
			if pt != PolicyTypeIngress && pt != PolicyTypeEgress {
				return ValidationError{Kind: ErrPolicyTypeInvalid, Name: key, Detail: string(pt)}
			}
		}
		for rIdx, r := range p.Ingress {
			if err := validateRulePorts(r.Ports); err != nil {
				return ValidationError{Kind: ErrRulePortInvalid, Name: fmt.Sprintf("%s.ingress[%d]", key, rIdx), Detail: err.Error()}
			}
			for fIdx, f := range r.From {
				if err := f.PodSelector.Validate(); err != nil {
					return ValidationError{Kind: ErrSelectorInvalid, Name: fmt.Sprintf("%s.ingress[%d].from[%d]", key, rIdx, fIdx), Detail: err.Error()}
				}
				if err := f.NamespaceSelector.Validate(); err != nil {
					return ValidationError{Kind: ErrSelectorInvalid, Name: fmt.Sprintf("%s.ingress[%d].from[%d]", key, rIdx, fIdx), Detail: err.Error()}
				}
			}
		}
		for rIdx, r := range p.Egress {
			if err := validateRulePorts(r.Ports); err != nil {
				return ValidationError{Kind: ErrRulePortInvalid, Name: fmt.Sprintf("%s.egress[%d]", key, rIdx), Detail: err.Error()}
			}
			for tIdx, t := range r.To {
				if err := t.PodSelector.Validate(); err != nil {
					return ValidationError{Kind: ErrSelectorInvalid, Name: fmt.Sprintf("%s.egress[%d].to[%d]", key, rIdx, tIdx), Detail: err.Error()}
				}
				if err := t.NamespaceSelector.Validate(); err != nil {
					return ValidationError{Kind: ErrSelectorInvalid, Name: fmt.Sprintf("%s.egress[%d].to[%d]", key, rIdx, tIdx), Detail: err.Error()}
				}
			}
		}
	}
	return nil
}

func validateRulePorts(ports []RulePort) error {
	for _, rp := range ports {
		if rp.IsNamed() {
			if rp.Number != 0 {
				return fmt.Errorf("port cannot be both named %q and numeric %d", rp.Name, rp.Number)
			}
			continue
		}
		if rp.Number < 1 || rp.Number > 65535 {
			return fmt.Errorf("numeric port %d out of range 1..65535", rp.Number)
		}
		if rp.Protocol != "" && rp.Protocol != ProtocolTCP && rp.Protocol != ProtocolUDP {
			return fmt.Errorf("protocol %q invalid", rp.Protocol)
		}
	}
	return nil
}

// Normalize orders slices deterministically so the same logical input always
// hashes identically and matrix output is stable.
func (s *Snapshot) Normalize() {
	sort.Slice(s.Namespaces, func(i, j int) bool { return s.Namespaces[i].Name < s.Namespaces[j].Name })
	sort.Slice(s.Endpoints, func(i, j int) bool {
		if s.Endpoints[i].Namespace != s.Endpoints[j].Namespace {
			return s.Endpoints[i].Namespace < s.Endpoints[j].Namespace
		}
		return s.Endpoints[i].Name < s.Endpoints[j].Name
	})
	sort.Slice(s.Policies, func(i, j int) bool {
		if s.Policies[i].Namespace != s.Policies[j].Namespace {
			return s.Policies[i].Namespace < s.Policies[j].Namespace
		}
		return s.Policies[i].Name < s.Policies[j].Name
	})
}
