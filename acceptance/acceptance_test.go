// Package acceptance_test binds the scenario fixtures to ordinary `go test`
// runs. It builds the production binary once, then executes every fixture as a
// subtest; a single divergent tick fails the subtest with every mismatch
// listed. The independent reference comparison happens inside the runner.
package acceptance_test

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	"replicactl/acceptance/runner"
)

var (
	repoDir string
	binPath string
	workDir string
)

func TestMain(m *testing.M) {
	repoDir = os.Getenv("REPO_DIR")
	if repoDir == "" {
		// .../a/acceptance -> repo root .../a
		wd, _ := os.Getwd()
		repoDir = filepath.Dir(wd)
	}
	var err error
	workDir, err = os.MkdirTemp("", "replicactl-acc-")
	if err != nil {
		panic(err)
	}
	binPath = filepath.Join(workDir, "replicactl")
	build := exec.Command("go", "build", "-o", binPath, "./app/cmd/replicactl")
	build.Dir = repoDir
	build.Env = append(os.Environ(), "GOPROXY=off", "CGO_ENABLED=0")
	if out, err := build.CombinedOutput(); err != nil {
		panic("build production binary: " + err.Error() + "\n" + string(out))
	}
	code := m.Run()
	_ = os.RemoveAll(workDir)
	os.Exit(code)
}

func TestScenarios(t *testing.T) {
	files, err := filepath.Glob(filepath.Join(repoDir, "acceptance", "scenarios", "*.json"))
	if err != nil {
		t.Fatal(err)
	}
	if len(files) == 0 {
		t.Fatal("no scenario fixtures found")
	}
	for _, path := range files {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var sc runner.Scenario
		if err := json.Unmarshal(raw, &sc); err != nil {
			t.Fatalf("parse %s: %v", path, err)
		}
		t.Run(sc.Name, func(t *testing.T) {
			scWork := filepath.Join(workDir, sc.Name)
			if err := os.MkdirAll(scWork, 0o755); err != nil {
				t.Fatal(err)
			}
			rep, err := runner.Run(sc, runner.RunConfig{
				RepoDir: repoDir, WorkDir: scWork, BinPath: binPath,
			})
			if err != nil {
				t.Fatalf("scenario could not run: %v", err)
			}
			if !rep.Passed {
				for _, r := range rep.Results {
					for _, msg := range r.Mismatches {
						t.Errorf("step %d request=%s at=%d: %s", r.Index, r.RequestID, r.At, msg)
					}
				}
			}
			// Every scenario must contain at least one asserted scale and one
			// noop-with-reason, or it is not really exercising the contract.
			var scales, noops int
			for _, r := range rep.Results {
				switch r.Actual.Action {
				case "scale_up", "scale_down":
					scales++
				case "noop":
					noops++
					if len(r.Actual.Reasons) == 0 {
						t.Errorf("step %d noop without a reason", r.Index)
					}
				}
			}
			if scales == 0 {
				t.Errorf("scenario asserted no scale actions at all")
			}
			if noops == 0 {
				t.Errorf("scenario asserted no explained no-actions at all")
			}
		})
	}
}

// TestFailureCategoryScenarios drives the production binary through its fault
// injection endpoint and asserts the concrete failure category on each path.
func TestFailureCategoriesOverHTTP(t *testing.T) {
	scWork := filepath.Join(workDir, "http-faults")
	if err := os.MkdirAll(scWork, 0o755); err != nil {
		t.Fatal(err)
	}
	rep, err := runner.RunFaultScenario(runner.FaultRunConfig{
		RepoDir: repoDir, WorkDir: scWork, BinPath: binPath,
	})
	if err != nil {
		t.Fatalf("fault scenario: %v", err)
	}
	for _, f := range rep {
		if !f.Passed {
			t.Errorf("fault %s: %s (http %d)", f.Name, f.Mismatch, f.HTTPCode)
		}
	}
}
