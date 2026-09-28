package runlogger

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"admission/internal/admission"
	"admission/internal/types"
)

func TestLoggerCapturesAndWritesJSONL(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "logs")
	l := New(dir)
	l.Log(admission.LogEntry{
		RunID: "run-A", Time: time.Unix(1, 0).UTC(), Level: "INFO",
		Phase: types.PhaseMutating, Pass: 1, Plugin: "p1",
		Message:    "applied 1 op(s)",
		Patch:      []types.PatchOp{{Op: types.OpAdd, Path: "/spec/cpu", Value: "500m"}},
		BeforeHash: "bbb", AfterHash: "aaa",
	})
	l.Log(admission.LogEntry{
		RunID: "run-B", Time: time.Unix(2, 0).UTC(), Level: "ERROR",
		Category: types.CatTimeout, Message: "boom",
	})

	if got := l.Entries("run-A"); len(got) != 1 || got[0].Plugin != "p1" {
		t.Fatalf("filtered entries wrong: %+v", got)
	}
	if ids := l.RunIDs(); len(ids) != 2 || ids[0] != "run-A" || ids[1] != "run-B" {
		t.Fatalf("RunIDs=%v", ids)
	}

	b, err := os.ReadFile(filepath.Join(dir, "run-run-A.jsonl"))
	if err != nil {
		t.Fatalf("read log file: %v", err)
	}
	line := strings.TrimSpace(string(b))
	if !strings.Contains(line, `"record":"run-log/v1"`) ||
		!strings.Contains(line, `"runId":"run-A"`) ||
		!strings.Contains(line, `/spec/cpu`) ||
		!strings.Contains(line, `"beforeHash":"bbb"`) {
		t.Fatalf("JSONL line missing replay fields: %s", line)
	}
}
