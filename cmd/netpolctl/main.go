// Command netpolctl evaluates a fixture offline, without a server or
// database. It is the quickest way to inspect decisions and matrices.
//
// Usage:
//
//	netpolctl matrix   --fixture test/fixtures/scenarios/named-ports.json [--port 80 --protocol TCP]
//	netpolctl check    --fixture fx.json --from u-client --to u-server1 --port 80 --protocol TCP
//	netpolctl validate --fixture fx.json
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"netpolicy/internal/domain"
	"netpolicy/internal/engine"
	"netpolicy/internal/source"
)

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	cmd := os.Args[1]
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	fxPath := fs.String("fixture", "", "path to fixture JSON")
	from := fs.String("from", "", "source endpoint uid (check)")
	to := fs.String("to", "", "destination endpoint uid (check)")
	port := fs.Int("port", 0, "numeric destination port")
	protoFlag := fs.String("protocol", "TCP", "TCP or UDP")
	all := fs.Bool("all-declared-ports", false, "matrix: sweep every declared endpoint port")
	_ = fs.Parse(os.Args[2:])

	if *fxPath == "" {
		fatal("missing --fixture")
	}
	raw, err := os.ReadFile(*fxPath)
	if err != nil {
		fatal("read fixture: " + err.Error())
	}
	snap, err := source.Parse(raw)
	if err != nil {
		fatal("invalid fixture: " + err.Error())
	}
	eng := engine.New(snap)

	switch cmd {
	case "validate":
		fmt.Printf("fixture valid: %d namespaces, %d endpoints, %d policies (hash %s)\n",
			len(snap.Namespaces), len(snap.Endpoints), len(snap.Policies), snap.SourceHash[:12])
	case "check":
		proto, err := domain.ParseProtocol(*protoFlag)
		if err != nil {
			fatal(err.Error())
		}
		d, err := eng.Check(engine.Input{SourceUID: *from, DestUID: *to, Protocol: proto, Port: *port})
		if err != nil {
			fatal(err.Error())
		}
		printJSON(d)
		if d.Verdict != engine.VerdictAllow {
			// Distinct exit code for DENY/UNDECIDABLE, handy in shell
			// pipelines. UNDECIDABLE is kept separate from hard errors.
			if d.Verdict == engine.VerdictUndecidable {
				os.Exit(4)
			}
			os.Exit(3)
		}
	case "matrix":
		if *all {
			res, err := eng.FullMatrix()
			if err != nil {
				fatal(err.Error())
			}
			printJSON(res)
			return
		}
		if *port == 0 {
			fatal("matrix needs --port or --all-declared-ports")
		}
		proto, err := domain.ParseProtocol(*protoFlag)
		if err != nil {
			fatal(err.Error())
		}
		m, err := eng.Matrix(engine.MatrixRequest{Protocol: proto, Port: *port})
		if err != nil {
			fatal(err.Error())
		}
		printJSON(m)
	default:
		usage()
		os.Exit(2)
	}
}

func printJSON(v any) {
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		fatal(err.Error())
	}
}

func fatal(msg string) {
	fmt.Fprintln(os.Stderr, "netpolctl: "+msg)
	os.Exit(1)
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: netpolctl {validate|check|matrix} [flags]")
}
