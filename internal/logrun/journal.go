// Package logrun maintains one test/run journal: every decision-relevant
// intermediate state (local state recorded, channel capture, marker send/
// receive, round completion/abort, transfer accept/reject with reason) is
// appended as a protocol.Event with a run id and a monotonic sequence number,
// and at Finalize time the journal plus the assertion verdicts are written to
// a JSON document under the configured log directory.
//
// The document is intentionally self-contained so a failing run can be
// replayed from it: it includes the scenario name, the exact step list,
// expected versus observed numbers and the human-readable judgment reason.
package logrun

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"clsnap/internal/apperr"
	"clsnap/internal/protocol"
	"clsnap/internal/store"
)

// Verdict records one assertion made about a run.
type Verdict struct {
	Name     string `json:"name"`
	Passed   bool   `json:"passed"`
	Reason   string `json:"reason"`
	Expected any    `json:"expected,omitempty"`
	Observed any    `json:"observed,omitempty"`
	Category string `json:"error_category,omitempty"` // apperr kind when failing
}

// Report is the replayable artifact of one run.
type Report struct {
	RunID       string           `json:"run_id"`
	Scenario    string           `json:"scenario"`
	StartedAt   string           `json:"started_at"`
	FinishedAt  string           `json:"finished_at"`
	Events      []protocol.Event `json:"events"`
	Verdicts    []Verdict        `json:"verdicts"`
	Passed      bool             `json:"passed"`
	Summary     string           `json:"summary"`
}

// Journal records events into an EventStore and buffers a report for disk.
type Journal struct {
	mu        sync.Mutex
	store     store.EventStore
	dir       string
	runID     string
	scenario  string
	startedAt time.Time
	events    []protocol.Event
	verdicts  []Verdict
	seq       uint64
}

// New creates a journal. dir may be "" to skip file output (still logs to the
// event store). runID should be unique; callers pass scenario+timestamp.
func New(s store.EventStore, dir, runID, scenario string) *Journal {
	return &Journal{
		store:     s,
		dir:       dir,
		runID:     runID,
		scenario:  scenario,
		startedAt: time.Now().UTC(),
	}
}

// RunID returns the run id.
func (j *Journal) RunID() string { return j.runID }

// Event appends one event, stamping run id / time / seq if unset.
func (j *Journal) Event(ctx context.Context, ev protocol.Event) protocol.Event {
	j.mu.Lock()
	ev.RunID = j.runID
	if ev.At == "" {
		ev.At = time.Now().UTC().Format(time.RFC3339Nano)
	}
	j.seq++
	ev.Seq = j.seq
	j.events = append(j.events, ev)
	j.mu.Unlock()
	if j.store != nil {
		_ = j.store.AppendEvent(ctx, ev) // best effort; report still written
	}
	return ev
}

// Check records a verdict. expected/observed are kept verbatim in JSON.
func (j *Journal) Check(name string, passed bool, reason string, expected, observed any) {
	j.mu.Lock()
	defer j.mu.Unlock()
	v := Verdict{Name: name, Passed: passed, Reason: reason, Expected: expected, Observed: observed}
	if !passed {
		if ae, ok := observed.(*apperr.Error); ok {
			v.Category = string(ae.Kind)
			v.Observed = ae.Error()
		}
	}
	j.verdicts = append(j.verdicts, v)
}

// CheckErrorClass asserts that err is a structured error of the given kind.
func (j *Journal) CheckErrorClass(name string, want apperr.Kind, err error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	v := Verdict{Name: name, Expected: string(want)}
	if err == nil {
		v.Passed = false
		v.Reason = "expected error of kind " + string(want) + " but call succeeded"
	} else if ae, ok := apperr.As(err); ok {
		v.Observed = map[string]string{"kind": string(ae.Kind), "code": ae.Code, "message": ae.Msg}
		v.Passed = ae.Kind == want
		if !v.Passed {
			v.Reason = fmt.Sprintf("error kind mismatch: want %s got %s (%s)", want, ae.Kind, ae.Code)
		} else {
			v.Reason = "failed with expected category " + string(want) + "/" + ae.Code
		}
		v.Category = string(ae.Kind)
	} else {
		v.Passed = false
		v.Observed = err.Error()
		v.Reason = "error is not a structured apperr.Error"
	}
	j.verdicts = append(j.verdicts, v)
}

// Finalize computes the overall result and writes the JSON report.
func (j *Journal) Finalize(ctx context.Context, summary string) (Report, string, error) {
	j.mu.Lock()
	passed := true
	for _, v := range j.verdicts {
		if !v.Passed {
			passed = false
			break
		}
	}
	rep := Report{
		RunID:      j.runID,
		Scenario:   j.scenario,
		StartedAt:  j.startedAt.Format(time.RFC3339Nano),
		FinishedAt: time.Now().UTC().Format(time.RFC3339Nano),
		Events:     append([]protocol.Event(nil), j.events...),
		Verdicts:   append([]Verdict(nil), j.verdicts...),
		Passed:     passed,
		Summary:    summary,
	}
	dir := j.dir
	j.mu.Unlock()

	path := ""
	if dir != "" {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return rep, "", apperr.Failure(apperr.CodeStoreIO, "Journal.Finalize", "mkdir logs", err)
		}
		path = filepath.Join(dir, j.runID+".json")
		b, err := json.MarshalIndent(rep, "", "  ")
		if err != nil {
			return rep, "", apperr.Failure(apperr.CodeFailure, "Journal.Finalize", "marshal report", err)
		}
		if err := os.WriteFile(path, b, 0o644); err != nil {
			return rep, "", apperr.Failure(apperr.CodeStoreIO, "Journal.Finalize", "write report", err)
		}
	}
	return rep, path, nil
}

// LoadReport reads a finalized report from disk (used by clsnapctl replay).
func LoadReport(path string) (Report, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return Report{}, apperr.Inputf(apperr.CodeUnknownRun, "report file not found: %s", path)
		}
		return Report{}, apperr.Failure(apperr.CodeStoreIO, "LoadReport", "read", err)
	}
	var rep Report
	if err := json.Unmarshal(b, &rep); err != nil {
		return Report{}, apperr.Failure(apperr.CodeFailure, "LoadReport", "decode", err)
	}
	return rep, nil
}
