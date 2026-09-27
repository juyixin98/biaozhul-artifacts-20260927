// Command genfixtures generates the fixed, committed flow-set fixtures used by
// the test suite and replay API.
//
// It is deliberately a separate program and uses its own math/rand stream:
// the reference inputs are NOT produced by the routing core under test. The
// generated files are deterministic for a given seed and committed under
// testdata/flowsets so every test/run replays the exact same corpus.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
)

type tupleWire struct {
	SrcIP   string `json:"src_ip"`
	DstIP   string `json:"dst_ip"`
	Proto   uint8  `json:"proto"`
	SrcPort uint16 `json:"src_port"`
	DstPort uint16 `json:"dst_port"`
}

type flowSetWire struct {
	Name  string      `json:"name"`
	Seed  int64       `json:"seed"`
	Flows []tupleWire `json:"flows"`
}

func main() {
	dir := flag.String("dir", "testdata/flowsets", "output directory")
	seed := flag.Int64("seed", 20260927, "PRNG seed")
	flag.Parse()

	if err := os.MkdirAll(*dir, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	// Named sets with explicit sizes. Generation draws src from 10.0.0.0/24,
	// dst from 10.1.0.0/24, protocols TCP(6)/UDP(17), ephemeral-ish ports.
	// Uniqueness is enforced by set membership, so the corpus contains no
	// duplicate five-tuples.
	sets := []struct {
		name string
		n    int
	}{
		{"flows_smoke", 300},
		{"flows_10k", 10000},
	}
	for _, s := range sets {
		rng := rand.New(rand.NewSource(*seed + int64(len(s.name))))
		set := flowSetWire{Name: s.name, Seed: *seed, Flows: make([]tupleWire, 0, s.n)}
		seen := make(map[string]bool, s.n)
		for len(set.Flows) < s.n {
			t := tupleWire{
				SrcIP:   fmt.Sprintf("10.0.0.%d", 1+rng.Intn(64)),
				DstIP:   fmt.Sprintf("10.1.0.%d", 1+rng.Intn(64)),
				Proto:   []uint8{6, 17}[rng.Intn(2)],
				SrcPort: uint16(1024 + rng.Intn(60000)),
				DstPort: uint16(1 + rng.Intn(1024)),
			}
			protoName := "tcp"
			if t.Proto == 17 {
				protoName = "udp"
			}
			key := fmt.Sprintf("%s|%s:%d|%s:%d", protoName, t.SrcIP, t.SrcPort, t.DstIP, t.DstPort)
			if seen[key] {
				continue
			}
			seen[key] = true
			set.Flows = append(set.Flows, t)
		}
		path := filepath.Join(*dir, s.name+".json")
		b, _ := json.MarshalIndent(set, "", "  ")
		if err := os.WriteFile(path, b, 0o644); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		fmt.Printf("wrote %s (%d flows, seed %d)\n", path, len(set.Flows), *seed)
	}
}
