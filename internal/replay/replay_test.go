package replay

import (
	"strings"
	"testing"

	"netsem/internal/analyzer"
	"netsem/internal/config"
)

func evaluatorFor(t *testing.T, text string) *Evaluator {
	t.Helper()
	rs, errs := config.Parse(strings.NewReader(text))
	if len(errs) > 0 {
		t.Fatalf("parse: %+v", errs)
	}
	regions, err := analyzer.Compile(rs)
	if err != nil {
		t.Fatal(err)
	}
	return NewEvaluator(1, rs.DefaultAction, regions, nil)
}

func TestFirstMatchStepsAndDefault(t *testing.T) {
	ev := evaluatorFor(t, `{
		"default_action":"deny",
		"rules":[
			{"id":"web","action":"allow","protocol":"tcp","source":"10.0.0.0/30","destination":"10.0.1.0/30","destination_ports":"80"}
		]
	}`)

	d := ev.Evaluate(PacketRequest{
		Family: "ipv4", Protocol: "tcp",
		SourceAddress: "10.0.0.1", DestinationAddress: "10.0.1.1",
		SourcePort: intp(3000), DestinationPort: intp(80),
	})
	if d.Decision != "allow" || d.MatchedRuleID != "web" || d.DecidedBy != "rule:web" {
		t.Fatalf("unexpected decision: %+v", d)
	}
	if len(d.Steps) != 1 || !d.Steps[0].Matched {
		t.Fatalf("must stop at first match: %+v", d.Steps)
	}

	d = ev.Evaluate(PacketRequest{
		Family: "ipv4", Protocol: "tcp",
		SourceAddress: "10.0.0.1", DestinationAddress: "10.0.1.1",
		SourcePort: intp(3000), DestinationPort: intp(81),
	})
	if d.Decision != "deny" || d.MatchedRuleID != "" || d.DecidedBy != "default" {
		t.Fatalf("unmatched packet should reach default: %+v", d)
	}
	last := d.Steps[len(d.Steps)-1]
	if last.RuleID != "default" || last.Action != "deny" {
		t.Fatalf("default step missing: %+v", d.Steps)
	}
}

func TestInvalidPacketSeparatesErrors(t *testing.T) {
	ev := evaluatorFor(t, `{"default_action":"deny","rules":[]}`)
	d := ev.Evaluate(PacketRequest{Family: "ipv4", Protocol: "tcp", SourceAddress: "bad", DestinationAddress: "10.0.0.1"})
	if d.Certain || len(d.Errors) == 0 || d.Decision != "deny" {
		t.Fatalf("invalid packet must be uncertain deny with errors: %+v", d)
	}
}

func intp(n int) *int { return &n }
