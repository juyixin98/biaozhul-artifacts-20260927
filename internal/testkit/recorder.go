// Package testkit provides shared test utilities: a replay-oriented run
// recorder that persists run ids, intermediate states and the human rationale
// behind each assertion to testlogs/runs.jsonl.
package testkit

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"
)

// repoRoot returns the repository root using this source file's compiled-in
// path, so replay logs land in <repo>/testlogs regardless of the package
// working directory `go test` runs the binary in.
func repoRoot() string {
	_, file, _, _ := runtime.Caller(0)
	// file = <root>/internal/testkit/recorder.go
	return filepath.Dir(filepath.Dir(filepath.Dir(file)))
}

type Recorder struct {
	t      *testing.T
	mu     sync.Mutex
	runID  string
	f      *os.File
	seq    int
	closed bool
}

// NewRecorder opens <repo>/testlogs/<name>.jsonl.
func NewRecorder(t *testing.T, name string) *Recorder {
	t.Helper()
	dir := filepath.Join(repoRoot(), "testlogs")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("create testlogs: %v", err)
	}
	runID := name + "-" + timestamp()
	f, err := os.OpenFile(filepath.Join(dir, name+".jsonl"),
		os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
	if err != nil {
		t.Fatalf("open run log: %v", err)
	}
	r := &Recorder{t: t, runID: runID, f: f}
	r.Event("run_start", map[string]any{"test": t.Name()})
	return r
}

func (r *Recorder) RunID() string { return r.runID }

// Step records one intermediate state and the reason why it matters.
func (r *Recorder) Step(stage string, state any, reason string, fields ...any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seq++
	rec := map[string]any{
		"run_id": r.runID, "test": r.t.Name(), "seq": r.seq,
		"stage": stage, "state": state, "reason": reason,
	}
	for i := 0; i+1 < len(fields); i += 2 {
		rec[fields[i].(string)] = fields[i+1]
	}
	r.write(rec)
	r.t.Logf("[%s] step %d (%s): %s | %s", r.runID, r.seq, stage, reason, compact(state))
}

// Event records an unstructured diagnostic event.
func (r *Recorder) Event(event string, fields map[string]any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	rec := map[string]any{"run_id": r.runID, "test": r.t.Name(), "event": event}
	for k, v := range fields {
		rec[k] = v
	}
	r.write(rec)
}

// Check records the outcome of an assertion and its rationale.
func (r *Recorder) Check(label string, ok bool, reason string, got, want any) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.seq++
	rec := map[string]any{
		"run_id": r.runID, "test": r.t.Name(), "seq": r.seq,
		"check": label, "pass": ok, "reason": reason, "got": got, "want": want,
	}
	r.write(rec)
	r.t.Logf("[%s] CHECK %-40s pass=%v (%s)", r.runID, label, ok, reason)
}

func (r *Recorder) write(rec map[string]any) {
	b, _ := json.Marshal(rec)
	if _, err := r.f.Write(append(b, '\n')); err != nil {
		r.t.Logf("recorder write failed: %v", err)
	}
}

func (r *Recorder) Close() {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return
	}
	r.closed = true
	r.write(map[string]any{
		"run_id": r.runID, "test": r.t.Name(), "event": "run_end", "steps": r.seq,
	})
	_ = r.f.Close()
}

func compact(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "<unmarshalable>"
	}
	s := string(b)
	if len(s) > 400 {
		return s[:400] + "...(truncated)"
	}
	return s
}

func timestamp() string {
	return time.Now().UTC().Format("20060102T150405.000000000")
}
