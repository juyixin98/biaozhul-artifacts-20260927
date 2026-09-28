// Package cluster implements the readiness-observation adapter against a
// LOCAL, synthetic replica group. There are no production accounts and no
// real business data: the "cluster" is an in-memory, mutex-protected fixture
// that tests and the demo seed programmatically drive through failure
// injection (Fail), voluntary moves (MarkDraining/MarkGone) and selector
// changes (Relabel).
//
// The adapter's contract is deliberately small and observation-shaped so a
// real adapter (k8s endpoints, a VM inventory, ...) could replace it without
// touching the coordinator: Observe returns a freshness-bounded snapshot of
// per-instance states, and it never reports anything it did not actually
// observe.
package cluster

import (
	"sync"
	"time"

	"evictor/internal/domain"
)

// SyntheticCluster is the local fixture standing in for a real replica
// placement backend. All methods are safe for concurrent use: the
// coordinator's control loop and the request handlers (and tests injecting
// failures) hit it concurrently.
type SyntheticCluster struct {
	mu        sync.Mutex
	instances map[string]*fixtureInstance
	// observedVersion stamps each mutation; Observe snapshots under the same
	// lock so callers never see a torn read.
	clock func() time.Time
}

type fixtureInstance struct {
	id         string
	group      string
	labels     map[string]string
	selVersion int64
	state      domain.InstanceState
	// failedAt records when the involuntary transition happened, so tests can
	// assert the coordinator did not mistake it for a budget decision.
	failedAt time.Time
}

func NewSyntheticCluster(now func() time.Time) *SyntheticCluster {
	if now == nil {
		now = time.Now
	}
	return &SyntheticCluster{instances: map[string]*fixtureInstance{}, clock: now}
}

// Add registers a ready replica.
func (c *SyntheticCluster) Add(id, group string, labels map[string]string, selVersion int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	cp := make(map[string]string, len(labels))
	for k, v := range labels {
		cp[k] = v
	}
	c.instances[id] = &fixtureInstance{
		id: id, group: group, labels: cp, selVersion: selVersion,
		state: domain.StateReady,
	}
}

// MarkDraining simulates the placement backend accepting a voluntary move.
func (c *SyntheticCluster) MarkDraining(id string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	in, ok := c.instances[id]
	if !ok || in.state != domain.StateReady {
		return false
	}
	in.state = domain.StateDraining
	return true
}

// MarkGone simulates a completed voluntary move.
func (c *SyntheticCluster) MarkGone(id string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	in, ok := c.instances[id]
	if !ok || in.state != domain.StateDraining {
		return false
	}
	in.state = domain.StateGone
	return true
}

// Fail injects an INVOLUNTARY failure. A failed instance stays failed —
// unlike a voluntary move there is no "drain" phase and nothing the
// coordinator approves can cause or prevent it.
func (c *SyntheticCluster) Fail(id string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	in, ok := c.instances[id]
	if !ok || in.state == domain.StateFailed {
		return false
	}
	in.state = domain.StateFailed
	in.failedAt = c.clock()
	return true
}

// Relabel simulates a pod-template / placement change: every instance of the
// group receives new labels and moves to a new selector version. Already
// failed instances keep their terminal state but still take the new version,
// matching how a rolled-out spec eventually reaches even dead members.
func (c *SyntheticCluster) Relabel(group string, newLabels map[string]string, newVersion int64) int {
	c.mu.Lock()
	defer c.mu.Unlock()
	n := 0
	for _, in := range c.instances {
		if in.group != group {
			continue
		}
		cp := make(map[string]string, len(newLabels))
		for k, v := range newLabels {
			cp[k] = v
		}
		in.labels = cp
		in.selVersion = newVersion
		n++
	}
	return n
}

// State returns one instance's current state (test/diagnostic helper).
func (c *SyntheticCluster) State(id string) (domain.InstanceState, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	in, ok := c.instances[id]
	if !ok {
		return "", false
	}
	return in.state, true
}

// Observe is the adapter method the coordinator calls. It returns a coherent
// point-in-time snapshot. maxAge is the observation's freshness contract:
// consumers must treat At older than maxAge as undecidable.
func (c *SyntheticCluster) Observe(group string, maxAge time.Duration) domain.Observation {
	c.mu.Lock()
	defer c.mu.Unlock()
	obs := domain.Observation{Group: group, At: c.clock(), MaxAge: maxAge}
	for _, in := range c.instances {
		if in.group != group {
			continue
		}
		labels := make(map[string]string, len(in.labels))
		for k, v := range in.labels {
			labels[k] = v
		}
		obs.Instances = append(obs.Instances, domain.Instance{
			ID:         in.id,
			Group:      in.group,
			Labels:     labels,
			State:      in.state,
			SelVersion: in.selVersion,
		})
	}
	return obs
}
