// Command reasm-cli replays one PCAP file through the offline IPv4
// reassembly backend, prints the JSON report, and can check the run
// against a genfixture manifest (exit status 0/1). It sends no network
// traffic.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"ipreasm/internal/config"
	"ipreasm/internal/reasm"
	"ipreasm/internal/replay"
	"ipreasm/internal/store"
)

func main() {
	cfgPath := flag.String("config", "config/reasm.json", "config file")
	pcapPath := flag.String("pcap", "", "PCAP file to replay")
	runID := flag.String("run", "cli-run", "run id for correlation")
	dbPath := flag.String("db", "", "override db_path from config")
	expectPath := flag.String("expect", "", "manifest.json to verify against")
	flag.Parse()

	if *pcapPath == "" {
		fmt.Fprintln(os.Stderr, "reasm-cli: -pcap is required")
		os.Exit(2)
	}
	cfg, err := config.Load(*cfgPath)
	if err != nil {
		fail(err)
	}
	if *dbPath != "" {
		cfg.DBPath = *dbPath
	}

	f, err := os.Open(*pcapPath)
	if err != nil {
		fail(err)
	}
	defer f.Close()
	st, err := store.Open(cfg.DBPath)
	if err != nil {
		fail(err)
	}
	defer st.Close()

	eng := &replay.Engine{
		RunID: *runID,
		Sink:  st,
		Cfg: reasm.Config{
			Timeout:          cfg.Timeout.Duration,
			MaxDatagramSize:  cfg.MaxDatagramSize,
			MaxDatagrams:     cfg.MaxDatagrams,
			MaxBufferedBytes: cfg.MaxBufferedBytes,
		},
	}
	rep, err := eng.RunPCAP(f)
	if err != nil {
		fail(err)
	}
	if *expectPath == "" {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(rep)
		return
	}
	ok, err := verifyManifest(*expectPath, *pcapPath, rep, st, *runID)
	if err != nil {
		fail(err)
	}
	if !ok {
		os.Exit(1)
	}
}

func fail(err error) {
	fmt.Fprintf(os.Stderr, "reasm-cli: %v\n", err)
	os.Exit(1)
}
