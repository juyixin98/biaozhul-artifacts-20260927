// Command pvreplay runs one synthetic fixture/config file through the
// engine and writes the full report (status, best routes, decision trace,
// cycle evidence) as JSON to stdout or a file. It connects to nothing.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"pathvector/internal/config"
	"pathvector/internal/replay"
	"pathvector/internal/store"
)

func main() {
	db := flag.String("db", ":memory:", "SQLite database to archive the run into (:memory: = ephemeral)")
	logPath := flag.String("log", "", "also write decision log lines to this file")
	flag.Parse()
	if flag.NArg() != 1 {
		fmt.Fprintln(os.Stderr, "usage: pvreplay [flags] <config.json>")
		os.Exit(2)
	}

	cfg, err := config.LoadFile(flag.Arg(0))
	if err != nil {
		// Validation failure: no run exists yet, so no report is emitted.
		fail("load config", err)
	}
	ctx := context.Background()
	st, err := store.Open(ctx, *db)
	if err != nil {
		fail("open store", err)
	}
	defer st.Close()

	var logFile *os.File
	if *logPath != "" {
		f, err := os.OpenFile(*logPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			fail("open log", err)
		}
		defer f.Close()
		logFile = f
	}
	var runner *replay.Runner
	if logFile != nil {
		runner = replay.NewRunner(st, logFile)
	} else {
		runner = replay.NewRunner(st)
	}

	res, err := runner.ExecuteConfig(ctx, cfg, nil)
	if res == nil {
		fail("execute", err)
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(res); err != nil {
		fail("encode report", err)
	}
	if err != nil {
		// Engine hard failure: report is printed; exit non-zero with the
		// classified error kind.
		fmt.Fprintf(os.Stderr, "run %s failed: %v\n", res.RunID, err)
		os.Exit(1)
	}
	fmt.Fprintf(os.Stderr, "run %s: %s / %s (%d steps)\n",
		res.RunID, res.Report.Status, res.Report.Reason, res.Report.Steps)
}

func fail(what string, err error) {
	// Exit 2 distinguishes input/usage failure (no run was created) from
	// engine hard failure (exit 1, a partial report was emitted).
	fmt.Fprintf(os.Stderr, "%s: %v\n", what, err)
	os.Exit(2)
}
