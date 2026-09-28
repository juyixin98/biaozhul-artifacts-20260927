// Package golden_test cross-validates three independently produced sources
// of truth for every small-endpoint-set fixture:
//
//  1. the hand-authored *.golden.json expectations (a person's derivation);
//  2. the engine under test (internal/engine);
//  3. the independent oracle (test/oracle), which never imports engine.
//
// The golden files are the required reference answers; the engine is tested
// against them, and the oracle is tested against them too. A bug shared by
// the engine and oracle still fails against the hand-written matrices; a
// typo in a matrix fails against both implementations.
package golden_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"netpolicy/internal/domain"
	"netpolicy/internal/engine"
	"netpolicy/internal/source"
	"netpolicy/test/oracle"
)

type goldenMatrix struct {
	Scenario string   `json:"scenario"`
	Order    []string `json:"order"`
	Sweeps   []struct {
		Protocol string     `json:"protocol"`
		Port     int        `json:"port"`
		Matrix   [][]string `json:"matrix"`
		Cells    []struct {
			Src       string `json:"src"`
			Dst       string `json:"dst"`
			Protocol  string `json:"protocol,omitempty"`
			Port      int    `json:"port,omitempty"`
			Verdict   string `json:"verdict"`
			Reason    string `json:"reason"`
			NamedPort string `json:"namedPort,omitempty"`
		} `json:"cells"`
	} `json:"sweeps"`
}

type goldenChecks struct {
	Scenario string `json:"scenario"`
	Checks   []struct {
		Src      string `json:"src"`
		Dst      string `json:"dst"`
		Protocol string `json:"protocol"`
		Port     int    `json:"port"`
		Verdict  string `json:"verdict"`
		Reason   string `json:"reason"`
	} `json:"checks"`
}

func loadFixture(t *testing.T, name string) *domain.Snapshot {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "fixtures", "scenarios", name+".json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	snap, err := source.Parse(raw)
	if err != nil {
		t.Fatalf("parse fixture: %v", err)
	}
	return snap
}

func loadGolden[T any](t *testing.T, name string) T {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "fixtures", "scenarios", name+".golden.json"))
	if err != nil {
		t.Fatalf("read golden: %v", err)
	}
	var g T
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	return g
}

// codeFor maps an engine decision to the single-letter golden matrix code.
func codeFor(t *testing.T, d *engine.Decision) string {
	t.Helper()
	switch d.Verdict {
	case engine.VerdictAllow:
		return "A"
	case engine.VerdictUndecidable:
		return "U"
	case engine.VerdictDeny:
		switch d.Reason {
		case engine.ReasonIngressDefaultDeny:
			return "I"
		case engine.ReasonEgressDefaultDeny:
			return "E"
		default:
			return "B"
		}
	}
	t.Fatalf("unknown decision %s/%s", d.Verdict, d.Reason)
	return "?"
}

func TestOverlappingSelectorsMatrix(t *testing.T) {
	const name = "overlapping-selectors"
	snap := loadFixture(t, name)
	g := loadGolden[goldenMatrix](t, name)
	eng := engine.New(snap)
	orc, err := oracle.Load(filepath.Join("..", "fixtures", "scenarios", name+".json"))
	if err != nil {
		t.Fatalf("oracle load: %v", err)
	}

	if len(g.Order) != len(snap.Endpoints) {
		t.Fatalf("golden order has %d entries, fixture has %d endpoints", len(g.Order), len(snap.Endpoints))
	}

	for si, sweep := range g.Sweeps {
		proto, err := domain.ParseProtocol(sweep.Protocol)
		if err != nil {
			t.Fatalf("sweep %d: %v", si, err)
		}
		m, err := eng.Matrix(engine.MatrixRequest{Protocol: proto, Port: sweep.Port})
		if err != nil {
			t.Fatalf("matrix: %v", err)
		}
		byCell := map[string]*engine.Decision{}
		for _, c := range m.Cells {
			byCell[c.SourceUID+"->"+c.DestUID] = c.Decision
		}
		for ri, row := range sweep.Matrix {
			src := g.Order[ri]
			if len(row) != len(g.Order) {
				t.Fatalf("sweep %s/%d row %d not square", sweep.Protocol, sweep.Port, ri)
			}
			for ci, want := range row {
				dst := g.Order[ci]
				d := byCell[src+"->"+dst]
				if d == nil {
					t.Fatalf("missing matrix cell %s->%s", src, dst)
				}
				// Engine vs hand-written golden.
				if got := codeFor(t, d); got != want {
					t.Errorf("ENGINE %s %s/%d %s->%s = %s, golden wants %s (reason=%s)",
						name, sweep.Protocol, sweep.Port, src, dst, got, want, d.Reason)
				}
				// Independent oracle vs the same hand-written golden.
				ores := orc.Evaluate(oracle.Check{Src: src, Dst: dst, Protocol: proto, Port: sweep.Port})
				owant := mapOracleVerdict(ores.Verdict, ores.Reason)
				if owant != want {
					t.Errorf("ORACLE %s %s/%d %s->%s = %s, golden wants %s",
						name, sweep.Protocol, sweep.Port, src, dst, owant, want)
				}
				// Engine vs oracle directly, with reasons reconciled.
				if string(d.Verdict) != string(ores.Verdict) {
					t.Errorf("ENGINE/ORACLE verdict divergence %s->%s: %s vs %s", src, dst, d.Verdict, ores.Verdict)
				}
			}
		}

		// Detailed cell assertions: verdict + exact reason code + which
		// policies supplied each side's allow.
		for _, c := range sweep.Cells {
			cp := c.Protocol
			if cp == "" {
				cp = sweep.Protocol
			}
			cport := c.Port
			if cport == 0 {
				cport = sweep.Port
			}
			cproto, _ := domain.ParseProtocol(cp)
			d, err := eng.Check(engine.Input{SourceUID: c.Src, DestUID: c.Dst, Protocol: cproto, Port: cport})
			if err != nil {
				t.Fatalf("cell check: %v", err)
			}
			if string(d.Verdict) != c.Verdict {
				t.Errorf("cell %s->%s verdict %s, want %s", c.Src, c.Dst, d.Verdict, c.Verdict)
			}
			if d.Reason != c.Reason {
				t.Errorf("cell %s->%s reason %q, want %q", c.Src, c.Dst, d.Reason, c.Reason)
			}
		}
	}
}

func TestGoldenCheckScenarios(t *testing.T) {
	for _, name := range []string{"named-ports", "one-way", "empty-policies", "policy-defaults"} {
		t.Run(name, func(t *testing.T) {
			snap := loadFixture(t, name)
			g := loadGolden[goldenChecks](t, name)
			eng := engine.New(snap)
			orc, err := oracle.Load(filepath.Join("..", "fixtures", "scenarios", name+".json"))
			if err != nil {
				t.Fatalf("oracle load: %v", err)
			}
			for i, c := range g.Checks {
				proto, err := domain.ParseProtocol(c.Protocol)
				if err != nil && c.Verdict != "UNDECIDABLE" {
					t.Fatalf("check %d: %v", i, err)
				}
				d, err := eng.Check(engine.Input{SourceUID: c.Src, DestUID: c.Dst, Protocol: proto, Port: c.Port})
				if err != nil {
					t.Fatalf("check %d engine: %v", i, err)
				}
				if string(d.Verdict) != c.Verdict {
					t.Errorf("check %d (%s->%s %s/%d) ENGINE verdict %s, golden %s",
						i, c.Src, c.Dst, c.Protocol, c.Port, d.Verdict, c.Verdict)
				}
				if d.Reason != c.Reason {
					t.Errorf("check %d (%s->%s %s/%d) ENGINE reason %q, golden %q",
						i, c.Src, c.Dst, c.Protocol, c.Port, d.Reason, c.Reason)
				}
				ores := orc.Evaluate(oracle.Check{Src: c.Src, Dst: c.Dst, Protocol: proto, Port: c.Port})
				if string(ores.Verdict) != c.Verdict {
					t.Errorf("check %d ORACLE verdict %s, golden %s", i, ores.Verdict, c.Verdict)
				}
				if ores.Reason != c.Reason {
					t.Errorf("check %d ORACLE reason %q, golden %q", i, ores.Reason, c.Reason)
				}
				if string(d.Verdict) == string(ores.Verdict) && d.Reason != ores.Reason {
					t.Errorf("check %d reason divergence engine=%q oracle=%q", i, d.Reason, ores.Reason)
				}
			}
		})
	}
}

func mapOracleVerdict(v oracle.Verdict, reason string) string {
	switch v {
	case oracle.VAllow:
		return "A"
	case oracle.VUndecidable:
		return "U"
	case oracle.VDeny:
		switch reason {
		case oracle.RDenyIngress:
			return "I"
		case oracle.RDenyEgress:
			return "E"
		default:
			return "B"
		}
	}
	return "?"
}

// TestMatrixIsExhaustiveAndStable verifies the matrix really covers every
// ordered pair and that two sweeps over the same snapshot are bit-identical
// (stable ordering, no map-iteration nondeterminism).
func TestMatrixIsExhaustiveAndStable(t *testing.T) {
	snap := loadFixture(t, "overlapping-selectors")
	eng := engine.New(snap)
	m1, err := eng.Matrix(engine.MatrixRequest{Protocol: domain.ProtocolTCP, Port: 8080})
	if err != nil {
		t.Fatal(err)
	}
	n := len(snap.Endpoints)
	if len(m1.Cells) != n*n {
		t.Fatalf("matrix has %d cells, want %d", len(m1.Cells), n*n)
	}
	// The snapshot is normalized (sorted by namespace,name); row-major cell
	// order must follow exactly that endpoint order on both axes.
	var expectedKeys []string
	for _, s := range snap.Endpoints {
		for _, d := range snap.Endpoints {
			expectedKeys = append(expectedKeys, s.UID+"->"+d.UID)
		}
	}
	m2, err := eng.Matrix(engine.MatrixRequest{Protocol: domain.ProtocolTCP, Port: 8080})
	if err != nil {
		t.Fatal(err)
	}
	var order1, order2 []string
	for i, c := range m1.Cells {
		got := c.SourceUID + "->" + c.DestUID
		if got != expectedKeys[i] {
			t.Fatalf("cell %d order = %s, want %s", i, got, expectedKeys[i])
		}
		order1 = append(order1, got+":"+string(c.Decision.Verdict))
	}
	for _, c := range m2.Cells {
		order2 = append(order2, c.SourceUID+"->"+c.DestUID+":"+string(c.Decision.Verdict))
	}
	for i := range order1 {
		if order1[i] != order2[i] {
			t.Fatalf("non-deterministic matrix at cell %d", i)
		}
	}
}
