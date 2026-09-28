// Package testlog provides structured, correlated test logging. Every
// line is JSON and carries the run id, test name, step/progress number
// and the input identity (xid/chaddr) the assertion is about, so a
// failure can be traced from a captured datagram to the exact check that
// failed. Unknown/abnormal states are logged at ERROR and never folded
// into a pass.
package testlog

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"sync"
	"testing"
	"time"
)

// Logger writes one JSON object per event.
type Logger struct {
	mu    sync.Mutex
	w     io.Writer
	runID string
	t     *testing.T
}

// New creates a logger writing to stdout (captured by `go test -v`) and,
// when TESTLOG_FILE is set, also to that file.
func New(t *testing.T, runID string) *Logger {
	t.Helper()
	var ws []io.Writer
	ws = append(ws, os.Stdout)
	if f := os.Getenv("TESTLOG_FILE"); f != "" {
		fh, err := os.OpenFile(f, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err == nil {
			t.Cleanup(func() { _ = fh.Close() })
			ws = append(ws, fh)
		}
	}
	return &Logger{w: io.MultiWriter(ws...), runID: runID, t: t}
}

// Entry is one log record.
type Entry struct {
	TS       string         `json:"ts_utc"`
	RunID    string         `json:"run_id"`
	Test     string         `json:"test"`
	Step     int            `json:"step"`
	Phase    string         `json:"phase"`
	XID      string         `json:"xid,omitempty"`
	CHAddr   string         `json:"chaddr,omitempty"`
	Client   string         `json:"client,omitempty"`
	Input    map[string]any `json:"input,omitempty"`
	Verdict  string         `json:"verdict,omitempty"`
	Expected string         `json:"expected,omitempty"`
	Actual   string         `json:"actual,omitempty"`
	Reason   string         `json:"reason,omitempty"`
	Msg      string         `json:"msg"`
}

// Recorder is a per-test step counter.
type Recorder struct {
	lg   *Logger
	step int
}

// For returns a step recorder for a sub-case name.
func (l *Logger) For(_ string) *Recorder {
	return &Recorder{lg: l}
}

func (r *Recorder) emit(phase string, e Entry) {
	r.step++
	e.Step = r.step
	e.Phase = phase
	r.lg.write(e)
}

// Step logs a computation/action step with its inputs.
func (r *Recorder) Step(msg string, kv ...any) {
	r.emit("step", Entry{Msg: msg, Input: kvMap(kv)})
}

// Progress logs a progress marker (percentage-free, meaningful milestones).
func (r *Recorder) Progress(msg string, kv ...any) {
	r.emit("progress", Entry{Msg: msg, Input: kvMap(kv)})
}

// Expect logs an assertion basis before checking.
func (r *Recorder) Expect(expected, basis string, kv ...any) {
	m := kvMap(kv)
	r.emit("expect", Entry{Expected: expected, Msg: basis, Input: m})
}

// Pass records a successful assertion.
func (r *Recorder) Pass(msg string, kv ...any) {
	r.emit("verdict", Entry{Verdict: "PASS", Msg: msg, Input: kvMap(kv)})
}

// Fail records a failed assertion with expected vs actual.
func (r *Recorder) Fail(expected, actual, reason string, kv ...any) {
	r.emit("verdict", Entry{
		Verdict: "FAIL", Expected: expected, Actual: actual, Reason: reason,
		Msg: "assertion failed", Input: kvMap(kv),
	})
}

// Unknown logs an abnormal/unknown state explicitly (never a pass).
func (r *Recorder) Unknown(state string, kv ...any) {
	r.emit("abnormal", Entry{Verdict: "UNKNOWN", Actual: state,
		Msg: "unexpected/unknown state", Input: kvMap(kv)})
}

// Annotate sets identity context applied to subsequent entries.
func (r *Recorder) Annotate(xid, chaddr, client string) RecorderCtx {
	return RecorderCtx{r: r, xid: xid, chaddr: chaddr, client: client}
}

// RecorderCtx carries identity across steps.
type RecorderCtx struct {
	r                   *Recorder
	xid, chaddr, client string
}

func (c RecorderCtx) with(e Entry) Entry {
	e.XID, e.CHAddr, e.Client = c.xid, c.chaddr, c.client
	return e
}

// Step logs under the carried identity.
func (c RecorderCtx) Step(msg string, kv ...any) {
	c.r.emit("step", c.with(Entry{Msg: msg, Input: kvMap(kv)}))
}

// Expect logs an assertion basis under the carried identity.
func (c RecorderCtx) Expect(expected, basis string, kv ...any) {
	c.r.emit("expect", c.with(Entry{Expected: expected, Msg: basis, Input: kvMap(kv)}))
}

// Pass records a successful assertion under the carried identity.
func (c RecorderCtx) Pass(msg string, kv ...any) {
	c.r.emit("verdict", c.with(Entry{Verdict: "PASS", Msg: msg, Input: kvMap(kv)}))
}

func (l *Logger) write(e Entry) {
	if e.TS == "" {
		e.TS = time.Now().UTC().Format(time.RFC3339Nano)
	}
	e.RunID = l.runID
	if e.Test == "" && l.t != nil {
		e.Test = l.t.Name()
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	raw, _ := json.Marshal(e)
	_, _ = fmt.Fprintln(l.w, string(raw))
}

func kvMap(kv []any) map[string]any {
	if len(kv) == 0 {
		return nil
	}
	m := map[string]any{}
	for i := 0; i+1 < len(kv); i += 2 {
		m[fmt.Sprint(kv[i])] = kv[i+1]
	}
	if len(kv)%2 == 1 {
		m["_extra"] = kv[len(kv)-1]
	}
	return m
}
