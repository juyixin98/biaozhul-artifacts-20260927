// Command natlab runs the local, replayable stateful NAT model.
//
// Subcommands:
//
//	natlab replay --trace traces/xxx.json [--config configs/natlab.json]
//	    Evaluate one trace, print the full report and exit.
//	natlab serve  [--config configs/natlab.json]
//	    Start the loopback-only HTTP replay interface.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"
	"time"

	"natlab/internal/config"
	"natlab/internal/replay"
	"natlab/internal/storage"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd := os.Args[1]
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	cfgPath := fs.String("config", "configs/natlab.json", "path to config JSON")
	tracePath := fs.String("trace", "", "path to trace fixture (replay only)")
	outPath := fs.String("out", "", "write JSON report here in addition to stdout")
	_ = fs.Parse(os.Args[2:])

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		fatal("config", err)
	}

	switch cmd {
	case "replay":
		if *tracePath == "" {
			fatal("args", fmt.Errorf("--trace is required"))
		}
		runReplay(cfg, *tracePath, *outPath)
	case "serve":
		runServe(cfg)
	case "version":
		fmt.Println("natlab 0.1.0 (local synthetic lab)")
	default:
		usage()
		os.Exit(2)
	}
}

func openStore(cfg config.Config) (storage.Store, func(), error) {
	if cfg.SQLitePath == "" {
		return storage.NewMemory(), func() {}, nil
	}
	st, err := storage.OpenSQLite(cfg.SQLitePath)
	if err != nil {
		return nil, nil, err
	}
	return st, func() { _ = st.Close() }, nil
}

func runReplay(cfg config.Config, tracePath, outPath string) {
	trace, err := replay.LoadTrace(tracePath)
	if err != nil {
		fatal("trace", err)
	}
	st, closeStore, err := openStore(cfg)
	if err != nil {
		fatal("store", err)
	}
	defer closeStore()

	runner := replay.NewRunner(cfg, st)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	rep, err := runner.Run(ctx, trace)
	if err != nil {
		// Even a compute failure carries a report with the failure decision.
		fmt.Fprintf(os.Stderr, "run ended with compute failure: %v\n", err)
	}
	b, _ := json.MarshalIndent(rep, "", "  ")
	fmt.Println(string(b))
	if outPath != "" {
		if err := os.WriteFile(outPath, b, 0o644); err != nil {
			fatal("write report", err)
		}
	}
	if err != nil {
		os.Exit(1)
	}
}

func runServe(cfg config.Config) {
	st, closeStore, err := openStore(cfg)
	if err != nil {
		fatal("store", err)
	}
	defer closeStore()

	srv := replay.NewServer(cfg, st).Handler()
	httpSrv := newHTTPServer(cfg.HTTPListen, srv)

	errCh := make(chan error, 1)
	go func() {
		fmt.Fprintf(os.Stderr, "natlab replay API on http://%s (loopback, synthetic metadata only)\n",
			cfg.HTTPListen)
		errCh <- httpSrv.ListenAndServe()
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errCh:
		fatal("serve", err)
	case sig := <-sigCh:
		fmt.Fprintf(os.Stderr, "\nreceived %s, shutting down\n", sig)
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = httpSrv.Shutdown(ctx)
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  natlab replay --trace traces/<file>.json [--config configs/natlab.json] [--out report.json]
  natlab serve  [--config configs/natlab.json]`)
}

func fatal(where string, err error) {
	fmt.Fprintf(os.Stderr, "natlab: %s: %v\n", where, err)
	os.Exit(1)
}
