// Package tests holds integration tests that cross package boundaries.
//
// These run the hand-authored scenarios in testdata/scenarios through the
// real three-node in-process cluster and assert concrete numbers. The
// expected values are authored in the fixture files (not produced by the
// code under test), and the independent replay checker recomputes them.
package tests

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"clsnap/tests/harness"
)

// TestScenarios drives every fixture found under testdata/scenarios.
func TestScenarios(t *testing.T) {
	files, err := filepath.Glob(filepath.Join("..", "testdata", "scenarios", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) < 3 {
		t.Fatalf("expected >=3 scenario fixtures, found %d", len(files))
	}
	for _, f := range files {
		sc, err := harness.LoadScenario(f)
		if err != nil {
			t.Fatalf("load %s: %v", f, err)
		}
		t.Run(sc.Name, func(t *testing.T) {
			runID := "test-" + sc.Name
			dir := t.TempDir()
			h, err := harness.New(context.Background(), sc, dir, runID)
			if err != nil {
				t.Fatal(err)
			}
			defer h.Close()
			if err := h.Run(context.Background()); err != nil {
				t.Fatalf("run: %v", err)
			}
			rep, path, err := h.Finalize(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			if !rep.Passed {
				for _, v := range rep.Verdicts {
					if !v.Passed {
						t.Errorf("verdict %q failed: %s (expected=%v observed=%v)",
							v.Name, v.Reason, v.Expected, v.Observed)
					}
				}
			}
			raw, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("report not written: %v", err)
			}
			if len(raw) < 200 || !strings.Contains(string(raw), runID) {
				t.Fatalf("report looks malformed: %d bytes", len(raw))
			}
			if len(rep.Events) == 0 {
				t.Fatal("report recorded no events")
			}
		})
	}
}


