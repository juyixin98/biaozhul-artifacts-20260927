package analyzercheck_test

import (
	"os"
	"testing"

	"fwrule/internal/analyzer"
	"fwrule/internal/config"
)

// TestIPv6SeparateModel verifies the IPv6 path independently: the analyzer
// builds a v6 partition (128-bit geometry) and classifies v6 rules exactly as
// the analogous v4 structure would classify, without any v4 cross-talk.
func TestIPv6SeparateModel(t *testing.T) {
	raw, err := os.ReadFile("../testdata/v6-mini.json")
	if err != nil {
		t.Fatal(err)
	}
	pol, err := config.Load(raw, "v6-mini")
	if err != nil {
		t.Fatal(err)
	}
	rep := analyzer.Analyze(pol, 0)
	k := kindsByRule(rep)

	if !k["v6-full-shadow"][analyzer.CatFullShadow] {
		t.Fatalf("v6-full-shadow kinds=%v", k["v6-full-shadow"])
	}
	if !k["v6-tail-deny-echo"][analyzer.CatRedundant] {
		t.Fatalf("v6-tail-deny-echo kinds=%v (default deny should make it redundant)",
			k["v6-tail-deny-echo"])
	}
	if k["v6-allow-broad"] != nil {
		t.Fatalf("v6-allow-broad should be clean, got %v", k["v6-allow-broad"])
	}

	// The full-shadow witness must be a v6 packet decided by an earlier rule.
	for _, d := range rep.Diagnostics {
		if d.RuleID != "v6-full-shadow" {
			continue
		}
		if d.Witness == nil || d.Witness.Family != "ipv6" {
			t.Fatalf("v6 witness missing/wrong family: %+v", d.Witness)
		}
		if d.Witness.DecidedBy != "v6-allow-broad" {
			t.Fatalf("witness decided_by=%s", d.Witness.DecidedBy)
		}
	}
}
