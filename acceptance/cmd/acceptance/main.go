// Command acceptance runs the checked-in scenario fixtures against the real
// replicactl binary (built on demand), compares each tick to both the
// hand-authored expectation in the fixture and the independent reference
// simulator, and writes a machine-readable JSON report plus a human-readable
// markdown summary. It is also driven from acceptance_test.go so the results
// are ordinary Go test failures when anything diverges.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"replicactl/acceptance/runner"
)

func main() {
	repoDir := flag.String("repo", ".", "repository root (contains go.work)")
	scenarioDir := flag.String("scenarios", "acceptance/scenarios", "directory of scenario JSON files")
	outJSON := flag.String("out-json", "results/acceptance-report.json", "JSON report output path")
	outMD := flag.String("out-md", "results/acceptance-report.md", "markdown report output path")
	only := flag.String("only", "", "comma-separated scenario names to run")
	flag.Parse()

	names, err := filepath.Glob(filepath.Join(*repoDir, *scenarioDir, "*.json"))
	if err != nil {
		fatal(err)
	}
	want := map[string]bool{}
	if *only != "" {
		for _, n := range strings.Split(*only, ",") {
			want[strings.TrimSpace(n)] = true
		}
	}

	workDir, err := os.MkdirTemp("", "replicactl-acceptance-")
	if err != nil {
		fatal(err)
	}
	defer os.RemoveAll(workDir)

	var reports []runner.Report
	failed := false
	for _, path := range names {
		var sc runner.Scenario
		b, err := os.ReadFile(path)
		if err != nil {
			fatal(err)
		}
		if err := json.Unmarshal(b, &sc); err != nil {
			fatal(fmt.Errorf("parse %s: %w", path, err))
		}
		if len(want) > 0 && !want[sc.Name] {
			continue
		}
		scWork := filepath.Join(workDir, sc.Name)
		if err := os.MkdirAll(scWork, 0o755); err != nil {
			fatal(err)
		}
		rep, err := runner.Run(sc, runner.RunConfig{RepoDir: *repoDir, WorkDir: scWork})
		if err != nil {
			fmt.Fprintf(os.Stderr, "scenario %s could not run: %v\n", sc.Name, err)
			failed = true
			continue
		}
		reports = append(reports, rep)
		status := "PASS"
		if !rep.Passed {
			status = "FAIL"
			failed = true
		}
		fmt.Printf("[%s] %s (%d reconcile ticks)\n", status, sc.Name, len(rep.Results))
		for _, r := range rep.Results {
			for _, m := range r.Mismatches {
				fmt.Printf("    step %d (%s @%d): %s\n", r.Index, r.RequestID, r.At, m)
			}
		}
	}

	if err := os.MkdirAll(filepath.Dir(*outJSON), 0o755); err != nil {
		fatal(err)
	}

	// Failure-category run: each case spawns a fresh binary on a fresh db.
	faultCases, err := runner.RunFaultScenario(runner.FaultRunConfig{
		RepoDir: *repoDir, WorkDir: filepath.Join(workDir, "fault-cases"),
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "fault scenario could not run: %v\n", err)
		failed = true
	}
	for _, fc := range faultCases {
		status := "PASS"
		if !fc.Passed {
			status = "FAIL"
			failed = true
			fmt.Printf("    fault %s: %s\n", fc.Name, fc.Mismatch)
		}
		fmt.Printf("[%s] fault:%s -> %s (http %d)\n", status, fc.Name, fc.Category, fc.HTTPCode)
	}
	jb, err := json.MarshalIndent(struct {
		AllPassed bool               `json:"all_passed"`
		Reports   []runner.Report    `json:"reports"`
		Faults    []runner.FaultCase `json:"fault_cases"`
	}{AllPassed: !failed, Reports: reports, Faults: faultCases}, "", "  ")
	if err != nil {
		fatal(err)
	}
	if err := os.WriteFile(*outJSON, jb, 0o644); err != nil {
		fatal(err)
	}
	if err := os.WriteFile(*outMD, []byte(toMarkdown(reports, faultCases)), 0o644); err != nil {
		fatal(err)
	}
	fmt.Printf("\nreports: %s\n         %s\n", *outJSON, *outMD)
	if failed {
		os.Exit(1)
	}
}

func toMarkdown(reports []runner.Report, faultCases []runner.FaultCase) string {
	var b strings.Builder
	b.WriteString("# Acceptance report\n\n")
	b.WriteString("Every reconcile tick is checked against (a) a hand-computed expectation embedded in the fixture and (b) an independent reference simulator in `acceptance/reference` that never imports the production code.\n\n")
	for _, rep := range reports {
		status := "PASS"
		if !rep.Passed {
			status = "FAIL"
		}
		fmt.Fprintf(&b, "## %s — %s\n\n%s\n\n", rep.Scenario, status, rep.Description)
		b.WriteString("| Tick | Request ID | Restart | Actual action | Actual desired | Reference | Hand expected | Mismatches |\n")
		b.WriteString("|---:|---|:--:|---|---:|---|---|---|\n")
		for _, r := range rep.Results {
			restarted := ""
			if r.Restarted {
				restarted = "yes"
			}
			mismatch := strings.Join(r.Mismatches, "<br>")
			if mismatch == "" {
				mismatch = "—"
			}
			fmt.Fprintf(&b, "| %d | `%s` | %s | %s | %d | %s/%d | %s/%d | %s |\n",
				r.At, r.RequestID, restarted,
				r.Actual.Action, r.Actual.Desired,
				r.Reference.Action, r.Reference.DesiredReplicas,
				func() string {
					if r.HandExpected != nil {
						return r.HandExpected.Action
					}
					return "n/a"
				}(),
				func() int {
					if r.HandExpected != nil {
						return r.HandExpected.DesiredReplicas
					}
					return -1
				}(),
				mismatch)
		}
		if len(rep.NoopJustifications) > 0 {
			b.WriteString("\n**Why no action was taken**\n\n")
			for _, n := range rep.NoopJustifications {
				fmt.Fprintf(&b, "- @%d — `%s`: %s\n", n.At, n.Reason, n.Detail)
			}
		}
		b.WriteString("\n")
	}
	if len(faultCases) > 0 {
		b.WriteString("## Failure categories (injected through the real binary)\n\n")
		b.WriteString("Each case injects one synthetic dependency fault into the local fixture and asserts the concrete category and HTTP 409 of the following reconcile — not merely that an endpoint responds.\n\n")
		b.WriteString("| Fault hook | HTTP | Expected category | Result | Detail |\n|---|---:|---|:--:|---|\n")
		for _, fc := range faultCases {
			status := "PASS"
			if !fc.Passed {
				status = "**FAIL**"
			}
			detail := fc.Mismatch
			if detail == "" {
				detail = "—"
			}
			fmt.Fprintf(&b, "| `%s` | %d | `%s` | %s | %s |\n",
				fc.Name, fc.HTTPCode, fc.Category, status, detail)
		}
		b.WriteString("\n")
	}
	return b.String()
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "acceptance:", err)
	os.Exit(2)
}
