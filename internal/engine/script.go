// Package engine runs deterministic offline replays: it loads a fixture
// script, wires the synthetic network model to the IGMPv2 state core,
// drives an injected clock, emulates in-transit query loss and host-side
// report suppression, journals everything into SQLite, and evaluates
// scenario assertions.
package engine

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"

	"igmpv2timer/internal/config"
	"igmpv2timer/internal/model"
)

// MemberSpec declares a reactive host. Delays are pinned in fixtures for
// deterministic report ordering. GeneralDelaysMs optionally pins the delay
// used after the Nth delivered general query (index 0 = gen 1), modeling
// the fresh random delay a real host draws each round; entries fall back to
// GeneralDelayMs.
type MemberSpec struct {
	Name  string `json:"name"`
	Addr  string `json:"addr"`
	Iface string `json:"iface"`
	// GeneralDelayMs is this host's report delay after a General Query
	// (must be <= query_response_interval_ms).
	GeneralDelayMs int64 `json:"general_delay_ms,omitempty"`
	// GeneralDelaysMs pins per-round general-query delays.
	GeneralDelaysMs []int64 `json:"general_delays_ms,omitempty"`
	// LMQDelayMs is the report delay after a group-specific query
	// (must be <= last_member_query_interval_ms).
	LMQDelayMs int64 `json:"lmq_delay_ms,omitempty"`
}

// ScriptEvent is one action on the timeline.
type ScriptEvent struct {
	At         int64  `json:"at_ms"`
	Kind       string `json:"kind"` // report|leave|force_query|advance|checkpoint
	Member     string `json:"member,omitempty"`
	Group      string `json:"group,omitempty"`
	Iface      string `json:"iface,omitempty"`
	ResponseTo int64  `json:"response_to_gen,omitempty"` // 0 = unsolicited/resolved at runtime
	// RefGeneral means "answer the Nth general query delivered before now"
	// (resolved at runtime). Used for stale-round fixtures.
	RefGeneral int    `json:"ref_general_n,omitempty"`
	Deliver    *bool  `json:"deliver,omitempty"` // force_query: simulate in-transit loss when false
	RequestID  string `json:"request_id,omitempty"`
	Note       string `json:"note,omitempty"`
}

// DropRule marks queries matching a packet type as lost in transit
// (emitted by the router but never delivered to hosts) at or after
// FromMs, for the first Count occurrences.
type DropRule struct {
	Packet string `json:"packet"` // QUERY_GENERAL | QUERY_GROUP
	Iface  string `json:"iface,omitempty"`
	Group  string `json:"group,omitempty"`
	FromMs int64  `json:"from_ms"`
	Count  int    `json:"count"` // 0 = unlimited
}

// ExpectDiag asserts on one diagnostic record.
type ExpectDiag struct {
	AtMs       int64    `json:"at_ms,omitempty"`
	Iface      string   `json:"iface,omitempty"`
	Group      string   `json:"group,omitempty"`
	Member     string   `json:"member,omitempty"`
	Verdict    string   `json:"verdict,omitempty"`
	Reason     string   `json:"reason,omitempty"`
	Packet     string   `json:"packet,omitempty"`
	RequestID  string   `json:"request_id,omitempty"`
	GenActive  int64    `json:"gen_active,omitempty"`
	GenApplied int64    `json:"gen_applied,omitempty"`
	DeadlineMs int64    `json:"deadline_ms,omitempty"` // exact membership deadline asserted
	Members    []string `json:"members,omitempty"`
}

// Assertion is one named expectation evaluated after the replay.
type Assertion struct {
	ID     string     `json:"id"`
	Check  string     `json:"check"` // see Check* constants
	Target ExpectDiag `json:"target,omitempty"`
	// CHECK_PRESENT / CHECK_ABSENT
	AtMs  int64  `json:"at_ms,omitempty"`
	Iface string `json:"iface_p,omitempty"`
	Group string `json:"group_p,omitempty"`
	// CHECK_COUNT: AtMs optionally restricts to packets emitted at/before it.
	Packet    string `json:"packet,omitempty"`
	Verdict   string `json:"verdict,omitempty"`
	AtMsCount int64  `json:"at_ms_count,omitempty"`
	Want      int    `json:"want,omitempty"`
	// CHECK_INTERVAL
	StartMs int64 `json:"start_ms,omitempty"`
	EndMs   int64 `json:"end_ms,omitempty"`
}

// Check names — part of the fixture contract.
const (
	CheckPresent    = "present_at"   // group present at AtMs
	CheckAbsent     = "absent_at"    // group absent at AtMs
	CheckDiag       = "diag_matches" // a diagnostic matching Target exists
	CheckCount      = "count_packets"
	CheckInterval   = "interval_equals"
	CheckGeneration = "generation_guard" // Target: GenActive/GenApplied
)

// Script is a complete replay fixture.
type Script struct {
	Name       string        `json:"name"`
	Summary    string        `json:"summary"`
	Iface      string        `json:"iface"`
	Group      string        `json:"group"`
	Timing     config.Timing `json:"timing_override,omitempty"`
	Members    []MemberSpec  `json:"members"`
	Events     []ScriptEvent `json:"events"`
	Drops      []DropRule    `json:"drop_rules"`
	UntilMs    int64         `json:"until_ms"`
	Assertions []Assertion   `json:"assertions"`
}

// LoadScript parses a JSON scenario file.
func LoadScript(path string) (*Script, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read script %s: %w", path, err)
	}
	var s Script
	if err := json.Unmarshal(raw, &s); err != nil {
		return nil, fmt.Errorf("parse script %s: %w", path, err)
	}
	if err := s.validate(); err != nil {
		return nil, err
	}
	return &s, nil
}

func (s *Script) validate() error {
	if s.Name == "" {
		return fmt.Errorf("script: name is required")
	}
	if s.Iface == "" {
		return fmt.Errorf("script %q: iface is required", s.Name)
	}
	if s.Group == "" {
		return fmt.Errorf("script %q: group is required", s.Name)
	}
	if s.UntilMs <= 0 {
		return fmt.Errorf("script %q: until_ms must be > 0", s.Name)
	}
	names := map[string]bool{}
	for i, m := range s.Members {
		if m.Name == "" || m.Addr == "" {
			return fmt.Errorf("script %q: members[%d] needs name and addr", s.Name, i)
		}
		if m.Iface == "" {
			// member defaults to the scenario interface
			// (mutate a normalized copy)
		}
		if names[m.Name] {
			return fmt.Errorf("script %q: duplicate member %q", s.Name, m.Name)
		}
		names[m.Name] = true
	}
	validKind := map[string]bool{
		"report": true, "leave": true, "force_query": true,
		"advance": true, "checkpoint": true,
	}
	for i, e := range s.Events {
		if !validKind[e.Kind] {
			return fmt.Errorf("script %q: events[%d] unknown kind %q", s.Name, i, e.Kind)
		}
		if e.At < 0 {
			return fmt.Errorf("script %q: events[%d] negative at_ms", s.Name, i)
		}
	}
	for i, a := range s.Assertions {
		switch a.Check {
		case CheckPresent, CheckAbsent, CheckDiag, CheckCount,
			CheckInterval, CheckGeneration:
		default:
			return fmt.Errorf("script %q: assertions[%d] unknown check %q",
				s.Name, i, a.Check)
		}
		if a.ID == "" {
			return fmt.Errorf("script %q: assertions[%d] missing id", s.Name, i)
		}
	}
	// deterministic event order: sort by (at_ms, original index implicit
	// via stable ordering); we keep script order among equal timestamps.
	sort.SliceStable(s.Events, func(i, j int) bool {
		return s.Events[i].At < s.Events[j].At
	})
	return nil
}

// normalizedMember returns the member spec with defaulted iface.
func (s *Script) normalizedMember(m MemberSpec) MemberSpec {
	if m.Iface == "" {
		m.Iface = s.Iface
	}
	return m
}

// Until returns the scenario horizon as model time.
func (s *Script) Until() model.Millis { return model.Millis(s.UntilMs) }

// Report is the replay result handed to callers/tests.
type Report struct {
	Script        string
	Diags         []model.Diag
	Emitted       []model.EmittedPkt
	Intervals     []model.Interval
	Snapshots     map[int64]model.StateSnapshot // keyed by checkpoint time
	FinalSnapshot model.StateSnapshot           // state at until_ms
	Assertions    []AssertionResult
}

// AssertionResult is the evaluated outcome of one assertion — independent
// tests consume these directly.
type AssertionResult struct {
	ID      string `json:"id"`
	Check   string `json:"check"`
	Pass    bool   `json:"pass"`
	Failure string `json:"failure_category,omitempty"`
	Detail  string `json:"detail"`
}
