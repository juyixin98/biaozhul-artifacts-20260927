package runlog

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"admission/internal/model"
)

func readFile(t *testing.T, p string) string {
	t.Helper()
	b, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestLogger_AppendAndReplay(t *testing.T) {
	dir := t.TempDir()
	l, err := Open(dir)
	if err != nil {
		t.Fatal(err)
	}
	for i, decision := range []model.Decision{model.DecisionAllowed, model.DecisionDenied} {
		line := Line{RunID: "run-X-00000" + string(rune('1'+i)), Decision: decision,
			Reason: model.ReasonTimeout, Steps: []model.Step{{Plugin: "p", Order: 1}}}
		if err := l.Append(line); err != nil {
			t.Fatal(err)
		}
	}
	if err := l.Close(); err != nil {
		t.Fatal(err)
	}
	data := readFile(t, filepath.Join(dir, "admission-runs.jsonl"))
	lines := strings.Split(strings.TrimSpace(data), "\n")
	if len(lines) != 2 {
		t.Fatalf("want 2 lines, got %d: %s", len(lines), data)
	}
	if !strings.Contains(lines[0], "\"runId\"") || !strings.Contains(lines[1], "timeout") {
		t.Fatalf("replay content missing: %s", data)
	}
}

func TestNilLoggerIsNoOp(t *testing.T) {
	var l *Logger
	if err := l.Append(Line{}); err != nil {
		t.Fatalf("nil append: %v", err)
	}
	if err := l.Close(); err != nil {
		t.Fatalf("nil close: %v", err)
	}
}

func TestOpen_EmptyDirReturnsNil(t *testing.T) {
	l, err := Open("")
	if err != nil || l != nil {
		t.Fatalf("want nil logger, got %v err=%v", l, err)
	}
}
