// Package testlog provides run-scoped, structured test logging. Every test
// binary run gets a RunID; each entry carries the test name, RunID, version
// metadata and a step/verdict so that a failure reproduction can be tied to
// the exact input identity, progress point and decision basis, instead of a
// bare "got != want".
package testlog

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"workbroker/internal/version"
)

var (
	runIDOnce sync.Once
	runID     string
)

// RunID returns the process-wide run identifier, generating one on first use.
// Honor BROKER_TEST_RUN_ID when set (used to correlate CI repro runs).
func RunID() string {
	runIDOnce.Do(func() {
		if v := os.Getenv("BROKER_TEST_RUN_ID"); strings.TrimSpace(v) != "" {
			runID = strings.TrimSpace(v)
			return
		}
		var b [8]byte
		if _, err := rand.Read(b[:]); err != nil {
			runID = "run-random-failed"
			return
		}
		runID = "run-" + hex.EncodeToString(b[:])
	})
	return runID
}

// Logger is a per-test structured line logger.
type Logger struct {
	t       testing.TB
	mu      sync.Mutex
	runID   string
	w       io.Writer
	builder strings.Builder
}

// New attaches a Logger to t. Lines are emitted via t.Logf and, when
// BROKER_TEST_LOG_FILE is set, appended to that file.
func New(t testing.TB) *Logger {
	t.Helper()
	l := &Logger{t: t, runID: RunID(), w: os.Stderr}
	if path := os.Getenv("BROKER_TEST_LOG_FILE"); path != "" {
		f, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err == nil {
			l.w = io.MultiWriter(os.Stderr, f)
			t.Cleanup(func() { _ = f.Close() })
		}
	}
	l.Banner("test start")
	return l
}

// Banner prints run/version identity.
func (l *Logger) Banner(msg string) {
	l.line("BANNER", msg, map[string]any{
		"run_id":         l.runID,
		"test":           l.t.Name(),
		"proto_version":  version.ProtocolVersion,
		"schema_version": version.SchemaVersion,
		"go_wall_clock":  time.Now().UTC().Format(time.RFC3339Nano),
	})
}

// Step records a computation/progress step with structured fields.
func (l *Logger) Step(step string, fields map[string]any) {
	l.line("STEP", step, fields)
}

// Verdict records an assertion basis. ok=false is reported as VERDICT-FAIL but
// does not fail the test by itself (callers use l.Fatalf/require).
func (l *Logger) Verdict(assertion string, ok bool, fields map[string]any) {
	tag := "VERDICT-OK"
	if !ok {
		tag = "VERDICT-FAIL"
	}
	l.line(tag, assertion, fields)
}

// Fatalf logs and fails the test, always including run identity.
func (l *Logger) Fatalf(format string, args ...any) {
	l.line("FATAL", fmt.Sprintf(format, args...), map[string]any{"run_id": l.runID})
	l.t.Fatalf("[%s] %s", l.runID, fmt.Sprintf(format, args...))
}

// Errorf logs and marks the test failed.
func (l *Logger) Errorf(format string, args ...any) {
	l.line("ERROR", fmt.Sprintf(format, args...), map[string]any{"run_id": l.runID})
	l.t.Errorf("[%s] %s", l.runID, fmt.Sprintf(format, args...))
}

func (l *Logger) line(tag, msg string, fields map[string]any) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.builder.Reset()
	fmt.Fprintf(&l.builder, "%s %s run=%s test=%s :: %s",
		time.Now().UTC().Format("15:04:05.000000"), tag, l.runID, l.t.Name(), msg)
	for k, v := range fields {
		fmt.Fprintf(&l.builder, " %s=%v", k, v)
	}
	line := l.builder.String()
	l.t.Log(line)
	if l.w != nil {
		fmt.Fprintln(l.w, line)
	}
}
