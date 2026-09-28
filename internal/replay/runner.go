// Package replay drives packet scripts through the NAT engine deterministically
// and compares them against hand-authored expectations. Fixtures are JSON files
// authored independently of the engine (they never import nat internals); the
// expectations are an external oracle.
package replay

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"sync"
	"time"

	"natlab/internal/config"
	"natlab/internal/model"
	"natlab/internal/nat"
)

// FixturePacket is one input. Group ties packets into one concurrent batch;
// within a group arrival order is intentionally unspecified.
type FixturePacket struct {
	Group  int          `json:"group"`
	Packet model.Packet `json:"packet"`
	Expect Expectation  `json:"expect"`
}

// Expectation is the oracle verdict for one packet.
type Expectation struct {
	Accepted    bool           `json:"accepted"`
	Category    model.Category `json:"category,omitempty"`
	Code        string         `json:"code,omitempty"`
	MappedPort  uint16         `json:"mapped_port,omitempty"` // when accepted and a mapping is touched
	State       string         `json:"state,omitempty"`       // mapping state after the decision
	ClockRewind bool           `json:"clock_rewind,omitempty"`
	Swept       int64          `json:"swept,omitempty"` // asserted only when > 0
}

// Invariants are whole-script oracle checks, used when per-packet values are
// nondeterministic (concurrent batches).
type Invariants struct {
	AcceptedCount int `json:"accepted_count,omitempty"`
	// CodeCounts asserts the number of rejections carrying each code.
	CodeCounts map[string]int `json:"code_counts,omitempty"`
	// MappedPorts asserts the exact multiset of ports given to accepted packets.
	MappedPorts []uint16 `json:"mapped_ports,omitempty"`
	// FinalActivePorts asserts that active mappings at the end own distinct ports.
	FinalActiveUnique bool `json:"final_active_unique,omitempty"`
}

// Fixture is one replay script.
type Fixture struct {
	Name        string           `json:"name"`
	Description string           `json:"description"`
	PortLow     uint16           `json:"port_pool_low,omitempty"`
	PortHigh    uint16           `json:"port_pool_high,omitempty"`
	Timeouts    *config.Timeouts `json:"timeouts,omitempty"`
	Invariants  *Invariants      `json:"invariants,omitempty"`
	Packets     []FixturePacket  `json:"packets"`
}

// LoadFixture parses a JSON fixture file.
func LoadFixture(path string) (*Fixture, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var f Fixture
	if err := json.Unmarshal(b, &f); err != nil {
		return nil, fmt.Errorf("parse fixture %q: %w", path, err)
	}
	return &f, nil
}

// ItemResult is the verdict on one packet.
type ItemResult struct {
	Index    int
	Group    int
	Packet   model.Packet
	Expect   Expectation
	Got      *model.Decision
	Event    *model.Event
	Compute  error
	Match    bool
	Mismatch string
}

// Report summarizes a replay run. RunID is what to quote when reproducing.
type Report struct {
	FixtureName string
	RunID       string
	Results     []ItemResult
	Passed      bool
	Failures    []string
}

// Runner replays one fixture against a fresh, isolated engine/store pair.
type Runner struct {
	cfg      *config.Config
	newStore func() nat.StateStore
	runID    string
	now      func() time.Time
}

// NewRunner builds a runner. newStore allows swapping SQLite for the in-memory
// fake; cfg supplies public IP, private prefixes and timeouts.
func NewRunner(cfg *config.Config, newStore func() nat.StateStore, runID string) *Runner {
	if runID == "" {
		runID = "replay-" + time.Now().UTC().Format("20060102T150405.000000")
	}
	return &Runner{cfg: cfg, newStore: newStore, runID: runID}
}

// Run executes the fixture. Groups run sequentially; packets within one group
// run concurrently to exercise the first-packet race.
func (r *Runner) Run(ctx context.Context, f *Fixture) (*Report, error) {
	cfg := *r.cfg
	if f.PortLow != 0 {
		cfg.PortLow = f.PortLow
	}
	if f.PortHigh != 0 {
		cfg.PortHigh = f.PortHigh
	}
	if f.Timeouts != nil {
		cfg.Timeouts = *f.Timeouts
	}
	if err := cfg.Resolve(); err != nil {
		return nil, err
	}
	store := r.newStore()
	eng := nat.NewEngine(&cfg, store)

	rep := &Report{FixtureName: f.Name, RunID: r.runID, Passed: true}
	results := make([]ItemResult, len(f.Packets))
	for i := range f.Packets {
		results[i] = ItemResult{Index: i, Group: f.Packets[i].Group,
			Packet: f.Packets[i].Packet, Expect: f.Packets[i].Expect}
	}

	groups := groupOrder(f.Packets)
	for _, g := range groups {
		var idxs []int
		for i := range f.Packets {
			if f.Packets[i].Group == g {
				idxs = append(idxs, i)
			}
		}
		if len(idxs) == 1 {
			r.runOne(ctx, eng, f.Packets[idxs[0]].Packet, &results[idxs[0]])
			continue
		}
		var wg sync.WaitGroup
		for _, i := range idxs {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				r.runOne(ctx, eng, f.Packets[i].Packet, &results[i])
			}(i)
		}
		wg.Wait()
	}

	rep.Results = results
	for i := range results {
		if !results[i].Match {
			rep.Passed = false
			rep.Failures = append(rep.Failures, fmt.Sprintf(
				"packet[%d] group=%d: %s", i, results[i].Group, results[i].Mismatch))
		}
	}
	if f.Invariants != nil {
		msgs, err := checkInvariants(ctx, store, r.runID, f.Invariants, results)
		if err != nil {
			return nil, err
		}
		for _, msg := range msgs {
			rep.Passed = false
			rep.Failures = append(rep.Failures, "invariant: "+msg)
		}
	}
	return rep, nil
}

// checkInvariants evaluates the whole-script oracle independently of packet
// order (needed for concurrent batches whose port assignment is nondeterministic).
// Ports are deduplicated by mapping id: one mapping touched by several packets
// still counts once. Final-active uniqueness is read back from the store, which
// is the only source that sees mappings closed by the expiry sweep.
func checkInvariants(ctx context.Context, st nat.StateStore, runID string, inv *Invariants, results []ItemResult) ([]string, error) {
	var fails []string
	accepted := 0
	codeCount := map[string]int{}
	portByMapping := map[int64]uint16{}
	var lastNow time.Time
	for _, r := range results {
		if r.Got == nil {
			continue
		}
		if r.Got.EffectiveAt.After(lastNow) {
			lastNow = r.Got.EffectiveAt
		}
		if r.Got.Accepted {
			accepted++
			if r.Got.Mapping != nil {
				portByMapping[r.Got.Mapping.ID] = r.Got.Mapping.MappedPort
			}
		} else {
			codeCount[r.Got.Code]++
		}
	}
	if inv.AcceptedCount != 0 && accepted != inv.AcceptedCount {
		fails = append(fails, fmt.Sprintf("accepted_count: want %d got %d", inv.AcceptedCount, accepted))
	}
	for code, want := range inv.CodeCounts {
		if got := codeCount[code]; got != want {
			fails = append(fails, fmt.Sprintf("code_count[%s]: want %d got %d", code, want, got))
		}
	}
	if inv.MappedPorts != nil {
		var got []uint16
		for _, p := range portByMapping {
			got = append(got, p)
		}
		sort.Slice(got, func(i, j int) bool { return got[i] < got[j] })
		want := append([]uint16(nil), inv.MappedPorts...)
		sort.Slice(want, func(i, j int) bool { return want[i] < want[j] })
		if fmt.Sprint(got) != fmt.Sprint(want) {
			fails = append(fails, fmt.Sprintf("mapped_ports multiset: want %v got %v", want, got))
		}
	}
	if inv.FinalActiveUnique {
		active, err := st.ListMappings(ctx, runID, true)
		if err != nil {
			return nil, err
		}
		seen := map[uint16]int64{}
		for _, m := range active {
			if !m.ExpiresAt.After(lastNow) {
				continue // ListMappings(activeOnly) excludes closed, not time-expired
			}
			if owner, dup := seen[m.MappedPort]; dup {
				fails = append(fails, fmt.Sprintf(
					"two active mappings share port %d (ids %d,%d)", m.MappedPort, owner, m.ID))
			}
			seen[m.MappedPort] = m.ID
		}
	}
	return fails, nil
}

func (r *Runner) runOne(ctx context.Context, eng *nat.Engine, pkt model.Packet, out *ItemResult) {
	res, err := eng.Evaluate(ctx, r.runID, pkt)
	out.Compute = err
	if err != nil {
		out.Match = false
		out.Mismatch = "compute failure: " + err.Error()
		return
	}
	out.Got = &res.Decision
	out.Event = res.Event
	out.Match, out.Mismatch = compare(out.Expect, res.Decision)
}

// compare is the assertion against the independent oracle.
func compare(want Expectation, got model.Decision) (bool, string) {
	if got.Accepted != want.Accepted {
		return false, fmt.Sprintf("accepted: want %v got %v (code=%s reason=%s)",
			want.Accepted, got.Accepted, got.Code, got.Reason)
	}
	if !want.Accepted {
		if got.Category != want.Category {
			return false, fmt.Sprintf("category: want %s got %s", want.Category, got.Category)
		}
		if got.Code != want.Code {
			return false, fmt.Sprintf("code: want %s got %s", want.Code, got.Code)
		}
		return true, ""
	}
	if want.MappedPort != 0 {
		if got.Mapping == nil {
			return false, "accepted but mapping is nil"
		}
		if got.Mapping.MappedPort != want.MappedPort {
			return false, fmt.Sprintf("mapped_port: want %d got %d",
				want.MappedPort, got.Mapping.MappedPort)
		}
	}
	if want.State != "" {
		if got.Mapping == nil {
			return false, "accepted but mapping is nil"
		}
		if got.Mapping.State != want.State {
			return false, fmt.Sprintf("state: want %s got %s", want.State, got.Mapping.State)
		}
	}
	if want.ClockRewind != got.ClockRewind {
		return false, fmt.Sprintf("clock_rewind: want %v got %v", want.ClockRewind, got.ClockRewind)
	}
	if want.Swept > 0 && got.Swept != want.Swept {
		return false, fmt.Sprintf("swept: want %d got %d", want.Swept, got.Swept)
	}
	return true, ""
}

func groupOrder(pkts []FixturePacket) []int {
	seen := map[int]bool{}
	var out []int
	for _, p := range pkts {
		if !seen[p.Group] {
			seen[p.Group] = true
			out = append(out, p.Group)
		}
	}
	sort.Ints(out)
	return out
}
