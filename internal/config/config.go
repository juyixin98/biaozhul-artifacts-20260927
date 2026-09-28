// Package config parses and validates firewall policy configuration into the
// typed netmodel representation. Parsing is strict: typos in protocol names
// are hard errors, never silently widened rules.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"fwrule/internal/netmodel"
)

// Action is a rule/default disposition.
type Action string

const (
	ActionAllow Action = "allow"
	ActionDeny  Action = "deny"
)

// RuleSpec is one rule as written in JSON configuration.
type RuleSpec struct {
	ID       string `json:"id"`
	Action   string `json:"action"`
	Protocol string `json:"protocol"`
	SrcCIDR  string `json:"src_cidr"`
	DstCIDR  string `json:"dst_cidr"`
	// SrcPort / DstPort are optional and only valid for tcp/udp. Accepts
	// "80", "8000-9000" or "any".
	SrcPort string `json:"src_port,omitempty"`
	DstPort string `json:"dst_port,omitempty"`
}

// policyFile is the on-disk schema. DefaultAction may be a shorthand string
// ("deny" applies to both families) or an object {"ipv4":..,"ipv6":..}.
type policyFile struct {
	Name          string          `json:"name"`
	DefaultAction json.RawMessage `json:"default_action"`
	Rules         []RuleSpec      `json:"rules"`
}

// CompilationNote is a non-fatal problem attached to a rule (empty match from
// a v4/v6 cross, etc.). Fatal problems are returned as compile errors.
type CompilationNote struct {
	RuleID string `json:"rule_id"`
	Code   string `json:"code"`
	Detail string `json:"detail"`
}

// CompiledRule is a validated rule in typed form.
type CompiledRule struct {
	Index  int
	ID     string
	Action Action
	Box    netmodel.MatchBox
	// ProtocolKnown is false for an unassigned numeric protocol (e.g. 99).
	// The rule still models exactly protocol 99; everything touching it is
	// flagged uncertain rather than guessed.
	ProtocolKnown bool
	// MatchEmpty is true when the rule can never match any packet (currently:
	// source and destination CIDRs from different families).
	MatchEmpty  bool
	EmptyReason string
	Spec        RuleSpec
}

// Policy is a fully parsed configuration.
type Policy struct {
	Name    string
	Default map[netmodel.Family]Action
	Rules   []*CompiledRule
	Notes   []CompilationNote
}

// LoadFile reads, parses and validates a policy JSON file.
func LoadFile(path string) (*Policy, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read policy %s: %w", path, err)
	}
	return Load(raw, path)
}

// Load parses and validates policy JSON bytes. source labels error messages.
func Load(raw []byte, source string) (*Policy, error) {
	var f policyFile
	if err := json.Unmarshal(raw, &f); err != nil {
		return nil, fmt.Errorf("%s: invalid JSON: %w", source, err)
	}
	if strings.TrimSpace(f.Name) == "" {
		return nil, fmt.Errorf("%s: policy name is required", source)
	}

	def, err := parseDefault(f.DefaultAction)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", source, err)
	}

	seenIDs := map[string]bool{}
	pol := &Policy{Name: f.Name, Default: def}
	for i, spec := range f.Rules {
		where := fmt.Sprintf("%s: rule[%d] (%s)", source, i, spec.ID)
		if strings.TrimSpace(spec.ID) == "" {
			return nil, fmt.Errorf("%s: missing id", where)
		}
		if seenIDs[spec.ID] {
			return nil, fmt.Errorf("%s: duplicate rule id", where)
		}
		seenIDs[spec.ID] = true

		var act Action
		switch Action(strings.ToLower(strings.TrimSpace(spec.Action))) {
		case ActionAllow:
			act = ActionAllow
		case ActionDeny:
			act = ActionDeny
		default:
			return nil, fmt.Errorf("%s: invalid action %q (want allow|deny)",
				where, spec.Action)
		}

		proto, err := netmodel.ParseProtocol(spec.Protocol)
		if err != nil {
			return nil, fmt.Errorf("%s: %w [UNKNOWN_PROTOCOL]", where, err)
		}

		src, err := netmodel.ParseCIDR(strings.TrimSpace(spec.SrcCIDR))
		if err != nil {
			return nil, fmt.Errorf("%s: bad src_cidr %q: %w", where, spec.SrcCIDR, err)
		}
		dst, err := netmodel.ParseCIDR(strings.TrimSpace(spec.DstCIDR))
		if err != nil {
			return nil, fmt.Errorf("%s: bad dst_cidr %q: %w", where, spec.DstCIDR, err)
		}

		srcPorts := netmodel.FullPorts
		dstPorts := netmodel.FullPorts
		if spec.SrcPort != "" || spec.DstPort != "" {
			if !proto.PortBearing() {
				return nil, fmt.Errorf(
					"%s: ports are only valid for tcp/udp, protocol is %s [PORT_NOT_ALLOWED]",
					where, proto)
			}
			if spec.SrcPort != "" {
				srcPorts, err = netmodel.ParsePortInterval(spec.SrcPort)
				if err != nil {
					return nil, fmt.Errorf("%s: bad src_port: %w", where, err)
				}
			}
			if spec.DstPort != "" {
				dstPorts, err = netmodel.ParsePortInterval(spec.DstPort)
				if err != nil {
					return nil, fmt.Errorf("%s: bad dst_port: %w", where, err)
				}
			}
		}

		cr := &CompiledRule{
			Index:         i,
			ID:            spec.ID,
			Action:        act,
			ProtocolKnown: proto.Kind == netmodel.ProtoAny || proto.KnownName,
			Spec:          spec,
		}

		if src.Fam != dst.Fam {
			// Not a hard error: it is a validly-parsed rule whose match set
			// is provably empty. The analyzer reports EMPTY_MATCH with the
			// reason; the rule is always removable.
			cr.MatchEmpty = true
			cr.EmptyReason = fmt.Sprintf(
				"src family %s != dst family %s: no packet can satisfy both",
				src.Fam, dst.Fam)
			// Still build a box (uses the src family) so the rule has a typed
			// representation, but the analyzer excludes empty rules from
			// geometry and handles them directly.
			cr.Box = netmodel.NewBox(src.Fam, proto, src, dst, srcPorts, dstPorts)
			pol.Notes = append(pol.Notes, CompilationNote{
				RuleID: spec.ID, Code: "EMPTY_MATCH", Detail: cr.EmptyReason,
			})
		} else {
			cr.Box = netmodel.NewBox(src.Fam, proto, src, dst, srcPorts, dstPorts)
		}
		pol.Rules = append(pol.Rules, cr)
	}
	return pol, nil
}

func parseDefault(raw json.RawMessage) (map[netmodel.Family]Action, error) {
	if len(raw) == 0 {
		return nil, fmt.Errorf("default_action is required (\"allow\"/\"deny\" or {\"ipv4\":..,\"ipv6\":..})")
	}
	var shorthand string
	if err := json.Unmarshal(raw, &shorthand); err == nil {
		a, err := normAction(shorthand)
		if err != nil {
			return nil, err
		}
		return map[netmodel.Family]Action{netmodel.FamV4: a, netmodel.FamV6: a}, nil
	}
	var perFamily struct {
		IPv4 string `json:"ipv4"`
		IPv6 string `json:"ipv6"`
	}
	if err := json.Unmarshal(raw, &perFamily); err != nil {
		return nil, fmt.Errorf("default_action must be a string or {ipv4,ipv6} object: %w", err)
	}
	out := map[netmodel.Family]Action{}
	if perFamily.IPv4 != "" {
		a, err := normAction(perFamily.IPv4)
		if err != nil {
			return nil, fmt.Errorf("default_action.ipv4: %w", err)
		}
		out[netmodel.FamV4] = a
	}
	if perFamily.IPv6 != "" {
		a, err := normAction(perFamily.IPv6)
		if err != nil {
			return nil, fmt.Errorf("default_action.ipv6: %w", err)
		}
		out[netmodel.FamV6] = a
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("default_action must specify at least one family")
	}
	return out, nil
}

func normAction(s string) (Action, error) {
	switch Action(strings.ToLower(strings.TrimSpace(s))) {
	case ActionAllow:
		return ActionAllow, nil
	case ActionDeny:
		return ActionDeny, nil
	default:
		return "", fmt.Errorf("invalid action %q (want allow|deny)", s)
	}
}

// DefaultFor returns the default action for a family or an error (reported as
// DEFAULT_NOT_CONFIGURED) when the policy does not cover it.
func (p *Policy) DefaultFor(f netmodel.Family) (Action, bool) {
	a, ok := p.Default[f]
	return a, ok
}
