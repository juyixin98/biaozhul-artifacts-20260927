package logx_test

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"placer/internal/logx"
	"placer/internal/version"
)

// TestLogger_CorrelatesRunAndVersion verifies each log line carries the
// run identity, component and version so a run can be traced end-to-end.
func TestLogger_CorrelatesRunAndVersion(t *testing.T) {
	var buf bytes.Buffer
	log := logx.New(&buf, logx.LevelInfo).With("scheduler", "run-log-77")
	log.Info("plan_done", map[string]any{"feasible": true})
	log.Error("conflict", errStub{"boom"}, map[string]any{"n": 3})

	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected 2 log lines, got %d", len(lines))
	}
	var info map[string]any
	if err := json.Unmarshal([]byte(lines[0]), &info); err != nil {
		t.Fatal(err)
	}
	if info["run_id"] != "run-log-77" {
		t.Fatalf("run_id missing: %+v", info["run_id"])
	}
	if info["component"] != "scheduler" {
		t.Fatalf("component missing: %+v", info["component"])
	}
	if info["version"] != version.String() {
		t.Fatalf("version missing: %+v", info["version"])
	}
	if info["event"] != "plan_done" || info["feasible"] != true {
		t.Fatalf("payload wrong: %+v", info)
	}
	if info["level"] != "info" {
		t.Fatalf("level wrong: %+v", info["level"])
	}

	var errLine map[string]any
	if err := json.Unmarshal([]byte(lines[1]), &errLine); err != nil {
		t.Fatal(err)
	}
	if errLine["level"] != "error" || errLine["err"] != "boom" {
		t.Fatalf("error line wrong: %+v", errLine)
	}
}

// TestLogger_Levels ensures debug is suppressed at info level and the
// timestamp is always present.
func TestLogger_Levels(t *testing.T) {
	var buf bytes.Buffer
	log := logx.New(&buf, logx.LevelInfo)
	log.Debug("hidden", nil)
	log.Info("shown", nil)
	out := buf.String()
	if strings.Contains(out, "hidden") {
		t.Fatal("debug line must be suppressed at info level")
	}
	if !strings.Contains(out, "shown") || !strings.Contains(out, `"ts"`) {
		t.Fatalf("info line with ts expected, got %q", out)
	}
}

// TestNewRunIDUniqueAndCorrelatable ensures generated ids are unique and
// have a stable prefix usable for grepping across logs.
func TestNewRunIDUniqueAndCorrelatable(t *testing.T) {
	seen := map[string]bool{}
	for k := 0; k < 100; k++ {
		id := logx.NewRunID()
		if !strings.HasPrefix(id, "r-") {
			t.Fatalf("run id must start with r-, got %q", id)
		}
		if seen[id] {
			t.Fatalf("duplicate run id %q", id)
		}
		seen[id] = true
	}
}

type errStub struct{ s string }

func (e errStub) Error() string { return e.s }
