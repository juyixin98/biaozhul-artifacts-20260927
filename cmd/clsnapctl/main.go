// Command clsnapctl is the operator/teaching CLI:
//
//	clsnapctl scenario -file testdata/scenarios/inflight.json
//	    Run a deterministic scenario in-process (memory stores), assert every
//	    hand-computed expectation independently, and write a replayable run
//	    report JSON under -logdir (default testdata/runs).
//
//	clsnapctl verify -scenario testdata/scenarios/inflight.json
//	    Validate the fixture file itself (accounting identities) without
//	    running the kernel.
//
//	clsnapctl transfer -url http://127.0.0.1:18081 -ref t1 -from a -to b -amount 5
//	clsnapctl snapshot -url http://127.0.0.1:18081 -id S1
//	clsnapctl record   -url http://127.0.0.1:18081 -id S1
//	    Drive a running three-process cluster over HTTP.
package main

import (
	"context"
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"clsnap/internal/apperr"
	"clsnap/tests/harness"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "scenario":
		err = runScenario(os.Args[2:])
	case "verify":
		err = runVerify(os.Args[2:])
	case "transfer", "snapshot", "record":
		err = runHTTP(os.Args[1], os.Args[2:])
	case "-h", "--help", "help":
		usage()
		return
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		if ae, ok := apperr.As(err); ok {
			fmt.Fprintf(os.Stderr, "category=%s code=%s\n", ae.Kind, ae.Code)
		}
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `clsnapctl - Chandy-Lamport teaching cluster CLI

subcommands:
  scenario   -file <scenario.json> [-logdir testdata/runs] [-store memory|postgres -dsn <pg>]
  verify     -file <scenario.json>
  transfer   -url <node> -ref <id> -from <acc> -to <acc> -amount <n>
  snapshot   -url <node> -id <snapshot-id>
  record     -url <node> -id <snapshot-id>
`)
}

func runScenario(args []string) error {
	fs := flag.NewFlagSet("scenario", flag.ContinueOnError)
	file := fs.String("file", "", "scenario fixture")
	logdir := fs.String("logdir", "testdata/runs", "report output directory")
	driver := fs.String("store", "memory", "memory|postgres (per-node scratch)")
	dsn := fs.String("dsn", "", "postgres DSN template (uses {db}) for three separate DBs)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *file == "" {
		return apperr.Inputf(apperr.CodeMalformed, "-file required")
	}
	sc, err := harness.LoadScenario(*file)
	if err != nil {
		return err
	}
	runID := fmt.Sprintf("%s-%s", sc.Name, time.Now().UTC().Format("20060102T150405.000000000Z"))
	ctx := context.Background()

	var opts []harness.Option
	if *driver == "postgres" {
		if *dsn == "" {
			return apperr.Inputf(apperr.CodeMalformed, "postgres store requires -dsn")
		}
		opts = append(opts, harness.WithPostgres(*dsn))
	}
	h, err := harness.New(ctx, sc, *logdir, runID, opts...)
	if err != nil {
		return err
	}
	defer h.Close()

	if err := h.Run(ctx); err != nil {
		return err
	}
	rep, path, err := h.Finalize(ctx)
	if err != nil {
		return err
	}
	fmt.Printf("run=%s scenario=%s passed=%v\n", rep.RunID, rep.Scenario, rep.Passed)
	for _, v := range rep.Verdicts {
		mark := "PASS"
		if !v.Passed {
			mark = "FAIL"
		}
		fmt.Printf("  [%s] %s — %s\n", mark, v.Name, v.Reason)
	}
	if path != "" {
		fmt.Println("report:", path)
	}
	if !rep.Passed {
		os.Exit(1)
	}
	return nil
}

func runVerify(args []string) error {
	fs := flag.NewFlagSet("verify", flag.ContinueOnError)
	file := fs.String("file", "", "scenario fixture")
	if err := fs.Parse(args); err != nil {
		return err
	}
	sc, err := harness.LoadScenario(*file)
	if err != nil {
		return err
	}
	if err := harness.ValidateFixture(sc); err != nil {
		return err
	}
	fmt.Printf("fixture %s is internally consistent (initial total %d)\n",
		sc.Name, sc.Expect.InitialTotal)
	return nil
}

func runHTTP(cmd string, args []string) error {
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	url := fs.String("url", "", "node base URL")
	ref := fs.String("ref", "", "transfer ref")
	from := fs.String("from", "", "source account")
	to := fs.String("to", "", "destination account")
	amount := fs.Uint64("amount", 0, "token amount")
	id := fs.String("id", "", "snapshot id")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *url == "" {
		return apperr.Inputf(apperr.CodeMalformed, "-url required")
	}
	ctx := context.Background()
	switch cmd {
	case "transfer":
		return postJSON(ctx, *url+"/transfers", map[string]any{
			"ref": *ref, "from": *from, "to": *to, "amount": *amount,
		})
	case "snapshot":
		return postJSON(ctx, *url+"/snapshots", map[string]any{"id": *id})
	case "record":
		return getJSON(ctx, *url+"/snapshots/"+*id)
	}
	return nil
}

func postJSON(ctx context.Context, url string, body any) error {
	b, _ := json.Marshal(body)
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(b))
	req.Header.Set("content-type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return apperr.Failure(apperr.CodeTransport, "http", url, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	fmt.Println(string(raw))
	if resp.StatusCode >= 300 {
		return apperr.Failure(apperr.CodeTransport, "http",
			fmt.Sprintf("status %d", resp.StatusCode), nil)
	}
	return nil
}

func getJSON(ctx context.Context, url string) error {
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return apperr.Failure(apperr.CodeTransport, "http", url, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var pretty bytes.Buffer
	if json.Indent(&pretty, raw, "", "  ") == nil {
		fmt.Println(pretty.String())
	} else {
		fmt.Println(string(raw))
	}
	if resp.StatusCode >= 300 {
		return apperr.Failure(apperr.CodeTransport, "http",
			fmt.Sprintf("status %d", resp.StatusCode), nil)
	}
	return nil
}
