// Package accept is the independent black-box acceptance verifier.
//
// It drives the REAL server binary over HTTP against a REAL SQLite database,
// including a real process restart. For every reconciliation it computes the
// expected replica count, action and failure categories with the independent
// oracle (which never imports the controller), compares them field by field,
// and records the reason behind every "no action" tick.
package accept

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"replicactl/internal/oracle"
)

// Result is one verifiable row of the acceptance report.
type Result struct {
	Scenario string
	Step     string
	Action   string
	Got      int32
	Expected int32
	Reasons  []string
	Pass     bool
	Detail   string
}

// Report is the full run.
type Report struct {
	Results []Result
	Passed  int
	Failed  int
}

type server struct {
	binary  string
	dbPath  string
	cfgPath string
	addr    string
	cmd     *exec.Cmd
}

type session struct {
	out           *Report
	srv           *server
	policy        oracle.Policy
	cur           int32
	preCur        int32
	active        []string
	reports       map[string]oracle.InstanceReport
	priors        []oracle.PriorObservation
	scenario      string
	demandPending *int64
	demandAt      time.Time
}

// BuildBinary compiles the server into binPath. It locates the module root
// via "go env GOMOD", so it works regardless of the caller's working dir.
func BuildBinary(ctx context.Context, binPath string) error {
	out, err := exec.CommandContext(ctx, "go", "env", "GOMOD").Output()
	if err != nil {
		return fmt.Errorf("locate go.mod: %w", err)
	}
	modFile := strings.TrimSpace(string(out))
	if modFile == "" || modFile == "/dev/null" {
		return fmt.Errorf("go.mod not found")
	}
	root := filepath.Dir(modFile)
	cmd := exec.CommandContext(ctx, "go", "build", "-o", binPath, "./cmd/replicactl")
	cmd.Dir = root
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func freeAddr() (string, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", err
	}
	addr := l.Addr().String()
	_ = l.Close()
	return addr, nil
}

func start(ctx context.Context, binary, dir, cfgJSON string) (*server, error) {
	dbPath := filepath.Join(dir, "accept.db")
	cfgPath := filepath.Join(dir, "config.json")
	if err := os.WriteFile(cfgPath, []byte(cfgJSON), 0o644); err != nil {
		return nil, err
	}
	addr, err := freeAddr()
	if err != nil {
		return nil, err
	}
	s := &server{binary: binary, dbPath: dbPath, cfgPath: cfgPath, addr: addr}
	if err := s.spawn(ctx); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *server) spawn(ctx context.Context) error {
	c := exec.CommandContext(ctx, s.binary,
		"-db", "file:"+s.dbPath, "-config", s.cfgPath,
		"-addr", s.addr, "-no-autotick")
	c.Stderr = os.Stderr
	if err := c.Start(); err != nil {
		return err
	}
	s.cmd = c
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get("http://" + s.addr + "/healthz")
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("server at %s never became healthy", s.addr)
}

func (s *server) stop() error {
	if s.cmd == nil || s.cmd.Process == nil {
		return nil
	}
	_ = s.cmd.Process.Signal(os.Interrupt)
	done := make(chan error, 1)
	go func() { done <- s.cmd.Wait() }()
	select {
	case <-done:
		return nil
	case <-time.After(5 * time.Second):
		_ = s.cmd.Process.Kill()
		return fmt.Errorf("server did not exit")
	}
}

// restart stops and starts the same binary against the SAME database file.
func (s *server) restart(ctx context.Context) error {
	if err := s.stop(); err != nil {
		return err
	}
	return s.spawn(ctx)
}

func (s *server) url(p string) string { return "http://" + s.addr + p }

func httpDo(method, urlv string, body any, rid string) (int, map[string]any, error) {
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, urlv, rdr)
	if err != nil {
		return 0, nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if rid != "" {
		req.Header.Set("X-Request-ID", rid)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	_ = json.Unmarshal(raw, &m)
	return resp.StatusCode, m, nil
}

const policyTpl = `{
  "metric": "requests_per_second",
  "target_load_per_instance": 100,
  "min_replicas": %d,
  "max_replicas": 20,
  "metric_freshness": "3s",
  "demand_freshness": "3s",
  "scale_down_stable_window": "3s",
  "scale_up_max_factor": 2.0,
  "scale_up_max_delta": 4,
  "tolerance": 0.10,
  "bootstrap_replicas": 1,
  "scale_from_zero_enabled": true,
  "initial_replicas": %d,
  "tick_interval": "1m",
  "listen_addr": "127.0.0.1:0"
}`

func policyFor(min, initial int32) (string, oracle.Policy) {
	p := oracle.Policy{
		TargetPerInstance: 100, MinReplicas: min, MaxReplicas: 20,
		Freshness: 3 * time.Second, DemandFreshness: 3 * time.Second,
		StableWindow: 3 * time.Second, UpFactor: 2, UpMaxDelta: 4,
		Tolerance: 0.10, Bootstrap: 1, FromZeroEnabled: true,
	}
	return fmt.Sprintf(policyTpl, min, initial), p
}

// Run executes the full acceptance suite.
func Run(ctx context.Context, workDir string, w io.Writer) (Report, error) {
	rep := &Report{}
	bin := filepath.Join(workDir, "replicactl-bin")
	if err := BuildBinary(ctx, bin); err != nil {
		return *rep, fmt.Errorf("build binary: %w", err)
	}

	type scenario struct {
		name    string
		min     int32
		initial int32
		fn      func(context.Context, *session) error
	}
	scenarios := []scenario{
		{"load-step", 0, 3, scenLoadStep},
		{"rate-cap", 0, 2, scenRateCap},
		{"missing-instance", 0, 3, scenMissing},
		{"delayed-stale", 0, 3, scenDelayedStale},
		{"short-spike-and-restart", 1, 3, scenSpikeRestart},
		{"scale-from-zero", 0, 0, scenFromZero},
	}

	var scenarioErr error
	for i, sc := range scenarios {
		dir := filepath.Join(workDir, fmt.Sprintf("s%d-%s", i, sc.name))
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return *rep, err
		}
		cfgJSON, pol := policyFor(sc.min, sc.initial)
		srv, err := start(ctx, bin, dir, cfgJSON)
		if err != nil {
			return *rep, fmt.Errorf("start %s: %w", sc.name, err)
		}
		sess := &session{
			out: rep, srv: srv, policy: pol, cur: sc.initial,
			reports: map[string]oracle.InstanceReport{}, scenario: sc.name,
		}
		for n := int32(1); n <= sc.initial; n++ {
			sess.active = append(sess.active, fmt.Sprintf("ins-%04d", n))
		}
		if err := sc.fn(ctx, sess); err != nil {
			rep.record(sc.name, "scenario", "error", 0, 0, nil, false, err.Error())
			scenarioErr = err
		}
		_ = srv.stop()
	}

	for _, r := range rep.Results {
		if r.Pass {
			rep.Passed++
		} else {
			rep.Failed++
		}
	}
	printReport(w, rep)
	if rep.Failed > 0 || scenarioErr != nil {
		return *rep, fmt.Errorf("acceptance failed: %d check(s) failed", rep.Failed)
	}
	return *rep, nil
}

// --- scenario actions ------------------------------------------------------

func (s *session) report(id string, value float64, observedAt time.Time) {
	_, body, err := httpDo("POST", s.srv.url("/v1/instances/"+id+"/samples"),
		map[string]any{"value": value, "observed_at": observedAt.UTC().Format(time.RFC3339Nano)}, "")
	if err != nil || body["status"] != "accepted" {
		panic(fmt.Sprintf("ingest failed: %v %v", err, body))
	}
	obs := observedAt.UTC()
	s.reports[id] = oracle.InstanceReport{ID: id, Value: value, ObservedAt: &obs}
}

func (s *session) reportAll(value float64, age time.Duration) {
	now := time.Now().UTC()
	for _, id := range s.active {
		s.report(id, value, now.Add(-age))
	}
}

func (s *session) reportExcept(value float64, age time.Duration, skip string) {
	now := time.Now().UTC()
	for _, id := range s.active {
		if id != skip {
			s.report(id, value, now.Add(-age))
		}
	}
	delete(s.reports, skip)
}

func (s *session) setDemand(pending int64, observedAt time.Time) {
	if _, _, err := httpDo("POST", s.srv.url("/v1/demand"),
		map[string]any{"pending": pending, "observed_at": observedAt.UTC().Format(time.RFC3339Nano)}, ""); err != nil {
		panic(err.Error())
	}
	s.demandPending = &pending
	s.demandAt = observedAt.UTC()
}

// reconcile calls the real endpoint and grades the response against the
// independent oracle, recording one report row.
func (s *session) reconcile(ctx context.Context, step string) map[string]any {
	now := time.Now().UTC()
	s.preCur = s.cur
	rid := fmt.Sprintf("req-%s-%s-%d", s.scenario, step, now.UnixNano())
	status, d, err := httpDo("POST", s.srv.url("/v1/reconcile"), nil, rid)
	if err != nil || status != http.StatusOK {
		s.out.record(s.scenario, step, "error", 0, 0, nil, false,
			fmt.Sprintf("reconcile http: %v status=%d", err, status))
		return d
	}

	var reps []oracle.InstanceReport
	for _, id := range s.active {
		if r, ok := s.reports[id]; ok {
			reps = append(reps, r)
		} else {
			reps = append(reps, oracle.InstanceReport{ID: id})
		}
	}
	dm := oracle.DemandState{}
	if s.demandPending != nil {
		dm = oracle.DemandState{Has: true, Pending: *s.demandPending, ObservedAt: s.demandAt}
	}
	exp := oracle.Expected(oracle.Input{
		Tick: now, Current: s.cur, Active: append([]string(nil), s.active...),
		Reports: reps, Demand: dm, Prior: append([]oracle.PriorObservation(nil), s.priors...),
		Policy: s.policy,
	})

	gotAction, _ := d["action"].(string)
	gotDesired := asI32(d["desired_replicas"])
	gotApplied := asI32(d["applied_replicas"])
	var gotReasons []string
	if ra, ok := d["reasons"].([]any); ok {
		for _, rr := range ra {
			if rm, ok := rr.(map[string]any); ok {
				gotReasons = append(gotReasons, rm["code"].(string))
			}
		}
	}

	// Independently update fleet model from reported actuator effects.
	if up, ok := d["scaled_up_ids"].([]any); ok {
		for _, u := range up {
			s.active = append(s.active, u.(string))
		}
	}
	if down, ok := d["scaled_down_ids"].([]any); ok {
		drop := map[string]bool{}
		for _, x := range down {
			drop[x.(string)] = true
		}
		var kept []string
		for _, id := range s.active {
			if !drop[id] {
				kept = append(kept, id)
			}
		}
		s.active = kept
	}

	detail := fmt.Sprintf("want action=%s/desired=%d; got action=%s/desired=%d/applied=%d",
		exp.Action, exp.DesiredReplicas, gotAction, gotDesired, gotApplied)
	pass := gotAction == exp.Action && gotDesired == exp.DesiredReplicas && gotApplied == exp.DesiredReplicas
	if !reasonSetsEqual(gotReasons, exp.ReasonCodes) {
		pass = false
		detail += fmt.Sprintf(" | reasons want=%v got=%v", exp.ReasonCodes, gotReasons)
	}
	if gt := asF64(d["total_load"]); !floatEq(gt, exp.TotalLoad) {
		pass = false
		detail += fmt.Sprintf(" | total_load want=%v got=%v", exp.TotalLoad, gt)
	}
	if d["request_id"] != rid {
		pass = false
		detail += " | request_id not echoed"
	}
	if d["location"] == nil || d["config_version"] == nil {
		pass = false
		detail += " | missing provenance"
	}
	s.out.record(s.scenario, step, gotAction, gotDesired, exp.DesiredReplicas, gotReasons, pass, detail)

	// Mirror the engine: an observation is saved whenever the downscale
	// branch runs (post-guards recommendation below the pre-tick count).
	if exp.RawDesired < s.preCur {
		s.priors = append(s.priors, oracle.PriorObservation{At: now, Replicas: exp.RawDesired})
	}
	s.cur = int32(len(s.active))
	if s.demandPending != nil && now.Sub(s.demandAt) > s.policy.DemandFreshness {
		s.demandPending = nil
	}
	return d
}

func (s *session) getFleet() (int32, []string) {
	_, body, err := httpDo("GET", s.srv.url("/v1/fleet"), nil, "")
	if err != nil {
		panic(err.Error())
	}
	n := asI32(body["replicas"])
	var ids []string
	if arr, ok := body["instances"].([]any); ok {
		for _, x := range arr {
			ids = append(ids, x.(string))
		}
	}
	return n, ids
}

func (r *Report) record(scenario, step, action string, got, want int32, reasons []string, pass bool, detail string) {
	r.Results = append(r.Results, Result{
		Scenario: scenario, Step: step, Action: action, Got: got,
		Expected: want, Reasons: reasons, Pass: pass, Detail: detail,
	})
}

// --- small helpers ---------------------------------------------------------

func asI32(v any) int32 {
	switch x := v.(type) {
	case float64:
		return int32(x)
	case int:
		return int32(x)
	}
	return -1
}

func asF64(v any) float64 {
	if x, ok := v.(float64); ok {
		return x
	}
	return -1
}

func floatEq(a, b float64) bool {
	d := a - b
	if d < 0 {
		d = -d
	}
	return d < 1e-9
}

func reasonSetsEqual(a, b []string) bool {
	x := append([]string(nil), a...)
	y := append([]string(nil), b...)
	sort.Strings(x)
	sort.Strings(y)
	return strings.Join(x, ",") == strings.Join(y, ",")
}

func joinIDs(ids []string) string { return "[" + strings.Join(ids, ",") + "]" }

func equalIDs(a, b []string) bool { return reasonSetsEqual(a, b) }

func printReport(w io.Writer, r *Report) {
	fmt.Fprintln(w, "")
	fmt.Fprintln(w, "==================== ACCEPTANCE VERIFICATION ====================")
	fmt.Fprintf(w, "%-26s %-16s %-10s %5s %5s  %s\n", "SCENARIO", "STEP", "ACTION", "GOT", "WANT", "REASONS / NOTE")
	fmt.Fprintln(w, strings.Repeat("-", 100))
	for _, x := range r.Results {
		mark := "PASS"
		if !x.Pass {
			mark = "FAIL"
		}
		fmt.Fprintf(w, "%-26s %-16s %-10s %5d %5d  [%s] %s\n",
			x.Scenario, x.Step, x.Action, x.Got, x.Expected, mark, strings.Join(x.Reasons, ","))
		if !x.Pass {
			fmt.Fprintf(w, "%44s^ %s\n", "", x.Detail)
		}
	}
	fmt.Fprintln(w, strings.Repeat("-", 100))
	fmt.Fprintf(w, "TOTAL: %d passed, %d failed\n", r.Passed, r.Failed)
	fmt.Fprintln(w, "================================================================")
}
