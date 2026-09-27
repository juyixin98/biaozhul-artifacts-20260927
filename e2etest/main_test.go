package e2etest

import (
	"os"
	"path/filepath"
	"testing"
)

// TestMain pins the replayable run-log directory to the top-level
// testdata/runs regardless of the package's own working directory, so every
// run id is kept in one place alongside the flow fixture.
func TestMain(m *testing.M) {
	abs, err := filepath.Abs(filepath.Join("..", "testdata", "runs"))
	if err == nil {
		_ = os.Setenv("FLEXHASH_TEST_LOG_DIR", abs)
	}
	os.Exit(m.Run())
}
