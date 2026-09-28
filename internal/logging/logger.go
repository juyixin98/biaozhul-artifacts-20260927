// Package logging provides a small structured logger used across the service.
//
// Every log line carries:
//
//   - service/version/commit  — which binary produced it;
//   - run_id                  — a per-process identity (correlates one run);
//   - request_id              — per HTTP request / per reconcile tick;
//   - plan_id / instance_id   — decision context when applicable.
//
// Unknown or error states are always logged at warn/error with an explicit
// "status=failed" field; callers are forbidden from logging failures as
// "status=ok" (the level-typed helpers enforce this convention structurally).
package logging

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"opp284/placement/internal/version"
)

// Level is a log severity.
type Level int

// Supported levels.
const (
	LevelDebug Level = iota
	LevelInfo
	LevelWarn
	LevelError
)

// ParseLevel parses debug|info|warn|error.
func ParseLevel(s string) (Level, error) {
	switch strings.ToLower(s) {
	case "", "info":
		return LevelInfo, nil
	case "debug":
		return LevelDebug, nil
	case "warn", "warning":
		return LevelWarn, nil
	case "error":
		return LevelError, nil
	default:
		return LevelInfo, fmt.Errorf("unknown log level %q", s)
	}
}

func (l Level) String() string {
	switch l {
	case LevelDebug:
		return "debug"
	case LevelInfo:
		return "info"
	case LevelWarn:
		return "warn"
	default:
		return "error"
	}
}

// Logger is safe for concurrent use.
type Logger struct {
	mu      sync.Mutex
	out     io.Writer
	level   Level
	human   bool
	runID   string
	fields  map[string]any
}

// New builds a logger with a fixed run identity.
func New(out io.Writer, level Level, human bool, runID string) *Logger {
	if out == nil {
		out = os.Stderr
	}
	return &Logger{
		out:    out,
		level:  level,
		human:  human,
		runID:  runID,
		fields: map[string]any{"service": "placement", "version": version.Version, "commit": version.Commit},
	}
}

// With returns a child logger carrying extra persistent fields.
func (l *Logger) With(fields map[string]any) *Logger {
	c := &Logger{
		out: l.out, level: l.level, human: l.human, runID: l.runID,
		fields: make(map[string]any, len(l.fields)+len(fields)),
	}
	for k, v := range l.fields {
		c.fields[k] = v
	}
	for k, v := range fields {
		c.fields[k] = v
	}
	return c
}

// RunID returns the run identity of this logger.
func (l *Logger) RunID() string { return l.runID }

func (l *Logger) log(level Level, msg string, status string, fields map[string]any) {
	if level < l.level {
		return
	}
	all := make(map[string]any, len(l.fields)+len(fields)+4)
	for k, v := range l.fields {
		all[k] = v
	}
	for k, v := range fields {
		all[k] = v
	}
	all["ts"] = time.Now().UTC().Format(time.RFC3339Nano)
	all["level"] = level.String()
	all["run_id"] = l.runID
	if status != "" {
		all["status"] = status
	}
	all["msg"] = msg

	l.mu.Lock()
	defer l.mu.Unlock()
	if l.human {
		fmt.Fprintln(l.out, renderLogfmt(all))
	} else {
		b, _ := json.Marshal(all)
		fmt.Fprintln(l.out, string(b))
	}
}

func renderLogfmt(m map[string]any) string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	// Stable-ish ordering: ts, level, run_id, msg, status first then alpha.
	front := []string{"ts", "level", "run_id", "msg", "status"}
	ordered := make([]string, 0, len(keys))
	used := map[string]bool{}
	for _, k := range front {
		if _, ok := m[k]; ok {
			ordered = append(ordered, k)
			used[k] = true
		}
	}
	rest := keys[:0]
	for _, k := range keys {
		if !used[k] {
			rest = append(rest, k)
		}
	}
	sort.Strings(rest)
	ordered = append(ordered, rest...)
	parts := make([]string, 0, len(ordered))
	for _, k := range ordered {
		parts = append(parts, k+"="+fmt.Sprintf("%v", m[k]))
	}
	return strings.Join(parts, " ")
}

// Debug/Info log neutral or successful events.
func (l *Logger) Debug(msg string, fields map[string]any) { l.log(LevelDebug, msg, "", fields) }
func (l *Logger) Info(msg string, fields map[string]any)  { l.log(LevelInfo, msg, "", fields) }

// OK logs a successful completion explicitly with status=ok.
func (l *Logger) OK(msg string, fields map[string]any) { l.log(LevelInfo, msg, "ok", fields) }

// Warn logs a degraded but handled state with status=degraded.
func (l *Logger) Warn(msg string, fields map[string]any) { l.log(LevelWarn, msg, "degraded", fields) }

// Fail logs a failure/error with status=failed. Failures must never be
// reported through OK/Info.
func (l *Logger) Fail(msg string, fields map[string]any) { l.log(LevelError, msg, "failed", fields) }
