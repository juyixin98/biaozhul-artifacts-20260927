// Package config parses and validates replay configurations: topology,
// per-router import/export policies, synthetic seed events and engine limits.
package config

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"pathvector/internal/ierr"
	"pathvector/internal/model"
)

// Match selects UPDATEs by exact prefix and/or peer router id. Empty fields
// are wildcards; multiple fields are AND-combined. Only exact prefix
// matching is supported (no prefix-lists/RPL) and documented as such.
type Match struct {
	Prefix string `json:"prefix,omitempty"`
	Peer   string `json:"peer,omitempty"`
}

func (m Match) matches(prefix, peer string) bool {
	if m.Prefix != "" && m.Prefix != prefix {
		return false
	}
	if m.Peer != "" && m.Peer != peer {
		return false
	}
	return true
}

// Action is the consequence of a matching rule. A rule either permits
// (allow: true) or denies (allow: false). Permit rules may rewrite
// attributes. Prepend is export-only: it repeats the speaker's own ASN in
// AS_PATH before the normal eBGP prepend.
type Action struct {
	Allow        bool    `json:"allow"`
	SetLocalPref *uint32 `json:"set_local_pref,omitempty"`
	SetMED       *uint32 `json:"set_med,omitempty"`
	Prepend      int     `json:"prepend,omitempty"`
}

// Rule is one first-match-wins policy entry.
type Rule struct {
	Name   string `json:"name"`
	Match  Match  `json:"match"`
	Action Action `json:"action"`
}

// Policy holds the independent import and export rule chains of one router.
type Policy struct {
	Import []Rule `json:"import"`
	Export []Rule `json:"export"`
}

// Config is the full replay input.
type Config struct {
	// Description is an optional human-readable note carried by fixtures.
	Description string         `json:"description,omitempty"`
	Topology    model.Topology `json:"topology"`
	// Policies is keyed by router id. Missing routers get permit-all.
	Policies map[string]*Policy `json:"policies,omitempty"`
	// Events are the synthetic seed UPDATEs, replayed in Seq order.
	Events []model.SeedEvent `json:"events"`
	// Budget is the maximum number of processing steps (seeds + internal
	// messages) before the run is reported non-converged.
	Budget int `json:"budget,omitempty"`
	// QueueCap bounds the internal message queue; exceeding it fails the
	// run with resource_exhausted.
	QueueCap int `json:"queue_cap,omitempty"`
}

const (
	defaultBudget   = 500
	defaultQueueCap = 20000
	maxPrepend      = 10
)

// LoadFile reads a JSON config from disk.
func LoadFile(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, ierr.Wrap(ierr.KindInvalidInput, "config.LoadFile", "cannot read "+path, err)
	}
	cfg, err := Parse(raw)
	if err != nil {
		return nil, err
	}
	return cfg, nil
}

// LoadFixture resolves a fixture name against a directory: name may be a
// bare id looked up as <dir>/<name>.json or an explicit path.
func LoadFixture(dir, name string) (*Config, error) {
	p := name
	if dir != "" && filepath.Base(name) == name {
		p = filepath.Join(dir, name+".json")
	}
	return LoadFile(p)
}

// Parse decodes and validates a config payload.
func Parse(raw []byte) (*Config, error) {
	const op = "config.Parse"
	var cfg Config
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&cfg); err != nil {
		return nil, ierr.Wrap(ierr.KindInvalidInput, op, "invalid JSON config", err)
	}
	if dec.More() {
		return nil, ierr.New(ierr.KindInvalidInput, op, "multiple JSON values in config payload")
	}
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &cfg, nil
}

// Validate runs the full cross-field validation. It builds the topology
// index so the same validated index is later reused by the engine.
func (c *Config) Validate() error {
	const op = "config.Validate"
	if c.Budget < 0 {
		return ierr.New(ierr.KindInvalidInput, op, "budget must be >= 0")
	}
	if c.Budget == 0 {
		c.Budget = defaultBudget
	}
	if c.QueueCap < 0 {
		return ierr.New(ierr.KindInvalidInput, op, "queue_cap must be >= 0")
	}
	if c.QueueCap == 0 {
		c.QueueCap = defaultQueueCap
	}
	idx, err := c.Topology.Build()
	if err != nil {
		return err
	}
	for id, p := range c.Policies {
		if _, ok := idx.Nodes[id]; !ok {
			return ierr.New(ierr.KindInvalidInput, op, "policy attached to unknown router "+id)
		}
		if err := validateRules(id, "import", p.Import, idx, false); err != nil {
			return err
		}
		if err := validateRules(id, "export", p.Export, idx, true); err != nil {
			return err
		}
	}
	seen := map[int]bool{}
	for i := range c.Events {
		e := &c.Events[i]
		if err := validateEvent(e, idx, i, seen); err != nil {
			return err
		}
	}
	return nil
}

func validateRules(router, dir string, rules []Rule, idx *model.Index, export bool) error {
	const op = "config.Validate"
	names := map[string]bool{}
	for i, r := range rules {
		if r.Name == "" {
			return ierr.New(ierr.KindInvalidInput, op,
				fmt.Sprintf("%s %s rule #%d has no name", router, dir, i))
		}
		if names[r.Name] {
			return ierr.New(ierr.KindInvalidInput, op,
				fmt.Sprintf("%s %s duplicate rule name %s", router, dir, r.Name))
		}
		names[r.Name] = true
		if r.Match.Prefix != "" {
			if _, err := model.ParsePrefix(r.Match.Prefix); err != nil {
				return ierr.Wrap(ierr.KindInvalidInput, op,
					fmt.Sprintf("%s %s rule %s: bad match prefix", router, dir, r.Name), err)
			}
		}
		if r.Match.Peer != "" {
			if _, ok := idx.Nodes[r.Match.Peer]; !ok {
				return ierr.New(ierr.KindInvalidInput, op,
					fmt.Sprintf("%s %s rule %s: match peer %s not in topology", router, dir, r.Name, r.Match.Peer))
			}
		}
		a := r.Action
		if !a.Allow && (a.SetLocalPref != nil || a.SetMED != nil || a.Prepend != 0) {
			return ierr.New(ierr.KindInvalidInput, op,
				fmt.Sprintf("%s %s rule %s: deny action cannot rewrite attributes", router, dir, r.Name))
		}
		if a.Prepend < 0 || a.Prepend > maxPrepend {
			return ierr.New(ierr.KindInvalidInput, op,
				fmt.Sprintf("%s %s rule %s: prepend out of range [0,%d]", router, dir, r.Name, maxPrepend))
		}
		// Direction of attribute authority, matching the BGP data plane:
		// local-pref is set on import only; MED and AS-path prepend are
		// set on export only.
		if !export {
			if a.SetMED != nil {
				return ierr.New(ierr.KindInvalidInput, op,
					fmt.Sprintf("%s import rule %s: set_med is export-only", router, r.Name))
			}
			if a.Prepend > 0 {
				return ierr.New(ierr.KindInvalidInput, op,
					fmt.Sprintf("%s import rule %s: prepend is export-only", router, r.Name))
			}
		} else if a.SetLocalPref != nil {
			return ierr.New(ierr.KindInvalidInput, op,
				fmt.Sprintf("%s export rule %s: set_local_pref is import-only", router, r.Name))
		}
	}
	return nil
}

func validateEvent(e *model.SeedEvent, idx *model.Index, i int, seen map[int]bool) error {
	const op = "config.Validate"
	if e.Seq <= 0 {
		return ierr.New(ierr.KindInvalidInput, op, fmt.Sprintf("event #%d: seq must be > 0", i))
	}
	if seen[e.Seq] {
		return ierr.New(ierr.KindInvalidInput, op, fmt.Sprintf("event #%d: duplicate seq %d", i, e.Seq))
	}
	seen[e.Seq] = true
	switch e.Kind {
	case model.SeedAnnounce, model.SeedWithdraw:
	default:
		return ierr.New(ierr.KindInvalidInput, op, fmt.Sprintf("event seq %d: unknown kind %q", e.Seq, e.Kind))
	}
	if _, ok := idx.Nodes[e.RouterID]; !ok {
		return ierr.New(ierr.KindInvalidInput, op, fmt.Sprintf("event seq %d: unknown router %s", e.Seq, e.RouterID))
	}
	pfx, err := model.ParsePrefix(e.Prefix)
	if err != nil {
		return ierr.Wrap(ierr.KindInvalidInput, op, fmt.Sprintf("event seq %d", e.Seq), err)
	}
	e.Prefix = pfx
	if e.Peer != "" {
		if _, ok := idx.Nodes[e.Peer]; !ok {
			return ierr.New(ierr.KindInvalidInput, op, fmt.Sprintf("event seq %d: unknown peer %s", e.Seq, e.Peer))
		}
		if _, ok := idx.Nodes[e.RouterID]; ok && !isPeer(idx, e.RouterID, e.Peer) {
			return ierr.New(ierr.KindInvalidInput, op,
				fmt.Sprintf("event seq %d: %s has no session with %s", e.Seq, e.RouterID, e.Peer))
		}
	}
	if e.NextHop != "" {
		if err := model.ParseAddress(e.NextHop); err != nil {
			return ierr.Wrap(ierr.KindInvalidInput, op, fmt.Sprintf("event seq %d", e.Seq), err)
		}
	}
	if e.LocalPref != nil && *e.LocalPref == 0 {
		return ierr.New(ierr.KindInvalidInput, op, fmt.Sprintf("event seq %d: local_pref must be > 0", e.Seq))
	}
	if _, err := model.ParseOrigin(e.Origin); err != nil {
		return ierr.Wrap(ierr.KindInvalidInput, op, fmt.Sprintf("event seq %d", e.Seq), err)
	}
	if e.Kind == model.SeedAnnounce && e.Peer == "" && e.NextHop == "" {
		return ierr.New(ierr.KindInvalidInput, op,
			fmt.Sprintf("event seq %d: local-origin announce requires next_hop", e.Seq))
	}
	return nil
}

func isPeer(idx *model.Index, a, b string) bool {
	for _, n := range idx.Neighbors(a) {
		if n == b {
			return true
		}
	}
	return false
}
