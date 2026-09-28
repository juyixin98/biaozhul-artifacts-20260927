// Failure-class and advisory tests: each asserts a specific machine-readable
// code and, where relevant, the 1-based entry position and producer location.
package engine_test

import (
	"testing"

	"cidrcov/internal/engine"
	"cidrcov/internal/ipparse"
)

func TestFailureClasses(t *testing.T) {
	cases := []struct {
		name     string
		allow    []string
		exclude  []string
		wantCode string
		wantList string
		wantIdx  int
	}{
		{"garbage cidr", []string{"not-an-ip"}, nil, engine.CodeInvalidEntry, "allow", 1},
		{"v4 prefix too long", []string{"10.0.0.0/33"}, nil, engine.CodeInvalidEntry, "allow", 1},
		{"v6 prefix too long", []string{"::/129"}, nil, engine.CodeInvalidEntry, "allow", 1},
		{"second allow bad", []string{"10.0.0.0/8", "999.1.1.1/32"}, nil, engine.CodeInvalidEntry, "allow", 2},
		{"exclude bad at index 3", []string{"10.0.0.0/8"},
			[]string{"10.0.0.0/9", "10.0.0.0/10", "10.0.0.0/11", "10.0.0.0/99"},
			engine.CodeInvalidEntry, "exclude", 4},
		{"negative-looking slash", []string{"10.0.0.0/-1"}, nil, engine.CodeInvalidEntry, "allow", 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res := engine.Compute(c.allow, c.exclude, engine.Options{})
			if res.Status != "error" {
				t.Fatalf("expected error status, got %q (prefixes=%d)", res.Status, len(res.Prefixes))
			}
			if len(res.Prefixes) != 0 {
				t.Fatalf("no prefixes may be emitted on parse failure, got %d", len(res.Prefixes))
			}
			var found bool
			for _, f := range res.Failures {
				if f.Code == c.wantCode && f.List == c.wantList && f.Index == c.wantIdx {
					found = true
					if f.Location != "ipparse.Parse" {
						t.Errorf("location = %q, want ipparse.Parse", f.Location)
					}
					if f.Input == "" {
						t.Errorf("failure should echo the offending input")
					}
				}
			}
			if !found {
				t.Fatalf("expected failure code=%s list=%s idx=%d; got %+v",
					c.wantCode, c.wantList, c.wantIdx, res.Failures)
			}
		})
	}
}

func TestEntryLimit(t *testing.T) {
	allow := make([]string, 3)
	for i := range allow {
		allow[i] = "0.0.0.0/0"
	}
	res := engine.Compute(allow, nil, engine.Options{MaxEntriesPerList: 2})
	if res.Status != "error" {
		t.Fatalf("expected error, got %s", res.Status)
	}
	if len(res.Failures) != 1 || res.Failures[0].Code != engine.CodeEntryLimitReached {
		t.Fatalf("want entry_limit_reached, got %+v", res.Failures)
	}
}

func TestHostBitsAdvisory(t *testing.T) {
	res := engine.Compute([]string{"10.0.0.5/24"}, nil, engine.Options{})
	if res.Status != "ok" {
		t.Fatalf("host-bits input is accepted (advisory), got failures %+v", res.Failures)
	}
	if !hasAdv(res, ipparse.AdvHostBitsCanonicalized) {
		t.Fatalf("expected host_bits_canonicalized advisory, got %+v", res.Advisories)
	}
	assertCIDRs(t, res, []string{"10.0.0.0/24"}, nil)
}

func TestBareIPAdvisory(t *testing.T) {
	res := engine.Compute([]string{"10.0.0.1"}, nil, engine.Options{})
	if !hasAdv(res, ipparse.AdvBareIPExpanded) {
		t.Fatalf("expected bare_ip_expanded advisory, got %+v", res.Advisories)
	}
}

func TestExcludeOutsideAllowAdvisory(t *testing.T) {
	res := engine.Compute([]string{"10.0.0.0/24"}, []string{"192.168.0.0/24"}, engine.Options{})
	if res.Status != "ok" {
		t.Fatalf("disjoint exclusion is not fatal, got %+v", res.Failures)
	}
	if !hasAdv(res, engine.AdvExcludeNotInAllow) {
		t.Fatalf("expected exclude_outside_allow advisory, got %+v", res.Advisories)
	}
	assertCIDRs(t, res, []string{"10.0.0.0/24"}, nil)
}

func TestDuplicateEntryAdvisory(t *testing.T) {
	res := engine.Compute([]string{"10.0.0.0/24", "10.0.0.0/24"}, nil, engine.Options{})
	if !hasAdv(res, engine.AdvDuplicateEntry) {
		t.Fatalf("expected duplicate_entry advisory, got %+v", res.Advisories)
	}
	assertCIDRs(t, res, []string{"10.0.0.0/24"}, nil)
}

func TestMappedIPv4InIPv6(t *testing.T) {
	// ::ffff:10.0.0.0/120 maps onto 10.0.0.0/24 in the v4 family.
	res := engine.Compute([]string{"::ffff:10.0.0.0/120"}, nil, engine.Options{})
	if res.Status == "error" {
		t.Fatalf("mapped address should be accepted: %+v", res.Failures)
	}
	if !hasAdv(res, ipparse.AdvV4MappedInV6) {
		t.Fatalf("expected ipv4_mapped_in_ipv6_rebased advisory, got %+v", res.Advisories)
	}
	assertCIDRs(t, res, []string{"10.0.0.0/24"}, nil)
}

func TestMixedFamiliesAreSeparated(t *testing.T) {
	res := engine.Compute(
		[]string{"10.0.0.0/24", "2001:db8::/64"},
		[]string{"10.0.0.5/32"},
		engine.Options{})
	if res.Status == "error" {
		t.Fatalf("unexpected failures %+v", res.Failures)
	}
	if res.V4PrefixCount == 0 || res.V6PrefixCount != 1 {
		t.Fatalf("families not separated: v4=%d v6=%d", res.V4PrefixCount, res.V6PrefixCount)
	}
}

func TestTraceExplainsStepsAndVersions(t *testing.T) {
	res := engine.Compute([]string{"10.0.0.0/24"}, nil, engine.Options{})
	if res.AlgorithmVersion == "" || res.ServiceVersion == "" || res.GoVersion == "" {
		t.Fatalf("versions must be populated: %+v", res)
	}
	steps := map[string]bool{}
	for _, st := range res.Trace {
		steps[st.Step] = true
		if st.Location == "" || st.Detail == "" {
			t.Fatalf("each trace step needs location and detail: %+v", st)
		}
	}
	for _, want := range []string{"start", "parse", "ipv4:union", "ipv4:subtract", "ipv4:decompose", "ipv4:verify", "finish"} {
		if !steps[want] {
			t.Fatalf("trace missing step %q; have %v", want, steps)
		}
	}
}

func TestSamePrefixInBothListsIsNotDuplicate(t *testing.T) {
	res := engine.Compute([]string{"172.16.0.0/12"}, []string{"172.16.0.0/12"}, engine.Options{})
	if hasAdv(res, engine.AdvDuplicateEntry) {
		t.Fatalf("same prefix in allow AND exclude is not a duplicate; got %+v", res.Advisories)
	}
	if res.Status != "empty" {
		t.Fatalf("fully excluded allow set must be empty, got status=%s", res.Status)
	}
}

func hasAdv(res *engine.Result, code string) bool {
	for _, a := range res.Advisories {
		if a.Code == code {
			return true
		}
	}
	return false
}
