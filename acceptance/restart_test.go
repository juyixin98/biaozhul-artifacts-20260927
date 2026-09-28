package acceptance_test

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"testing"
	"time"
)

// binaryClient talks to a real rollctl process over HTTP. This is the strongest
// restart fixture: the entire process dies and is relaunched against the same
// SQLite file.
type binaryClient struct {
	t      *testing.T
	base   string
	hc     *http.Client
	dbPath string
	bin    string
	cmd    *exec.Cmd
	cancel context.CancelFunc
	logBuf *bytes.Buffer
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("free port: %v", err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	l.Close()
	return port
}

func buildBinary(t *testing.T) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("binary restart test is unix-only")
	}
	dir := t.TempDir()
	bin := filepath.Join(dir, "rollctl")
	// go test runs with the package dir as cwd; resolve the module root (one
	// level up from acceptance/) so `go build` finds ./cmd/rollctl.
	_, thisFile, _, _ := runtime.Caller(0)
	root := filepath.Dir(filepath.Dir(thisFile))
	pkgPath := filepath.Join(root, "cmd", "rollctl")
	cmd := exec.Command("go", "build", "-o", bin, pkgPath)
	var out bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &out
	if err := cmd.Run(); err != nil {
		t.Fatalf("build binary: %v\n%s", err, out.String())
	}
	return bin
}

func startBinary(t *testing.T, bin, dbPath string, port, capacity int) *binaryClient {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	logBuf := &bytes.Buffer{}
	cmd := exec.CommandContext(ctx, bin,
		"-manual",
		"-http", "127.0.0.1:"+strconv.Itoa(port),
		"-db", dbPath,
		"-sim-capacity", strconv.Itoa(capacity),
	)
	cmd.Stdout = logBuf
	cmd.Stderr = logBuf
	if err := cmd.Start(); err != nil {
		cancel()
		t.Fatalf("start binary: %v\n%s", err, logBuf.String())
	}
	bc := &binaryClient{t: t, base: "http://127.0.0.1:" + strconv.Itoa(port),
		hc: &http.Client{Timeout: 5 * time.Second}, dbPath: dbPath,
		bin: bin, cmd: cmd, cancel: cancel, logBuf: logBuf}
	bc.waitReady(30)
	t.Cleanup(func() {
		bc.cancel()
		_ = cmd.Wait()
	})
	return bc
}

func (bc *binaryClient) waitReady(tries int) {
	for i := 0; i < tries; i++ {
		resp, err := bc.hc.Get(bc.base + "/healthz")
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			if resp.StatusCode == 200 {
				var h map[string]any
				if json.Unmarshal(body, &h) == nil {
					return
				}
			}
		}
		time.Sleep(100 * time.Millisecond)
	}
	bc.t.Fatalf("binary never became ready\n%s", bc.logBuf.String())
}

// kill terminates the process but leaves the DB file intact.
func (bc *binaryClient) kill() {
	bc.cancel()
	if bc.cmd.Process != nil {
		_ = bc.cmd.Process.Signal(os.Interrupt)
	}
	done := make(chan error, 1)
	go func() { done <- bc.cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		_ = bc.cmd.Process.Kill()
		<-done
	}
}

func (bc *binaryClient) restart(port, capacity int) {
	bc.t.Helper()
	bc.kill()
	nb := startBinary(bc.t, bc.bin, bc.dbPath, port, capacity)
	bc.base, bc.hc, bc.cmd, bc.cancel, bc.logBuf = nb.base, nb.hc, nb.cmd, nb.cancel, nb.logBuf
}

func (bc *binaryClient) req(method, path string, body any, rid string) (int, map[string]any) {
	bc.t.Helper()
	var rdr io.Reader
	if body != nil {
		raw, _ := json.Marshal(body)
		rdr = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, bc.base+path, rdr)
	if err != nil {
		bc.t.Fatalf("new request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	if rid != "" {
		req.Header.Set("X-Request-Id", rid)
	}
	resp, err := bc.hc.Do(req)
	if err != nil {
		bc.t.Fatalf("http %s %s: %v", method, path, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	var out map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &out)
	}
	return resp.StatusCode, out
}

func (bc *binaryClient) tick() {
	if code, body := bc.req("POST", "/admin/tick", nil, ""); code != 200 {
		bc.t.Fatalf("tick: %d %v", code, body)
	}
}

func (bc *binaryClient) ticks(n int) {
	for i := 0; i < n; i++ {
		bc.tick()
	}
}

func (bc *binaryClient) status(name string) map[string]any {
	code, body := bc.req("GET", "/api/v1/workloads/"+name, nil, "")
	if code != 200 {
		bc.t.Fatalf("status: %d %v", code, body)
	}
	return body
}

func (bc *binaryClient) release(id string) map[string]any {
	code, body := bc.req("GET", "/api/v1/releases/"+id, nil, "")
	if code != 200 {
		bc.t.Fatalf("release: %d %v", code, body)
	}
	return body
}

// TestBinaryRestartMidRollout runs a real process: bootstrap, start a rollout,
// kill mid-flight, relaunch on the same SQLite file, and verify the rollout
// resumes and converges with the same release id and version.
func TestBinaryRestartMidRollout(t *testing.T) {
	bin := buildBinary(t)
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "data", "rollctl.db")
	port1, port2 := freePort(t), freePort(t)
	bc := startBinary(t, bin, dbPath, port1, 16)

	// Install fixtures and a workload through the public/admin API.
	mustCode := func(code int, body map[string]any) {
		t.Helper()
		if code != 200 && code != 201 {
			t.Fatalf("unexpected http code %d: %v", code, body)
		}
	}
	mustCode(bc.req("PUT", "/admin/simulator/workloads/svc/revisions/v1/behavior",
		map[string]any{"mode": "normal", "readyDelayTicks": 1}, ""))
	mustCode(bc.req("PUT", "/admin/simulator/workloads/svc/revisions/v2/behavior",
		map[string]any{"mode": "normal", "readyDelayTicks": 1}, ""))

	pol := map[string]any{
		"maxSurge": 1, "maxUnavailable": 0, "readyThresholdTicks": 2,
		"deadlineTicks": 60, "maxStartFailures": 0,
	}
	mustCode(bc.req("POST", "/api/v1/workloads",
		map[string]any{"name": "svc", "replicas": 3, "revision": "v1", "policy": pol}, "req-seed"))

	// Settle bootstrap.
	for i := 0; i < 60; i++ {
		bc.tick()
		s := bc.status("svc")
		if intField(s, "live") == 3 && intField(s, "available") == 3 {
			break
		}
	}
	s := bc.status("svc")
	if intField(s, "available") != 3 {
		t.Fatalf("bootstrap did not settle: %v", s)
	}

	// Start rollout and advance a few ticks: must be mid-flight.
	code, rel := bc.req("POST", "/api/v1/workloads/svc/releases",
		map[string]any{"revision": "v2"}, "req-rollout-7")
	if code != 201 {
		t.Fatalf("release create: %d %v", code, rel)
	}
	relID := strField(rel, "id")
	bc.ticks(4)
	mid := bc.release(relID)
	if strField(mid, "state") != "active" {
		t.Fatalf("expected release active before kill, got %s", strField(mid, "state"))
	}
	midStatus := bc.status("svc")
	// Capture independent expectations (oracle) at the kill boundary.
	o := newOracle(3, 1, 0)
	o.check(t, 999, midStatus)
	preLive, preAvail := intField(midStatus, "live"), intField(midStatus, "available")

	// Hard restart on the same file, new port, new process.
	bc.restart(port2, 16)

	// Health reports the persisted tick continuing, not resetting.
	code, health := bc.req("GET", "/healthz", nil, "")
	if code != 200 {
		t.Fatalf("health: %d", code)
	}
	// We advanced: 1 create + bootstrap ticks + 1 release-create + 4 rollout
	// ticks, and only admin ticks persist. The exact value is internal, but the
	// clock must strictly be past the 4 post-release ticks and continue
	// monotonically after restart (verified by a further tick below).
	tickBefore := int64(health["tick"].(float64))
	if tickBefore < 5 {
		t.Fatalf("persisted tick unexpectedly small after restart: %v", tickBefore)
	}
	bc.tick()
	_, health2 := bc.req("GET", "/healthz", nil, "")
	if int64(health2["tick"].(float64)) != tickBefore+1 {
		t.Fatalf("logical clock did not continue after restart: %d -> %v", tickBefore, health2["tick"])
	}

	post := bc.status("svc")
	if intField(post, "live") != preLive || intField(post, "available") != preAvail {
		t.Fatalf("fleet changed across process restart: pre live=%d avail=%d; post live=%d avail=%d",
			preLive, preAvail, intField(post, "live"), intField(post, "available"))
	}
	// Same release row is still the active one (history preserved).
	postRel := bc.release(relID)
	if strField(postRel, "state") != "active" || strField(postRel, "requestId") != "req-rollout-7" {
		t.Fatalf("release not resumed identically after restart: %v", postRel)
	}

	// Drive to completion with per-step oracle.
	for step := 1; step <= 120; step++ {
		bc.tick()
		r := bc.release(relID)
		st := strField(r, "state")
		if st == "active" || st == "pending" {
			o.check(t, step, bc.status("svc"))
		}
		if st == "succeeded" {
			break
		}
		if st == "failed" {
			t.Fatalf("rollout failed after restart: %s", strField(r, "failMessage"))
		}
		if step == 120 {
			t.Fatal("rollout did not converge after restart")
		}
	}
	final := bc.status("svc")
	wl, _ := final["workload"].(map[string]any)
	if strField(wl, "currentRevision") != "v2" {
		t.Fatalf("final revision = %q, want v2", strField(wl, "currentRevision"))
	}
	if intField(final, "live") != 3 || intField(final, "available") != 3 {
		t.Fatalf("final fleet live=%d available=%d, want 3/3", intField(final, "live"), intField(final, "available"))
	}

	// History is still intact after a full process restart.
	code, list := bc.req("GET", "/api/v1/workloads/svc/releases", nil, "")
	if code != 200 {
		t.Fatalf("list: %d %v", code, list)
	}
	rels, _ := list["releases"].([]any)
	if len(rels) != 2 {
		t.Fatalf("history after restart = %d rows, want 2 (bootstrap + rollout)", len(rels))
	}

	// Persisted log/result explainability: the failed-or-succeeded events keep
	// request ids after restart.
	code, ev := bc.req("GET", "/api/v1/workloads/svc/events", nil, "")
	if code != 200 {
		t.Fatalf("events: %d %v", code, ev)
	}
	events, _ := ev["events"].([]any)
	sawCorrelated := false
	for _, e := range events {
		em, _ := e.(map[string]any)
		if strField(em, "requestId") == "req-rollout-7" {
			sawCorrelated = true
		}
	}
	if !sawCorrelated {
		t.Fatal("events lost request correlation across restart")
	}
}

// TestBinaryPersistenceDBFile simply asserts the DB file exists after a run
// (SQLite, not in-memory state).
func TestBinaryPersistenceDBFile(t *testing.T) {
	bin := buildBinary(t)
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "state.db")
	bc := startBinary(t, bin, dbPath, freePort(t), 8)
	bc.req("PUT", "/admin/simulator/workloads/z/revisions/v1/behavior",
		map[string]any{"mode": "normal"}, "")
	bc.req("POST", "/api/v1/workloads",
		map[string]any{"name": "z", "replicas": 1, "revision": "v1",
			"policy": map[string]any{"maxSurge": 1, "maxUnavailable": 0, "readyThresholdTicks": 1, "deadlineTicks": 10}}, "")
	bc.ticks(5)
	bc.kill()
	if _, err := os.Stat(dbPath); err != nil {
		t.Fatalf("sqlite file not persisted: %v", err)
	}
	// WAL/sidecars may exist; at minimum the main file must be non-empty.
	fi, err := os.Stat(dbPath)
	if err != nil || fi.Size() == 0 {
		t.Fatalf("sqlite file empty: size=%d err=%v", fi.Size(), err)
	}
}
