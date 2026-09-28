// Package oracle is an INDEPENDENT re-implementation of first-match rule
// semantics used only as the reference answer for the black-box test suite.
//
// It deliberately shares no code with the implementation under test: it does
// not import netsem's analyzer/netmodel/config packages. Rules and answers
// are derived directly from first principles here, then compared against the
// running service's HTTP responses.
package oracle

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"strconv"
	"strings"
)

// PortRange is an inclusive port interval; Lo==0&&Hi==65535 means wildcard.
type PortRange struct{ Lo, Hi int }

// Spec is one rule in the reference model.
type Spec struct {
	ID       string
	Action   string // "allow" | "deny"
	Family   string // "ipv4" | "ipv6" | "any" (both)
	Proto    string // "tcp" | "udp" | "sctp" | "icmp" | "icmpv6" | "any" | decimal "0".."255"
	SrcCIDRs []string
	DstCIDRs []string
	SrcPorts PortRange
	DstPorts PortRange
}

// Pk is a concrete packet in one family.
type Pk struct {
	Family  string
	Proto   int
	Src     string
	Dst     string
	SrcPort int
	DstPort int
}

// Model is the ordered reference ruleset plus default action.
type Model struct {
	DefaultAction string
	Rules         []Spec
}

var portBearing = map[int]bool{6: true, 17: true, 132: true}

func protoNumber(tok string) (int, bool) {
	switch tok {
	case "tcp":
		return 6, true
	case "udp":
		return 17, true
	case "sctp":
		return 132, true
	case "icmp":
		return 1, true
	case "icmpv6":
		return 58, true
	case "any":
		return -1, true
	}
	n, err := strconv.Atoi(tok)
	if err != nil || n < 0 || n > 255 {
		return 0, false
	}
	return n, true
}

// ruleFamily returns the families a rule projects into.
func ruleFamily(s Spec) []string {
	if s.Family == "ipv4" {
		return []string{"ipv4"}
	}
	if s.Family == "ipv6" {
		return []string{"ipv6"}
	}
	return []string{"ipv4", "ipv6"}
}

func inRange(p *int, r PortRange) bool { return *p >= r.Lo && *p <= r.Hi }

func cidrContains(list []string, addr string) bool {
	if len(list) == 0 {
		return true
	}
	a := netip.MustParseAddr(addr)
	for _, c := range list {
		pfx := netip.MustParsePrefix(c)
		if a.Is4() != pfx.Addr().Is4() {
			continue
		}
		if pfx.Contains(a) {
			return true
		}
	}
	return false
}

// Matches reports whether packet p lies in the rule's region (ignoring
// order). This is the independent semantic definition.
func Matches(s Spec, p Pk) bool {
	okFam := false
	for _, f := range ruleFamily(s) {
		if f == p.Family {
			okFam = true
		}
	}
	if !okFam {
		return false
	}
	n, ok := protoNumber(s.Proto)
	if !ok {
		return false
	}
	if s.Proto != "any" && p.Proto != n {
		return false
	}
	if !cidrContains(s.SrcCIDRs, p.Src) || !cidrContains(s.DstCIDRs, p.Dst) {
		return false
	}
	// Port semantics: only tcp/udp/sctp carry ports. A rule of "any" with
	// restricted ports applies those restrictions to port-bearing protocols
	// and matches other protocols regardless of the port fields.
	restricted := !(s.SrcPorts.Lo == 0 && s.SrcPorts.Hi == 65535 &&
		s.DstPorts.Lo == 0 && s.DstPorts.Hi == 65535)
	portsOK := inRange(&p.SrcPort, s.SrcPorts) && inRange(&p.DstPort, s.DstPorts)
	if s.Proto == "any" {
		if portBearing[p.Proto] {
			return portsOK
		}
		return !restricted || true
	}
	if portBearing[p.Proto] {
		return portsOK
	}
	// Explicit non-port protocol (icmp, icmpv6, numeric): port axes ignored.
	return true
}

// Decision is an oracle result.
type Decision struct {
	Action    string // "allow" | "deny"
	RuleID    string // "" when default
	ByDefault bool
}

// Decide evaluates strict first-match.
func (m *Model) Decide(p Pk) Decision {
	for _, s := range m.Rules {
		if Matches(s, p) {
			return Decision{Action: s.Action, RuleID: s.ID}
		}
	}
	return Decision{Action: m.DefaultAction, ByDefault: true}
}

// Without returns a copy of the model with one rule removed.
func (m *Model) Without(id string) *Model {
	cp := &Model{DefaultAction: m.DefaultAction}
	for _, r := range m.Rules {
		if r.ID != id {
			cp.Rules = append(cp.Rules, r)
		}
	}
	return cp
}

// --- config serialization (the wire format the service defines) ---

type wireRule struct {
	ID               string `json:"id"`
	Action           string `json:"action"`
	Protocol         string `json:"protocol"`
	Source           string `json:"source"`
	Destination      string `json:"destination"`
	SourcePorts      string `json:"source_ports"`
	DestinationPorts string `json:"destination_ports"`
}

type wireConfig struct {
	DefaultAction string     `json:"default_action"`
	Rules         []wireRule `json:"rules"`
}

func portString(r PortRange) string {
	if r.Lo == 0 && r.Hi == 65535 {
		return "*"
	}
	if r.Lo == r.Hi {
		return strconv.Itoa(r.Lo)
	}
	return fmt.Sprintf("%d-%d", r.Lo, r.Hi)
}

// ConfigJSON renders the ruleset in the service's configuration format.
func (m *Model) ConfigJSON() []byte {
	cfg := wireConfig{DefaultAction: m.DefaultAction}
	for _, s := range m.Rules {
		fam := s.Family
		if fam == "" {
			fam = "any"
		}
		wr := wireRule{
			ID: s.ID, Action: s.Action, Protocol: s.Proto,
			Source:           strings.Join(s.SrcCIDRs, ","),
			Destination:      strings.Join(s.DstCIDRs, ","),
			SourcePorts:      portString(s.SrcPorts),
			DestinationPorts: portString(s.DstPorts),
		}
		// Wire format uses addresses to pin the family; a both-family rule
		// leaves source/destination empty.
		if fam == "ipv4" && len(s.SrcCIDRs) == 0 && len(s.DstCIDRs) == 0 {
			wr.Source, wr.Destination = "", ""
		}
		cfg.Rules = append(cfg.Rules, wr)
	}
	b, _ := json.Marshal(cfg)
	return b
}
