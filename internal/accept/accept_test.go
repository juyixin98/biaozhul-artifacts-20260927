package accept_test

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"replicactl/internal/accept"
)

// TestBlackBoxAcceptance builds the real server binary and runs the full
// scenario suite (including a real process restart) against it.
func TestBlackBoxAcceptance(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping black-box acceptance in -short mode")
	}
	dir := t.TempDir()
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()

	reportPath := filepath.Join(dir, "acceptance-report.txt")
	f, err := os.Create(reportPath)
	if err != nil {
		t.Fatal(err)
	}
	rep, err := accept.Run(ctx, dir, f)
	_ = f.Close()
	if err != nil {
		data, _ := os.ReadFile(reportPath)
		t.Fatalf("acceptance run failed: %v\n%s", err, string(data))
	}
	if rep.Failed != 0 {
		data, _ := os.ReadFile(reportPath)
		t.Fatalf("%d acceptance check(s) failed:\n%s", rep.Failed, string(data))
	}
}
