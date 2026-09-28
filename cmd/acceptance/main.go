// Command acceptance runs the independent black-box acceptance verifier.
//
// It builds and launches the real replicactl server over HTTP with a real
// SQLite database, drives load-step / missing / delayed / short-spike /
// service-restart / scale-from-zero scenarios, and grades every
// reconciliation against the independent oracle package. Exit status is
// non-zero if any check fails, so it can gate releases.
package main

import (
	"context"
	"flag"
	"fmt"
	"os"
	"time"

	"replicactl/internal/accept"
)

func main() {
	workDir := flag.String("workdir", ".", "directory for the built binary and per-scenario databases")
	timeout := flag.Duration("timeout", 60*time.Second, "overall verifier timeout")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	rep, err := accept.Run(ctx, *workDir, os.Stdout)
	if err != nil {
		fmt.Fprintf(os.Stderr, "\nverifier error: %v\n", err)
		os.Exit(1)
	}
	fmt.Printf("\nverification summary: %d passed, %d failed\n", rep.Passed, rep.Failed)
}
