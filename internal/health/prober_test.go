package health_test

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"flowrouter/internal/config"
	"flowrouter/internal/health"
	"flowrouter/internal/router"
)

func members() []config.Member {
	return []config.Member{
		{ID: "a", Address: "127.0.0.1:1", Weight: 1},
		{ID: "b", Address: "127.0.0.1:2", Weight: 1},
	}
}

// fakeDialer returns per-member results from a script under a mutex so tests
// can flip behavior while the prober runs.
type fakeDialer struct {
	mu      sync.Mutex
	failing map[string]bool
	calls   map[string]int
}

func (f *fakeDialer) fail(id string, v bool) {
	f.mu.Lock()
	f.failing[id] = v
	f.mu.Unlock()
}

func (f *fakeDialer) dial(_ context.Context, addr string, _ time.Duration) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls[addr]++
	if f.failing[addr] {
		return errors.New("connection refused (synthetic)")
	}
	return nil
}

func newProber(t *testing.T, rt *router.Router, fd *fakeDialer, failures int) *health.Prober {
	t.Helper()
	p, err := health.New(rt, 5*time.Millisecond, 20*time.Millisecond, failures, nil)
	if err != nil {
		t.Fatal(err)
	}
	p.SetDialer(fd.dial)
	return p
}

func TestProbeMarksDownAfterThreshold(t *testing.T) {
	rt := router.New(16, 0)
	if _, _, err := rt.Load(members(), nil); err != nil {
		t.Fatal(err)
	}
	fd := &fakeDialer{failing: map[string]bool{}, calls: map[string]int{}}
	p := newProber(t, rt, fd, 2)
	fd.fail("127.0.0.1:1", true)

	// sweep 1: below threshold, still up
	p.RunOnce()
	if !rt.Current().Members[0].Up {
		t.Fatal("a down after only one failure")
	}
	// sweep 2: threshold reached -> excluded immediately
	p.RunOnce()
	if rt.Current().Members[0].Up || rt.Current().Members[0].OnRing {
		t.Fatal("a not excluded after reaching failure threshold")
	}
	if rt.Current().Members[0].ID != "a" {
		t.Fatalf("member ordering: %s", rt.Current().Members[0].ID)
	}
	// b must still be up
	if !rt.Current().Members[1].Up {
		t.Fatal("b should remain up")
	}
	if n := p.Strikes()["a"]; n < 2 {
		t.Fatalf("strikes for a = %d, want >= 2", n)
	}
}

func TestBackgroundProberEventuallyMarksDown(t *testing.T) {
	rt := router.New(16, 0)
	if _, _, err := rt.Load(members(), nil); err != nil {
		t.Fatal(err)
	}
	fd := &fakeDialer{failing: map[string]bool{}, calls: map[string]int{}}
	p := newProber(t, rt, fd, 2)
	fd.fail("127.0.0.1:1", true)
	p.Start()
	defer p.Stop()
	waitFor(t, 2*time.Second, func() bool { return !rt.Current().Members[0].Up })
}

func TestProbeSuccessNeverRecovers(t *testing.T) {
	rt := router.New(16, 0)
	if _, _, err := rt.Load(members(), nil); err != nil {
		t.Fatal(err)
	}
	// manually mark a down first
	if _, _, err := rt.SetDown(rt.Current().Version, "a", "manual"); err != nil {
		t.Fatal(err)
	}
	fd := &fakeDialer{failing: map[string]bool{}, calls: map[string]int{}} // everything "reachable"
	p := newProber(t, rt, fd, 1)
	for i := 0; i < 3; i++ {
		p.RunOnce()
	}
	// even with repeated successful probes, a must NOT be auto-recovered
	if rt.Current().Members[0].Up {
		t.Fatal("probing must never mark a member back up; recovery is explicit")
	}
}

func TestSingleFailureBelowThresholdKeepsMemberUp(t *testing.T) {
	rt := router.New(16, 0)
	if _, _, err := rt.Load(members(), nil); err != nil {
		t.Fatal(err)
	}
	fd := &fakeDialer{failing: map[string]bool{}, calls: map[string]int{}}
	p := newProber(t, rt, fd, 3)
	fd.fail("127.0.0.1:1", true)

	// two failed sweeps -> still up because failures=3 is required
	p.RunOnce()
	p.RunOnce()
	if !rt.Current().Members[0].Up {
		t.Fatal("member marked down before reaching the failure threshold")
	}
	if n := p.Strikes()["a"]; n != 2 {
		t.Fatalf("strikes=%d want 2", n)
	}
	// third failed sweep -> down
	p.RunOnce()
	if rt.Current().Members[0].Up {
		t.Fatal("member must be down after exactly 3 consecutive failures")
	}
	if got := rt.Current().Members[0].DownReason; got != "probe_failed" {
		t.Fatalf("down reason=%q want probe_failed", got)
	}

	// a later successful probe resets strikes but, per policy, never brings
	// the member back up automatically.
	fd.fail("127.0.0.1:1", false)
	p.RunOnce()
	if p.Strikes()["a"] != 0 {
		t.Fatalf("strikes after success=%d want 0", p.Strikes()["a"])
	}
	if rt.Current().Members[0].Up {
		t.Fatal("successful probe must not auto-recover a down member")
	}
}

func TestBadProbeConfigRejected(t *testing.T) {
	rt := router.New(16, 0)
	if _, err := health.New(rt, 0, time.Millisecond, 1, nil); err == nil {
		t.Fatal("invalid probe interval must fail")
	}
}

func waitFor(t *testing.T, d time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition not met within timeout")
}
