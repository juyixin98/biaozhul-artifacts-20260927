package config

import (
	"os"
	"path/filepath"
	"testing"
)

func writeConfig(t *testing.T, body string) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, "policy.json")
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

const validBody = `{
  "listen": "127.0.0.1:0",
  "databasePath": "x.db",
  "maxPasses": 4,
  "reconcileIntervalMs": 100,
  "mutators": [
    {"type": "defaults",  "failPolicy": "closed", "timeoutMs": 50},
    {"type": "capacity",  "failPolicy": "open",   "timeoutMs": 50, "perReplica": 10},
    {"type": "stamp-uid", "failPolicy": "closed", "timeoutMs": 50}
  ],
  "validators": [
    {"type": "schema", "failPolicy": "closed", "timeoutMs": 50, "maxReplicas": 5},
    {"type": "quota",  "failPolicy": "closed", "timeoutMs": 50}
  ]
}`

func TestLoad_Valid(t *testing.T) {
	cfg, err := Load(writeConfig(t, validBody))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Mutators[1].Fail != "open" {
		t.Fatalf("explicit fail policy lost: %s", cfg.Mutators[1].Fail)
	}
	if cfg.MaxPasses != 4 {
		t.Fatalf("config parse wrong: %+v", cfg)
	}
}

func TestLoad_RejectsImplicitPolicyAndUnknownType(t *testing.T) {
	cases := map[string]string{
		"missing failPolicy": `{
		  "reconcileIntervalMs": 100,
		  "mutators": [{"type": "defaults", "timeoutMs": 50}],
		  "validators": []}`,
		"bad policy value": `{
		  "reconcileIntervalMs": 100,
		  "mutators": [{"type": "defaults", "failPolicy": "maybe", "timeoutMs": 50}],
		  "validators": []}`,
		"unknown plugin": `{
		  "reconcileIntervalMs": 100,
		  "mutators": [{"type": "nope", "failPolicy": "closed", "timeoutMs": 50}],
		  "validators": []}`,
		"zero timeout": `{
		  "reconcileIntervalMs": 100,
		  "mutators": [{"type": "defaults", "failPolicy": "closed", "timeoutMs": 0}],
		  "validators": []}`,
		"negative maxPasses": `{
		  "maxPasses": -1, "reconcileIntervalMs": 100,
		  "mutators": [], "validators": []}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			// "maxPasses 0 explicit" actually omits maxPasses (default 5);
			// adjust expectation: unknown key is ignored, that case must fail
			// for a different reason — reconcileIntervalMs is missing there.
			_, err := Load(writeConfig(t, body))
			if err == nil {
				t.Fatalf("expected config rejection")
			}
		})
	}
}
