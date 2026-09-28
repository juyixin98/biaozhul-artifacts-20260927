// Simulator is a local, fully synthetic ProcessManager. Per (workload,
// revision) a Behavior fixture decides whether starts succeed, when processes
// become ready, whether readiness jitters and whether/when processes exit.
//
// Process rows and behaviors are persisted in the same SQLite store so a
// controller restart (or a whole-binary restart) reattaches to the exact
// simulated process table. Time is logical: the controller drives it with
// SetTick, which makes every fault fixture reproducible.

package adapter

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"sync"

	"rollctl/internal/store"
)

// Behavior modes.
const (
	ModeNormal          = "normal"
	ModeStartRejected   = "start_rejected"
	ModeCrashAfterStart = "crash_after_start"
	ModeFlap            = "flap"
	ModeReadyLost       = "ready_lost"
)

// Behavior is the fixture for one revision of one workload.
type Behavior struct {
	// Mode selects the failure shape; ModeNormal is the healthy default.
	Mode string `json:"mode"`
	// StartDelayTicks: process is "starting" (running but not ready is
	// reported separately; with delay > 0 it is not even running yet) for this
	// many ticks after Start.
	StartDelayTicks int64 `json:"startDelayTicks,omitempty"`
	// ReadyDelayTicks: after StartDelayTicks, the process runs but reports
	// not-ready for this many ticks.
	ReadyDelayTicks int64 `json:"readyDelayTicks,omitempty"`
	// FlapReadyTicks / FlapDownTicks for ModeFlap: alternating readiness.
	FlapReadyTicks int64 `json:"flapReadyTicks,omitempty"`
	FlapDownTicks  int64 `json:"flapDownTicks,omitempty"`
	// ReadyHoldTicks for ModeReadyLost: ready for this many ticks, then
	// permanently not-ready (but still running).
	ReadyHoldTicks int64 `json:"readyHoldTicks,omitempty"`
	// ExitAfterTicks for ModeCrashAfterStart: running ticks before the process
	// exits.
	ExitAfterTicks int64 `json:"exitAfterTicks,omitempty"`
}

// DefaultBehavior is the healthy fixture used when no behavior was configured.
func DefaultBehavior() Behavior {
	return Behavior{Mode: ModeNormal, ReadyDelayTicks: 1}
}

func (b Behavior) validate() error {
	switch b.Mode {
	case ModeNormal, ModeStartRejected, ModeCrashAfterStart, ModeFlap, ModeReadyLost:
	default:
		return fmt.Errorf("unknown fixture mode %q", b.Mode)
	}
	if b.Mode == ModeFlap && (b.FlapReadyTicks <= 0 || b.FlapDownTicks <= 0) {
		return errors.New("flap mode requires positive flapReadyTicks and flapDownTicks")
	}
	return nil
}

// procEntry is the in-memory view of one simulated process.
type procEntry struct {
	procID      string
	workload    string
	revision    string
	releaseID   string
	startedTick int64
	snapshot    Behavior // fixture frozen at start time
}

// Simulator implements ProcessManager against synthetic fixtures.
type Simulator struct {
	mu       sync.Mutex
	st       *store.Store
	procs    map[string]*procEntry
	tick     int64
	capacity int
}

// NewSimulator creates (or reattaches to) a simulator. Existing process rows
// in the store are loaded so restarts do not lose the simulated fleet.
func NewSimulator(ctx context.Context, st *store.Store, capacity int) (*Simulator, error) {
	s := &Simulator{st: st, procs: map[string]*procEntry{}, capacity: capacity}
	if v, _, err := st.GetSimBehavior(ctx, "__meta__", "__capacity__"); err == nil && v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			s.capacity = n
		}
	}
	rows, err := st.ListSimProcesses(ctx)
	if err != nil {
		return nil, err
	}
	for _, r := range rows {
		if r.Exited {
			// Exited rows are kept until the controller reaps them, so that the
			// exit is observable after a restart.
		}
		b := DefaultBehavior()
		if r.FailKind != "" {
			_ = json.Unmarshal([]byte(r.FailKind), &b)
		}
		s.procs[r.ProcID] = &procEntry{
			procID:      r.ProcID,
			workload:    r.Workload,
			revision:    r.Revision,
			releaseID:   r.ReleaseID,
			startedTick: r.StartedTick,
			snapshot:    b,
		}
	}
	return s, nil
}

// SetTick advances the logical clock (TickDriven).
func (s *Simulator) SetTick(tick int64) {
	s.mu.Lock()
	s.tick = tick
	s.mu.Unlock()
}

// Capacity reports the current process-slot limit.
func (s *Simulator) Capacity() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.capacity
}

// SetCapacity changes the process-slot limit and persists it.
func (s *Simulator) SetCapacity(ctx context.Context, n int) error {
	s.mu.Lock()
	s.capacity = n
	s.mu.Unlock()
	return s.st.PutSimBehavior(ctx, "__meta__", "__capacity__", strconv.Itoa(n))
}

// SetBehavior installs the fixture for one revision of a workload.
func (s *Simulator) SetBehavior(ctx context.Context, workload, revision string, b Behavior) error {
	if err := b.validate(); err != nil {
		return err
	}
	raw, _ := json.Marshal(b)
	return s.st.PutSimBehavior(ctx, workload, revision, string(raw))
}

func newID() string {
	var b [8]byte
	_, _ = rand.Read(b[:])
	return "p-" + hex.EncodeToString(b[:])
}

func (s *Simulator) behaviorFor(ctx context.Context, workload, revision string) (Behavior, error) {
	raw, ok, err := s.st.GetSimBehavior(ctx, workload, revision)
	if err != nil {
		return Behavior{}, err
	}
	if !ok {
		return DefaultBehavior(), nil
	}
	b := Behavior{}
	if err := json.Unmarshal([]byte(raw), &b); err != nil {
		return Behavior{}, fmt.Errorf("behavior fixture for %s/%s: %w", workload, revision, err)
	}
	if err := b.validate(); err != nil {
		return Behavior{}, err
	}
	return b, nil
}

// Start implements ProcessManager.
func (s *Simulator) Start(ctx context.Context, meta StartMeta) (string, error) {
	b, err := s.behaviorFor(ctx, meta.Workload, meta.Revision)
	if err != nil {
		return "", err
	}
	if b.Mode == ModeStartRejected {
		// The manager refuses to create the process at all.
		return "", fmt.Errorf("%w: fixture mode start_rejected for %s/%s", ErrStartRejected, meta.Workload, meta.Revision)
	}

	s.mu.Lock()
	live := 0
	for _, p := range s.procs {
		if !s.isExitedLocked(p) {
			live++
		}
	}
	if live >= s.capacity {
		s.mu.Unlock()
		return "", fmt.Errorf("%w: %d slots in use, limit %d (%s/%s)", ErrCapacity, live, s.capacity, meta.Workload, meta.Revision)
	}
	id := newID()
	p := &procEntry{procID: id, workload: meta.Workload, revision: meta.Revision, releaseID: meta.ReleaseID, startedTick: s.tick, snapshot: b}
	s.procs[id] = p
	tick := s.tick
	s.mu.Unlock()

	raw, _ := json.Marshal(b)
	if err := s.st.PutSimProcess(ctx, store.SimProcRow{
		ProcID: id, Workload: meta.Workload, Revision: meta.Revision, ReleaseID: meta.ReleaseID,
		StartedTick: tick, LastTick: tick, FailKind: string(raw),
	}); err != nil {
		return "", err
	}
	return id, nil
}

// isExitedLocked computes whether a process has already exited by current tick.
// Requires s.mu held.
func (s *Simulator) isExitedLocked(p *procEntry) bool {
	if p.snapshot.Mode != ModeCrashAfterStart {
		return false
	}
	age := s.tick - p.startedTick
	return age >= p.snapshot.StartDelayTicks+p.snapshot.ExitAfterTicks
}

// Observe implements ProcessManager.
func (s *Simulator) Observe(ctx context.Context, procID string) (ProcStatus, error) {
	s.mu.Lock()
	p, ok := s.procs[procID]
	if !ok {
		s.mu.Unlock()
		return ProcStatus{}, ProcGone
	}
	tick := s.tick
	b := p.snapshot
	age := tick - p.startedTick

	st := ProcStatus{ProcID: procID, Running: true}

	if b.Mode == ModeCrashAfterStart && age >= b.StartDelayTicks+b.ExitAfterTicks {
		st.Running = false
		st.Reason = ReasonExited
		exitTick := p.startedTick + b.StartDelayTicks + b.ExitAfterTicks
		s.mu.Unlock()
		_ = s.persistExit(ctx, p, exitTick)
		return st, nil
	}

	if age < b.StartDelayTicks {
		st.Running = false
		st.Ready = false
		st.Reason = "starting"
		s.mu.Unlock()
		return st, nil
	}
	st.Running = true
	ramp := b.StartDelayTicks + b.ReadyDelayTicks
	switch b.Mode {
	case ModeFlap:
		if age < ramp {
			st.Ready = false
			st.Reason = "starting"
			break
		}
		cycle := b.FlapReadyTicks + b.FlapDownTicks
		pos := (age - ramp) % cycle
		if pos < b.FlapReadyTicks {
			st.Ready = true
		} else {
			st.Ready = false
			st.Reason = "readiness jitter (fixture)"
		}
	case ModeReadyLost:
		if age < ramp {
			st.Ready = false
			st.Reason = "starting"
		} else if age < ramp+b.ReadyHoldTicks {
			st.Ready = true
		} else {
			st.Ready = false
			st.Reason = "readiness lost (fixture)"
		}
	default: // ModeNormal
		st.Ready = age >= ramp
		if !st.Ready {
			st.Reason = "starting"
		}
	}
	lastTick := tick
	s.mu.Unlock()
	_ = s.st.PutSimProcess(ctx, store.SimProcRow{
		ProcID: p.procID, Workload: p.workload, Revision: p.revision, ReleaseID: p.releaseID,
		StartedTick: p.startedTick, LastTick: lastTick,
		Exited: !st.Running && st.Reason == ReasonExited,
		ExitTick: func() int64 {
			if st.Reason == ReasonExited {
				return p.startedTick + b.StartDelayTicks + b.ExitAfterTicks
			}
			return 0
		}(),
		FailKind: func() string { raw, _ := json.Marshal(b); return string(raw) }(),
	})
	return st, nil
}

func (s *Simulator) persistExit(ctx context.Context, p *procEntry, exitTick int64) error {
	raw, _ := json.Marshal(p.snapshot)
	return s.st.PutSimProcess(ctx, store.SimProcRow{
		ProcID: p.procID, Workload: p.workload, Revision: p.revision, ReleaseID: p.releaseID,
		StartedTick: p.startedTick, LastTick: exitTick, Exited: true, ExitTick: exitTick, FailKind: string(raw),
	})
}

// Terminate implements ProcessManager: release the slot and delete the row.
func (s *Simulator) Terminate(ctx context.Context, procID string) error {
	s.mu.Lock()
	_, ok := s.procs[procID]
	if ok {
		delete(s.procs, procID)
	}
	s.mu.Unlock()
	if !ok {
		return ProcGone
	}
	return s.st.DeleteSimProcess(ctx, procID)
}

// LiveCount reports how many non-exited process slots are currently occupied
// (across all workloads). This backs the global capacity fixture.
func (s *Simulator) LiveCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	n := 0
	for _, p := range s.procs {
		if !s.isExitedLocked(p) {
			n++
		}
	}
	return n
}
