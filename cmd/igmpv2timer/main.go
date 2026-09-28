// Command igmpv2timer runs the offline IGMPv2 membership/query timer
// service in one of three modes:
//
//	igmpv2timer replay <scenario.json>  run a synthetic replay, print trace
//	igmpv2timer serve                  start the HTTP API
//	igmpv2timer scenarios              list bundled scenarios
//
// This binary simulates RFC 2236 host-facing membership timers only; it is
// not a multicast routing protocol.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"time"

	"igmpv2timer/internal/clock"
	"igmpv2timer/internal/config"
	"igmpv2timer/internal/core"
	"igmpv2timer/internal/engine"
	"igmpv2timer/internal/server"
	"igmpv2timer/internal/store"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "igmpv2timer:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		usage()
		return fmt.Errorf("a subcommand is required")
	}
	switch args[0] {
	case "-h", "--help", "help":
		usage()
		return nil
	case "replay", "serve", "scenarios":
	default:
		usage()
		return fmt.Errorf("unknown subcommand %q", args[0])
	}

	fs := flag.NewFlagSet(args[0], flag.ContinueOnError)
	fs.Usage = func() { usage() }
	cfgPath := fs.String("config", "configs/config.json", "path to JSON config (empty=built-in RFC defaults)")
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		return err
	}

	switch args[0] {
	case "replay":
		if fs.NArg() != 1 {
			return fmt.Errorf("usage: igmpv2timer replay <scenario.json>")
		}
		return cmdReplay(cfg, fs.Arg(0))
	case "serve":
		return cmdServe(cfg)
	case "scenarios":
		return cmdScenarios()
	}
	return nil
}

func cmdReplay(cfg config.Config, path string) error {
	script, err := engine.LoadScript(path)
	if err != nil {
		return err
	}
	st, err := store.Open(cfg.SQLitePath)
	if err != nil {
		return err
	}
	defer st.Close()
	if err := st.Reset(); err != nil {
		return err
	}
	r, err := engine.NewRunner(cfg, script, st)
	if err != nil {
		return err
	}
	rep, err := r.Run()
	if err != nil {
		return err
	}
	fmt.Print(rep.Trace())

	// Rebuild state from the event journal and prove persistence round-trips.
	rb, err := engine.RebuildFromJournal(r.Config(), st, script.Until(), rep)
	if err != nil {
		return fmt.Errorf("journal rebuild failed: %w", err)
	}
	if rb.OK {
		fmt.Printf("JOURNAL REBUILD OK: %d group(s), %d interval(s) reproduced from SQLite\n",
			len(rb.RebuiltSnapshot.Groups), len(rb.RebuiltIntervals))
	} else {
		fmt.Println("JOURNAL REBUILD MISMATCH:", rb.Detail)
	}

	failed := 0
	for _, a := range rep.Assertions {
		if !a.Pass {
			failed++
		}
	}
	if failed > 0 {
		return fmt.Errorf("%d assertion(s) failed", failed)
	}
	return nil
}

func cmdServe(cfg config.Config) error {
	st, err := store.Open(cfg.SQLitePath)
	if err != nil {
		return err
	}
	defer st.Close()
	clk := clock.New()
	c, err := core.New(cfg, clk)
	if err != nil {
		return err
	}
	srv := server.New(cfg, clk, c, st, nil)
	httpSrv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	fmt.Fprintf(os.Stderr, "igmpv2timer listening on %s (scope: offline membership timers)\n", cfg.HTTPAddr)
	return httpSrv.ListenAndServe()
}

func cmdScenarios() error {
	matches, err := filepath.Glob("testdata/scenarios/*.json")
	if err != nil {
		return err
	}
	for _, m := range matches {
		raw, err := os.ReadFile(m)
		if err != nil {
			return err
		}
		var head struct {
			Name    string `json:"name"`
			Summary string `json:"summary"`
		}
		if err := json.Unmarshal(raw, &head); err != nil {
			return err
		}
		fmt.Printf("%s\n  %s\n  %s\n", m, head.Name, head.Summary)
	}
	return nil
}

func usage() {
	fmt.Fprint(os.Stderr, `igmpv2timer — offline IGMPv2 (RFC 2236) membership/query timer service

USAGE
  igmpv2timer [-config configs/config.json] replay <scenario.json>
  igmpv2timer [-config configs/config.json] serve
  igmpv2timer scenarios

SCOPE
  Simulates host-facing IGMPv2 membership timers (report suppression,
  Group Membership Interval, Last-Member-Query). It is NOT a multicast
  routing protocol and performs no real packet I/O.
`)
}
