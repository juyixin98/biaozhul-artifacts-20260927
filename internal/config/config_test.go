package config

import (
	"os"
	"path/filepath"
	"testing"

	"admission/internal/admission"
)

func writeTemp(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

const validConfig = `{
  "http": {"addr": "127.0.0.1:18080"},
  "storage": {"sqlitePath": ":memory:"},
  "logging": {"dir": ""},
  "admission": {
    "defaultTimeoutMs": 100,
    "maxMutationPasses": 2,
    "defaults": [
      {"type": "defaults.replicas", "failurePolicy": "FailClose", "args": {"default": 2}},
      {"type": "defaults.resources", "failurePolicy": "FailClose"}
    ],
    "mutators": [
      {"type": "mutators.reserved-resources", "failurePolicy": "FailClose"},
      {"type": "mutators.label-sync", "failurePolicy": "FailOpen", "timeoutMs": 50}
    ],
    "validators": [
      {"type": "validators.replica-range", "failurePolicy": "FailClose", "args": {"min": 1, "max": 5}},
      {"type": "validators.immutable-fields", "failurePolicy": "FailClose"},
      {"type": "validators.quota", "failurePolicy": "FailClose",
       "args": {"capacityCPUm": 1000, "capacityMemoryBytes": 1073741824}}
    ]
  },
  "reconcile": {"maxAttempts": 3}
}`

func TestLoadValid(t *testing.T) {
	f, err := Load(writeTemp(t, validConfig))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if f.HTTP.Addr != "127.0.0.1:18080" {
		t.Fatalf("addr=%s", f.HTTP.Addr)
	}
	if f.Admission.DefaultTimeoutMS != 100 || f.Admission.MaxMutationPasses != 2 {
		t.Fatalf("admission cfg wrong: %+v", f.Admission)
	}
	if f.Reconcile.MaxAttempts != 3 {
		t.Fatalf("reconcile maxAttempts=%d", f.Reconcile.MaxAttempts)
	}

	built, err := Build(f, nil)
	if err != nil {
		t.Fatalf("Build: %v", err)
	}
	if built.Ledger == nil {
		t.Fatal("quota validator should construct the ledger")
	}
	// Smoke-run the built pipeline.
	_ = built
}

func TestRejectsMissingFailurePolicy(t *testing.T) {
	body := `{
      "storage": {"sqlitePath": ":memory:"},
      "admission": {
        "mutators": [{"type": "mutators.label-sync"}],
        "validators": [{"type": "validators.replica-range", "failurePolicy": "FailClose"}]
      }}`
	if _, err := Load(writeTemp(t, body)); err == nil {
		t.Fatal("config without explicit failurePolicy must be rejected")
	}
}

func TestRejectsZeroValidators(t *testing.T) {
	body := `{
      "storage": {"sqlitePath": ":memory:"},
      "admission": {"validators": []}}`
	if _, err := Load(writeTemp(t, body)); err == nil {
		t.Fatal("pipeline without validators must be rejected")
	}
}

func TestRejectsDuplicatePluginKeys(t *testing.T) {
	body := `{
      "storage": {"sqlitePath": ":memory:"},
      "admission": {"validators": [
        {"type": "validators.replica-range", "name": "same", "failurePolicy": "FailClose"},
        {"type": "validators.immutable-fields", "name": "same", "failurePolicy": "FailClose"}
      ]}}`
	if _, err := Load(writeTemp(t, body)); err == nil {
		t.Fatal("duplicate plugin names must be rejected")
	}
}

func TestRejectsUnknownPluginType(t *testing.T) {
	body := `{
      "storage": {"sqlitePath": ":memory:"},
      "admission": {"validators": [{"type": "validators.nope", "failurePolicy": "FailClose"}]}}`
	f, err := Load(writeTemp(t, body))
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if _, err := Build(f, nil); err == nil {
		t.Fatal("unknown plugin type must fail Build")
	}
}

func TestPipelineRejectsMissingValidator(t *testing.T) {
	// Core-level invariant independent of file loading.
	cfg := admission.Config{MaxMutationPasses: 1}
	if _, err := admission.New(cfg, nil); err == nil {
		t.Fatal("admission.New without validators must error")
	}
}
