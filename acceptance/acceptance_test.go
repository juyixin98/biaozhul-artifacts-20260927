package acceptance_test

import (
	"io"
	"net/http"
	"path/filepath"
	"testing"
)

// oracle is the INDEPENDENT reference. It is written only from the contract
// (baseline + declared policy) and the HTTP-reported counts; it contains no
// code from the controller package and no knowledge of the controller's
// internals. Every rollout step is checked against it.
type oracle struct {
	baseline       int
	maxSurge       int
	maxUnavailable int
}

// Bounds are the independently derived legal interval for one step.
type bounds struct {
	liveMin, liveMax int
	availMin         int
}

func newOracle(baseline, surge, unavail int) *oracle {
	return &oracle{baseline: baseline, maxSurge: surge, maxUnavailable: unavail}
}

func (o *oracle) bounds() bounds {
	minAvail := o.baseline - o.maxUnavailable
	if minAvail < 0 {
		minAvail = 0
	}
	// Live can never be negative; upper bound is baseline + surge. The lower
	// bound on live during a safe rollout is at least minAvail (available is a
	// subset of live).
	return bounds{liveMin: minAvail, liveMax: o.baseline + o.maxSurge, availMin: minAvail}
}

func (o *oracle) check(t *testing.T, step int, s map[string]any) {
	t.Helper()
	b := o.bounds()
	live, avail := intField(s, "live"), intField(s, "available")
	if live > b.liveMax {
		t.Fatalf("step %d: maxSurge violated live=%d > %d", step, live, b.liveMax)
	}
	if live < b.liveMin {
		t.Fatalf("step %d: live=%d below safe minimum %d", step, live, b.liveMin)
	}
	if avail < b.availMin {
		t.Fatalf("step %d: maxUnavailable violated available=%d < %d", step, avail, b.availMin)
	}
	if avail > live {
		t.Fatalf("step %d: impossible available=%d > live=%d", step, avail, live)
	}
}

// driveWithOracle ticks and checks the oracle after every tick while the
// release is non-terminal, returning the terminal release JSON.
func driveWithOracle(t *testing.T, c *client, id, workload string, o *oracle, max int) map[string]any {
	t.Helper()
	for step := 1; step <= max; step++ {
		tick(c)
		r := releaseByID(c, id)
		st := strField(r, "state")
		if st == "pending" || st == "active" {
			o.check(t, step, status(c, workload))
		}
		if st == "succeeded" || st == "failed" {
			return r
		}
	}
	t.Fatalf("release %s not terminal after %d ticks", id, max)
	return nil
}

func dbPath(t *testing.T) string { return filepath.Join(t.TempDir(), "acc.db") }

// TestHTTP_HappyRollout_StepwiseConstraints: end-to-end through HTTP with an
// independent oracle on every step, plus exact final version/counts.
func TestHTTP_HappyRollout_StepwiseConstraints(t *testing.T) {
	c := startInProc(t, dbPath(t), 16)
	p := policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 40}
	setBehavior(t, c, "shop", "v1", map[string]any{"mode": "normal", "readyDelayTicks": 1})
	setBehavior(t, c, "shop", "v2", map[string]any{"mode": "normal", "readyDelayTicks": 1})
	createWorkload(t, c, "shop", 3, "v1", p)
	settle(t, c, "shop", 3)

	rel := startRelease(t, c, "shop", "v2", nil)
	id := strField(rel, "id")
	final := driveWithOracle(t, c, id, "shop", newOracle(3, 1, 0), 120)
	if strField(final, "state") != "succeeded" {
		t.Fatalf("state=%s msg=%s", strField(final, "state"), strField(final, "failMessage"))
	}
	s := status(c, "shop")
	wl, _ := s["workload"].(map[string]any)
	if strField(wl, "currentRevision") != "v2" {
		t.Fatalf("final currentRevision = %q, want v2", strField(wl, "currentRevision"))
	}
	if intField(s, "live") != 3 || intField(s, "available") != 3 {
		t.Fatalf("final counts live=%d available=%d", intField(s, "live"), intField(s, "available"))
	}
	byRev, _ := s["byRevision"].(map[string]any)
	v2, _ := byRev["v2"].(map[string]any)
	if intField(v2, "live") != 3 || intField(v2, "available") != 3 {
		t.Fatalf("v2 counts = %v", v2)
	}
}

// TestHTTP_StartFailure_CategoryAndServing is the independent assertion of the
// start-failure fixture over HTTP: exact failure category, serving unchanged,
// and a structured error category is not conflated with a transport error.
func TestHTTP_StartFailure_CategoryAndServing(t *testing.T) {
	c := startInProc(t, dbPath(t), 16)
	p := policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 20, MaxStartFailures: 0}
	setBehavior(t, c, "api", "v1", map[string]any{"mode": "normal"})
	setBehavior(t, c, "api", "bad", map[string]any{"mode": "start_rejected"})
	createWorkload(t, c, "api", 2, "v1", p)
	settle(t, c, "api", 2)

	rel := startRelease(t, c, "api", "bad", &p)
	final := driveWithOracle(t, c, strField(rel, "id"), "api", newOracle(2, 1, 0), 60)
	if strField(final, "state") != "failed" || strField(final, "failureCategory") != "start_failed" {
		t.Fatalf("final = %s/%q", strField(final, "state"), strField(final, "failureCategory"))
	}
	if strField(final, "failMessage") == "" {
		t.Fatal("failed release must carry an explanatory message")
	}
	s := status(c, "api")
	wl, _ := s["workload"].(map[string]any)
	if strField(wl, "currentRevision") != "v1" {
		t.Fatalf("current revision changed after failed release: %q", strField(wl, "currentRevision"))
	}
	if intField(s, "available") != 2 {
		t.Fatalf("serving availability = %d, want 2", intField(s, "available"))
	}
}

// TestHTTP_ReadinessFlap_NeverReady proves created/promoted-then-unstable
// instances are never trusted as the final serving set.
func TestHTTP_ReadinessFlap_NeverReady(t *testing.T) {
	c := startInProc(t, dbPath(t), 16)
	p := policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 3, DeadlineTicks: 8, MaxStartFailures: 0}
	setBehavior(t, c, "flap", "v1", map[string]any{"mode": "normal"})
	setBehavior(t, c, "flap", "v2", map[string]any{
		"mode": "flap", "readyDelayTicks": 1, "flapReadyTicks": 1, "flapDownTicks": 1,
	})
	createWorkload(t, c, "flap", 2, "v1", p)
	settle(t, c, "flap", 2)

	rel := startRelease(t, c, "flap", "v2", &p)
	final := driveWithOracle(t, c, strField(rel, "id"), "flap", newOracle(2, 1, 0), 150)
	if strField(final, "failureCategory") != "readiness_flapping" {
		t.Fatalf("category = %q, want readiness_flapping", strField(final, "failureCategory"))
	}
	s := status(c, "flap")
	byRev, _ := s["byRevision"].(map[string]any)
	v2, _ := byRev["v2"].(map[string]any)
	if intField(v2, "available") != 0 {
		t.Fatalf("flapping v2 was counted available: %v", v2)
	}
}

// TestHTTP_CapacityFixture_ExactClass independently drives a capacity
// shortfall and asserts the specific class (not a generic stall) and that the
// event stream distinguishes uncertain blocks from hard failures.
func TestHTTP_CapacityFixture_ExactClass(t *testing.T) {
	c := startInProc(t, dbPath(t), 3)
	p := policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 5, MaxStartFailures: 0}
	setBehavior(t, c, "cap", "v1", map[string]any{"mode": "normal"})
	setBehavior(t, c, "cap", "v2", map[string]any{"mode": "normal"})
	createWorkload(t, c, "cap", 3, "v1", p)
	settle(t, c, "cap", 3)

	rel := startRelease(t, c, "cap", "v2", &p)
	final := driveWithOracle(t, c, strField(rel, "id"), "cap", newOracle(3, 1, 0), 60)
	if strField(final, "failureCategory") != "insufficient_capacity" {
		t.Fatalf("category=%q want insufficient_capacity", strField(final, "failureCategory"))
	}
	// Events must separate uncertain capacity blocks (certain=false) from the
	// final hard failure (certain=true).
	var sawUncertainBlock, sawHardFail bool
	for _, e := range events(c, "cap") {
		em, _ := e.(map[string]any)
		if strField(em, "releaseId") != strField(rel, "id") {
			continue
		}
		if strField(em, "type") == "blocked_capacity" && !em["certain"].(bool) {
			sawUncertainBlock = true
		}
		if strField(em, "type") == "rollout_failed" && em["certain"].(bool) {
			sawHardFail = true
		}
	}
	if !sawUncertainBlock || !sawHardFail {
		t.Fatalf("event semantics wrong: uncertainBlock=%v hardFail=%v", sawUncertainBlock, sawHardFail)
	}
}

// TestHTTP_RequestCorrelation ties a client-supplied request id to every event
// of the resulting release — the required end-to-end correlation.
func TestHTTP_RequestCorrelation(t *testing.T) {
	c := startInProc(t, dbPath(t), 16)
	p := policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 20}
	setBehavior(t, c, "corr", "v1", map[string]any{"mode": "normal"})
	createWorkload(t, c, "corr", 1, "v1", p)
	settle(t, c, "corr", 1)

	c.rid = "corr-req-4242"
	rel := startRelease(t, c, "corr", "v2", nil)
	id := strField(rel, "id")
	// v2 has no behavior configured: default healthy fixture -> succeeds.
	driveWithOracle(t, c, id, "corr", newOracle(1, 1, 0), 90)
	matched := 0
	for _, e := range events(c, "corr") {
		em, _ := e.(map[string]any)
		if strField(em, "releaseId") == id {
			if strField(em, "requestId") != "corr-req-4242" {
				t.Fatalf("event %s missing request id, got %q", strField(em, "type"), strField(em, "requestId"))
			}
			matched++
		}
	}
	if matched == 0 {
		t.Fatal("no events carried the client request id")
	}
	// Fetching the release echoes the same id.
	if strField(releaseByID(c, id), "requestId") != "corr-req-4242" {
		t.Fatal("release row does not retain request id")
	}
}

// TestHTTP_RollbackHistory verifies via HTTP that rollback is a new release
// with kind=rollback, prior rows survive, and ordering/versions are exact.
func TestHTTP_RollbackHistory(t *testing.T) {
	c := startInProc(t, dbPath(t), 16)
	p := policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 20}
	setBehavior(t, c, "hist", "v1", map[string]any{"mode": "normal"})
	setBehavior(t, c, "hist", "v2", map[string]any{"mode": "normal"})
	setBehavior(t, c, "hist", "v3", map[string]any{"mode": "start_rejected"})
	createWorkload(t, c, "hist", 2, "v1", p)
	settle(t, c, "hist", 2)

	r2 := driveWithOracle(t, c, strField(startRelease(t, c, "hist", "v2", nil), "id"), "hist", newOracle(2, 1, 0), 90)
	if strField(r2, "state") != "succeeded" {
		t.Fatalf("v2 rollout: %s", strField(r2, "state"))
	}
	r3 := driveWithOracle(t, c, strField(startRelease(t, c, "hist", "v3", &p), "id"), "hist", newOracle(2, 1, 0), 60)
	if strField(r3, "failureCategory") != "start_failed" {
		t.Fatalf("v3: %q", strField(r3, "failureCategory"))
	}

	// Rollback to v1 (new operation).
	body, _, code := c.do("POST", "/api/v1/workloads/hist/rollback", map[string]any{"targetRevision": "v1"})
	if code != 201 {
		t.Fatalf("rollback http %d: %v", code, body)
	}
	if strField(body, "kind") != "rollback" || strField(body, "revision") != "v1" {
		t.Fatalf("rollback row wrong: kind=%q rev=%q", strField(body, "kind"), strField(body, "revision"))
	}
	rbID := strField(body, "id")
	rbFinal := driveWithOracle(t, c, rbID, "hist", newOracle(2, 1, 0), 90)
	if strField(rbFinal, "state") != "succeeded" {
		t.Fatalf("rollback did not succeed: %s", strField(rbFinal, "state"))
	}

	list, _, code := c.do("GET", "/api/v1/workloads/hist/releases", nil)
	if code != 200 {
		t.Fatalf("list releases: %d", code)
	}
	rels, _ := list["releases"].([]any)
	if len(rels) != 4 {
		t.Fatalf("history length = %d, want 4", len(rels))
	}
	wantSeq := []string{"rollback", "rollout", "rollout", "bootstrap"}
	for i, want := range wantSeq {
		rm, _ := rels[i].(map[string]any)
		if strField(rm, "kind") != want {
			t.Fatalf("history[%d] kind=%q want %q", i, strField(rm, "kind"), want)
		}
	}
}

// TestHTTP_ErrorSemantics asserts concrete failure categories on the wire,
// not mere reachability: 404, 409 active release, 422 no rollback target,
// 400 bad body.
func TestHTTP_ErrorSemantics(t *testing.T) {
	c := startInProc(t, dbPath(t), 16)
	p := policy{MaxSurge: 1, MaxUnavailable: 0, ReadyThresholdTicks: 2, DeadlineTicks: 40}
	setBehavior(t, c, "err", "v1", map[string]any{"mode": "normal", "readyDelayTicks": 1})
	createWorkload(t, c, "err", 1, "v1", p)

	// 404 unknown workload.
	body, _, code := c.do("GET", "/api/v1/workloads/nope", nil)
	if code != 404 || strField(body, "category") != "not_found" {
		t.Fatalf("expected 404/not_found, got %d %v", code, body)
	}

	// 400 malformed JSON.
	req, _ := newRawRequest(c, "POST", "/api/v1/workloads", "{not json")
	resp, err := c.hc.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("malformed body status = %d, want 400", resp.StatusCode)
	}

	// 409 while a release is active.
	_ = startRelease(t, c, "err", "v9", nil) // pending; do not tick
	body, _, code = c.do("POST", "/api/v1/workloads/err/releases", map[string]any{"revision": "v10"})
	if code != 409 || strField(body, "category") != "active_release" {
		t.Fatalf("expected 409/active_release, got %d %v", code, body)
	}

	// 422 rollback target on a workload that only ever had the one revision
	// (after active release finishes first, to avoid the 409).
	settle(t, c, "err", 1)
	// v9 uses default healthy behavior, so it succeeds; after that v9 serves,
	// and rollback to an unknown revision must be 422.
	body, _, code = c.do("POST", "/api/v1/workloads/err/rollback", map[string]any{"targetRevision": "ghost"})
	if code != 422 || strField(body, "category") != "no_rollback_target" {
		t.Fatalf("expected 422/no_rollback_target, got %d %v", code, body)
	}
}

func newRawRequest(c *client, method, path, raw string) (*http.Request, error) {
	req, err := http.NewRequest(method, c.base+path, stringReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.rid != "" {
		req.Header.Set("X-Request-Id", c.rid)
	}
	return req, nil
}

func stringReader(s string) io.Reader { return &strReader{s: s} }

type strReader struct {
	s string
	i int
}

func (r *strReader) Read(p []byte) (int, error) {
	if r.i >= len(r.s) {
		return 0, io.EOF
	}
	n := copy(p, r.s[r.i:])
	r.i += n
	return n, nil
}
