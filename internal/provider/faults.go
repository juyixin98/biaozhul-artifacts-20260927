package provider

import (
	"sync"

	"infraplanner/internal/model"
)

// Fault kinds that can be injected into the simulated provider. Each fault
// arms exactly one (or N) matching operation and is then consumed.
const (
	// FaultCreateExhaust: create fails with resource exhaustion.
	FaultCreateExhaust = "create_exhausted"
	// FaultCreateCommitLost: the create commits server-side but the response
	// is lost. The next Observe shows the resource. This is the ambiguous
	// "response lost" case the reconciler must recover from without
	// creating twice.
	FaultCreateCommitLost = "create_commit_response_lost"
	// FaultCreateUnknown: create may or may not have committed; Observe is
	// also made to show nothing (simulates a truly ambiguous outcome that
	// only resolves on re-observe later).
	FaultCreateUnknown = "create_unknown"
	// FaultUpdateTransient / FaultDeleteTransient: transient compute errors
	// that succeed on retry.
	FaultUpdateTransient = "update_transient"
	FaultDeleteTransient = "delete_transient"
	// FaultDeleteNotFound: delete target has already vanished (idempotent).
	FaultDeleteNotFound = "delete_not_found"
	// FaultCreateHang: create blocks until the context is cancelled, used
	// to simulate a process killed mid-operation (interruption).
	FaultCreateHang = "create_hang"
	// FaultUpdateHang / FaultDeleteHang are the interruption faults for the
	// other two mutating operations.
	FaultUpdateHang = "update_hang"
	FaultDeleteHang = "delete_hang"
)

// Fault is one armed fault.
type Fault struct {
	ID        string
	Kind      string
	Target    model.Key // match exact logical key; zero Kind = any
	Remaining int       // number of times to fire before being consumed (>=1)
}

// faultBook holds armed faults with a mutex.
type faultBook struct {
	mu    sync.Mutex
	armed []*Fault
}

func newFaultBook() *faultBook { return &faultBook{} }

func (b *faultBook) arm(f Fault) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if f.Remaining < 1 {
		f.Remaining = 1
	}
	b.armed = append(b.armed, &f)
}

// take returns and consumes one matching fault for the given op/key, or nil.
func (b *faultBook) take(kind string, k model.Key) *Fault {
	b.mu.Lock()
	defer b.mu.Unlock()
	for i, f := range b.armed {
		if f.Kind != kind {
			continue
		}
		if f.Target.Kind != "" && f.Target != k {
			continue
		}
		f.Remaining--
		if f.Remaining <= 0 {
			b.armed = append(b.armed[:i], b.armed[i+1:]...)
		}
		return f
	}
	return nil
}

// list is a snapshot for diagnostics.
func (b *faultBook) list() []Fault {
	b.mu.Lock()
	defer b.mu.Unlock()
	out := make([]Fault, 0, len(b.armed))
	for _, f := range b.armed {
		out = append(out, *f)
	}
	return out
}

func (b *faultBook) clear() {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.armed = nil
}
