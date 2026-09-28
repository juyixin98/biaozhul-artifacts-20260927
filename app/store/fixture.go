package store

import (
	"database/sql"
	"fmt"
	"sync"
	"time"

	"replicactl/core/model"
)

// DurableFixture is the local synthetic workload backed by SQLite so a service
// restart restores fleet size, the latest per-instance reports and the demand
// signal. It implements controller.Fleet and controller.MetricSource and
// exposes explicit fault-injection hooks for the failure tests.
type DurableFixture struct {
	db *sql.DB
	mu sync.Mutex

	maxSlots int
	// Clock is overridable for deterministic tests; defaults to wall clock.
	Clock func() int64

	// Fault injection hooks — nil means healthy.
	FailSetReplicas error
	FailCurrentRead error
	FailMetricRead  error
	FailDemandRead  error
}

// NewDurableFixture opens the durable fixture. replicas persists in
// fleet_state; the initial call seeds it only when absent.
func NewDurableFixture(db *sql.DB, cfg model.Config) (*DurableFixture, error) {
	if _, err := db.Exec(
		`INSERT OR IGNORE INTO fleet_state(key,value) VALUES('replicas',?)`,
		cfg.MinReplicas); err != nil {
		return nil, fmt.Errorf("seed fleet state: %w", err)
	}
	return &DurableFixture{
		db:       db,
		maxSlots: cfg.MaxReplicas,
		Clock:    func() int64 { return time.Now().Unix() },
	}, nil
}

// SeedReplicas overwrites the persisted fleet size directly. It exists for
// scenario fixtures that start a run with a non-zero fleet; it deliberately
// bypasses the apply fault hook (seeding is test setup, not a controller act).
func (f *DurableFixture) SeedReplicas(n int) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if n < 0 || n > f.maxSlots {
		return fmt.Errorf("seed: requested %d replicas outside [0,%d]", n, f.maxSlots)
	}
	if _, err := f.db.Exec(`UPDATE fleet_state SET value=? WHERE key='replicas'`, n); err != nil {
		return fmt.Errorf("seed fleet size: %w", err)
	}
	return nil
}

// CurrentReplicas implements controller.Fleet.
func (f *DurableFixture) CurrentReplicas() (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.FailCurrentRead != nil {
		return 0, f.FailCurrentRead
	}
	var n int
	if err := f.db.QueryRow(`SELECT value FROM fleet_state WHERE key='replicas'`).Scan(&n); err != nil {
		return 0, fmt.Errorf("read fleet size: %w", err)
	}
	return n, nil
}

// SetReplicas implements controller.Fleet and persists the change inside the
// controller tick; the new value is durable before the decision is written.
func (f *DurableFixture) SetReplicas(n int) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.FailSetReplicas != nil {
		return f.FailSetReplicas
	}
	if n < 0 || n > f.maxSlots {
		return fmt.Errorf("adapter: requested %d replicas outside [0,%d]", n, f.maxSlots)
	}
	if _, err := f.db.Exec(`UPDATE fleet_state SET value=? WHERE key='replicas'`, n); err != nil {
		return fmt.Errorf("persist fleet size: %w", err)
	}
	return nil
}

func (f *DurableFixture) now() int64 { return f.Clock() }

// SubmitSample persists the latest report for an active instance. A report
// from an inactive (scaled-down) slot is rejected, as is a future timestamp.
func (f *DurableFixture) SubmitSample(s model.LoadSample) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if err := model.ValidateSample(s, f.now()); err != nil {
		return err
	}
	var replicas int
	if err := f.db.QueryRow(`SELECT value FROM fleet_state WHERE key='replicas'`).Scan(&replicas); err != nil {
		return fmt.Errorf("read fleet size: %w", err)
	}
	idx := slotIndex(s.InstanceID)
	if idx < 0 || idx >= replicas {
		return fmt.Errorf("instance %q is not in the active fleet of %d", s.InstanceID, replicas)
	}
	if _, err := f.db.Exec(`
		INSERT INTO samples(instance_id,load,reported_at) VALUES(?,?,?)
		ON CONFLICT(instance_id) DO UPDATE SET load=excluded.load, reported_at=excluded.reported_at`,
		s.InstanceID, s.Load, s.ReportedAt); err != nil {
		return fmt.Errorf("persist sample: %w", err)
	}
	return nil
}

// PostDemand persists the out-of-band zero-replica demand signal.
func (f *DurableFixture) PostDemand(present bool, at int64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	v := 0
	if present {
		v = 1
	}
	if _, err := f.db.Exec(`
		INSERT INTO demand(id,present,reported_at,has_signal) VALUES(1,?,?,1)
		ON CONFLICT(id) DO UPDATE SET present=excluded.present,
			reported_at=excluded.reported_at, has_signal=1`, v, at); err != nil {
		return fmt.Errorf("persist demand: %w", err)
	}
	return nil
}

// LatestSamples implements controller.MetricSource: one row per active slot.
func (f *DurableFixture) LatestSamples(now int64) ([]model.Sample, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.FailMetricRead != nil {
		return nil, f.FailMetricRead
	}
	var replicas int
	if err := f.db.QueryRow(`SELECT value FROM fleet_state WHERE key='replicas'`).Scan(&replicas); err != nil {
		return nil, fmt.Errorf("read fleet size: %w", err)
	}
	reported := map[string]model.LoadSample{}
	rows, err := f.db.Query(`SELECT instance_id, load, reported_at FROM samples`)
	if err != nil {
		return nil, fmt.Errorf("read samples: %w", err)
	}
	for rows.Next() {
		var s model.LoadSample
		if err := rows.Scan(&s.InstanceID, &s.Load, &s.ReportedAt); err != nil {
			rows.Close()
			return nil, err
		}
		reported[s.InstanceID] = s
	}
	rows.Close()

	out := make([]model.Sample, 0, replicas)
	for i := 1; i <= replicas; i++ {
		id := fmt.Sprintf("instance-%03d", i)
		if s, ok := reported[id]; ok {
			out = append(out, model.Sample{InstanceID: id, Load: s.Load, ReportedAt: s.ReportedAt})
			continue
		}
		out = append(out, model.Sample{InstanceID: id, Missing: true})
	}
	return out, nil
}

// LatestDemand implements controller.MetricSource.
func (f *DurableFixture) LatestDemand(now int64) (model.DemandSignal, bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.FailDemandRead != nil {
		return model.DemandSignal{}, false, f.FailDemandRead
	}
	var present, has int
	var at int64
	err := f.db.QueryRow(`SELECT present, reported_at, has_signal FROM demand WHERE id=1`).Scan(&present, &at, &has)
	if err == sql.ErrNoRows || has == 0 {
		return model.DemandSignal{}, false, nil
	}
	if err != nil {
		return model.DemandSignal{}, false, fmt.Errorf("read demand: %w", err)
	}
	return model.DemandSignal{Present: present == 1, ReportedAt: at}, true, nil
}

// ClearFaults resets every injection hook.
func (f *DurableFixture) ClearFaults() {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.FailSetReplicas = nil
	f.FailCurrentRead = nil
	f.FailMetricRead = nil
	f.FailDemandRead = nil
}

func slotIndex(id string) int {
	var n int
	if _, err := fmt.Sscanf(id, "instance-%d", &n); err != nil {
		return -1
	}
	return n - 1
}
