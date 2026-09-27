// Command igmpd is the offline IGMPv2 membership/query timing service.
//
// Usage:
//
//	igmpd serve   -addr 127.0.0.1:8080 -db file:igmpq.db
//	igmpd replay  -scenario file.json [-db :memory:]
//
// "serve" starts the HTTP replay API; "replay" runs one scenario file and
// prints the result JSON to stdout (optionally persisting it).
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"

	"igmpq/internal/cats"
	"igmpq/internal/httpapi"
	"igmpq/internal/replay"
	"igmpq/internal/store"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var code int
	switch os.Args[1] {
	case "serve":
		code = cmdServe(os.Args[2:])
	case "replay":
		code = cmdReplay(os.Args[2:])
	default:
		usage()
		code = 2
	}
	os.Exit(code)
}

func usage() {
	fmt.Fprintf(os.Stderr, `igmpd - offline IGMPv2 membership & group-query timing service

usage:
  igmpd serve  -addr 127.0.0.1:8080 -db file:igmpq.db
  igmpd replay -scenario scenario.json [-db :memory:]

This is a synthetic replay tool, not a multicast routing protocol.
`)
}

func cmdServe(args []string) int {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	addr := fs.String("addr", "127.0.0.1:8080", "listen address")
	db := fs.String("db", "file:igmpq.db", "SQLite DSN (file:... or :memory:)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	st, err := store.Open(*db)
	if err != nil {
		log.Printf("open store: %v", err)
		return 1
	}
	defer st.Close()
	srv := httpapi.New(st, log.Default())
	log.Printf("listening on %s (db=%s)", *addr, *db)
	if err := http.ListenAndServe(*addr, srv); err != nil {
		log.Printf("serve: %v", err)
		return 1
	}
	return 0
}

func cmdReplay(args []string) int {
	fs := flag.NewFlagSet("replay", flag.ContinueOnError)
	scenario := fs.String("scenario", "", "scenario JSON file (required)")
	db := fs.String("db", "", "optional SQLite DSN to persist the run")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *scenario == "" {
		fmt.Fprintln(os.Stderr, "replay: -scenario is required")
		return 2
	}
	data, err := os.ReadFile(*scenario)
	if err != nil {
		fmt.Fprintf(os.Stderr, "replay: read scenario: %v\n", err)
		return 1
	}
	sc, err := replay.LoadFile(data)
	if err != nil {
		return fail(err)
	}
	res, err := replay.Run(sc)
	if err != nil {
		return fail(err)
	}
	if *db != "" {
		st, err := store.Open(*db)
		if err != nil {
			fmt.Fprintf(os.Stderr, "replay: open store: %v\n", err)
			return 1
		}
		defer st.Close()
		id, err := st.SaveRun(context.Background(), sc, res)
		if err != nil {
			fmt.Fprintf(os.Stderr, "replay: save run: %v\n", err)
			return 1
		}
		res.RunID = id
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(res); err != nil {
		fmt.Fprintf(os.Stderr, "replay: encode result: %v\n", err)
		return 1
	}
	return 0
}

func fail(err error) int {
	fmt.Fprintf(os.Stderr, "error [%s]: %v\n", cats.CategoryOf(err), err)
	return 1
}
