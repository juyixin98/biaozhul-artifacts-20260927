// Command fwrule-analyze performs offline first-match shadow/redundancy
// analysis of a policy file and prints the report as JSON. It needs no server
// or database.
//
// Exit codes: 0 analysis completed (diagnostics may still be present);
// 2 the policy failed to compile; 1 any other error.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"

	"fwrule/internal/analyzer"
	"fwrule/internal/config"
)

func main() {
	pretty := flag.Bool("pretty", true, "indent JSON output")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "usage: %s [-pretty] policy.json\n", os.Args[0])
		flag.PrintDefaults()
	}
	flag.Parse()
	if flag.NArg() != 1 {
		flag.Usage()
		os.Exit(1)
	}
	raw, err := os.ReadFile(flag.Arg(0))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
	pol, err := config.Load(raw, flag.Arg(0))
	if err != nil {
		fmt.Fprintf(os.Stderr, "policy compile error: %v\n", err)
		os.Exit(2)
	}
	rep := analyzer.Analyze(pol, 0)
	enc := json.NewEncoder(os.Stdout)
	if *pretty {
		enc.SetIndent("", "  ")
	}
	if err := enc.Encode(rep); err != nil {
		fmt.Fprintf(os.Stderr, "encode: %v\n", err)
		os.Exit(1)
	}
}
