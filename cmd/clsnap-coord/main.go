// Command clsnap-coord runs a teaching scenario against three in-process
// nodes (or three real node processes with -http), judges the observed
// snapshots against the independent oracle and the fixture's hand-authored
// expectations, and writes a replayable JSON report.
//
// Usage:
//
//	clsnap-coord -scenario testdata/scenarios/01_inflight_conservation.json
//	clsnap-coord -scenario <file> -http   # use running HTTP nodes
//	clsnap-coord -scenario <file> -store postgres
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"clsnap/internal/harness"
	"clsnap/internal/runner"
	"clsnap/internal/scenario"
	"clsnap/internal/store"
)

func main() {
	scPath := flag.String("scenario", "", "path to scenario JSON (required)")
	useHTTP := flag.Bool("http", false, "drive three already-running HTTP nodes (ports 18081-18083)")
	storeDriver := flag.String("store", "memory", "in-process store: memory|postgres")
	dsn := flag.String("dsn", "postgres://clsnap:clsnap@127.0.0.1:5432/clsnap?sslmode=disable", "postgres DSN")
	outDir := flag.String("outdir", "runlogs", "directory for JSON reports")
	runID := flag.String("run", "", "run id (default: timestamped)")
	failFast := flag.Bool("fail-fast", false, "exit non-zero on first failed judgement")
	flag.Parse()

	if *scPath == "" {
		fmt.Fprintln(os.Stderr, "-scenario is required")
		os.Exit(2)
	}
	raw, err := os.ReadFile(*scPath)
	must(err, "read scenario")
	var sc scenario.Scenario
	must(json.Unmarshal(raw, &sc), "parse scenario")
	must(sc.Validate(), "invalid scenario fixture")

	id := *runID
	if id == "" {
		id = runner.NewRunID()
	}
	ctx := context.Background()

	// Independent prediction, computed before touching the system.
	predictions, err := scenario.RunOracle(&sc)
	must(err, "oracle failure (fixture inconsistency)")

	var drivers map[string]runner.Driver
	var cluster *harness.Cluster

	if *useHTTP {
		drivers = map[string]runner.Driver{}
		ports := map[string]string{
			"n1": "http://127.0.0.1:18081",
			"n2": "http://127.0.0.1:18082",
			"n3": "http://127.0.0.1:18083",
		}
		for _, n := range sc.NodeIDs() {
			var others []string
			for _, o := range sc.NodeIDs() {
				if o != n {
					others = append(others, o)
				}
			}
			drivers[n] = runner.NewHTTPDriver(n, ports[n], others)
		}
	} else {
		opts := []harness.Option{harness.WithPumpInterval(3 * time.Millisecond)}
		if *storeDriver == "postgres" {
			opts = append(opts, harness.WithStoreFactory(
				func(id string, peers []string, runIDArg string) store.Store {
					st, err := store.OpenPg(ctx, store.PgConfig{
						DSN: *dsn, Schema: "run_" + sanitize(runIDArg) + "_" + id,
						NodeID: id, RunID: runIDArg, OutboxCap: 4096,
					})
					must(err, "open pg for "+id)
					return st
				}))
		}
		cluster, err = harness.NewCluster(ctx, id, sc.InitialBalance[sc.NodeIDs()[0]], opts...)
		must(err, "build cluster")
		drivers = cluster.Drivers
		defer cluster.Shutdown(ctx)
	}

	rep, err := runner.Run(ctx, &sc, drivers, runner.Options{
		RunID: id, StepTimeout: 5 * time.Second, SettleTimeout: 10 * time.Second,
		Out: os.Stdout,
	})
	must(err, "run scenario")

	// Cross-check the oracle against the hand-authored fixture, and attach the
	// oracle prediction to each judgement. This is what guarantees the
	// reference answer was not produced by the system under test.
	oracleMismatch := crossCheckOracle(&sc, predictions, rep)
	for snapID, p := range predictions {
		if j, ok := rep.Judgements[snapID]; ok {
			j.Oracle = map[string]interface{}{
				"complete":      p.Complete,
				"local":         p.Local,
				"channels":      p.Channels,
				"in_flight_sum": p.InFlightSum,
				"global_total":  p.GlobalTotal,
				"aborted":       p.Aborted,
			}
		}
	}

	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "mkdir runlogs: %v\n", err)
	}
	outFile := filepath.Join(*outDir, filepath.Base(sc.Name)+"_"+id+".json")
	must(writeJSON(outFile, rep), "write report")
	writeLatest(filepath.Join(*outDir, "latest-"+sc.Name+".json"), rep)

	fmt.Println()
	fmt.Println("================ RUN SUMMARY ================")
	fmt.Printf("scenario : %s\nrun id   : %s\nreport   : %s\n", sc.Name, id, outFile)
	for _, snapID := range sortedKeys(rep.Judgements) {
		j := rep.Judgements[snapID]
		status := "PASS"
		if !j.Pass {
			status = "FAIL"
		}
		fmt.Printf("[%s] snapshot %s\n", status, snapID)
		if j.Conservation != nil {
			fmt.Printf("       conservation: %v (%s)\n",
				j.Conservation.Pass, j.Conservation.Explanation)
		}
		for _, r := range j.Reasons {
			fmt.Printf("       - %s\n", r)
		}
	}
	for _, f := range rep.Failures {
		status := "PASS"
		if !f.Matched {
			status = "FAIL"
		}
		fmt.Printf("[%s] step %2d classified failure want=%s got=%s/%s\n",
			status, f.Step, f.Want, f.Got, f.Code)
	}
	if len(oracleMismatch) > 0 {
		rep.Pass = false
		fmt.Println("[FAIL] oracle disagrees with hand-authored expectation:")
		for _, m := range oracleMismatch {
			fmt.Println("       - " + m)
		}
	}
	fmt.Printf("final balances: %v\n", rep.FinalBalances)
	fmt.Printf("OVERALL: %s\n", map[bool]string{true: "PASS", false: "FAIL"}[rep.Pass])

	if !rep.Pass && *failFast {
		os.Exit(1)
	}
	if !rep.Pass {
		os.Exit(1)
	}
}

func sanitize(s string) string {
	out := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9':
			out = append(out, c)
		default:
			out = append(out, '_')
		}
	}
	if len(out) > 30 {
		out = out[len(out)-30:]
	}
	return string(out)
}

// crossCheckOracle compares the independent oracle's prediction with the
// hand-authored numbers in the fixture. Both are external to the system under
// test; if they disagree it is a fixture bug and the run cannot be trusted.
func crossCheckOracle(sc *scenario.Scenario, pred map[string]scenario.Prediction, rep *runner.Report) []string {
	var mism []string
	for snapID, exp := range sc.Expect {
		p, ok := pred[snapID]
		if !ok {
			mism = append(mism, snapID+": oracle produced no prediction")
			continue
		}
		// Aborted rounds have no valid cut; only the abort decision is judged.
		if exp.ExpectAborted {
			if !p.Complete && len(p.Aborted) == 0 {
				mism = append(mism, snapID+": expected abort but oracle shows a complete cut")
			}
			continue
		}
		if exp.Complete != p.Complete && len(p.Aborted) == 0 {
			mism = append(mism, fmt.Sprintf(
				"%s: hand-authored complete=%v but oracle complete=%v",
				snapID, exp.Complete, p.Complete))
		}
		for node, want := range exp.Local {
			if p.Local[node] != want {
				mism = append(mism, fmt.Sprintf(
					"%s local %s: hand-authored %d but oracle %d",
					snapID, node, want, p.Local[node]))
			}
		}
		for ch, wantMsgs := range exp.Channels {
			gotMsgs := p.Channels[ch]
			if len(gotMsgs) != len(wantMsgs) {
				mism = append(mism, fmt.Sprintf(
					"%s channel %s: hand-authored %d in-flight, oracle %d",
					snapID, ch, len(wantMsgs), len(gotMsgs)))
				continue
			}
			for i := range wantMsgs {
				if wantMsgs[i] != gotMsgs[i] {
					mism = append(mism, fmt.Sprintf(
						"%s channel %s msg %d: hand-authored %+v oracle %+v",
						snapID, ch, i, wantMsgs[i], gotMsgs[i]))
				}
			}
		}
		if exp.InFlightSum != p.InFlightSum {
			mism = append(mism, fmt.Sprintf(
				"%s in-flight sum: hand-authored %d oracle %d",
				snapID, exp.InFlightSum, p.InFlightSum))
		}
	}
	return mism
}

func must(err error, what string) {
	if err != nil {
		fmt.Fprintf(os.Stderr, "%s: %v\n", what, err)
		os.Exit(1)
	}
}

func writeJSON(path string, v interface{}) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

func writeLatest(path string, v interface{}) {
	_ = writeJSON(path, v)
}

func sortedKeys(m map[string]*runner.SnapshotJudgement) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	// simple sort
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j] < out[i] {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}
