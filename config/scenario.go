// Package config parses and validates the synthetic scenario definition:
// the fixed small topology, per-session import/export policies and the
// ordered stream of synthetic UPDATE/WITHDRAWAL events.
//
// Parsing (shape/type errors) and validation (semantic errors) are kept
// separate so failure categories remain distinguishable:
//
//	JSON syntax errors            -> INPUT/PAYLOAD_SYNTAX
//	unknown enum / bad range      -> INPUT/INVALID_CONFIG
//	unknown router/session/prefix -> INPUT/UNREFERENCED_ENTITY
//
// The parsed *Scenario is an immutable, indexed structure; other packages
// never see the raw JSON DTOs.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"pvsim/model"
)

// ---------------------------------------------------------------------------
// Parsed domain structures
// ---------------------------------------------------------------------------

// Scenario is the full validated replay input.
type Scenario struct {
	Name         string
	MaxSteps     int
	Routers      []*Router
	Sessions     []*Session
	Events       []*Event
	routerByName map[string]*Router
	sessByID     map[string]*Session
	sessByPeers  map[string]map[string]*Session // routerA -> routerB -> session
}

// Router is one speaking node in the fixed topology.
type Router struct {
	Name    string
	ASN     uint32
	Ordinal int // declaration order; doubles as deterministic router id
}

// Session is a directed pair of BGP speakers. Sessions are undirected at
// the topology level: both ends receive/send messages on one session, and
// each side keeps its own independent import/export policy.
type Session struct {
	ID       string
	A, B     string        // router names
	Type     string        // "ebgp" or "ibgp"
	IGPCostA int           // IGP cost A->next-hop B (iBGP next-hop resolution)
	IGPCostB int           // IGP cost B->next-hop A
	ImportA  []*PolicyRule // policy applied at A on routes learned from B
	ImportB  []*PolicyRule // policy applied at B on routes learned from A
	ExportA  []*PolicyRule // policy applied at A before sending toward B
	ExportB  []*PolicyRule // policy applied at B before sending toward A
}

// PolicyRule is one first-match rule of an import/export policy.
//
// All match fields are optional; an empty rule matches every route.
// Rules are evaluated in order; the first matching rule decides
// (permit/deny + actions). If no rule matches, the route is permitted
// unmodified (see README "Policy semantics").
type PolicyRule struct {
	Name    string
	Match   Match
	Actions []Action
	Deny    bool
}

// Match selects routes by prefix, path or attributes.
type Match struct {
	Prefix         string   // exact prefix equality
	PrefixSet      []string // membership (any match)
	ASPathContains []uint32 // path contains any listed AS
	ASPathEquals   []uint32 // exact ordered path
	ASPathLengthGT int      // -1 = unset
	ASPathLengthLT int      // -1 = unset
	LocalPrefGT    *int
	LocalPrefLT    *int
	MedGT          *int
	MedLT          *int
	Origin         []string
	FromRouter     string // only meaningful on import: direct peer router
}

// Action mutates permitted route attributes.
type Action struct {
	Type         ActionType
	SetLocalPref *int
	SetMed       *int
	SetOrigin    *string
	PrependAS    []uint32 // prepended in given order, left to right
}

// ActionType enumerates supported policy actions.
type ActionType string

const (
	ActionSetLocalPref ActionType = "set_local_pref"
	ActionSetMed       ActionType = "set_med"
	ActionSetOrigin    ActionType = "set_origin"
	ActionPrependAS    ActionType = "prepend_as"
)

// Event is one synthetic external UPDATE or WITHDRAWAL, injected as if
// received from a configured neighbor session (no public-network peer).
type Event struct {
	Seq      int
	Router   string // receiving router
	Peer     string // neighbor router (must share a session with Router)
	Prefix   string
	Kind     string      // "update" or "withdraw"
	Attrs    model.Attrs // meaningful for update
	AttrsSet bool
}

// LookupRouter resolves a router by name.
func (s *Scenario) LookupRouter(name string) (*Router, bool) {
	r, ok := s.routerByName[name]
	return r, ok
}

// LookupSession resolves a session by id.
func (s *Scenario) LookupSession(id string) (*Session, bool) {
	x, ok := s.sessByID[id]
	return x, ok
}

// SessionBetween returns the session linking two routers and the IGP cost
// from `from` toward the next-hop `to`.
func (s *Scenario) SessionBetween(from, to string) (*Session, int, bool) {
	if m := s.sessByPeers[from]; m != nil {
		if x, ok := m[to]; ok {
			if x.A == from {
				return x, x.IGPCostA, true
			}
			return x, x.IGPCostB, true
		}
	}
	return nil, 0, false
}

// ImportPolicy returns the import policy applied at `at` on routes arriving
// from `from` on the given session.
func (s *Scenario) ImportPolicy(at string, sess *Session) []*PolicyRule {
	if sess.A == at {
		return sess.ImportA
	}
	return sess.ImportB
}

// ExportPolicy returns the export policy applied at `at` toward `to`.
func (s *Scenario) ExportPolicy(at string, sess *Session) []*PolicyRule {
	if sess.A == at {
		return sess.ExportA
	}
	return sess.ExportB
}

// ---------------------------------------------------------------------------
// JSON DTOs
// ---------------------------------------------------------------------------

type scenarioDTO struct {
	Name     string       `json:"name"`
	MaxSteps int          `json:"max_steps"`
	Routers  []routerDTO  `json:"routers"`
	Sessions []sessionDTO `json:"sessions"`
	Events   []eventDTO   `json:"events"`
}

type routerDTO struct {
	Name string `json:"name"`
	ASN  uint32 `json:"asn"`
}

type policyDTO struct {
	Name    string      `json:"name"`
	Deny    bool        `json:"deny"`
	Match   matchDTO    `json:"match"`
	Actions []actionDTO `json:"actions"`
}

type matchDTO struct {
	Prefix         string   `json:"prefix"`
	PrefixSet      []string `json:"prefix_set"`
	ASPathContains []uint32 `json:"as_path_contains"`
	ASPathEquals   []uint32 `json:"as_path_equals"`
	ASPathLengthGT *int     `json:"as_path_length_gt"`
	ASPathLengthLT *int     `json:"as_path_length_lt"`
	LocalPrefGT    *int     `json:"local_pref_gt"`
	LocalPrefLT    *int     `json:"local_pref_lt"`
	MedGT          *int     `json:"med_gt"`
	MedLT          *int     `json:"med_lt"`
	Origin         []string `json:"origin"`
	FromRouter     string   `json:"from_router"`
}

type actionDTO struct {
	Type         string   `json:"type"`
	SetLocalPref *int     `json:"set_local_pref"`
	SetMed       *int     `json:"set_med"`
	SetOrigin    *string  `json:"set_origin"`
	PrependAS    []uint32 `json:"prepend_as"`
}

type sessionDTO struct {
	ID       string      `json:"id"`
	A        string      `json:"a"`
	B        string      `json:"b"`
	Type     string      `json:"type"`
	IGPCostA int         `json:"igp_cost_a"`
	IGPCostB int         `json:"igp_cost_b"`
	ImportA  []policyDTO `json:"import_a"`
	ImportB  []policyDTO `json:"import_b"`
	ExportA  []policyDTO `json:"export_a"`
	ExportB  []policyDTO `json:"export_b"`
}

type eventDTO struct {
	Seq    int       `json:"seq"`
	Router string    `json:"router"`
	Peer   string    `json:"peer"`
	Prefix string    `json:"prefix"`
	Kind   string    `json:"kind"`
	Attrs  *attrsDTO `json:"attrs"`
}

type attrsDTO struct {
	LocalPref *int     `json:"local_pref"`
	ASPath    []uint32 `json:"as_path"`
	Med       *int     `json:"med"`
	Origin    string   `json:"origin"`
}

// ---------------------------------------------------------------------------
// Load / parse
// ---------------------------------------------------------------------------

// LoadFile parses a scenario from a JSON file.
func LoadFile(path string) (*Scenario, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, model.NewError(model.KindInput, "FILE_UNREADABLE", "read scenario %q: %v", path, err)
	}
	return Parse(raw)
}

// Parse parses and validates a scenario from JSON bytes.
func Parse(raw []byte) (*Scenario, error) {
	var d scenarioDTO
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&d); err != nil {
		return nil, model.NewError(model.KindInput, "PAYLOAD_SYNTAX", "scenario is not valid JSON: %v", err)
	}
	sc := &Scenario{
		Name:         d.Name,
		MaxSteps:     d.MaxSteps,
		routerByName: map[string]*Router{},
		sessByID:     map[string]*Session{},
		sessByPeers:  map[string]map[string]*Session{},
	}
	for i, r := range d.Routers {
		rr := &Router{Name: r.Name, ASN: r.ASN, Ordinal: i}
		sc.Routers = append(sc.Routers, rr)
		sc.routerByName[r.Name] = rr
	}
	for _, sd := range d.Sessions {
		sess, err := buildSession(sd)
		if err != nil {
			return nil, err
		}
		sc.Sessions = append(sc.Sessions, sess)
		sc.sessByID[sess.ID] = sess
		addPeer(sc.sessByPeers, sess.A, sess.B, sess)
		addPeer(sc.sessByPeers, sess.B, sess.A, sess)
	}
	for i, e := range d.Events {
		ev, err := buildEvent(e)
		if err != nil {
			return nil, err
		}
		if ev.Seq == 0 {
			ev.Seq = i + 1
		}
		sc.Events = append(sc.Events, ev)
	}
	if err := sc.validate(); err != nil {
		return nil, err
	}
	return sc, nil
}

func addPeer(m map[string]map[string]*Session, a, b string, s *Session) {
	mm := m[a]
	if mm == nil {
		mm = map[string]*Session{}
		m[a] = mm
	}
	mm[b] = s
}

func buildSession(d sessionDTO) (*Session, error) {
	if d.ID == "" {
		return nil, model.NewError(model.KindInput, "INVALID_CONFIG", "session missing id")
	}
	t := strings.ToLower(d.Type)
	if t == "" {
		t = "ebgp"
	}
	if t != "ebgp" && t != "ibgp" {
		return nil, model.NewError(model.KindInput, "INVALID_CONFIG",
			"session %q: type must be ebgp|ibgp, got %q", d.ID, d.Type)
	}
	if d.IGPCostA < 0 || d.IGPCostB < 0 {
		return nil, model.NewError(model.KindInput, "INVALID_CONFIG",
			"session %q: igp costs must be >= 0", d.ID)
	}
	s := &Session{ID: d.ID, A: d.A, B: d.B, Type: t,
		IGPCostA: d.IGPCostA, IGPCostB: d.IGPCostB}
	var err error
	if s.ImportA, err = buildPolicies(d.ImportA, d.ID, "import_a"); err != nil {
		return nil, err
	}
	if s.ImportB, err = buildPolicies(d.ImportB, d.ID, "import_b"); err != nil {
		return nil, err
	}
	if s.ExportA, err = buildPolicies(d.ExportA, d.ID, "export_a"); err != nil {
		return nil, err
	}
	if s.ExportB, err = buildPolicies(d.ExportB, d.ID, "export_b"); err != nil {
		return nil, err
	}
	return s, nil
}

func buildPolicies(in []policyDTO, sessID, side string) ([]*PolicyRule, error) {
	out := make([]*PolicyRule, 0, len(in))
	for i, p := range in {
		r := &PolicyRule{Name: p.Name, Deny: p.Deny}
		label := func() string {
			if r.Name != "" {
				return fmt.Sprintf("session %q %s rule %q", sessID, side, r.Name)
			}
			return fmt.Sprintf("session %q %s rule #%d", sessID, side, i+1)
		}
		m := p.Match
		r.Match = Match{
			Prefix: m.Prefix, PrefixSet: m.PrefixSet,
			ASPathContains: m.ASPathContains, ASPathEquals: m.ASPathEquals,
			LocalPrefGT: m.LocalPrefGT, LocalPrefLT: m.LocalPrefLT,
			MedGT: m.MedGT, MedLT: m.MedLT,
			Origin: m.Origin, FromRouter: m.FromRouter,
		}
		r.Match.ASPathLengthGT = -1
		r.Match.ASPathLengthLT = -1
		if m.ASPathLengthGT != nil {
			r.Match.ASPathLengthGT = *m.ASPathLengthGT
		}
		if m.ASPathLengthLT != nil {
			r.Match.ASPathLengthLT = *m.ASPathLengthLT
		}
		for _, o := range r.Match.Origin {
			if _, ok := model.ParseOrigin(o); !ok {
				return nil, model.NewError(model.KindInput, "INVALID_CONFIG",
					"%s: unknown origin %q", label(), o)
			}
		}
		for _, a := range p.Actions {
			act, err := buildAction(a, label())
			if err != nil {
				return nil, err
			}
			if p.Deny {
				return nil, model.NewError(model.KindInput, "INVALID_CONFIG",
					"%s: deny rules cannot carry actions", label())
			}
			r.Actions = append(r.Actions, act)
		}
		out = append(out, r)
	}
	return out, nil
}

func buildAction(a actionDTO, label string) (Action, error) {
	switch ActionType(a.Type) {
	case ActionSetLocalPref:
		if a.SetLocalPref == nil {
			return Action{}, model.NewError(model.KindInput, "INVALID_CONFIG",
				"%s: set_local_pref requires set_local_pref value", label)
		}
		if *a.SetLocalPref < 0 {
			return Action{}, model.NewError(model.KindInput, "INVALID_CONFIG",
				"%s: local_pref must be >= 0", label)
		}
		return Action{Type: ActionSetLocalPref, SetLocalPref: a.SetLocalPref}, nil
	case ActionSetMed:
		if a.SetMed == nil || *a.SetMed < 0 {
			return Action{}, model.NewError(model.KindInput, "INVALID_CONFIG",
				"%s: set_med requires non-negative value", label)
		}
		return Action{Type: ActionSetMed, SetMed: a.SetMed}, nil
	case ActionSetOrigin:
		if a.SetOrigin == nil {
			return Action{}, model.NewError(model.KindInput, "INVALID_CONFIG",
				"%s: set_origin requires set_origin value", label)
		}
		if _, ok := model.ParseOrigin(*a.SetOrigin); !ok {
			return Action{}, model.NewError(model.KindInput, "INVALID_CONFIG",
				"%s: unknown origin %q", label, *a.SetOrigin)
		}
		return Action{Type: ActionSetOrigin, SetOrigin: a.SetOrigin}, nil
	case ActionPrependAS:
		if len(a.PrependAS) == 0 {
			return Action{}, model.NewError(model.KindInput, "INVALID_CONFIG",
				"%s: prepend_as requires prepend_as list", label)
		}
		for _, asn := range a.PrependAS {
			if asn == 0 {
				return Action{}, model.NewError(model.KindInput, "INVALID_CONFIG",
					"%s: prepend_as values must be 1..4294967295", label)
			}
		}
		return Action{Type: ActionPrependAS, PrependAS: a.PrependAS}, nil
	default:
		return Action{}, model.NewError(model.KindInput, "INVALID_CONFIG",
			"%s: unknown action type %q", label, a.Type)
	}
}

func buildEvent(d eventDTO) (*Event, error) {
	k := strings.ToLower(d.Kind)
	if k != "update" && k != "withdraw" {
		return nil, model.NewError(model.KindInput, "INVALID_CONFIG",
			"event seq=%d: kind must be update|withdraw, got %q", d.Seq, d.Kind)
	}
	if d.Prefix == "" {
		return nil, model.NewError(model.KindInput, "INVALID_CONFIG",
			"event seq=%d: missing prefix", d.Seq)
	}
	ev := &Event{Seq: d.Seq, Router: d.Router, Peer: d.Peer, Prefix: d.Prefix, Kind: k}
	if d.Attrs != nil {
		ev.AttrsSet = true
		at, err := attrsFromDTO(d.Attrs)
		if err != nil {
			return nil, model.NewError(model.KindInput, "INVALID_CONFIG",
				"event seq=%d: %v", d.Seq, err)
		}
		ev.Attrs = at
	}
	return ev, nil
}

func attrsFromDTO(d *attrsDTO) (model.Attrs, error) {
	var a model.Attrs
	a.LocalPref = d.LocalPref
	a.ASPath = d.ASPath
	a.Med = d.Med
	o, ok := model.ParseOrigin(d.Origin)
	if !ok {
		return a, fmt.Errorf("unknown origin %q", d.Origin)
	}
	a.Origin = o
	return a, nil
}
