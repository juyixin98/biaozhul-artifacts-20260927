package analyzer

import (
	"strings"
	"testing"

	"netsem/internal/config"
)

func parseConfig(t *testing.T, jsonText string) *config.Ruleset {
	t.Helper()
	rs, errs := config.Parse(strings.NewReader(jsonText))
	if len(errs) > 0 {
		t.Fatalf("unexpected parse errors: %+v", errs)
	}
	return rs
}

func findDiag(t *testing.T, rep *Report, ruleID, family, kind string) Diagnostic {
	t.Helper()
	for _, d := range rep.Diagnostics {
		if d.RuleID == ruleID && d.Family == family && d.Kind == kind {
			return d
		}
	}
	t.Fatalf("diagnostic %s/%s/%s not found; have %+v", ruleID, family, kind, rep.Diagnostics)
	return Diagnostic{}
}

func TestShadowingAndWitness(t *testing.T) {
	rs := parseConfig(t, `{
		"default_action": "deny",
		"rules": [
			{"id":"w","action":"allow","protocol":"tcp","source":"10.0.0.0/30","destination":"10.0.1.0/30","destination_ports":"80-90"},
			{"id":"n","action":"deny","protocol":"tcp","source":"10.0.0.0/30","destination":"10.0.1.0/30","destination_ports":"85"},
			{"id":"p","action":"allow","protocol":"tcp","source":"10.0.0.0/30","destination":"10.0.1.0/30","destination_ports":"90-92"}
		]
	}`)
	rep, err := Analyze(rs)
	if err != nil {
		t.Fatal(err)
	}
	// n is fully shadowed by w.
	full := findDiag(t, rep, "n", "ipv4", DiagFullShadow)
	if full.Witness == nil || full.Witness.MatchedRuleID != "w" {
		t.Fatalf("witness for n must cite w: %+v", full.Witness)
	}
	if full.Witness.DestinationPort != 85 {
		t.Fatalf("witness dport want 85, got %d", full.Witness.DestinationPort)
	}
	// p partially shadowed: 90 is inside w, 91-92 live.
	part := findDiag(t, rep, "p", "ipv4", DiagPartialShadow)
	if len(part.Covered) != 1 || len(part.Covered[0].Products) == 0 {
		t.Fatalf("covered partition missing: %+v", part.Covered)
	}
	found := false
	for _, pr := range part.Covered[0].Products {
		if strings.Contains(pr.DestinationPorts, "91") || strings.Contains(pr.DestinationPorts, "92") {
			found = true
		}
		if pr.DestinationPorts == "90" {
			t.Fatalf("live partition must not contain only shadowed port 90")
		}
	}
	if !found {
		t.Fatalf("covered partition must retain live ports 91-92: %+v", part.Covered[0].Products)
	}
	// w must be clean.
	for _, d := range rep.Diagnostics {
		if d.RuleID == "w" {
			t.Fatalf("w must not be flagged: %+v", d)
		}
	}
}

func TestRedundantDefaultDeny(t *testing.T) {
	rs := parseConfig(t, `{
		"default_action": "deny",
		"rules": [
			{"id":"d","action":"deny","protocol":"udp","source":"192.168.0.0/24","destination":"10.0.0.0/24"}
		]
	}`)
	rep, err := Analyze(rs)
	if err != nil {
		t.Fatal(err)
	}
	findDiag(t, rep, "d", "ipv4", DiagRedundant)
}

func TestIPv6Separation(t *testing.T) {
	rs := parseConfig(t, `{
		"default_action": "deny",
		"rules": [
			{"id":"v4","action":"allow","protocol":"tcp","source":"10.0.0.0/30","destination":"10.0.1.0/30","destination_ports":"80"},
			{"id":"v6","action":"allow","protocol":"tcp","source":"2001:db8::/126","destination":"2001:db9::/126","destination_ports":"80"}
		]
	}`)
	rep, err := Analyze(rs)
	if err != nil {
		t.Fatal(err)
	}
	if len(rep.Diagnostics) != 0 {
		t.Fatalf("independent families must not shadow each other: %+v", rep.Diagnostics)
	}
}
