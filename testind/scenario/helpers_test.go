// Package scenario_test contains the INDEPENDENT scenario tests. They live
// outside the internal packages and derive expected outcomes from the
// independent reference implementation in testind/oracle — never from the
// core under test. Each test asserts concrete results (deadlines, members,
// timestamps, generations) and the exact failure category of every
// rejection, not just "the API is callable".
package scenario_test

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"igmpv2timer/internal/config"
	"igmpv2timer/internal/engine"
	"igmpv2timer/internal/model"
	"igmpv2timer/internal/store"
	"igmpv2timer/testind/oracle"
)

func repoRoot(t *testing.T) string {
	t.Helper()
	wd, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	// testind/scenario -> two levels up
	return filepath.Dir(filepath.Dir(wd))
}

func scenarioPath(t *testing.T, name string) string {
	t.Helper()
	p := filepath.Join(repoRoot(t), "testdata", "scenarios", name)
	if _, err := os.Stat(p); err != nil {
		t.Fatalf("scenario fixture missing: %v", err)
	}
	return p
}

// runBoth executes the core-under-test replay and the independent oracle
// against the same fixture file.
func runBoth(t *testing.T, file string) (*engine.Report, *oracle.Expectation, config.Config, *store.Store) {
	t.Helper()
	path := scenarioPath(t, file)
	cfg, err := config.Load(filepath.Join(repoRoot(t), "configs", "config.json"))
	if err != nil {
		t.Fatal(err)
	}
	script, err := engine.LoadScript(path)
	if err != nil {
		t.Fatal(err)
	}
	st, err := store.Open(":memory:")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = st.Close() })

	runner, err := engine.NewRunner(cfg, script, st)
	if err != nil {
		t.Fatalf("runner construction: %v", err)
	}
	rep, err := runner.Run()
	if err != nil {
		t.Fatalf("replay execution: %v", err)
	}
	exp, err := oracle.ExpectedFor(path)
	if err != nil {
		t.Fatalf("oracle: %v", err)
	}
	return rep, exp, runner.Config(), st
}

// assertAllEngineAssertionsPass is the first gate: the scenario's embedded
// assertions must all be green; failures carry the documented category.
func assertAllEngineAssertionsPass(t *testing.T, rep *engine.Report) {
	t.Helper()
	for _, a := range rep.Assertions {
		if !a.Pass {
			t.Errorf("assertion %s FAILED category=%s detail=%s",
				a.ID, a.Failure, a.Detail)
		}
	}
}

// verdictKey collapses a diag for set comparison.
func verdictKey(at int64, member, verdict, reason string) string {
	return fmt.Sprintf("%d|%s|%s|%s", at, member, verdict, reason)
}

// findCoreDiag locates a core diagnostic by (verdict, member, reason).
func findCoreDiag(t *testing.T, rep *engine.Report, verdict model.Verdict,
	member, reason string, wantAt int64) model.Diag {
	t.Helper()
	var hits []model.Diag
	for _, d := range rep.Diags {
		if d.Verdict == verdict && d.Member == member && d.Reason == reason {
			hits = append(hits, d)
		}
	}
	if len(hits) == 0 {
		t.Fatalf("expected diag %s/%s/%s@%d — got diags: %s",
			verdict, member, reason, wantAt, summarize(rep))
	}
	if wantAt >= 0 {
		for _, d := range hits {
			if int64(d.At) == wantAt {
				return d
			}
		}
		t.Fatalf("diag %s/%s/%s found but not at %d (got %v)",
			verdict, member, reason, wantAt, atList(hits))
	}
	return hits[0]
}

func atList(ds []model.Diag) []int64 {
	var out []int64
	for _, d := range ds {
		out = append(out, int64(d.At))
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

func summarize(rep *engine.Report) string {
	var b strings.Builder
	for _, d := range rep.Diags {
		b.WriteString(verdictKey(int64(d.At), d.Member, string(d.Verdict), d.Reason))
		b.WriteByte(' ')
	}
	return b.String()
}

// crossCheckVerdicts compares every (time, member, verdict, reason) tuple of
// the core against the independent oracle's expected set, reporting
// differences in both directions.
func crossCheckVerdicts(t *testing.T, rep *engine.Report, exp *oracle.Expectation) {
	t.Helper()
	multiset := func(rows [][4]string) map[string]int {
		m := map[string]int{}
		for _, r := range rows {
			m[strings.Join(r[:], "|")]++
		}
		return m
	}

	var coreRows [][4]string
	for _, d := range rep.Diags {
		coreRows = append(coreRows, [4]string{
			fmt.Sprintf("%d", d.At), d.Member, string(d.Verdict), d.Reason,
		})
	}
	var oracleRows [][4]string
	for _, d := range exp.Diags {
		oracleRows = append(oracleRows, [4]string{
			fmt.Sprintf("%d", d.At), d.Member, d.Verdict, d.Reason,
		})
	}

	coreSet := multiset(coreRows)
	oraSet := multiset(oracleRows)

	for key, n := range oraSet {
		if got := coreSet[key]; got < n {
			t.Errorf("oracle predicts %q ×%d but core produced %d", key, n, got)
		}
	}
	for key, n := range coreSet {
		if want := oraSet[key]; n > want {
			// The router's own general-query emissions are tracked by the
			// oracle as generation counters rather than verdict rows.
			if strings.HasSuffix(key, "|general_query_emitted") ||
				strings.HasSuffix(key, "|query_lost_in_transit") {
				continue
			}
			t.Errorf("core produced %q ×%d but oracle predicted %d", key, n, want)
		}
	}
}
