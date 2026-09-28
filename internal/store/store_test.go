package store

import (
	"context"
	"path/filepath"
	"testing"
)

func TestConfigReportAndRequestRoundTrip(t *testing.T) {
	ctx := context.Background()
	s, err := Open(ctx, filepath.Join(t.TempDir(), "x.db"), "unit")
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()

	v, err := s.SaveConfig(ctx, `{"default_action":"deny"}`, true, []byte("[]"))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.SaveReport(ctx, v, []byte(`{"default_action":"deny","diagnostics":[]}`)); err != nil {
		t.Fatal(err)
	}
	got, err := s.Report(ctx, v)
	if err != nil || string(got) == "" {
		t.Fatalf("report round trip: %v %s", err, got)
	}
	if lv, _ := s.LatestVersion(ctx); lv != v {
		t.Fatalf("latest version = %d, want %d", lv, v)
	}

	err = s.LogRequest(ctx, LogEntry{
		RequestID: "req_1", Version: v, Family: "ipv4",
		PacketJSON: []byte(`{}`), Decision: "deny",
		Steps:          []Step{{Order: 1, RuleID: "default", Matched: true, Action: "deny"}},
		Certain:        false,
		Uncertainties:  []string{"u"},
		Errors:         []string{"bad address"},
		SourceLocation: "unit-test",
	})
	if err != nil {
		t.Fatal(err)
	}
	rl, err := s.GetRequest(ctx, "req_1")
	if err != nil {
		t.Fatal(err)
	}
	if rl.Decision != "deny" || rl.Version != v || len(rl.Steps) != 1 ||
		rl.Instance != "unit" || rl.SourceLocation != "unit-test" {
		t.Fatalf("round trip mismatch: %+v", rl)
	}
	if len(rl.Errors) != 1 || rl.Errors[0] != "bad address" {
		t.Fatalf("errors not persisted separately: %+v", rl.Errors)
	}
	if len(rl.Uncertainties) != 1 || rl.Uncertainties[0] != "u" {
		t.Fatalf("uncertainties not persisted: %+v", rl.Uncertainties)
	}
}
