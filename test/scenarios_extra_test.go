package indcheck_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"testing"

	"netsem-test/harness"
	"netsem-test/oracle"
)

// Scenario 9: explainability and request correlation. The evaluate response
// and the persisted log row share a request id; both carry the config
// version, ordered steps, instance and source location. Invalid input yields
// a non-2xx decision whose failure reasons are listed separately from
// uncertainties.
func TestScenario9_ExplainabilityAndCorrelation(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	m := &oracle.Model{
		DefaultAction: "deny",
		Rules: []oracle.Spec{
			{ID: "allow-web", Action: "allow", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 80}},
			{ID: "deny-rest", Action: "deny", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 1, Hi: 65535}},
		},
	}
	cr := submit(t, h, m)
	if !cr.ParseOK {
		t.Fatalf("parse: %+v", cr.ParseErrors)
	}

	pk := oracle.Pk{Family: "ipv4", Proto: 6,
		Src: oracle.Addrs("ipv4", netA)[0], Dst: oracle.Addrs("ipv4", netB)[0],
		SrcPort: 5000, DstPort: 80}
	d := evalPacket(t, h, pk)

	// Request identity.
	if d.RequestID == "" || len(d.RequestID) < 8 {
		t.Fatalf("missing request id: %q", d.RequestID)
	}
	if d.Version != cr.Version {
		t.Fatalf("decision version %d != config version %d", d.Version, cr.Version)
	}
	if d.Instance != "test-instance" || d.SourceLocation == "" {
		t.Fatalf("decision lacks processing location: instance=%q loc=%q", d.Instance, d.SourceLocation)
	}
	// Steps must visit the first (matching) rule in order and stop.
	if len(d.Steps) != 1 || !d.Steps[0].Matched || d.Steps[0].RuleID != "allow-web" ||
		d.Steps[0].Order != 1 || d.Steps[0].Action != "allow" {
		t.Fatalf("unexpected steps: %+v", d.Steps)
	}

	// The correlated log row must be retrievable by request id and agree.
	var logged requestLog
	harness.MustGet(t, h.BaseURL+"/requests/"+d.RequestID, http.StatusOK, &logged)
	if logged.RequestID != d.RequestID || logged.Version != d.Version {
		t.Fatalf("log row identity mismatch: %+v", logged)
	}
	if logged.Decision != "allow" || logged.MatchedRuleID != "allow-web" {
		t.Fatalf("log row decision mismatch: %+v", logged)
	}
	if logged.Instance != d.Instance || logged.SourceLocation != d.SourceLocation {
		t.Fatalf("log row location mismatch: %+v vs %+v", logged, d)
	}
	if len(logged.Steps) != 1 || logged.Steps[0].RuleID != "allow-web" {
		t.Fatalf("log row steps mismatch: %+v", logged.Steps)
	}

	// Unknown request id is a clean 404.
	harness.MustGet(t, h.BaseURL+"/requests/req_does_not_exist", http.StatusNotFound, nil)

	// A non-matching packet falls through to the default; the final step
	// records that explicitly.
	pk2 := oracle.Pk{Family: "ipv4", Proto: 6,
		Src: oracle.Addrs("ipv4", netC)[0], Dst: oracle.Addrs("ipv4", netB)[0],
		SrcPort: 1, DstPort: 80}
	d2 := evalPacket(t, h, pk2)
	if d2.Decision != "deny" || d2.DecidedBy != "default" {
		t.Fatalf("expected default deny, got %s/%s", d2.Decision, d2.DecidedBy)
	}
	last := d2.Steps[len(d2.Steps)-1]
	if last.RuleID != "default" || last.Action != "deny" || !last.Matched {
		t.Fatalf("final step must record default deny, got %+v", last)
	}

	// Invalid input: HTTP 422, hard failure reasons in errors, decision
	// deny, and the row is still persisted for correlation.
	body, _ := json.Marshal(map[string]any{
		"family":              "ipv4",
		"protocol":            "tcp",
		"source_address":      "10.0.0.1",
		"destination_address": "not-an-ip",
	})
	resp, err := http.Post(h.BaseURL+"/evaluate", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnprocessableEntity {
		t.Fatalf("want 422 for invalid packet, got %d", resp.StatusCode)
	}
	var bad decision
	if err := json.NewDecoder(resp.Body).Decode(&bad); err != nil {
		t.Fatal(err)
	}
	if bad.Certain {
		t.Fatalf("invalid packet must be marked uncertain")
	}
	if len(bad.Errors) == 0 || bad.Decision != "deny" || bad.DecidedBy != "error" {
		t.Fatalf("failure reasons must be listed separately and denied: %+v", bad)
	}
	var badLog requestLog
	harness.MustGet(t, h.BaseURL+"/requests/"+bad.RequestID, http.StatusOK, &badLog)
	if badLog.Certain || len(badLog.Errors) == 0 {
		t.Fatalf("persisted row must retain the failure reasons separately: %+v", badLog)
	}

	// Report endpoints round-trip versioning.
	var rep report
	harness.MustGet(t, fmt.Sprintf("%s/reports/%d", h.BaseURL, cr.Version), http.StatusOK, &rep)
	if rep.DefaultAction != "deny" {
		t.Fatalf("report version mismatch")
	}
	harness.MustGet(t, h.BaseURL+"/reports/9999", http.StatusNotFound, nil)
}

// Scenario 10: address-axis shadowing — equal ports, different source CIDRs;
// a later rule inside an earlier source prefix is fully shadowed.
func TestScenario10_AddressAxisShadowing(t *testing.T) {
	h := harness.New(t)
	defer h.Close(t)

	inner := "10.0.0.0/31" // .0,.1 ⊂ netA (.0..3)
	m := &oracle.Model{
		DefaultAction: "deny",
		Rules: []oracle.Spec{
			{ID: "outer", Action: "allow", Proto: "tcp", SrcCIDRs: []string{netA}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 80}},
			{ID: "inner", Action: "deny", Proto: "tcp", SrcCIDRs: []string{inner}, DstCIDRs: []string{netB},
				SrcPorts: fullPorts(), DstPorts: oracle.PortRange{Lo: 80, Hi: 80}},
		},
	}
	cr := submit(t, h, m)
	if !cr.ParseOK {
		t.Fatalf("parse: %+v", cr.ParseErrors)
	}
	pks := oracle.Enumerate(oracle.Universe{Family: "ipv4", Protos: []int{6},
		SrcAddrs: oracle.Addrs("ipv4", netA), DstAddrs: oracle.Addrs("ipv4", netB),
		SrcPorts: []int{0}, DstPorts: []int{80}})
	assertExhaustive(t, h, m, pks)
	assertDiagnostics(t, cr, m, map[string][]oracle.Pk{"ipv4": pks})
	if !hasDiag(cr, "inner", "ipv4", "fully_shadowed") {
		t.Fatalf("inner source prefix must be fully shadowed by outer: %+v", cr.Report.Diagnostics)
	}
	// The witness must cite a source inside the inner prefix.
	for _, d := range cr.Report.Diagnostics {
		if d.RuleID == "inner" {
			if d.Witness.SourceAddress != "10.0.0.0" {
				t.Fatalf("witness source should be 10.0.0.0, got %s", d.Witness.SourceAddress)
			}
		}
	}
}
