// Command genflows generates the fixed synthetic 5-tuple flow fixture used
// by the distribution and migration tests. The generator is a deterministic
// 64-bit LCG (no time/randomness seeding) so the fixture is reproducible
// byte-for-byte and can be checked in alongside the code.
//
// Usage:
//
//	go run ./cmd/genflows -n 20000 -seed 20260927 -o testdata/flows/flows.json
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
)

// lcg: Numerical Recipes constants, 64-bit. Pure deterministic arithmetic.
type lcg struct{ state uint64 }

func newLCG(seed uint64) *lcg { return &lcg{state: seed} }

func (g *lcg) next() uint64 {
	g.state = g.state*6364136223846793005 + 1442695040888963407
	return g.state
}

// bounded returns a pseudo-random value in [0, n).
func (g *lcg) bounded(n uint64) uint64 { return g.next() % n }

type tupleJSON struct {
	SrcIP    string `json:"src_ip"`
	SrcPort  uint16 `json:"src_port"`
	DstIP    string `json:"dst_ip"`
	DstPort  uint16 `json:"dst_port"`
	Protocol string `json:"protocol"`
}

type fixture struct {
	Seed      uint64      `json:"seed"`
	Count     int         `json:"count"`
	Generator string      `json:"generator"`
	Flows     []tupleJSON `json:"flows"`
}

func ipString(v uint32) string {
	return fmt.Sprintf("10.%d.%d.%d", (v>>16)&0xff, (v>>8)&0xff, v&0xff)
}

func main() {
	n := flag.Int("n", 20000, "number of flows")
	seed := flag.Uint64("seed", 20260927, "LCG seed")
	out := flag.String("o", "testdata/flows/flows.json", "output path")
	flag.Parse()

	g := newLCG(*seed)
	protos := []string{"tcp", "udp", "icmp"}
	flows := make([]tupleJSON, 0, *n)
	for i := 0; i < *n; i++ {
		proto := protos[g.bounded(uint64(len(protos)))]
		var sp, dp uint16
		if proto == "icmp" {
			// Ports are unused for ICMP; keep them zero to stay honest.
		} else {
			sp = uint16(1024 + g.bounded(64511))
			dp = uint16(1 + g.bounded(65534))
		}
		flows = append(flows, tupleJSON{
			SrcIP:    ipString(uint32(g.bounded(1 << 24))),
			SrcPort:  sp,
			DstIP:    ipString(uint32(g.bounded(1 << 24))),
			DstPort:  dp,
			Protocol: proto,
		})
	}

	f := fixture{Seed: *seed, Count: *n, Generator: "lcg64:NumericalRecipes", Flows: flows}
	if err := os.MkdirAll(filepath.Dir(*out), 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	b, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if err := os.WriteFile(*out, b, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Printf("wrote %d flows (seed %d) to %s\n", *n, *seed, *out)
}
