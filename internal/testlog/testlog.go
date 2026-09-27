// Package testlog records structured, replayable test evidence: a run id,
// key intermediate states and the reason for each assertion outcome. Logs
// are written as JSON files (testdata/runs by default) and, when a *store.Store
// is supplied, also into the run_logs table exposed at GET /v1/runs.
package testlog

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"testing"
	"time"
)

// Step is one recorded intermediate state or check.
type Step struct {
	At     string `json:"at"`
	Title  string `json:"title"`
	Detail any    `json:"detail,omitempty"`
	// Outcome: "info" | "pass" | "fail".
	Outcome string `json:"outcome"`
	// FailureClass: input_error|state_conflict|resource_exhausted|
	// computation_failed|unavailable (only for outcome fail).
	FailureClass string `json:"failure_class,omitempty"`
	Reason       string `json:"reason,omitempty"`
}

// Logger accumulates a run's evidence.
type Logger struct {
	mu        sync.Mutex
	t         *testing.T
	RunID     string `json:"run_id"`
	TestName  string `json:"test_name"`
	StartedAt string `json:"started_at"`
	Steps     []Step `json:"steps"`
	Result    string `json:"result"` // running|passed|failed
}

// New creates a run logger with a fixed, human-quotable run id.
// Format: run-<UTC YYYYMMDDTHHMMSS>-<test-slug>.
func New(t *testing.T, testName string) *Logger {
	t.Helper()
	slug := testName
	for i := 0; i < len(slug); i++ {
		c := slug[i]
		switch {
		case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9', c == '-':
		default:
			slug = slug[:i] + "-" + slug[i+1:]
		}
	}
	runID := fmt.Sprintf("run-%s-%s", time.Now().UTC().Format("20060102T150405"), slug)
	return &Logger{
		t: t, RunID: runID, TestName: testName,
		StartedAt: time.Now().UTC().Format(time.RFC3339Nano),
		Result:    "running",
	}
}

// Info records an intermediate state.
func (l *Logger) Info(title string, detail any) {
	l.add(Step{At: time.Now().UTC().Format(time.RFC3339Nano), Title: title, Detail: detail, Outcome: "info"})
}

// Pass records a successful assertion and its reason.
func (l *Logger) Pass(title, reason string, detail any) {
	l.add(Step{At: time.Now().UTC().Format(time.RFC3339Nano), Title: title, Detail: detail,
		Outcome: "pass", Reason: reason})
}

// Failf records a failed assertion with its failure class and reason, then
// fails the owning test. Use this instead of t.Fatalf so evidence survives.
func (l *Logger) Failf(class, title, format string, args ...any) {
	reason := fmt.Sprintf(format, args...)
	l.add(Step{At: time.Now().UTC().Format(time.RFC3339Nano), Title: title,
		Outcome: "fail", FailureClass: class, Reason: reason})
	l.Finish(false)
	l.t.Fatalf("[%s] %s: %s", class, title, reason)
}

func (l *Logger) add(s Step) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.Steps = append(l.Steps, s)
}

// Finish marks the overall result and flushes the JSON file.
func (l *Logger) Finish(passed bool) string {
	l.mu.Lock()
	defer l.mu.Unlock()
	if passed {
		l.Result = "passed"
	} else {
		l.Result = "failed"
	}
	dir := os.Getenv("FLEXHASH_TEST_LOG_DIR")
	if dir == "" {
		dir = "testdata/runs"
	}
	_ = os.MkdirAll(dir, 0o755)
	path := filepath.Join(dir, l.RunID+".json")
	// Steps already carry timestamps; deterministic order for file output.
	sort.SliceStable(l.Steps, func(i, j int) bool { return i < j })
	b, _ := json.MarshalIndent(l, "", "  ")
	if err := os.WriteFile(path, b, 0o644); err != nil {
		l.t.Logf("testlog: cannot write %s: %v", path, err)
	}
	l.t.Logf("testlog: run %s -> %s (%d steps, result=%s)",
		l.RunID, path, len(l.Steps), l.Result)
	return path
}
