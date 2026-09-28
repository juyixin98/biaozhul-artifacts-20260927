// Package domain holds the resource model of the restricted container network
// policy system: namespaces, workload endpoints, label selectors and
// NetworkPolicy-like policy objects. It has no dependencies on storage,
// transport or evaluation logic.
package domain

import (
	"encoding/json"
	"fmt"
)

// Requirement is one label-based constraint, following the shape of a
// Kubernetes LabelSelectorRequirement.
type Requirement struct {
	Key      string   `json:"key"`
	Operator Operator `json:"operator"`
	Values   []string `json:"values"`
}

// Operator enumerates the supported selector operators.
type Operator string

// Supported selector operators (mirrors the k8s label selector operators).
const (
	OpIn           Operator = "In"
	OpNotIn        Operator = "NotIn"
	OpExists       Operator = "Exists"
	OpDoesNotExist Operator = "DoesNotExist"
)

// Selector is a label selector. The zero value is nil-ish: prefer constructing
// it through Parse so that an empty selector object ({}) is distinguishable
// from "no selector present" at the call site where that distinction matters
// (policy peer blocks).
type Selector struct {
	MatchLabels map[string]string `json:"matchLabels,omitempty"`
	MatchExprs  []Requirement     `json:"matchExpressions,omitempty"`
}

// ParseSelector decodes a raw JSON selector. A nil/absent raw value yields
// (nil, nil) meaning "no selector"; an empty object {} yields a non-nil
// selector that matches every label set.
func ParseSelector(raw json.RawMessage) (*Selector, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var s Selector
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, err
	}
	return &s, nil
}

// IsAbsent reports whether the selector is absent (nil). A non-nil empty
// selector {} is NOT absent: it matches everything.
func (s *Selector) IsAbsent() bool { return s == nil }

// Matches evaluates the selector against a label set. An absent selector
// (nil) matches nothing; an empty selector {} matches everything.
func (s *Selector) Matches(labels map[string]string) bool {
	if s == nil {
		return false
	}
	for k, v := range s.MatchLabels {
		if got, ok := labels[k]; !ok || got != v {
			return false
		}
	}
	for _, req := range s.MatchExprs {
		if !matchesExpr(labels, req) {
			return false
		}
	}
	return true
}

func matchesExpr(labels map[string]string, req Requirement) bool {
	v, present := labels[req.Key]
	switch req.Operator {
	case OpIn:
		return present && contains(req.Values, v)
	case OpNotIn:
		// k8s semantics: a missing key satisfies NotIn.
		return !present || !contains(req.Values, v)
	case OpExists:
		return present
	case OpDoesNotExist:
		return !present
	default:
		return false
	}
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}

// Validate applies k8s-shaped structural rules to a selector.
func (s *Selector) Validate() error {
	if s == nil {
		return nil
	}
	for k := range s.MatchLabels {
		if err := validateLabelKey(k); err != nil {
			return fmt.Errorf("matchLabels: %w", err)
		}
	}
	for _, req := range s.MatchExprs {
		if err := validateLabelKey(req.Key); err != nil {
			return err
		}
		switch req.Operator {
		case OpIn, OpNotIn:
			if len(req.Values) == 0 {
				return fmt.Errorf("matchExpressions: operator %s requires at least one value for key %q", req.Operator, req.Key)
			}
		case OpExists, OpDoesNotExist:
			if len(req.Values) != 0 {
				return fmt.Errorf("matchExpressions: operator %s takes no values for key %q", req.Operator, req.Key)
			}
		default:
			return fmt.Errorf("matchExpressions: unknown operator %q for key %q", req.Operator, req.Key)
		}
	}
	return nil
}
