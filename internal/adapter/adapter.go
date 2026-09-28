// Package adapter models the downstream "actuator" the reconciliation loop
// drives. A real deployment would implement e.g. a Kubernetes API client
// here; for local execution we provide a file adapter and a fault adapter
// used by failure-injection tests.
package adapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// DesiredState is one reconcile work item.
type DesiredState struct {
	Kind     string          `json:"kind"`
	Name     string          `json:"name"`
	Revision int64           `json:"revision"`
	Live     json.RawMessage `json:"live"`
}

// Outcome is what an Apply call reports.
type Outcome struct {
	// Synced true means the downstream now reflects DesiredState exactly.
	Synced bool
	// Retryable failures are retried with backoff; non-retryable failures
	// mark the resource failed without further attempts.
	Retryable bool
	Reason    string
	Message   string
}

// Adapter is the actuator contract.
type Adapter interface {
	Apply(ctx context.Context, d DesiredState) Outcome
	Name() string
}

// ErrFault is the sentinel injected by FaultAdapter on demand.
var ErrFault = errors.New("injected adapter fault")

// FileAdapter materializes every resource as a JSON file under RootDir.
// Writing atomically (tmp + rename) makes a crashed reconcile observable as
// either the old or the new content, never a truncated file.
type FileAdapter struct {
	RootDir string

	mu sync.Mutex
}

func (f *FileAdapter) Name() string { return "file:" + f.RootDir }

func (f *FileAdapter) Apply(ctx context.Context, d DesiredState) Outcome {
	f.mu.Lock()
	defer f.mu.Unlock()
	dir := filepath.Join(f.RootDir, d.Kind)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return Outcome{Retryable: true, Reason: "mkdir_failed", Message: err.Error()}
	}
	final := filepath.Join(dir, d.Name+".json")
	tmp, err := os.CreateTemp(dir, ".tmp-*")
	if err != nil {
		return Outcome{Retryable: true, Reason: "tmp_failed", Message: err.Error()}
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	enc := json.NewEncoder(tmp)
	enc.SetIndent("", "  ")
	payload := map[string]any{
		"kind": d.Kind, "name": d.Name, "revision": d.Revision, "live": json.RawMessage(d.Live),
	}
	if err := enc.Encode(payload); err != nil {
		tmp.Close()
		return Outcome{Retryable: true, Reason: "write_failed", Message: err.Error()}
	}
	if err := tmp.Close(); err != nil {
		return Outcome{Retryable: true, Reason: "write_failed", Message: err.Error()}
	}
	if err := os.Rename(tmpName, final); err != nil {
		return Outcome{Retryable: true, Reason: "rename_failed", Message: err.Error()}
	}
	return Outcome{Synced: true}
}

// ReadBack is used by tests to verify the file adapter wrote the expected JSON.
func (f *FileAdapter) ReadBack(kind, name string) ([]byte, error) {
	return os.ReadFile(filepath.Join(f.RootDir, kind, name+".json"))
}

// FaultConfig drives the FaultAdapter.
type FaultConfig struct {
	// FailResources maps "kind/name" -> number of Apply calls that fail
	// before the adapter starts succeeding.
	FailN map[string]int
	// Permanent marks failures as non-retryable.
	Permanent bool
	// Reason/Message annotate the failure.
	Reason  string
	Message string
}

// FaultAdapter wraps an inner adapter and fails the first FailN[key] applies
// for each resource key. It records every call so tests can assert retries.
type FaultAdapter struct {
	Inner  Adapter
	Cfg    FaultConfig
	Record []DesiredState

	mu sync.Mutex
}

func (a *FaultAdapter) Name() string { return "fault(" + a.Inner.Name() + ")" }

func (a *FaultAdapter) Apply(ctx context.Context, d DesiredState) Outcome {
	a.mu.Lock()
	key := d.Kind + "/" + d.Name
	remaining := a.Cfg.FailN[key]
	if remaining > 0 {
		a.Cfg.FailN[key] = remaining - 1
		reason := a.Cfg.Reason
		if reason == "" {
			reason = "injected_fault"
		}
		msg := a.Cfg.Message
		if msg == "" {
			msg = fmt.Sprintf("%s (%d failures left before recovery)", ErrFault, remaining-1)
		}
		a.Record = append(a.Record, d)
		a.mu.Unlock()
		return Outcome{Retryable: !a.Cfg.Permanent, Reason: reason, Message: msg}
	}
	a.Record = append(a.Record, d)
	a.mu.Unlock()
	return a.Inner.Apply(ctx, d)
}

// Calls returns a snapshot of observed Apply calls (deep-copied live bytes).
func (a *FaultAdapter) Calls() []DesiredState {
	a.mu.Lock()
	defer a.mu.Unlock()
	out := make([]DesiredState, len(a.Record))
	copy(out, a.Record)
	return out
}

// UnavailableAdapter models an actuator that cannot be reached at all.
type UnavailableAdapter struct{}

func (UnavailableAdapter) Name() string { return "unavailable" }

func (UnavailableAdapter) Apply(context.Context, DesiredState) Outcome {
	return Outcome{Retryable: true, Reason: "adapter_unavailable", Message: "actuator is offline"}
}
