// Package logging provides the run-scoped structured logger. Every entry
// carries the run id and a monotonically increasing run sequence number so
// a failure can be replayed from the log alone. Entries are emitted to the
// console as JSON lines and optionally mirrored to a per-run JSONL file.
package logging

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"

	"infraplanner/internal/model"
)

// Event is one structured log entry.
type Event struct {
	Time   string         `json:"time"`
	Run    string         `json:"run"`
	Num    int64          `json:"run_seq"`
	Level  string         `json:"level"`
	Stage  string         `json:"stage"`
	Msg    string         `json:"msg"`
	Fields map[string]any `json:"fields,omitempty"`
}

// Logger is safe for concurrent use.
type Logger struct {
	mu    sync.Mutex
	runID string
	num   int64
	w     io.Writer
	file  *os.File
	echo  bool
}

// New creates a logger writing to stdout and to <dir>/<runID>.jsonl.
func New(runID, dir string, echo bool) (*Logger, error) {
	l := &Logger{runID: runID, w: os.Stdout, echo: echo}
	if dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, err
		}
		p := filepath.Join(dir, runID+".jsonl")
		f, err := os.OpenFile(p, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			return nil, err
		}
		l.file = f
	}
	return l, nil
}

// RunID returns the run id.
func (l *Logger) RunID() string { return l.runID }

func (l *Logger) log(level, stage, msg string, fields map[string]any) {
	l.mu.Lock()
	l.num++
	e := Event{
		Time:   time.Now().UTC().Format(time.RFC3339Nano),
		Run:    l.runID,
		Num:    l.num,
		Level:  level,
		Stage:  stage,
		Msg:    msg,
		Fields: fields,
	}
	b, _ := json.Marshal(e)
	b = append(b, '\n')
	if l.file != nil {
		_, _ = l.file.Write(b)
	}
	if l.echo {
		fmt.Fprint(l.w, string(b))
	}
	l.mu.Unlock()
}

// Info logs an informational event.
func (l *Logger) Info(stage, msg string, fields map[string]any) {
	l.log("info", stage, msg, fields)
}

// Warn logs a warning.
func (l *Logger) Warn(stage, msg string, fields map[string]any) {
	l.log("warn", stage, msg, fields)
}

// Error logs an error event with category/code.
func (l *Logger) Error(stage, msg string, err error, fields map[string]any) {
	if fields == nil {
		fields = map[string]any{}
	}
	if me, ok := model.AsError(err); ok {
		fields["error_cat"] = me.Category
		fields["error_code"] = me.Code
	}
	fields["error"] = err.Error()
	l.log("error", stage, msg, fields)
}

// Close flushes the file.
func (l *Logger) Close() error {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file != nil {
		return l.file.Close()
	}
	return nil
}

// FilePath reports the mirror file (empty when file logging is disabled).
func (l *Logger) FilePath() string {
	if l.file == nil {
		return ""
	}
	return l.file.Name()
}
