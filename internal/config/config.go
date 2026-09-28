// Package config parses and validates rule-set configuration files.
//
// It performs only syntactic and per-rule semantic validation; cross-rule
// first-match analysis lives in package analyzer. Errors carry stable
// category strings (see the ErrorCategory constants) so callers and tests
// can assert failure classes instead of matching message text.
package config

import (
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"strings"

	"netsem/internal/netmodel"
)

// ErrorCategory values are part of the API contract.
const (
	ErrUnknownProtocol        = "unknown_protocol"
	ErrInvalidCIDR            = "invalid_cidr"
	ErrInvalidPort            = "invalid_port"
	ErrMixedAddressFamily     = "mixed_address_family"
	ErrProtocolFamilyMismatch = "protocol_family_mismatch"
	ErrPortNotApplicable      = "port_not_applicable"
	ErrInvalidAction          = "invalid_action"
	ErrDuplicateRuleID        = "duplicate_rule_id"
	ErrInvalidConfig          = "invalid_config"
)

// ParseError is one configuration error with a machine-readable category.
type ParseError struct {
	Category string `json:"category"`
	RuleID   string `json:"rule_id,omitempty"`
	Message  string `json:"message"`
}

func (e *ParseError) Error() string {
	if e.RuleID != "" {
		return fmt.Sprintf("%s (rule %s): %s", e.Category, e.RuleID, e.Message)
	}
	return fmt.Sprintf("%s: %s", e.Category, e.Message)
}

// Rule is a validated rule in source order. Empty Sources/Destinations mean
// the wildcard over the family.
type Rule struct {
	ID       string              `json:"id"`
	Action   string              `json:"action"` // "allow" | "deny"
	ProtoTok string              `json:"protocol"`
	Proto    netmodel.Protocol   `json:"-"`
	Family   netmodel.Family     `json:"-"` // "" = applies to both families
	Sources  []netmodel.CIDRInfo `json:"-"`
	Dests    []netmodel.CIDRInfo `json:"-"`
	SrcPorts netmodel.Int1D      `json:"-"`
	DstPorts netmodel.Int1D      `json:"-"`

	// Notes are non-fatal findings surfaced separately from hard errors:
	// masked host bits, unknown numeric protocol numbers, etc.
	Notes []string `json:"-"`
}

// Ruleset is the parsed configuration.
type Ruleset struct {
	DefaultAction string `json:"default_action"` // "allow" | "deny"
	Rules         []Rule `json:"rules"`
}

type rawRule struct {
	ID               string `json:"id"`
	Action           string `json:"action"`
	Protocol         string `json:"protocol"`
	Source           string `json:"source"`
	Destination      string `json:"destination"`
	SourcePorts      string `json:"source_ports"`
	DestinationPorts string `json:"destination_ports"`
	Comment          string `json:"comment,omitempty"`
}

type rawConfig struct {
	DefaultAction string    `json:"default_action"`
	Rules         []rawRule `json:"rules"`
}

// Parse decodes and validates a JSON configuration stream. On hard errors it
// returns all errors found (a later error does not hide earlier ones).
func Parse(r io.Reader) (*Ruleset, []*ParseError) {
	var raw rawConfig
	dec := json.NewDecoder(r)
	if err := dec.Decode(&raw); err != nil {
		return nil, []*ParseError{{Category: ErrInvalidConfig, Message: "config is not valid JSON: " + err.Error()}}
	}
	var errs []*ParseError

	da := raw.DefaultAction
	if da == "" {
		da = "deny"
	}
	if da != "allow" && da != "deny" {
		errs = append(errs, &ParseError{Category: ErrInvalidAction, Message: fmt.Sprintf("default_action must be allow or deny, got %q", da)})
	}

	seen := map[string]bool{}
	rs := &Ruleset{DefaultAction: da}
	for i, rr := range raw.Rules {
		id := rr.ID
		if id == "" {
			id = fmt.Sprintf("rule#%d", i+1)
		}
		if seen[id] {
			errs = append(errs, &ParseError{Category: ErrDuplicateRuleID, RuleID: id, Message: "duplicate rule id"})
		}
		seen[id] = true

		rule := Rule{ID: id, Action: rr.Action, ProtoTok: rr.Protocol}
		if rr.Action != "allow" && rr.Action != "deny" {
			errs = append(errs, &ParseError{Category: ErrInvalidAction, RuleID: id, Message: fmt.Sprintf("action must be allow or deny, got %q", rr.Action)})
		}
		if rr.Protocol == "" {
			errs = append(errs, &ParseError{Category: ErrInvalidConfig, RuleID: id, Message: "protocol is required"})
			rs.Rules = append(rs.Rules, rule)
			continue
		}
		pset, proto, unknown, perr := netmodel.ProtocolSet(rr.Protocol)
		_ = pset
		if perr != nil {
			errs = append(errs, &ParseError{Category: ErrUnknownProtocol, RuleID: id, Message: perr.Error()})
			rs.Rules = append(rs.Rules, rule)
			continue
		}
		rule.Proto = proto
		if unknown {
			rule.Notes = append(rule.Notes, fmt.Sprintf("protocol number %s has no known name in the catalog; semantics (ports, ICMP-ness) are uncertain", rr.Protocol))
		}

		// Ports.
		sp, sperr := netmodel.ParsePortRange(orStar(rr.SourcePorts))
		dp, dperr := netmodel.ParsePortRange(orStar(rr.DestinationPorts))
		if sperr != nil {
			errs = append(errs, &ParseError{Category: ErrInvalidPort, RuleID: id, Message: "source_ports: " + sperr.Error()})
		}
		if dperr != nil {
			errs = append(errs, &ParseError{Category: ErrInvalidPort, RuleID: id, Message: "destination_ports: " + dperr.Error()})
		}
		rule.SrcPorts = sp
		rule.DstPorts = dp

		// Port applicability: non-port protocols collapse the port axes.
		if rr.Protocol != "any" && !proto.CarriesPorts {
			if rr.SourcePorts != "" && rr.SourcePorts != "*" && rr.SourcePorts != "any" {
				errs = append(errs, &ParseError{Category: ErrPortNotApplicable, RuleID: id, Message: fmt.Sprintf("protocol %q does not carry ports; source_ports must be *", rr.Protocol)})
			}
			if rr.DestinationPorts != "" && rr.DestinationPorts != "*" && rr.DestinationPorts != "any" {
				errs = append(errs, &ParseError{Category: ErrPortNotApplicable, RuleID: id, Message: fmt.Sprintf("protocol %q does not carry ports; destination_ports must be *", rr.Protocol)})
			}
			rule.SrcPorts = netmodel.Point1D(netmodel.BitsPort, big.NewInt(0))
			rule.DstPorts = netmodel.Point1D(netmodel.BitsPort, big.NewInt(0))
		}

		// "any" with restricted ports is a real configuration but its
		// semantics are subtle: flag it as an uncertainty so it is never
		// confused with "all traffic to those ports".
		if rr.Protocol == "any" && sperr == nil && dperr == nil &&
			(!sp.Equals(netmodel.Universe1D(netmodel.BitsPort)) ||
				!dp.Equals(netmodel.Universe1D(netmodel.BitsPort))) {
			rule.Notes = append(rule.Notes, `protocol "any" with restricted ports: port constraints apply only to port-bearing protocols (tcp/udp/sctp); non-port protocols (icmp, unknown numbers, ...) match regardless of port fields`)
		}

		// Source and destination CIDRs.
		fam := proto.Family
		srcs, srcFam, srcErr := parseCIDRField(rr.Source, "source", id, proto.Family, &rule, &errs)
		dsts, dstFam, dstErr := parseCIDRField(rr.Destination, "destination", id, proto.Family, &rule, &errs)
		rule.Sources, rule.Dests = srcs, dsts

		switch {
		case srcErr && dstErr:
			// family unknown; leave both-family projection off
		case srcErr:
			fam = dstFam
		case dstErr:
			fam = srcFam
		default:
			if srcFam != "" && dstFam != "" && srcFam != dstFam {
				errs = append(errs, &ParseError{Category: ErrMixedAddressFamily, RuleID: id,
					Message: fmt.Sprintf("source is %s but destination is %s; one rule cannot span families", srcFam, dstFam)})
			}
			if srcFam != "" {
				fam = srcFam
			} else if dstFam != "" {
				fam = dstFam
			}
		}
		rule.Family = fam
		rs.Rules = append(rs.Rules, rule)
	}
	if len(errs) > 0 {
		return rs, errs
	}
	return rs, nil
}

// parseCIDRField parses one CIDR field. The field may be a wildcard ("",
// "*", "any") or a comma-separated list of CIDRs (their union matches). The
// bool reports a hard parse error. The returned family is "" for wildcard
// and all listed CIDRs must share one family.
func parseCIDRField(tok, label, ruleID string, protoFam netmodel.Family, rule *Rule, errs *[]*ParseError) ([]netmodel.CIDRInfo, netmodel.Family, bool) {
	if tok == "" || tok == "*" || tok == "any" {
		return nil, "", false
	}
	var out []netmodel.CIDRInfo
	var fam netmodel.Family
	hardErr := false
	for _, part := range strings.Split(tok, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		c, err := netmodel.ParseCIDR(part)
		if err != nil {
			*errs = append(*errs, &ParseError{Category: ErrInvalidCIDR, RuleID: ruleID, Message: label + ": " + err.Error()})
			hardErr = true
			continue
		}
		if c.HostBitsSet {
			rule.Notes = append(rule.Notes, fmt.Sprintf("%s CIDR %s had host bits set; matching uses masked prefix %s", label, part, c.Prefix.String()))
		}
		if protoFam != "" && c.Family != protoFam {
			*errs = append(*errs, &ParseError{Category: ErrProtocolFamilyMismatch, RuleID: ruleID,
				Message: fmt.Sprintf("protocol only applies to %s but %s CIDR %s is %s", protoFam, label, part, c.Family)})
			hardErr = true
			continue
		}
		if fam == "" {
			fam = c.Family
		} else if fam != c.Family {
			*errs = append(*errs, &ParseError{Category: ErrMixedAddressFamily, RuleID: ruleID,
				Message: fmt.Sprintf("%s field mixes %s and %s CIDRs", label, fam, c.Family)})
			hardErr = true
			continue
		}
		out = append(out, c)
	}
	return out, fam, hardErr
}

func orStar(s string) string {
	if s == "" {
		return "*"
	}
	return s
}
