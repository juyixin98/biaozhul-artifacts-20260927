// Command tcpreplay runs the offline TCP bidirectional stream reassembly
// service.
//
// Subcommands:
//
//	tcpreplay serve   --config configs/tcpreplay.json
//	    Start the HTTP replay API.
//
//	tcpreplay replay  --config configs/tcpreplay.json --pcap FILE [--request-id ID] [--out DIR]
//	    One-shot offline analysis of a capture: analyzes against an isolated
//	    engine, persists evidence to SQLite, prints the summary and (with
//	    --out) writes one raw file per generation/direction plus a JSON report.
//
// The replay subcommand uses the same ingest pipeline as the HTTP API; it
// exists for reproducible offline runs and CI.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"tcpreplay/internal/config"
	"tcpreplay/internal/diagnose"
	"tcpreplay/internal/service"
	"tcpreplay/internal/storage"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "serve":
		err = runServe(os.Args[2:])
	case "replay":
		err = runReplay(os.Args[2:])
	case "-h", "--help", "help":
		usage()
	default:
		usage()
		err = fmt.Errorf("unknown subcommand %q", os.Args[1])
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "tcpreplay:", err)
		os.Exit(1)
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `tcpreplay - offline TCP bidirectional stream reassembly

Usage:
  tcpreplay serve  --config <config.json>
  tcpreplay replay --config <config.json> --pcap <file.pcap> [--request-id ID] [--out DIR]
`)
}

func loadConfigOrExit(path string) config.Config {
	cfg, err := config.Load(path)
	if err != nil {
		fail(err)
	}
	return cfg
}

func fail(err error) {
	fmt.Fprintln(os.Stderr, "tcpreplay:", err)
	os.Exit(1)
}

func runServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	cfgPath := fs.String("config", "configs/tcpreplay.json", "path to JSON config")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg := loadConfigOrExit(*cfgPath)
	if dir := filepath.Dir(cfg.DBPath); dir != "." && dir != ":memory:" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return fmt.Errorf("create db dir: %w", err)
		}
	}
	store, err := storage.Open(sqliteDSN(cfg.DBPath))
	if err != nil {
		return err
	}
	defer store.Close()
	diag := diagnose.NewLogger(os.Stderr, cfg.PayloadPreview)
	srv := service.NewServer(store, cfg, diag)
	httpSrv := &http.Server{
		Addr:              cfg.HTTP.Listen,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}
	errCh := make(chan error, 1)
	go func() {
		fmt.Fprintf(os.Stderr, "tcpreplay: listening on %s (db=%s policy=%s preview=%t)\n",
			cfg.HTTP.Listen, cfg.DBPath, cfg.OverlapPolicy, cfg.PayloadPreview)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	select {
	case err := <-errCh:
		return err
	case sig := <-stop:
		fmt.Fprintf(os.Stderr, "\ntcpreplay: %s received, shutting down\n", sig)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	return httpSrv.Shutdown(ctx)
}

func sqliteDSN(path string) string {
	if path == ":memory:" {
		return "file::memory:?cache=shared&_pragma=busy_timeout(5000)"
	}
	return "file:" + path + "?_pragma=busy_timeout(5000)"
}
