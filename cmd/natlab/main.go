// Command natlab runs the local stateful NAT model.
//
//	natlab serve   -config configs/natlab.json
//	natlab replay  -config configs/natlab.json -fixture testdata/fixtures/xxx.json [-run-id id] [-mem]
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"strconv"
	"time"

	"natlab/internal/config"
	"natlab/internal/memstore"
	"natlab/internal/nat"
	"natlab/internal/replay"
	"natlab/internal/server"
	"natlab/internal/store"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "natlab:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		usage()
		return fmt.Errorf("missing subcommand")
	}
	switch args[0] {
	case "serve":
		return cmdServe(args[1:])
	case "replay":
		return cmdReplay(args[1:])
	case "-h", "--help", "help":
		usage()
		return nil
	default:
		usage()
		return fmt.Errorf("unknown subcommand %q", args[0])
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, `usage:
  natlab serve   -config configs/natlab.json
  natlab replay  -config configs/natlab.json -fixture testdata/fixtures/<name>.json [-run-id ID] [-mem]`)
}

func openStore(ctx context.Context, path string) (*store.SQLiteStore, error) {
	return store.Open(ctx, path)
}

func cmdServe(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	cfgPath := fs.String("config", "", "path to JSON config (defaults built-in)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		return err
	}
	ctx := context.Background()
	st, err := openStore(ctx, cfg.DBPath)
	if err != nil {
		return err
	}
	defer st.Close()

	logw, closeLog, err := openAccessLog(cfg.LogPath)
	if err != nil {
		return err
	}
	defer closeLog()

	eng := nat.NewEngine(cfg, st)
	srv := &httpServer{cfg: cfg, handler: server.New(eng).Handler(), logw: logw}
	return srv.listenAndServe()
}

func cmdReplay(args []string) error {
	fs := flag.NewFlagSet("replay", flag.ContinueOnError)
	cfgPath := fs.String("config", "", "path to JSON config")
	fixture := fs.String("fixture", "", "path to fixture JSON")
	runID := fs.String("run-id", "", "run id (default: timestamped)")
	useMem := fs.Bool("mem", false, "use in-memory store instead of SQLite")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *fixture == "" {
		return fmt.Errorf("-fixture is required")
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		return err
	}
	fx, err := replay.LoadFixture(*fixture)
	if err != nil {
		return err
	}
	newStore := func() nat.StateStore {
		if *useMem {
			return memstore.New()
		}
		// Shared in-memory SQLite with a process-unique name: the single open
		// connection keeps it alive for the replay and it is discarded on exit.
		name := "natlab_replay_" + strconv.FormatInt(time.Now().UnixNano(), 36)
		st, err := openStore(context.Background(), "file:"+name+"?mode=memory&cache=shared")
		if err != nil {
			panic(err)
		}
		return st
	}
	runner := replay.NewRunner(cfg, newStore, *runID)
	rep, err := runner.Run(context.Background(), fx)
	if err != nil {
		return err
	}

	enc := json.NewEncoder(os.Stdout)
	for i := range rep.Results {
		r := &rep.Results[i]
		row := map[string]any{
			"run_id":      rep.RunID,
			"index":       r.Index,
			"group":       r.Group,
			"match":       r.Match,
			"mismatch":    r.Mismatch,
			"observed_at": r.Packet.ObservedAt,
		}
		if r.Got != nil {
			row["accepted"] = r.Got.Accepted
			row["category"] = string(r.Got.Category)
			row["code"] = r.Got.Code
			row["reason"] = r.Got.Reason
			if r.Got.Mapping != nil {
				row["mapped_port"] = r.Got.Mapping.MappedPort
				row["state"] = r.Got.Mapping.State
			}
			row["swept_expired"] = r.Got.Swept
			row["clock_rewind"] = r.Got.ClockRewind
		}
		if err := enc.Encode(row); err != nil {
			return err
		}
	}
	fmt.Printf("fixture=%s run_id=%s passed=%v failures=%d\n",
		rep.FixtureName, rep.RunID, rep.Passed, len(rep.Failures))
	if !rep.Passed {
		for _, fmsg := range rep.Failures {
			fmt.Fprintln(os.Stderr, "FAIL:", fmsg)
		}
		os.Exit(2)
	}
	return nil
}

func openAccessLog(path string) (io.Writer, func(), error) {
	if path == "" {
		return os.Stderr, func() {}, nil
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, nil, err
	}
	return f, func() { _ = f.Close() }, nil
}
