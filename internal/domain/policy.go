package domain

import (
	"encoding/json"
	"fmt"
	"strings"
)

// Protocol is a layer-4 protocol. Only TCP and UDP are modeled, as in k8s.
type Protocol string

// Supported protocols.
const (
	ProtocolTCP Protocol = "TCP"
	ProtocolUDP Protocol = "UDP"
)

// ParseProtocol parses a protocol string case-insensitively.
func ParseProtocol(s string) (Protocol, error) {
	switch strings.ToUpper(strings.TrimSpace(s)) {
	case "TCP", "":
		return ProtocolTCP, nil
	case "UDP":
		return ProtocolUDP, nil
	default:
		return "", fmt.Errorf("unsupported protocol %q (want TCP or UDP)", s)
	}
}

// Port describes one port exposed by an endpoint. Named ports are resolved at
// evaluation time against the *destination* endpoint, never globally.
type Port struct {
	// Name is optional; when set it may be referenced by policy rules.
	Name string `json:"name,omitempty"`
	// Number is required (1..65535).
	Number int `json:"number"`
	// Protocol defaults to TCP.
	Protocol Protocol `json:"protocol,omitempty"`
}

// RulePort is a port reference inside a policy rule. It is either a numeric
// port or a named port; the JSON form mirrors k8s (a bare number or a string).
type RulePort struct {
	Number   int      `json:"number,omitempty"`
	Name     string   `json:"name,omitempty"`
	Protocol Protocol `json:"protocol,omitempty"`
}

// UnmarshalJSON accepts either 8080, "http" or {"number":8080,...}.
func (rp *RulePort) UnmarshalJSON(data []byte) error {
	trimmed := strings.TrimSpace(string(data))
	if len(trimmed) > 0 && trimmed[0] != '{' {
		// Bare scalar: number or quoted name.
		var n int
		if err := json.Unmarshal(data, &n); err == nil {
			rp.Number = n
			return nil
		}
		var name string
		if err := json.Unmarshal(data, &name); err != nil {
			return fmt.Errorf("rule port must be a number or string, got %s", trimmed)
		}
		rp.Name = name
		return nil
	}
	type alias RulePort
	var a alias
	if err := json.Unmarshal(data, &a); err != nil {
		return err
	}
	*rp = RulePort(a)
	return nil
}

// MarshalJSON renders bare scalars where possible, keeping fixtures readable.
func (rp RulePort) MarshalJSON() ([]byte, error) {
	if rp.Name != "" {
		return json.Marshal(rp.Name)
	}
	if rp.Protocol != "" {
		return json.Marshal(struct {
			Number   int      `json:"number"`
			Protocol Protocol `json:"protocol,omitempty"`
		}{Number: rp.Number, Protocol: rp.Protocol})
	}
	return json.Marshal(rp.Number)
}

// IsNamed reports whether the rule references a named port.
func (rp RulePort) IsNamed() bool { return rp.Name != "" }

// Peer is the remote side of a rule. All four scope knobs are independent;
// nil selectors mean "this knob is absent". An explicit empty selector {}
// means "match all" for that knob.
type Peer struct {
	PodSelector       *Selector `json:"podSelector,omitempty"`
	NamespaceSelector *Selector `json:"namespaceSelector,omitempty"`
}

// UnmarshalJSON keeps nil vs empty-selector distinguishable.
func (p *Peer) UnmarshalJSON(data []byte) error {
	var raw struct {
		PodSelector       json.RawMessage `json:"podSelector"`
		NamespaceSelector json.RawMessage `json:"namespaceSelector"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	var err error
	if p.PodSelector, err = ParseSelector(raw.PodSelector); err != nil {
		return fmt.Errorf("podSelector: %w", err)
	}
	if p.NamespaceSelector, err = ParseSelector(raw.NamespaceSelector); err != nil {
		return fmt.Errorf("namespaceSelector: %w", err)
	}
	return nil
}

// IngressRule allows traffic into a selected workload.
type IngressRule struct {
	Ports []RulePort `json:"ports,omitempty"`
	From  []Peer     `json:"from,omitempty"`
}

// EgressRule allows traffic out of a selected workload.
type EgressRule struct {
	Ports []RulePort `json:"ports,omitempty"`
	To    []Peer     `json:"to,omitempty"`
}

// Policy is a NetworkPolicy-shaped object. PodSelector is required and must be
// non-nil; {} selects all workloads in the policy's namespace.
type Policy struct {
	Name        string        `json:"name"`
	Namespace   string        `json:"namespace"`
	PodSelector Selector      `json:"podSelector"`
	PolicyTypes []PolicyType  `json:"policyTypes,omitempty"`
	Ingress     []IngressRule `json:"ingress,omitempty"`
	Egress      []EgressRule  `json:"egress,omitempty"`
}

// PolicyType marks a direction as isolated by the policy.
type PolicyType string

// Policy type tags.
const (
	PolicyTypeIngress PolicyType = "Ingress"
	PolicyTypeEgress  PolicyType = "Egress"
)

// EffectivePolicyTypes applies the k8s defaulting rule: an empty policyTypes
// list defaults to Ingress plus Egress when the policy carries egress rules,
// otherwise Ingress only. The defaulted list is deterministic (Ingress first).
func (p *Policy) EffectivePolicyTypes() []PolicyType {
	if len(p.PolicyTypes) > 0 {
		out := make([]PolicyType, len(p.PolicyTypes))
		copy(out, p.PolicyTypes)
		return out
	}
	out := []PolicyType{PolicyTypeIngress}
	if len(p.Egress) > 0 {
		out = append(out, PolicyTypeEgress)
	}
	return out
}

// HasPolicyType reports whether the (defaulted) policy covers a direction.
func (p *Policy) HasPolicyType(t PolicyType) bool {
	for _, pt := range p.EffectivePolicyTypes() {
		if pt == t {
			return true
		}
	}
	return false
}
