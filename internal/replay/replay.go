// Package replay drives the NAT model from synthetic trace fixtures and serves
// as the integration seam between state storage, the engine and the replay
// interface (CLI/HTTP).
package replay

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"natlab/internal/config"
	"natlab/internal/model"
	"natlab/internal/nat"
	"natlab/internal/storage"
)

// Trace is one replayable scenario.
type Trace struct {
	// RunID is fixed for a fixture so reruns are comparable. Required.
	RunID string `json:"run_id"`
	Name  string `json:"name"`
	// Config optionally overrides selected fields; nil means engine defaults.
	Config *config.Config `json:"config,omitempty"`
	// StartAt is the trace-relative epoch. Packet times may be absolute or use
	// observed_at relative to it; fixtures use absolute RFC3339 times.
	StartAt time.Time      `json:"start_at"`
	Packets []model.Packet `json:"packets"`
}

// LoadTrace reads a JSON (single object) or .jsonl (one packet per line) trace.
func LoadTrace(path string) (*Trace, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("replay: read trace %q: %w", path, err)
	}
	return ParseTrace(b, path)
}

// ParseTrace accepts the full-object JSON form, or JSONL where the first line
// is the trace header and subsequent lines are packets.
func ParseTrace(b []byte, name string) (*Trace, error) {
	var t Trace
	if err := json.Unmarshal(b, &t); err == nil && t.RunID != "" {
		if err := t.Check(); err != nil {
			return nil, err
		}
		return &t, nil
	}
	// JSONL form.
	type header struct {
		RunID   string         `json:"run_id"`
		Name    string         `json:"name"`
		Config  *config.Config `json:"config,omitempty"`
		StartAt time.Time      `json:"start_at"`
	}
	var h header
	var pkts []model.Packet
	dec := json.NewDecoder(bytes.NewReader(b))
	first := true
	for dec.More() {
		raw := json.RawMessage{}
		if err := dec.Decode(&raw); err != nil {
			return nil, fmt.Errorf("replay: parse jsonl %q: %w", name, err)
		}
		if first {
			if err := json.Unmarshal(raw, &h); err != nil {
				return nil, fmt.Errorf("replay: jsonl header %q: %w", name, err)
			}
			first = false
			continue
		}
		var p model.Packet
		if err := json.Unmarshal(raw, &p); err != nil {
			return nil, fmt.Errorf("replay: jsonl packet %q: %w", name, err)
		}
		pkts = append(pkts, p)
	}
	if h.RunID == "" {
		return nil, fmt.Errorf("replay: trace %q missing run_id", name)
	}
	t = Trace{RunID: h.RunID, Name: h.Name, Config: h.Config, StartAt: h.StartAt, Packets: pkts}
	if err := t.Check(); err != nil {
		return nil, err
	}
	return &t, nil
}

// Check validates the fixture contract before execution.
func (t *Trace) Check() error {
	if t.RunID == "" {
		return fmt.Errorf("trace run_id is required")
	}
	seen := map[int64]bool{}
	for i := range t.Packets {
		p := &t.Packets[i]
		if p.Seq == 0 {
			p.Seq = int64(i) + 1
		}
		if seen[p.Seq] {
			return fmt.Errorf("trace %s: duplicate seq %d", t.RunID, p.Seq)
		}
		seen[p.Seq] = true
	}
	return nil
}

// Report is the run summary, serializable into test logs.
type Report struct {
	RunID      string              `json:"run_id"`
	Name       string              `json:"name"`
	StartedAt  time.Time           `json:"started_at"`
	FinishedAt time.Time           `json:"finished_at"`
	Stats      model.Stats         `json:"stats"`
	Decisions  []model.Decision    `json:"decisions"`
	FinalMap   []model.MappingView `json:"final_mappings"`
}

// Runner executes traces against a fresh (or restored) engine.
type Runner struct {
	baseCfg config.Config
	store   storage.Store
}

// NewRunner builds a runner. Mappings are persisted through st.
func NewRunner(cfg config.Config, st storage.Store) *Runner {
	return &Runner{baseCfg: cfg, store: st}
}

// Run evaluates every packet of a trace in order and returns the complete
// decision log (including lifecycle_expired events).
func (r *Runner) Run(ctx context.Context, t *Trace) (*Report, error) {
	cfg := r.baseCfg
	if t.Config != nil {
		cfg = *t.Config
	}
	eng, err := nat.New(t.RunID, cfg, r.store)
	if err != nil {
		return nil, err
	}
	if err := restoreEngine(ctx, eng, cfg, r.store, t.RunID); err != nil {
		return nil, err
	}

	rep := &Report{RunID: t.RunID, Name: t.Name, StartedAt: time.Now().UTC()}
	for i := range t.Packets {
		if _, perr := eng.Process(ctx, t.Packets[i]); perr != nil {
			// Compute failure is already decision-logged; stop the run so the
			// test sees a bounded, explainable result.
			decisions, _ := r.store.ListDecisions(ctx, t.RunID, 0, 0)
			rep.Decisions = decisions
			rep.Stats = eng.Stats()
			rep.FinishedAt = time.Now().UTC()
			return rep, perr
		}
	}
	rep.FinishedAt = time.Now().UTC()
	rep.Stats = eng.Stats()
	rep.FinalMap = eng.Snapshots()
	rep.Decisions, err = r.store.ListDecisions(ctx, t.RunID, 0, 0)
	if err != nil {
		return nil, err
	}
	return rep, nil
}

// restoreEngine rebuilds the port allocator bookkeeping from persisted
// mappings so a run can continue after reopening its SQLite file.
func restoreEngine(ctx context.Context, eng *nat.Engine, cfg config.Config,
	st storage.Store, runID string) error {
	// Engine exposes Restore via a narrow interface to keep package imports
	// acyclic (storage is a lower layer than nat).
	return eng.Restore(ctx, st, runID)
}
