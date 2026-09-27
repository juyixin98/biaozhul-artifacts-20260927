package testutil

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"flowrouter/internal/flow"
)

// RepoRoot resolves the repository root from any test package.
func RepoRoot(t *testing.T) string {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	// internal/testutil/testutil.go -> repo root is two levels up.
	return filepath.Clean(filepath.Join(filepath.Dir(file), "..", ".."))
}

type TupleWire struct {
	SrcIP   string `json:"src_ip"`
	DstIP   string `json:"dst_ip"`
	Proto   uint8  `json:"proto"`
	SrcPort uint16 `json:"src_port"`
	DstPort uint16 `json:"dst_port"`
}

type FlowSetWire struct {
	Name  string      `json:"name"`
	Seed  int64       `json:"seed"`
	Flows []TupleWire `json:"flows"`
}

// LoadFlowSet reads one committed fixture and parses it through the real flow
// parser (so normalization is exercised too).
func LoadFlowSet(t *testing.T, name string) ([]flow.FiveTuple, int64) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(RepoRoot(t), "testdata", "flowsets", name+".json"))
	if err != nil {
		t.Fatalf("load flowset %s: %v", name, err)
	}
	var w FlowSetWire
	if err := json.Unmarshal(raw, &w); err != nil {
		t.Fatalf("parse flowset %s: %v", name, err)
	}
	out := make([]flow.FiveTuple, 0, len(w.Flows))
	for i, tw := range w.Flows {
		f, err := flow.Parse(tw.SrcIP, tw.DstIP, tw.Proto, tw.SrcPort, tw.DstPort)
		if err != nil {
			t.Fatalf("flowset %s tuple %d: %v", name, i, err)
		}
		out = append(out, f)
	}
	return out, w.Seed
}

// ScenarioMatrix is the shared member/version matrix consumed by both the
// Python oracle and Go tests.
type ScenarioMatrix struct {
	VNodesPerWeight int        `json:"vnodes_per_weight"`
	MaxVNodes       int        `json:"max_vnodes"`
	Scenarios       []Scenario `json:"scenarios"`
}

type Scenario struct {
	Name      string        `json:"name"`
	MaxVNodes int           `json:"max_vnodes"`
	Versions  []ScenarioVer `json:"versions"`
	Trans     [][2]int      `json:"transitions"`
}

type ScenarioVer struct {
	Members []ScenarioMember `json:"members"`
}

type ScenarioMember struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
	Up     *bool  `json:"up"`
}

func LoadScenarios(t *testing.T) *ScenarioMatrix {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(RepoRoot(t), "testdata", "scenarios", "scenarios.json"))
	if err != nil {
		t.Fatalf("load scenarios: %v", err)
	}
	var m ScenarioMatrix
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("parse scenarios: %v", err)
	}
	return &m
}

// GoldenAllocation and friends mirror the oracle JSON exactly (all numeric
// fields must decode identically; avoid float64 ambiguity by asserting with
// tolerance).
type Golden struct {
	GeneratedBy string                    `json:"generated_by"`
	FlowSet     string                    `json:"flowset"`
	FlowCount   int                       `json:"flow_count"`
	Scenarios   map[string]GoldenScenario `json:"scenarios"`
}

type GoldenScenario struct {
	Allocations []GoldenAlloc               `json:"allocations"`
	Transitions map[string]GoldenTransition `json:"transitions"`
	Traffic     []GoldenTraffic             `json:"traffic"`
	Owners      []GoldenOwner               `json:"owners"`
}

type GoldenAlloc struct {
	Counts   map[string]int `json:"counts"`
	Base     map[string]int `json:"base"`
	Extra    map[string]int `json:"extra"`
	Total    int            `json:"total"`
	Strategy string         `json:"strategy"`
	CappedTo int            `json:"capped_to"`
}

type GoldenTransition struct {
	Moved            int            `json:"moved"`
	Stayed           int            `json:"stayed"`
	MoveFraction     float64        `json:"move_fraction"`
	BaselineMoved    int            `json:"baseline_moved"`
	BaselineFraction *float64       `json:"baseline_move_fraction"`
	Reasons          map[string]int `json:"reasons"`
}

type GoldenTraffic struct {
	Routed int                `json:"routed"`
	Counts map[string]int     `json:"counts"`
	Share  map[string]float64 `json:"share"`
}

type GoldenOwner struct {
	K string    `json:"k"`
	H uint64    `json:"h"`
	O []*string `json:"o"`
}

func LoadGolden(t *testing.T, flowset string) *Golden {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(RepoRoot(t), "testdata", "oracle",
		"golden_"+flowset+".json"))
	if err != nil {
		t.Fatalf("load golden for %s (run testdata/oracle/oracle.py first): %v", flowset, err)
	}
	var g Golden
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatalf("parse golden: %v", err)
	}
	return &g
}

// IsUp returns the declared up state, defaulting to true when absent.
func (m ScenarioMember) IsUp() bool {
	if m.Up == nil {
		return true
	}
	return *m.Up
}
