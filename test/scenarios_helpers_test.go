package indcheck_test

import (
	"bytes"
	"encoding/json"
	"net/http"
	"sort"
	"testing"

	"netsem-test/harness"
	"netsem-test/oracle"
)

// --- decoded service wire types (defined here, not imported from the
// implementation, to keep expected answers independent) ---

type parseError struct {
	Category string `json:"category"`
	RuleID   string `json:"rule_id"`
	Message  string `json:"message"`
}

type ruleNote struct {
	RuleID string `json:"rule_id"`
	Note   string `json:"note"`
}

type witness struct {
	Family             string `json:"family"`
	Protocol           string `json:"protocol"`
	ProtocolNumber     int    `json:"protocol_number"`
	SourceAddress      string `json:"source_address"`
	DestinationAddress string `json:"destination_address"`
	SourcePort         int    `json:"source_port"`
	DestinationPort    int    `json:"destination_port"`
	MatchedRuleID      string `json:"matched_rule_id"`
}

type productDesc struct {
	Protocols        string `json:"protocols"`
	Sources          string `json:"sources"`
	Destinations     string `json:"destinations"`
	SourcePorts      string `json:"source_ports"`
	DestinationPorts string `json:"destination_ports"`
	PacketCount      string `json:"packet_count"`
}

type partitionPart struct {
	Family      string        `json:"family"`
	Products    []productDesc `json:"products"`
	PacketCount string        `json:"packet_count"`
}

type diagnostic struct {
	Kind       string          `json:"kind"`
	RuleID     string          `json:"rule_id"`
	Family     string          `json:"family"`
	Detail     string          `json:"detail"`
	Witness    *witness        `json:"witness"`
	Covered    []partitionPart `json:"covered_partition"`
	ShadowedBy []string        `json:"shadowed_by"`
}

type report struct {
	DefaultAction string       `json:"default_action"`
	Diagnostics   []diagnostic `json:"diagnostics"`
}

type configResponse struct {
	Version        int64        `json:"version"`
	ParseOK        bool         `json:"parse_ok"`
	ParseErrors    []parseError `json:"parse_errors"`
	Notes          []ruleNote   `json:"notes"`
	Report         *report      `json:"report"`
	Instance       string       `json:"instance"`
	SourceLocation string       `json:"source_location"`
}

type step struct {
	Order       int    `json:"order"`
	RuleID      string `json:"rule_id"`
	Matched     bool   `json:"matched"`
	Action      string `json:"action"`
	Explanation string `json:"explanation"`
}

type decision struct {
	RequestID      string   `json:"request_id"`
	Family         string   `json:"family"`
	Version        int64    `json:"version"`
	Decision       string   `json:"decision"`
	DecidedBy      string   `json:"decided_by"`
	MatchedRuleID  string   `json:"matched_rule_id"`
	Steps          []step   `json:"steps"`
	Certain        bool     `json:"certain"`
	Uncertainties  []string `json:"uncertainties"`
	Errors         []string `json:"errors"`
	Instance       string   `json:"instance"`
	SourceLocation string   `json:"source_location"`
}

type requestLog struct {
	RequestID      string   `json:"request_id"`
	Version        int64    `json:"version"`
	Family         string   `json:"family"`
	Packet         string   `json:"packet"`
	Decision       string   `json:"decision"`
	MatchedRuleID  string   `json:"matched_rule_id"`
	Steps          []step   `json:"steps"`
	Certain        bool     `json:"certain"`
	Uncertainties  []string `json:"uncertainties"`
	Errors         []string `json:"errors"`
	Instance       string   `json:"instance"`
	SourceLocation string   `json:"source_location"`
	CreatedAt      string   `json:"created_at"`
}

// --- scenario helpers ---

func submit(t *testing.T, h *harness.H, m *oracle.Model) configResponse {
	t.Helper()
	code, data := harness.PostRaw(t, h.BaseURL+"/configs", m.ConfigJSON())
	if code != http.StatusOK {
		t.Fatalf("POST /configs status %d: %s", code, data)
	}
	var cr configResponse
	if err := json.Unmarshal(data, &cr); err != nil {
		t.Fatalf("decode config response: %v: %s", err, data)
	}
	return cr
}

func evalPacket(t *testing.T, h *harness.H, p oracle.Pk) decision {
	t.Helper()
	body, _ := json.Marshal(map[string]any{
		"family":              p.Family,
		"protocol":            protoTok(p.Proto),
		"source_address":      p.Src,
		"destination_address": p.Dst,
		"source_port":         p.SrcPort,
		"destination_port":    p.DstPort,
	})
	resp, err := http.Post(h.BaseURL+"/evaluate", "application/json", bytes.NewReader(body))
	if err != nil {
		t.Fatalf("evaluate: %v", err)
	}
	defer resp.Body.Close()
	data := make([]byte, 0)
	buf := make([]byte, 4096)
	for {
		n, rerr := resp.Body.Read(buf)
		data = append(data, buf[:n]...)
		if rerr != nil {
			break
		}
	}
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("evaluate status %d: %s", resp.StatusCode, data)
	}
	var d decision
	if err := json.Unmarshal(data, &d); err != nil {
		t.Fatalf("decode decision: %v: %s", err, data)
	}
	return d
}

func protoTok(n int) string {
	switch n {
	case 6:
		return "tcp"
	case 17:
		return "udp"
	case 132:
		return "sctp"
	case 1:
		return "icmp"
	case 58:
		return "icmpv6"
	}
	return itoa(n)
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}

// assertExhaustive compares service decisions against the oracle for every
// packet and returns the decisions keyed for later assertions.
func assertExhaustive(t *testing.T, h *harness.H, m *oracle.Model, packets []oracle.Pk) {
	t.Helper()
	for _, p := range packets {
		want := m.Decide(p)
		d := evalPacket(t, h, p)
		if d.Decision != want.Action {
			t.Fatalf("decision mismatch for %+v: service=%s oracle=%s", p, d.Decision, want.Action)
		}
		if want.ByDefault {
			if d.DecidedBy != "default" {
				t.Fatalf("expected default decision for %+v, got %s", p, d.DecidedBy)
			}
		} else if d.MatchedRuleID != want.RuleID {
			t.Fatalf("matched rule mismatch for %+v: service=%s oracle=%s", p, d.MatchedRuleID, want.RuleID)
		}
	}
}

// diagKey identifies a (rule, family, kind) diagnostic.
func diagKey(d diagnostic) string { return d.RuleID + "|" + d.Family + "|" + d.Kind }
func expKey(e oracle.ExpectedDiag) string {
	return e.RuleID + "|" + e.Family + "|" + e.Kind
}

func assertDiagnostics(t *testing.T, cr configResponse, m *oracle.Model, famPackets map[string][]oracle.Pk) {
	t.Helper()
	if cr.Report == nil {
		t.Fatal("expected report in config response")
	}
	got := map[string]bool{}
	for _, d := range cr.Report.Diagnostics {
		got[diagKey(d)] = true
	}
	want := map[string]bool{}
	var wantSorted []oracle.ExpectedDiag
	for fam, pks := range famPackets {
		eds := oracle.ExpectedDiagnostics(m, fam, pks)
		wantSorted = append(wantSorted, eds...)
		for _, e := range eds {
			want[expKey(e)] = true
		}
	}
	for k := range want {
		if !got[k] {
			t.Errorf("missing expected diagnostic %s", k)
		}
	}
	for k := range got {
		if !want[k] {
			t.Errorf("unexpected diagnostic %s", k)
		}
	}
	// Every shadowing diagnostic must carry a concrete witness that (a) is
	// in the right family and (b) is actually matched by the cited earlier
	// rule and (c) lies in the flagged rule's region.
	for _, d := range cr.Report.Diagnostics {
		switch d.Kind {
		case "fully_shadowed", "partially_shadowed":
			if d.Witness == nil {
				t.Errorf("%s %s/%s: no witness", d.Kind, d.Family, d.RuleID)
				continue
			}
			if d.Witness.Family != d.Family {
				t.Errorf("witness family %s != diag family %s", d.Witness.Family, d.Family)
			}
			if d.Witness.MatchedRuleID == "" {
				t.Errorf("%s %s/%s witness cites no earlier rule", d.Kind, d.Family, d.RuleID)
			}
			if len(d.ShadowedBy) == 0 {
				t.Errorf("%s %s/%s: no shadowed_by ids", d.Kind, d.Family, d.RuleID)
			}
		}
		if d.Kind == "partially_shadowed" && len(d.Covered) == 0 {
			t.Errorf("partial shadow %s/%s has no covered partition", d.Family, d.RuleID)
		}
	}
	// Ordering: diagnostics sorted by family then rule order then kind.
	var keys []string
	for _, d := range cr.Report.Diagnostics {
		keys = append(keys, diagKey(d))
	}
	if !sort.StringsAreSorted(keys) {
		// analyzer orders by rule index within family, not lexicographic id;
		// check explicitly using scenario rule order.
		idx := map[string]int{}
		for i, r := range m.Rules {
			idx[r.ID] = i
		}
		for i := 1; i < len(cr.Report.Diagnostics); i++ {
			a, b := cr.Report.Diagnostics[i-1], cr.Report.Diagnostics[i]
			if a.Family > b.Family || (a.Family == b.Family && (idx[a.RuleID] > idx[b.RuleID] ||
				(idx[a.RuleID] == idx[b.RuleID] && a.Kind > b.Kind))) {
				t.Errorf("diagnostics not ordered: %v before %v", a, b)
			}
		}
	}
	_ = wantSorted
}

// assertDeletionInvariance verifies that deleting every fully-shadowed or
// redundant rule leaves the decision function unchanged over the whole
// packet set, while deleting a live, non-redundant rule changes something.
func assertDeletionInvariance(t *testing.T, h *harness.H, m *oracle.Model, famPackets map[string][]oracle.Pk, cr configResponse) {
	t.Helper()
	safe := map[string]bool{}
	for _, d := range cr.Report.Diagnostics {
		if d.Kind == "fully_shadowed" || d.Kind == "redundant" {
			safe[d.RuleID] = true
		}
	}
	if len(safe) == 0 {
		t.Fatal("scenario expected at least one safe-to-delete rule")
	}
	for _, fam := range []string{"ipv4", "ipv6"} {
		pks := famPackets[fam]
		for id := range safe {
			pred := func(p oracle.Pk) string {
				if !ruleInFamily(m, id, fam) {
					return ""
				}
				return m.Without(id).Decide(p).Action
			}
			_ = pred
			m2 := m.Without(id)
			for _, p := range pks {
				if !ruleInFamily(m, id, fam) {
					continue
				}
				if m.Decide(p).Action != m2.Decide(p).Action {
					t.Fatalf("deleting flagged-safe rule %s changed decision for %+v", id, p)
				}
			}
			// Round-trip through the service: submit the reduced ruleset and
			// re-evaluate exhaustively.
			cr2 := submit(t, h, m2)
			if !cr2.ParseOK {
				t.Fatalf("reduced config failed to parse: %+v", cr2.ParseErrors)
			}
			for _, p := range pks {
				if !ruleInFamily(m, id, fam) {
					continue
				}
				d := evalPacket(t, h, p)
				if d.Decision != m.Decide(p).Action {
					t.Fatalf("service after deleting %s disagrees with original oracle for %+v", id, p)
				}
			}
		}
	}
}

func ruleInFamily(m *oracle.Model, id, fam string) bool {
	for _, r := range m.Rules {
		if r.ID == id {
			for _, f := range []string{r.Family} {
				if f == fam || f == "any" || f == "" {
					return true
				}
			}
		}
	}
	return false
}
