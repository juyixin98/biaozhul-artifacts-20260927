// Package runner executes acceptance scenarios against the real replicactl
// binary over HTTP. A scenario run spawns the production binary as a child
// process on a fresh SQLite file, replays metric/demand/tick steps, restarts
// the process when a step asks for it, and compares every tick against BOTH
// the hand-authored expectation in the fixture and the independent reference
// simulator.
package runner

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
	"strconv"
	"time"

	"replicactl/acceptance/reference"
)

// ---- Fixture schema ----------------------------------------------------------

// Scenario is one checked-in, hand-authored fixture file.
type Scenario struct {
	Name            string `json:"name"`
	Description     string `json:"description"`
	InitialReplicas int    `json:"initial_replicas"`
	Steps           []Step `json:"steps"`
}

// Step is one wall-clock event in the scenario. Exactly one op is meaningful.
type Step struct {
	At         int64   `json:"at"`
	Kind       string  `json:"kind"` // "metric" | "demand" | "reconcile"
	Instance   string  `json:"instance,omitempty"`
	Load       float64 `json:"load,omitempty"`
	ReportedAt int64   `json:"reported_at,omitempty"` // metric report timestamp (late when < At)
	Present    *bool   `json:"present,omitempty"`
	Restart    bool    `json:"restart_before,omitempty"` // reconcile only: kill & respawn first
	// Expect is the hand-authored expected reconcile outcome.
	Expect    *Expect `json:"expect,omitempty"`
	RequestID string  `json:"request_id,omitempty"`
}

// Expect is the hand-computed expected result declared in the fixture.
type Expect struct {
	Action          string   `json:"action"`
	DesiredReplicas int      `json:"desired_replicas"`
	Reasons         []string `json:"reasons"`
	RawDesired      int      `json:"raw_desired"`
	Fresh           int      `json:"fresh"`
	Stale           int      `json:"stale"`
	Missing         int      `json:"missing"`
	TotalLoad       float64  `json:"total_load"`
	FailureClass    string   `json:"failure_class,omitempty"`
}

// StepResult records what all three parties said at one reconcile step.
type StepResult struct {
	Index        int               `json:"index"`
	RequestID    string            `json:"request_id"`
	At           int64             `json:"at"`
	HandExpected *Expect           `json:"hand_expected"`
	Reference    reference.Outcome `json:"reference"`
	Actual       ActualTick        `json:"actual"`
	Restarted    bool              `json:"restarted"`
	Mismatches   []string          `json:"mismatches"`
}

// ActualTick is the production HTTP response.
type ActualTick struct {
	HTTPCode      int      `json:"http_code"`
	Action        string   `json:"action"`
	Desired       int      `json:"desired_replicas"`
	Reasons       []string `json:"reasons"`
	FailureClass  string   `json:"failure_class"`
	FailureDetail string   `json:"failure_detail"`
	RawDesired    int      `json:"raw_desired"`
	Fresh         int      `json:"fresh"`
	Stale         int      `json:"stale"`
	Missing       int      `json:"missing"`
	TotalLoad     float64  `json:"total_load"`
}

// Report is the verifiable output of one scenario.
type Report struct {
	Scenario    string       `json:"scenario"`
	Description string       `json:"description"`
	BinaryPath  string       `json:"binary_path"`
	Passed      bool         `json:"passed"`
	Results     []StepResult `json:"results"`
	// NoopJustifications lists every deliberate non-action with its reason.
	NoopJustifications []NoopLine `json:"noop_justifications"`
}

// NoopLine explains one "no action taken" tick.
type NoopLine struct {
	At     int64  `json:"at"`
	Reason string `json:"reason"`
	Detail string `json:"detail"`
}

// RunConfig carries the locations a run needs.
type RunConfig struct {
	RepoDir  string // repository root (to locate app/cmd)
	WorkDir  string // temp dir for binary, config and db
	BinPath  string // prebuilt binary; built when empty
	HTTPPort int    // 0 = pick a free port
	KeepDB   bool
}

// Run executes one scenario end to end.
func Run(sc Scenario, cfg RunConfig) (Report, error) {
	rep := Report{Scenario: sc.Name, Description: sc.Description, Passed: true}

	bin := cfg.BinPath
	if bin == "" {
		var err error
		bin, err = buildBinary(cfg.RepoDir, cfg.WorkDir)
		if err != nil {
			return rep, err
		}
	}
	rep.BinaryPath = bin

	port := cfg.HTTPPort
	if port == 0 {
		p, err := freePort()
		if err != nil {
			return rep, err
		}
		port = p
	}
	addr := "127.0.0.1:" + strconv.Itoa(port)
	dbPath := filepath.Join(cfg.WorkDir, "acceptance.db")
	_ = os.Remove(dbPath)
	configPath := filepath.Join(cfg.WorkDir, "acceptance-config.json")
	if err := os.WriteFile(configPath, []byte(fmt.Sprintf(`{
  "target_load_per_instance": 10,
  "max_scale_up_factor": 2,
  "max_scale_up_floor": 1,
  "scale_down_stable_window_seconds": 60,
  "stale_skew_seconds": 30,
  "tolerance": 0.10,
  "min_fresh_fraction": 0.5,
  "min_replicas": 0,
  "max_replicas": 16,
  "bootstrap_replicas": 1,
  "http_addr": %q,
  "database_dsn": %q
}
`, addr, "file:"+dbPath)), 0o644); err != nil {
		return rep, err
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	proc, err := startBinary(ctx, bin, configPath)
	if err != nil {
		return rep, err
	}
	if err := waitReady(addr, 10*time.Second); err != nil {
		proc.kill()
		return rep, err
	}
	defer proc.kill()

	base := "http://" + addr
	client := &http.Client{Timeout: 5 * time.Second}

	// Seed the initial fleet size.
	if sc.InitialReplicas > 0 {
		if _, _, err := postJSON(client, base+"/v1/admin/seed", "", map[string]any{"replicas": sc.InitialReplicas}); err != nil {
			return rep, fmt.Errorf("seed: %w", err)
		}
	}

	// Independent reference starts in the same state.
	sim := reference.NewSim(reference.DefaultParams(), sc.InitialReplicas)

	for i, st := range sc.Steps {
		switch st.Kind {
		case "metric":
			if _, _, err := postJSON(client, base+"/v1/metrics", "acc-metric",
				map[string]any{"instance_id": st.Instance, "load": st.Load, "reported_at": st.ReportedAt}); err != nil {
				return rep, fmt.Errorf("step %d metric: %w", i, err)
			}
			sim.PutSample(st.Instance, st.Load, st.ReportedAt, st.At)
		case "demand":
			present := false
			if st.Present != nil {
				present = *st.Present
			}
			if _, _, err := postJSON(client, base+"/v1/demand", "acc-demand",
				map[string]any{"present": present, "reported_at": st.ReportedAt}); err != nil {
				return rep, fmt.Errorf("step %d demand: %w", i, err)
			}
			sim.PutDemand(present, st.ReportedAt)
		case "reconcile":
			if st.Restart {
				if err := proc.restart(ctx, configPath); err != nil {
					return rep, fmt.Errorf("step %d restart: %w", i, err)
				}
				if err := waitReady(addr, 10*time.Second); err != nil {
					return rep, fmt.Errorf("step %d post-restart readiness: %w", i, err)
				}
				// The independent sim is reconstructed from its exported
				// durable state, mirroring the binary reading its SQLite file.
				sim = reference.Restore(reference.DefaultParams(), sim.Export())
			}

			expectedRef := sim.Reconcile(st.At)
			actual, err := reconcile(client, base, st.At, st.RequestID)
			if err != nil {
				return rep, fmt.Errorf("step %d reconcile: %w", i, err)
			}

			res := StepResult{
				Index: i, RequestID: st.RequestID, At: st.At,
				HandExpected: st.Expect, Reference: expectedRef, Actual: actual,
				Restarted: st.Restart,
			}
			res.Mismatches = compare(st.Expect, expectedRef, actual)
			if len(res.Mismatches) > 0 {
				rep.Passed = false
			}
			if actual.Action == "noop" {
				rep.NoopJustifications = append(rep.NoopJustifications, NoopLine{
					At: st.At, Reason: firstReason(actual.Reasons),
					Detail: noopDetail(firstReason(actual.Reasons)),
				})
			}
			rep.Results = append(rep.Results, res)
		default:
			return rep, fmt.Errorf("step %d: unknown kind %q", i, st.Kind)
		}
	}
	return rep, nil
}

// compare checks the actual result against both the hand-authored fixture
// expectation and the independent reference outcome.
func compare(hand *Expect, ref reference.Outcome, actual ActualTick) []string {
	var ms []string
	add := func(format string, a ...any) { ms = append(ms, fmt.Sprintf(format, a...)) }

	if hand == nil {
		add("fixture declares no hand-authored expectation for this tick")
	} else {
		if actual.HTTPCode != http.StatusOK {
			add("hand: http code %d want 200", actual.HTTPCode)
		}
		if actual.Action != hand.Action {
			add("hand: action %q want %q", actual.Action, hand.Action)
		}
		if actual.Desired != hand.DesiredReplicas {
			add("hand: desired %d want %d", actual.Desired, hand.DesiredReplicas)
		}
		if !sameReasons(actual.Reasons, hand.Reasons) {
			add("hand: reasons %v want %v", actual.Reasons, hand.Reasons)
		}
		if actual.RawDesired != hand.RawDesired {
			add("hand: raw_desired %d want %d", actual.RawDesired, hand.RawDesired)
		}
		if actual.Fresh != hand.Fresh || actual.Stale != hand.Stale || actual.Missing != hand.Missing {
			add("hand: classified fresh/stale/missing = %d/%d/%d want %d/%d/%d",
				actual.Fresh, actual.Stale, actual.Missing, hand.Fresh, hand.Stale, hand.Missing)
		}
		if !floatEq(actual.TotalLoad, hand.TotalLoad) {
			add("hand: total_load %v want %v", actual.TotalLoad, hand.TotalLoad)
		}
	}

	// Independent reference cross-check (not the same code as the service).
	if actual.Action != ref.Action {
		add("reference: action %q want %q", actual.Action, ref.Action)
	}
	if actual.Desired != ref.DesiredReplicas {
		add("reference: desired %d want %d", actual.Desired, ref.DesiredReplicas)
	}
	if !sameReasons(actual.Reasons, ref.Reasons) {
		add("reference: reasons %v want %v", actual.Reasons, ref.Reasons)
	}
	if actual.RawDesired != ref.RawDesired {
		add("reference: raw_desired %d want %d", actual.RawDesired, ref.RawDesired)
	}
	if actual.Fresh != ref.Fresh || actual.Stale != ref.Stale || actual.Missing != ref.Missing {
		add("reference: classified %d/%d/%d want %d/%d/%d",
			actual.Fresh, actual.Stale, actual.Missing, ref.Fresh, ref.Stale, ref.Missing)
	}
	if !floatEq(actual.TotalLoad, ref.TotalLoad) {
		add("reference: total_load %v want %v", actual.TotalLoad, ref.TotalLoad)
	}
	return ms
}

// ---- HTTP helpers -------------------------------------------------------------

func reconcile(client *http.Client, base string, at int64, rid string) (ActualTick, error) {
	code, body, err := postJSON(client, base+"/v1/reconcile", rid, map[string]any{"at": at})
	if err != nil && code == 0 {
		return ActualTick{}, err
	}
	a := ActualTick{HTTPCode: code}
	a.Action, _ = body["action"].(string)
	a.Desired = num(body, "desired_replicas")
	a.FailureClass, _ = body["category"].(string)
	a.FailureDetail, _ = body["error"].(string)
	if rs, ok := body["reasons"].([]any); ok {
		for _, r := range rs {
			if s, ok := r.(string); ok {
				a.Reasons = append(a.Reasons, s)
			}
		}
	}
	if obs, ok := body["observation"].(map[string]any); ok {
		a.RawDesired = num(obs, "raw_desired")
		a.Fresh = num(obs, "fresh_count")
		a.Stale = num(obs, "stale_count")
		a.Missing = num(obs, "missing_count")
		if v, ok := obs["total_load"].(float64); ok {
			a.TotalLoad = v
		}
	}
	if a.FailureClass == "" {
		a.FailureClass, _ = body["failure_class"].(string)
	}
	return a, nil
}

func postJSON(client *http.Client, url, rid string, v any) (int, map[string]any, error) {
	b, _ := json.Marshal(v)
	req, err := http.NewRequest(http.MethodPost, url, bytes.NewReader(b))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if rid != "" {
		req.Header.Set("X-Request-ID", rid)
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	out := map[string]any{}
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out, nil
}

func num(m map[string]any, k string) int {
	switch v := m[k].(type) {
	case float64:
		return int(v)
	case int:
		return v
	}
	return 0
}

func sameReasons(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	// Reason order is part of the contract for scale actions; compare exact.
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func floatEq(a, b float64) bool {
	d := a - b
	if d < 0 {
		d = -d
	}
	return d < 1e-9
}

func firstReason(rs []string) string {
	if len(rs) == 0 {
		return ""
	}
	return rs[0]
}

func noopDetail(reason string) string {
	switch reason {
	case "NO_FRESH_METRICS":
		return "no instance reported within the stale skew; stale data cannot trigger scale-up"
	case "FRESH_FRACTION_LOW":
		return "too few fresh reports; missing instances imputed at target load, uncertainty too high"
	case "WITHIN_TOLERANCE":
		return "average utilisation inside the +/-10% deadband"
	case "WINDOW_PENDING":
		return "scale-down level not observed for the full 60s stable window (or a higher point remains in it)"
	case "ZERO_NO_DEMAND":
		return "fleet at zero and no fresh out-of-band demand signal exists"
	case "ZERO_DEMAND_STALE":
		return "fleet at zero but the only demand signal is older than the stale skew"
	case "MIN_REPLICAS_FLOOR":
		return "computed target below the configured minimum; fleet already at the floor"
	case "MAX_REPLICAS_CAPPED":
		return "computed target above the configured maximum; fleet already at the cap"
	}
	return reason
}

// ---- Process management --------------------------------------------------------

type process struct {
	cmd     *exec.Cmd
	cancel  context.CancelFunc
	logPath string
}

func startBinary(ctx context.Context, bin, configPath string) (*process, error) {
	cctx, cancel := context.WithCancel(ctx)
	logFile, err := os.CreateTemp(filepath.Dir(configPath), "server-*.log")
	if err != nil {
		cancel()
		return nil, err
	}
	cmd := exec.CommandContext(cctx, bin, "-config", configPath)
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	if err := cmd.Start(); err != nil {
		logFile.Close()
		cancel()
		return nil, fmt.Errorf("start %s: %w", bin, err)
	}
	return &process{cmd: cmd, cancel: cancel, logPath: logFile.Name()}, nil
}

func (p *process) kill() {
	if p.cmd != nil && p.cmd.Process != nil {
		_ = p.cmd.Process.Signal(os.Interrupt)
		done := make(chan struct{})
		go func() { _ = p.cmd.Wait(); close(done) }()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			p.cancel()
			<-done
		}
	}
	p.cancel()
}

func (p *process) restart(ctx context.Context, configPath string) error {
	p.kill()
	np, err := startBinary(ctx, p.cmd.Path, configPath)
	if err != nil {
		return err
	}
	*p = *np
	return nil
}

func waitReady(addr string, within time.Duration) error {
	deadline := time.Now().Add(within)
	url := "http://" + addr + "/healthz"
	client := &http.Client{Timeout: time.Second}
	for time.Now().Before(deadline) {
		resp, err := client.Get(url)
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode == 200 && bytes.Contains(body, []byte("ok")) {
				return nil
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	return fmt.Errorf("service at %s not ready within %s", addr, within)
}

func buildBinary(repoDir, workDir string) (string, error) {
	out := filepath.Join(workDir, "replicactl")
	cmd := exec.Command("go", "build", "-o", out, "./app/cmd/replicactl")
	cmd.Dir = repoDir
	cmd.Env = append(os.Environ(), "GOPROXY=off", "CGO_ENABLED=0")
	var errb bytes.Buffer
	cmd.Stderr = &errb
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("go build: %w (%s)", err, errb.String())
	}
	return out, nil
}

func freePort() (int, error) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port, nil
}
