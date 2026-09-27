// Package config parses and validates the querier timing configuration.
//
// The parameters follow RFC 2236 (IGMPv2) naming. All durations are given
// in seconds in the JSON form and converted to milliseconds internally.
// Defaults are the RFC 2236 defaults.
package config

import (
	"encoding/json"
	"fmt"
	"sort"

	"igmpq/internal/cats"
)

// RFC 2236 defaults.
const (
	DefaultQueryInterval           = 125 // seconds
	DefaultQueryResponseInterval   = 10  // seconds
	DefaultRobustnessVariable      = 2
	DefaultLastMemberQueryInterval = 1 // seconds
)

// Config is the querier timing configuration.
//
// A zero-valued field means "use the RFC default", except Interfaces which
// must always be declared explicitly.
type Config struct {
	// Interfaces is the set of interface names events may refer to.
	Interfaces []string `json:"interfaces"`

	// QueryIntervalSec is the interval between general queries (QI).
	QueryIntervalSec int `json:"query_interval_sec"`
	// QueryResponseIntervalSec is the max response time in queries (QRI).
	QueryResponseIntervalSec int `json:"query_response_interval_sec"`
	// RobustnessVariable tolerates packet loss (RV). Must be >= 2.
	RobustnessVariable int `json:"robustness_variable"`
	// LastMemberQueryIntervalSec is the interval between group-specific
	// queries sent after a leave (LMQI).
	LastMemberQueryIntervalSec int `json:"last_member_query_interval_sec"`
	// LastMemberQueryCount is how many group-specific queries are sent
	// before a group is dropped (LMQC). Defaults to RobustnessVariable.
	LastMemberQueryCount int `json:"last_member_query_count"`
}

// Derived holds the timing values computed from Config. These are the
// numbers the engine actually schedules on.
type Derived struct {
	// GroupMembershipIntervalMS = RV*QI + QRI. How long a group is kept
	// after the last accepted report.
	GroupMembershipIntervalMS int64 `json:"group_membership_interval_ms"`
	// LastMemberQueryTimeMS = LMQI*LMQC. How long the last-member phase
	// lasts after the final member leaves.
	LastMemberQueryTimeMS int64 `json:"last_member_query_time_ms"`
}

// Derived computes the derived timing values. c must be normalized first.
func (c Config) Derived() Derived {
	gmi := int64(c.RobustnessVariable*c.QueryIntervalSec+c.QueryResponseIntervalSec) * 1000
	lmqt := int64(c.LastMemberQueryIntervalSec*c.LastMemberQueryCount) * 1000
	return Derived{GroupMembershipIntervalMS: gmi, LastMemberQueryTimeMS: lmqt}
}

// Parse decodes JSON config, applies RFC defaults and validates.
func Parse(raw json.RawMessage) (Config, error) {
	var c Config
	if len(raw) == 0 {
		raw = []byte(`{}`)
	}
	if err := json.Unmarshal(raw, &c); err != nil {
		return Config{}, cats.New(cats.InvalidConfig, "config is not valid JSON: "+err.Error())
	}
	return Normalize(c)
}

// Normalize applies defaults and validates the config.
func Normalize(c Config) (Config, error) {
	if c.QueryIntervalSec == 0 {
		c.QueryIntervalSec = DefaultQueryInterval
	}
	if c.QueryResponseIntervalSec == 0 {
		c.QueryResponseIntervalSec = DefaultQueryResponseInterval
	}
	if c.RobustnessVariable == 0 {
		c.RobustnessVariable = DefaultRobustnessVariable
	}
	if c.LastMemberQueryIntervalSec == 0 {
		c.LastMemberQueryIntervalSec = DefaultLastMemberQueryInterval
	}
	if c.LastMemberQueryCount == 0 {
		c.LastMemberQueryCount = c.RobustnessVariable
	}
	return c, validate(c)
}

func validate(c Config) error {
	fail := func(field, msg string) error {
		return cats.New(cats.InvalidConfig, fmt.Sprintf("field %s: %s", field, msg))
	}
	if len(c.Interfaces) == 0 {
		return fail("interfaces", "at least one interface must be declared")
	}
	seen := map[string]bool{}
	for _, ifc := range c.Interfaces {
		if ifc == "" {
			return fail("interfaces", "interface names must not be empty")
		}
		if seen[ifc] {
			return fail("interfaces", "duplicate interface "+ifc)
		}
		seen[ifc] = true
	}
	if c.QueryIntervalSec <= 0 {
		return fail("query_interval_sec", "must be > 0")
	}
	if c.QueryResponseIntervalSec <= 0 {
		return fail("query_response_interval_sec", "must be > 0")
	}
	// RFC 2236 §4: the Robustness Variable must not be zero and should
	// not be one.
	if c.RobustnessVariable < 2 {
		return fail("robustness_variable", "must be >= 2")
	}
	if c.LastMemberQueryIntervalSec <= 0 {
		return fail("last_member_query_interval_sec", "must be > 0")
	}
	if c.LastMemberQueryCount <= 0 {
		return fail("last_member_query_count", "must be > 0")
	}
	return nil
}

// HasInterface reports whether name is a declared interface.
func (c Config) HasInterface(name string) bool {
	for _, ifc := range c.Interfaces {
		if ifc == name {
			return true
		}
	}
	return false
}

// InterfacesSorted returns declared interfaces in stable order (for logs).
func (c Config) InterfacesSorted() []string {
	out := append([]string(nil), c.Interfaces...)
	sort.Strings(out)
	return out
}
