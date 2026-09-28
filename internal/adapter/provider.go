// Package adapter is the boundary to the "real" environment. Only a simulated
// provider is implemented, but the reconcile engine depends solely on the
// Provider interface: swapping in a different local backend needs no engine
// changes.
//
// The simulator is stateful and durable for the life of the process, supports
// explicit fault injection (including the critical "create committed but
// response lost" ambiguity), records every operation as evidence, and enforces
// a capacity quota (resource exhaustion).
package adapter

import (
	"context"
	"sync"

	"infraplanner/internal/model"
)

// Op is one provider call, recorded in execution order as evidence.
type Op struct {
	Seq     int               `json:"seq"`
	Method  string            `json:"method"` // Read|Create|Update|Delete
	Ref     string            `json:"ref"`
	IdemKey string            `json:"idem_key,omitempty"`
	Attrs   map[string]string `json:"attrs,omitempty"`
	Result  string            `json:"result"` // ok|error|response_lost
	Code    string            `json:"code,omitempty"`
	Token   string            `json:"token,omitempty"`
}

// FaultRule injects one fault, then consumes itself (unless Persistent).
type FaultRule struct {
	// Match method + ref (ref may be "*" for any).
	Method string
	Ref    string
	// OnceOnly fires only the first matching call; otherwise every match.
	OnceOnly bool
	// Kind: "response_lost" | "transient" | "exhausted"
	Kind string
}

// Provider is the resource backend contract.
type Provider interface {
	Observe(ctx context.Context) (*model.ObservedSet, error)
	// Read returns the live resource; (nil,nil) means it does not exist.
	Read(ctx context.Context, ref model.Ref) (*model.Resource, error)
	// Create with an idempotency key. A repeated key MUST return the same
	// result instead of creating a second instance.
	Create(ctx context.Context, s model.Spec, idemKey string) (token string, err error)
	Update(ctx context.Context, ref model.Ref, attrs map[string]string) error
	Delete(ctx context.Context, ref model.Ref, idemKey string) error
	// Ops returns the full call history (evidence).
	Ops() []Op
}

// Sim is the simulated environment.
type Sim struct {
	mu        sync.Mutex
	resources map[string]*model.Resource
	seq       int
	ops       []Op
	faults    []FaultRule
	// capacity: max number of existing resources (exhaustion simulation).
	capacity int
	// idempotency ledger: key -> (ref, token)
	idem map[string]idemRecord
	// lostCreates: keys whose create committed but whose response was lost.
	// The next Read reveals them — this models discovering a lost-commit.
	lostCreates map[string]bool
}

type idemRecord struct {
	ref   model.Ref
	token string
}

// NewSim builds an empty simulator. capacity<=0 means unlimited.
func NewSim(capacity int) *Sim {
	return &Sim{
		resources:   map[string]*model.Resource{},
		capacity:    capacity,
		idem:        map[string]idemRecord{},
		lostCreates: map[string]bool{},
	}
}

// Seed installs resources as pre-existing reality (e.g. an external stack).
func (s *Sim) Seed(rs ...*model.Resource) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, r := range rs {
		cp := *r
		if cp.Attrs == nil {
			cp.Attrs = map[string]string{}
		}
		s.resources[cp.Ref.String()] = &cp
	}
}

// InjectFault adds a fault rule.
func (s *Sim) InjectFault(f FaultRule) {
	s.mu.Lock()
	s.faults = append(s.faults, f)
	s.mu.Unlock()
}

func (s *Sim) consumeFault(method string, ref model.Ref) (FaultRule, bool) {
	for i, f := range s.faults {
		if f.Method != method {
			continue
		}
		if f.Ref != "*" && f.Ref != ref.String() {
			continue
		}
		if f.OnceOnly {
			s.faults = append(s.faults[:i], s.faults[i+1:]...)
		}
		return f, true
	}
	return FaultRule{}, false
}

func (s *Sim) log(method string, ref model.Ref, key string, attrs map[string]string, result, code, token string) {
	s.seq++
	s.ops = append(s.ops, Op{
		Seq: s.seq, Method: method, Ref: ref.String(), IdemKey: key,
		Attrs: cloneAttrs(attrs), Result: result, Code: code, Token: token,
	})
}

func cloneAttrs(a map[string]string) map[string]string {
	if a == nil {
		return nil
	}
	out := make(map[string]string, len(a))
	for k, v := range a {
		out[k] = v
	}
	return out
}

// Ops returns a copy of the operation evidence.
func (s *Sim) Ops() []Op {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Op, len(s.ops))
	copy(out, s.ops)
	return out
}

// CountOps helpers for tests -----------------------------------------------

func (s *Sim) countBy(method, ref string) int {
	n := 0
	for _, op := range s.ops {
		if op.Method == method && (ref == "" || op.Ref == ref) {
			n++
		}
	}
	return n
}

// CreateCount is a test helper: number of Create calls for a ref.
func (s *Sim) CreateCount(ref string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.countBy("Create", ref)
}

// DeleteCount is a test helper.
func (s *Sim) DeleteCount(ref string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.countBy("Delete", ref)
}

func (s *Sim) Observe(ctx context.Context) (*model.ObservedSet, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	set := &model.ObservedSet{Resources: map[string]*model.Resource{}}
	for k, r := range s.resources {
		cp := *r
		cp.Attrs = cloneAttrs(r.Attrs)
		cp.DependsOn = append([]string(nil), r.DependsOn...)
		set.Resources[k] = &cp
	}
	return set, nil
}

func (s *Sim) Read(ctx context.Context, ref model.Ref) (*model.Resource, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.resources[ref.String()]
	if !ok {
		s.log("Read", ref, "", nil, "ok", "NOT_FOUND", "")
		return nil, nil
	}
	// A resource whose create response was lost becomes a normal existing
	// resource as soon as reality is read (and its state normalizes).
	if s.lostCreates[ref.String()] {
		delete(s.lostCreates, ref.String())
		if r.State == model.StateCreateLost {
			r.State = model.StateExists
		}
	}
	s.log("Read", ref, "", nil, "ok", "FOUND", r.ProviderToken)
	cp := *r
	cp.Attrs = cloneAttrs(r.Attrs)
	return &cp, nil
}

func (s *Sim) Create(ctx context.Context, spec model.Spec, idemKey string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ref := spec.Ref()

	// Idempotency: replay of a known key returns the original outcome and
	// MUST NOT create a second resource.
	if rec, ok := s.idem[idemKey]; ok {
		s.log("Create", ref, idemKey, spec.Attrs, "ok", "IDEMPOTENT_REPLAY", rec.token)
		return rec.token, nil
	}

	if f, hit := s.consumeFault("Create", ref); hit {
		switch f.Kind {
		case "exhausted":
			s.log("Create", ref, idemKey, spec.Attrs, "error", "QUOTA", "")
			return "", errQuoted()
		case "transient":
			s.log("Create", ref, idemKey, spec.Attrs, "error", "TRANSIENT", "")
			return "", errTransient()
		case "response_lost":
			// The commit lands; the caller never sees a response. Record the
			// idempotency key so a replay is safe, but return an unknown-commit.
			token := s.putResource(ref, spec, true)
			s.idem[idemKey] = idemRecord{ref: ref, token: token}
			s.lostCreates[ref.String()] = true
			s.log("Create", ref, idemKey, spec.Attrs, "response_lost", "RESPONSE_LOST", "")
			return "", errLost()
		}
	}

	if s.capacity > 0 {
		n := 0
		for _, r := range s.resources {
			if r.State == model.StateExists {
				n++
			}
		}
		if n >= s.capacity {
			s.log("Create", ref, idemKey, spec.Attrs, "error", "QUOTA", "")
			return "", errQuoted()
		}
	}

	token := s.putResource(ref, spec, false)
	s.idem[idemKey] = idemRecord{ref: ref, token: token}
	s.log("Create", ref, idemKey, spec.Attrs, "ok", "CREATED", token)
	return token, nil
}

// putResource performs the actual state insertion under lock.
func (s *Sim) putResource(ref model.Ref, spec model.Spec, lost bool) string {
	token := "tok-" + ref.ID + "-" + itoa(s.seq+1)
	st := model.StateExists
	if lost {
		st = model.StateCreateLost
	}
	s.resources[ref.String()] = &model.Resource{
		Ref: ref, State: st, Attrs: cloneAttrs(spec.Attrs),
		Protected: spec.Protected, DependsOn: append([]string(nil), spec.DependsOn...),
		ProviderToken: token,
	}
	return token
}

func (s *Sim) Update(ctx context.Context, ref model.Ref, attrs map[string]string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if f, hit := s.consumeFault("Update", ref); hit {
		if f.Kind == "transient" {
			s.log("Update", ref, "", attrs, "error", "TRANSIENT", "")
			return errTransient()
		}
	}
	r, ok := s.resources[ref.String()]
	if !ok {
		s.log("Update", ref, "", attrs, "error", "NOT_FOUND", "")
		return errNotFound()
	}
	if r.State != model.StateExists {
		s.log("Update", ref, "", attrs, "error", "BAD_STATE", "")
		return errState(string(r.State))
	}
	r.Attrs = cloneAttrs(attrs)
	s.log("Update", ref, "", attrs, "ok", "UPDATED", "")
	return nil
}

func (s *Sim) Delete(ctx context.Context, ref model.Ref, idemKey string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if rec, ok := s.idem[idemKey]; ok {
		s.log("Delete", ref, idemKey, nil, "ok", "IDEMPOTENT_REPLAY", rec.token)
		return nil
	}
	if f, hit := s.consumeFault("Delete", ref); hit {
		switch f.Kind {
		case "transient":
			s.log("Delete", ref, idemKey, nil, "error", "TRANSIENT", "")
			return errTransient()
		case "response_lost":
			delete(s.resources, ref.String())
			delete(s.lostCreates, ref.String())
			s.idem[idemKey] = idemRecord{ref: ref, token: "deleted"}
			s.log("Delete", ref, idemKey, nil, "response_lost", "RESPONSE_LOST", "")
			return errLost()
		}
	}
	r, ok := s.resources[ref.String()]
	if !ok {
		s.log("Delete", ref, idemKey, nil, "ok", "ALREADY_GONE", "")
		s.idem[idemKey] = idemRecord{ref: ref, token: "deleted"}
		return nil
	}
	_ = r
	delete(s.resources, ref.String())
	delete(s.lostCreates, ref.String())
	s.idem[idemKey] = idemRecord{ref: ref, token: "deleted"}
	s.log("Delete", ref, idemKey, nil, "ok", "DELETED", "")
	return nil
}
