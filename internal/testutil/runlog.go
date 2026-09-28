// Package testutil provides the run-logging helpers shared by every test.
// Each test execution gets a stable RUN-<n> identifier; intermediate states and
// judgment reasons are written both to t.Logf and to
// testdata/runs/<package>/run-<id>.log so a failure can be reproduced.
package testutil

import (
	"fmt"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

var runCounter int64

// RunLogger accumulates replay-grade lines for one test.
type RunLogger struct {
	T     *testing.T
	RunID string
	Start time.Time
	lines []string
	path  string
}

// NewRun creates a logger with a process-unique run number.
func NewRun(t *testing.T, component string) *RunLogger {
	n := atomic.AddInt64(&runCounter, 1)
	rid := fmt.Sprintf("RUN-%04d", n)
	dir := filepath.Join("..", "..", "testdata", "runs", component)
	_ = os.MkdirAll(dir, 0o755)
	path := filepath.Join(dir, "run-"+rid+".log")
	l := &RunLogger{T: t, RunID: rid, Start: time.Now().UTC(), path: path}
	l.Infof("=== %s :: %s started %s ===", rid, t.Name(), l.Start.Format(time.RFC3339Nano))
	return l
}

// Infof records one line.
func (l *RunLogger) Infof(format string, args ...any) {
	line := fmt.Sprintf("[%s] %s", l.RunID, fmt.Sprintf(format, args...))
	l.lines = append(l.lines, line)
	l.T.Log(line)
}

// Step records a packet evaluation: index, key inputs, verdict and reason.
func (l *RunLogger) Step(i int, summary, verdict string, accepted bool, detail string) {
	l.Infof("step=%d %s -> accepted=%v verdict=%s detail=%s", i, summary, accepted, verdict, detail)
}

// Finish writes the file and reports its path.
func (l *RunLogger) Finish(passed bool) {
	status := "PASS"
	if !passed {
		status = "FAIL"
	}
	l.Infof("=== %s %s duration=%s log=%s ===", l.RunID, status, time.Since(l.Start), l.path)
	content := ""
	for _, ln := range l.lines {
		content += ln + "\n"
	}
	if err := os.WriteFile(l.path, []byte(content), 0o644); err != nil {
		l.T.Logf("write run log: %v", err)
	}
	l.T.Logf("replay log written: %s", l.path)
}
